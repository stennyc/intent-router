"""intent-router v2.5 — 分工台账（主专家全程知道子专家在做什么，完成时交接）。

Owner 裁决（2026-11-17 "动手"，设计提案 v2.5 拍板）:
- 缺口实证: 派发发生在主 LLM 处理消息之前 → 主专家零感知子专家在做什么（各做各的
  起点）；RUNNING 期间 poll 静默 → 持续零感知；交接被动且有窗口（终态才注入）。
- L1 派发同轮台账卡: 派发成功那一轮注入 "## 分工台账"（slug + 任务 + 进行中 +
  勿重复 + delegation_id 审计键 L4）→ 主专家从开工第一秒知道分工。
- L2 运行期提醒: lifecycle poll RUNNING 不再静默, 每轮尾部一行（字节稳定,
  缓存安全）→ 主专家持续知道。UNKNOWN 重试轮保持静默（结果去向由 lost 交接卡交代,
  提醒语义只属于"还在干活"的状态）。
- L3 终态交接卡: recovered/failed/lost 全部升级为 "## 子专家交接" 语义,
  带 "主专家下一步" 指令（采纳/核验而非重做; 失败/丢失 → 主进程自行处理或重派）。
- 缓存不变量不变: 全部 append-only 尾部加法（前缀字节扩展, test_prefix_cache_stable
  的 startswith 检查继续钉死）。
- 折入裁决: F-2（3 元组兼容分支删除, 生产不可达）+ F-4（lost 诊断断言收紧
  "unavailable"）。

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
    spec = importlib.util.spec_from_file_location("intent_router_v25_ledger", root / "__init__.py")
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
    (experts / "engineering-backend-architect.md").write_text("BACKEND-EXPERT-BODY", encoding="utf-8")
    (experts / "engineering-security-engineer.md").write_text("SECURITY-EXPERT-BODY", encoding="utf-8")
    mod._EXPERTS_DIR = experts
    mod._LOG_PATH = tmp_path / "logs" / "dispatches.jsonl"
    mod._fetch_expert_from_github = lambda slug: None
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


def _dispatch_lifecycle(env, svc, task="做个安全审查"):
    """turn1 锚点 engineering → turn2 切 security 域触发 lifecycle 派发;
    返回 (sid, 派发轮返回 r) —— v2.5 需要派发轮注入面, 故捕获 r。"""
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, task, first=False)
    d = [e for e in _events(env) if e["event"] == "dispatch" and e.get("channel") == "subagent_lifecycle"]
    assert d, f"no lifecycle dispatch event; got: {_events(env)}"
    return d[0]["delegation_id"], r


# ── L1: 派发同轮台账卡（含 L4 审计键） ─────────────────────────────────────

def test_l1_ledger_card_on_lifecycle_success(env):
    """L1+L4: lifecycle 成功派发那一轮注入台账卡——主专家从开工第一秒知道分工。"""
    svc = _FakeLifecycle()
    sid, r = _dispatch_lifecycle(env, svc, task="做个安全审查")
    assert r is not None, "v2.5: 成功派发轮必须注入（旧行为是零注入 → 各做各的）"
    ctx_out = r["context"]
    assert "分工台账" in ctx_out, ctx_out
    assert "engineering-security-engineer" in ctx_out, ctx_out
    assert "做个安全审查" in ctx_out, "任务原话须进台账（主专家知道子专家在做什么）"
    assert "勿重复" in ctx_out, ctx_out
    assert sid in ctx_out, "L4: 台账卡带 delegation_id 审计键（跨重启可重新锚定）"


def test_l1_ledger_card_on_channel_two_success(env):
    """L1: 通道②（delegate_task, CLI 模式）成功派发同轮也注入台账卡。"""
    env.ctx.lifecycle_svc = None          # 旧宿主形态 → 通道②
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "做个安全审查", first=False)
    d = [e for e in _events(env) if e["event"] == "dispatch" and e.get("channel") == "delegate_task"]
    assert d, _events(env)
    assert r is not None and "分工台账" in r["context"], r
    assert d[0]["delegation_id"] in r["context"], "L4: 审计键 = delegation_id"
    assert "engineering-security-engineer" in r["context"]


def test_l1_no_ledger_when_fully_refused(env):
    """全拒（双通道拒绝 → 降级注入）: 子专家并未开工 → 不得出现台账卡。"""
    env.ctx.lifecycle_svc = None
    env.ctx.dispatch_result = json.dumps({"error": "delegate_task requires a parent agent context."})
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "做个安全审查", first=False)
    assert r is not None and "SECURITY-EXPERT-BODY" in r["context"]   # 降级注入照旧
    assert "分工台账" not in r["context"], "全拒时子专家没开工, 台账卡是假的"


# ── L2: 运行期每轮一行提醒（字节稳定） ──────────────────────────────────────

def test_l2_running_reminder_each_turn_byte_stable(env):
    """L2: RUNNING 期间每轮尾部一行提醒, 同 entry 内字节不变（缓存友好）;
    running 追踪不清账、无结果事件。"""
    svc = _FakeLifecycle()
    sid, _r = _dispatch_lifecycle(env, svc)
    svc.status_script[sid] = "RUNNING"
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "engineering", "confidence": 0.9},
    ]
    ra = _turn(env, "继续", first=False)
    rb = _turn(env, "嗯", first=False)
    assert ra is not None and "处理中" in ra["context"], ra
    assert rb is not None and "处理中" in rb["context"], rb
    la = [ln for ln in ra["context"].splitlines() if "处理中" in ln][0]
    lb = [ln for ln in rb["context"].splitlines() if "处理中" in ln][0]
    assert la == lb, "同一 running 窗口内提醒行字节必须稳定"
    assert "engineering-security-engineer" in la and "做个安全审查" in la, la
    assert env.mod._SESSIONS["s1"].lifecycle, "running 子专家保留到下一轮"
    assert not [e for e in _events(env) if e["event"].startswith("subagent-result-")]


def test_l2_reminder_fires_on_skip_running_turns_too(env):
    """L2: 同域 skip-running 轮（重派被守卫拦下）也带提醒——主专家知道还在跑。"""
    svc = _FakeLifecycle()
    sid, _r = _dispatch_lifecycle(env, svc)
    svc.status_script[sid] = "RUNNING"
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r = _turn(env, "再做个安全审查", first=False)
    sk = [e for e in _events(env) if e["event"] == "skip-running"]
    assert sk, "同域第 2 次派发应被 running 守卫拦下"
    assert r is not None and "处理中" in r["context"], r
    assert len(svc.launch_calls) == 1, "running 期间不重派"


def test_l2_unknown_retry_turns_stay_silent(env):
    """设计点 (a): UNKNOWN 重试窗口保持静默——提醒语义只属于"还在干活";
    结果去向由 3 连 lost 交接卡一次性交代（test_l3_lost 钉死）。"""
    svc = _FakeLifecycle()
    sid, _r = _dispatch_lifecycle(env, svc)
    svc.status_script[sid] = "UNKNOWN"
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r = _turn(env, "继续", first=False)
    assert r is None, "UNKNOWN 重试轮不注入提醒（结果去向由 lost 卡交代）"
    assert env.mod._SESSIONS["s1"].lifecycle, "重试窗口内不清账"


# ── L3: 终态交接卡（recovered / failed / lost 全升级为"交接"语义） ─────────

def test_l3_recovered_handoff_card(env):
    """L3: SUCCEEDED → "## 子专家交接" + findings + 主专家下一步（采纳/核验而非重做）。"""
    svc = _FakeLifecycle()
    sid, _r = _dispatch_lifecycle(env, svc)
    svc.status_script[sid] = "SUCCEEDED"
    svc.results[sid] = SimpleNamespace(summary="SEC-FINDINGS: fix SQLi in /orders")
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r = _turn(env, "那单元测试怎么写", first=False)
    ctx_out = r["context"]
    assert "SEC-FINDINGS: fix SQLi in /orders" in ctx_out
    assert "交接" in ctx_out, ctx_out
    assert "主专家下一步" in ctx_out, "交接卡必须给出主专家的下一步指令"
    assert "而非重做" in ctx_out and "filemem" in ctx_out   # F4 既有面保留
    assert env.mod._SESSIONS["s1"].lifecycle == {}           # 交接后清账


def test_l3_failed_handoff_card(env):
    """L3: FAILED 也是交接——主专家知道"子专家没干成", 自行处理或重派。"""
    svc = _FakeLifecycle()
    sid, _r = _dispatch_lifecycle(env, svc)
    svc.status_script[sid] = "FAILED"
    svc.results[sid] = SimpleNamespace(summary=None, error_message="provider 500",
                                       error_classification="FAILED")
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r = _turn(env, "那单元测试怎么写", first=False)
    assert r is not None, "失败也要交接（主专家不能不知道子专家没干成）"
    ctx_out = r["context"]
    assert "交接" in ctx_out and "FAILED" in ctx_out, ctx_out
    assert "主专家下一步" in ctx_out, ctx_out
    assert "SEC-FINDINGS" not in ctx_out
    assert env.mod._SESSIONS["s1"].lifecycle == {}


def test_l3_lost_handoff_card(env):
    """L3: lost（3 连 UNKNOWN 清账）交接卡 = 丢失交代 + filemem 检索键 + 主专家下一步。"""
    svc = _FakeLifecycle()
    sid, _r = _dispatch_lifecycle(env, svc)
    svc.status_script[sid] = "UNKNOWN"
    r3 = None
    for _ in range(3):
        env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
        r3 = _turn(env, "继续", first=False)
    lost = [e for e in _events(env) if e["event"] == "subagent-result-lost"]
    assert lost and lost[0]["delegation_id"] == sid
    ctx_out = r3["context"]
    assert "交接" in ctx_out and sid in ctx_out, ctx_out
    assert "filemem" in ctx_out and "而非重做" in ctx_out, "F4 检索指引保留"
    assert "主专家下一步" in ctx_out, "丢失也给出主专家下一步（自行处理/重派）"
    card = env.mod._lost_pointer("engineering-security-engineer", sid)
    assert "交接" in card and "filemem" in card, card


# ── F-4: lost 诊断字段断言收紧 ──────────────────────────────────────────────

def test_f4_lost_diagnostics_tightened_unavailable(env):
    """F-4: 测试环境宿主模块不可 import → resolver 诊断确定性 "unavailable"
    （旧断言是 6 值白名单, 收紧为精确值, 防诊断面静默漂移）。"""
    svc = _FakeLifecycle()
    sid, _r = _dispatch_lifecycle(env, svc)
    svc.status_script[sid] = "UNKNOWN"
    for _ in range(3):
        env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
        _turn(env, "继续", first=False)
    lost = [e for e in _events(env) if e["event"] == "subagent-result-lost"]
    assert lost, _events(env)
    assert lost[0]["resolver_session"] == "unavailable", lost[0]
    assert lost[0]["frozen_session"] == "unavailable", lost[0]
