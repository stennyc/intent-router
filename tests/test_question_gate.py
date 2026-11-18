"""intent-router v2.9 — 问句前置门（v2.8-c 候选, owner 裁决 2026-11-18 "A+C" 之 A）。

失败类（11-18 活体三点实证, 全部池 jsonl 有档）:
- "完成了吗"（4 字恰过 G1 阈值）→ aux 分类 writing 0.95 → 误派 writing-assistant（sa-0-5d1391cc）
- "推，然后继续"（操作指令）→ eng 0.85 → 误派 backend-architect（sa-0-d504d511）
- "已重启 gateway , 接下来呢？" → 又派发一个子专家
状态/元问句的价值在主进程整合（答问题的上下文全在主会话）, 子专家盲并行会话
= 误路由噪声 + 各做各的。

设计（纯插件侧, 宿主零改动, 确定性模式不依赖 LLM —— 与 /expert 命令解析同风格）:
- _QUESTION_RE: 句尾 ？/?/吗/呢/嘛 或 句首疑问词（为什么/为何/为啥/是不是/有没有/
  是否/能否/可否/接下来/下一步）→ 后续轮静默跳过 aux 分类（零事件, 与 G1 同构）。
- 同轮终态放行旗标例外不变: st.redispatch_due 非空（本轮刚 lost/failed 清账）时
  问句消息也必须分类 —— 否则 v2.4.3 owner 策略 B「消息分类到新域时同轮重派」
  路径被护栏阻断（test_review_fixes 对 G1 的钉住语义, 本文件对 v2.9-c 同等钉住）。
- 范围纪律（明确不做）: 祈使操作词（"推，然后继续" 类 >4 字指令词）不在本轮
  判别面（用户控制词表开放, 做成硬词表=维护陷阱）——记 G1b 候选；
  "帮我修一下吗" 类任务+问句尾 → 跳过（可接受: 主专家当轮自答, 子专家可 /expert 显式补）。

Run: C:/tmp/cmap-venv/Scripts/python -m pytest tests/ -q   (pytest + stdlib only)
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "plugin.yaml"


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_v29_question_gate", root / "__init__.py")
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeLLM:
    def __init__(self):
        self.scripts = []
        self.calls = []

    def complete_structured(self, **kw):
        self.calls.append(kw)
        parsed = None
        if self.scripts:
            parsed = self.scripts.pop(0)
        return SimpleNamespace(parsed=parsed)


class _FakeHandle:
    def __init__(self, sid, session_id="s1"):
        self.subagent_id = sid
        self.parent_session_id = session_id

    def to_dict(self):
        return {"subagent_id": self.subagent_id, "parent_session_id": self.parent_session_id}


class _FakeLifecycle:
    def __init__(self):
        self.launch_calls = []
        self.status_script = {}
        self.results = {}
        self._n = 0
        self.raise_on_launch = None

    def launch(self, request):
        self.launch_calls.append(dict(request))
        if self.raise_on_launch is not None:
            raise self.raise_on_launch
        self._n += 1
        sid = f"sub-{self._n:04d}"
        self.status_script.setdefault(sid, "RUNNING")
        return _FakeHandle(sid)

    def status(self, hdict):
        return SimpleNamespace(state=self.status_script.get(hdict.get("subagent_id"), "UNKNOWN"))

    def result(self, hdict):
        return self.results.get(hdict.get("subagent_id"))


class _FakeCtx:
    def __init__(self):
        self.llm = _FakeLLM()
        self.dispatch_calls = []
        self.hooks = {}
        self._n = 0
        self.lifecycle_svc = None
        self.dispatch_result = None

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def subagent_lifecycle(self):
        return self.lifecycle_svc

    def dispatch_tool(self, tool_name, args, **kw):
        self.dispatch_calls.append((tool_name, args, kw))
        if self.dispatch_result is not None:
            return self.dispatch_result
        self._n += 1
        return json.dumps({
            "status": "dispatched", "mode": "background",
            "delegation_id": f"deleg_{self._n:04d}",
            "subagent_ids": [f"sa-{self._n:04d}"],
        })


@pytest.fixture()
def env(tmp_path):
    mod = _load_plugin()
    experts = tmp_path / "experts"
    experts.mkdir()
    for slug in ("engineering-backend-architect", "engineering-security-engineer",
                 "engineering-devops-automator"):
        (experts / f"{slug}.md").write_text(f"{slug}-BODY", encoding="utf-8")
    mod._EXPERTS_DIR = experts
    mod._LOG_PATH = tmp_path / "logs" / "dispatches.jsonl"
    mod._fetch_expert_from_github = lambda slug: None
    # G3 隔离: filemem 指向不存在目录（fail-soft []）, 单测绝不扫真实中央存储
    mod._FILEMEM_MEMORIES_DIR = tmp_path / "filemem_missing"
    ctx = _FakeCtx()
    return SimpleNamespace(mod=mod, ctx=ctx, experts=experts, log=tmp_path / "logs" / "dispatches.jsonl")


def _turn(env, user, first, history=None, session_id="s1"):
    mod, ctx = env.mod, env.ctx
    return mod._make_hook(ctx)(
        session_id=session_id, user_message=user, conversation_history=history or [],
        is_first_turn=first, model="m", platform="cli",
    )


def _events(env):
    events = []
    if env.log.exists():
        for line in env.log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def _dispatched(env, svc):
    """turn1 锚点 engineering → turn2 切 security 域触发 lifecycle 派发。"""
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "做个安全审查", first=False)
    d = [e for e in _events(env) if e["event"] == "dispatch" and e.get("channel") == "subagent_lifecycle"]
    assert d, f"no lifecycle dispatch event; got: {_events(env)}"
    return d[0]["delegation_id"]


# ── _QUESTION_RE 直接面（模式表钉住）────────────────────────────────────────

def test_question_pattern_table(env):
    q = env.mod._QUESTION_RE
    skipped = [
        "完成了吗", "接下来呢", "？", "为什么没接住", "是不是有 bug",
        "有没有可能", "下一步是什么", "帮我修一下吗", "重启 gateway 了？",
        "为何停了", "接下来怎么办",
    ]
    kept = [
        "做个安全审查", "帮我写个 REST 后端 API", "部署一下 CI",
        "推", "继续", "A+C", "动手", "沉淀", "推，然后继续",
    ]
    for m in skipped:
        assert q.search(m), f"应跳过（问句）: {m!r}"
    for m in kept:
        assert not q.search(m), f"不应跳过（非问句, 照旧分类）: {m!r}"


# ── 后续轮端到端 ────────────────────────────────────────────────────────────

def test_question_ending_skips_classify_silently(env):
    """'完成了吗'（4 字过 G1, 问句门命中）→ 静默跳过 aux: 零分类调用、零事件。"""
    svc = _FakeLifecycle()
    _dispatched(env, svc)
    calls_before = len(env.ctx.llm.calls)
    evs_before = len(_events(env))
    r = _turn(env, "完成了吗", first=False)      # 不给脚本: 若误分类 parsed=None 也无派发
    assert len(env.ctx.llm.calls) == calls_before, "问句消息不得进入 aux 分类"
    assert len(_events(env)) == evs_before, "静默跳过必须零事件（与 G1 同构）"
    assert r is None or "分工台账" not in r.get("context", ""), r


def test_question_lead_word_skips_classify(env):
    """句首疑问词（'为什么…'）→ 同静默跳过。"""
    svc = _FakeLifecycle()
    _dispatched(env, svc)
    calls_before = len(env.ctx.llm.calls)
    _turn(env, "为什么主专家没接住", first=False)
    assert len(env.ctx.llm.calls) == calls_before, "句首疑问词消息不得进入 aux 分类"


def test_imperative_task_still_classifies(env):
    """回归网: 祈使任务（无问句标记）不被问句门误拦 —— 照旧分类派发。"""
    svc = _FakeLifecycle()
    _dispatched(env, svc)
    env.ctx.llm.scripts.append({"category": "devops", "confidence": 0.9})
    _turn(env, "部署一下 CI", first=False)
    d = [e for e in _events(env) if e["event"] == "dispatch" and e.get("category") == "devops"]
    assert d, f"祈使任务必须照旧分类派发; events={_events(env)}"


def test_question_with_redispatch_due_still_classifies(env):
    """同轮终态放行例外（G1 语义继承）: 本轮刚 lost 清账（任务未交付）时,
    问句消息也必须分类 —— 否则 owner 策略 B「消息分类到新域时同轮重派」被护栏阻断。
    第 3 轮（UNKNOWN×3 判 lost, redispatch_due 置位）用问句消息 + 新域脚本,
    断言分类发生 + 同轮 devops 重派。"""
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "UNKNOWN"
    for _ in (1, 2):
        env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
        _turn(env, "那单元测试怎么写", first=False)
    env.ctx.llm.scripts.append({"category": "devops", "confidence": 0.9})
    calls_before = len(env.ctx.llm.calls)
    _turn(env, "继续呢？", first=False)          # 问句尾 + 同轮 lost 清账 → 旗标放行
    assert len(env.ctx.llm.calls) > calls_before, \
        "同轮终态放行旗标缺失: 问句消息被静默跳过, 重派路径被阻断"
    d = [e for e in _events(env) if e["event"] == "dispatch" and e.get("category") == "devops"]
    assert d, f"lost 同轮新域重派未发生; events={_events(env)}"


def test_lost_event_still_settles_on_question_turn(env):
    """问句消息轮若恰逢 lost 清账: lost 事件仍正常记（门只跳分类, 不碰轮询面）。"""
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "UNKNOWN"
    for i in (1, 2, 3):
        env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
        _turn(env, "为什么还没好", first=False)   # 三轮全问句 → 分类静默, 轮询/清账不受影响
    lost = [e for e in _events(env) if e["event"] == "subagent-result-lost"]
    assert lost and lost[0]["delegation_id"] == sid, _events(env)
    assert env.mod._SESSIONS["s1"].lifecycle == {}
