"""intent-router v2.6 P1 — 分序开关：post-LLM 拉起（launch_timing=post）。

Owner 裁决（2026-11-17 "时序做"）: 治"主专家还没准备好/分工，子专家先被拉起"的
时序倒置——把 lifecycle/delegate_task 的实际 launch 从 pre_llm_call 挪到
post_llm_call（宿主 turn_finalizer.py:494, tool loop 结束后每 turn 一次；worker
线程经 copy_context 继承 → 父 agent contextvar 绑定与 pre 时刻等价，v2.4.3 B
冻结快照机制直接复用）。

宿主只读实证（2026-11-17）:
- post_llm_call kwargs: session_id/task_id/turn_id/user_message/assistant_response/
  conversation_history/model/platform/side_question_token —— 无 parent_session_id
  → P0 守卫在 post 面用 platform=="subagent" + st.guarded（pre 守卫打标）双信号
- post_llm_call ∈ _HOOK_TIMEOUT_BOUNDED_HOOKS → 30s 界内 worker, fail-open
- 默认 launch_timing=pre → 全部既有行为零变化（既有 77+8 用例天然回归网）

post 模式时序（每轮用户消息）:
  pre_llm:  分类 → 新域 → 记 dispatch-pending（不拉起、不设 running、无台账卡）
  主 LLM 回复用户
  post_llm: 消费 pending → 真 launch（lifecycle 优先, 旧通道拒绝记 refused）
            成功 → ledger_due（下一 pre 轮注入"上轮回复后已启动"台账卡）
            全拒 → fallback_due（下一 pre 轮降级注入专家全文）
  下一 pre: L1 台账卡（post 变体）/ 降级注入 + L2/L3 轮询面照旧

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
    spec = importlib.util.spec_from_file_location("intent_router_v26_timing", root / "__init__.py")
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
    """带 get_config 的宿主形态（launch_timing 面）。settings 缺省 = pre 模式。"""

    def __init__(self):
        self.llm = _FakeLLM()
        self.dispatch_calls = []
        self.hooks = {}
        self._n = 0
        self.lifecycle_svc = None
        self.dispatch_result = None
        self.settings = {}
        self.get_config_calls = []

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

    def get_config(self, key, default=None):
        self.get_config_calls.append(key)
        return self.settings.get(key, default)


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
    # 宿主加载序列: register() 一次性挂 pre_llm_call + post_llm_call（_post 依赖 ctx.hooks）
    mod.register(ctx)
    return SimpleNamespace(mod=mod, ctx=ctx, experts=experts, log=tmp_path / "logs" / "dispatches.jsonl")


def _turn(env, user, first, history=None, session_id="s1", platform="cli"):
    mod, ctx = env.mod, env.ctx
    return mod._make_hook(ctx)(
        session_id=session_id, user_message=user, conversation_history=history or [],
        is_first_turn=first, model="m", platform=platform,
    )


def _post(env, user, session_id="s1", platform="cli", assistant="（主专家已回复）"):
    """模拟宿主 post_llm_call（turn_finalizer kwargs 面）。"""
    mod, ctx = env.mod, env.ctx
    assert "post_llm_call" in ctx.hooks, "post_llm_call hook 未注册"
    return ctx.hooks["post_llm_call"](
        session_id=session_id, user_message=user, assistant_response=assistant,
        conversation_history=[], model="m", platform=platform,
    )


def _events(env):
    events = []
    if env.log.exists():
        for line in env.log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def _setup_post_mode(env, svc):
    """post 模式 + lifecycle 通道 + turn1 锚点 + turn2 切 security 域（pre 侧只记 pending）。"""
    env.ctx.settings = {"launch_timing": "post"}
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "做个安全审查", first=False)
    return r


# ── post 模式核心时序 ───────────────────────────────────────────────────────

def test_post_mode_defers_launch_to_post_hook(env):
    """P1 本体: pre 轮只记 pending（不拉起/无台账卡）; post 轮才真 launch;
    下一 pre 轮注入"上轮回复后已启动"台账卡。"""
    svc = _FakeLifecycle()
    r2 = _setup_post_mode(env, svc)
    evs = _events(env)
    # pre 侧: 只记 pending, 零 launch, 零台账卡
    assert [e for e in evs if e["event"] == "dispatch-pending"], evs
    assert not [e for e in evs if e["event"] == "dispatch"], "pre 轮不得真派发"
    assert len(svc.launch_calls) == 0
    assert r2 is None or "分工台账" not in (r2 or {}).get("context", "")
    st = env.mod._SESSIONS["s1"]
    assert st.pending, "pending 标记必须留存到 post 轮"
    # post 侧: 真 launch + running 守卫
    _post(env, "做个安全审查")
    assert len(svc.launch_calls) == 1, "post 轮执行真派发"
    d = [e for e in _events(env) if e["event"] == "dispatch"]
    assert d and d[0]["channel"] == "subagent_lifecycle", _events(env)
    assert env.mod._SESSIONS["s1"].pending == {}
    assert env.mod._SESSIONS["s1"].running, "launch 成功后占 running 守卫"
    # 下一 pre 轮: L1 台账卡（post 变体文案）+ L2 提醒（RUNNING）
    env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
    r3 = _turn(env, "继续", first=False)
    assert r3 is not None
    assert "分工台账" in r3["context"] and "上轮" in r3["context"], r3["context"]
    assert "处理中" in r3["context"], "L2 提醒面照旧"


def test_post_mode_pending_same_domain_skips(env):
    """pending 未 launch 前, 同域再触发 → skip-pending（不重复记、不改原任务）。"""
    svc = _FakeLifecycle()
    _setup_post_mode(env, svc)
    st = env.mod._SESSIONS["s1"]
    assert len(st.pending) == 1
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r = _turn(env, "再做个安全审查", first=False)
    sk = [e for e in _events(env) if e["event"] == "skip-pending"]
    assert sk, _events(env)
    assert len(st.pending) == 1, "原 pending 任务保持不变"
    assert r is None or "分工台账" not in (r or {}).get("context", "")


def test_post_mode_refused_falls_back_next_pre_turn(env):
    """post 轮全拒（lifecycle 不可用 + 旧通道 tool_error）→ fallback_due,
    下一 pre 轮降级注入专家全文（编程价值不丢, 时延一轮）。"""
    svc = _FakeLifecycle()
    svc.raise_on_launch = RuntimeError("No active Hermes parent session is available.")
    env.ctx.dispatch_result = json.dumps({"error": "delegate_task requires a parent agent context."})
    r2 = _setup_post_mode(env, svc)
    assert not [e for e in _events(env) if e["event"] == "dispatch"], "pre 轮未派发"
    _post(env, "做个安全审查")
    evs = _events(env)
    assert [e for e in evs if e["event"] == "dispatch-refused"], evs
    assert not [e for e in evs if e["event"] == "dispatch"], "全拒不得记真派发"
    st = env.mod._SESSIONS["s1"]
    assert st.fallback_due, "全拒须记 fallback_due（注入顺延到下一 pre 轮）"
    env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
    r3 = _turn(env, "继续", first=False)
    assert r3 is not None and "SECURITY-EXPERT-BODY" in r3["context"]
    assert [e for e in _events(env) if e["event"] == "dispatch-fallback-injection"]


def test_post_mode_subagent_sessions_guarded(env):
    """P0 对称: 子 agent session 的 post 轮不得执行 pending 派发
    （platform=subagent + st.guarded 双信号）。"""
    svc = _FakeLifecycle()
    env.ctx.settings = {"launch_timing": "post"}
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    # 主 session 记 pending
    _turn(env, "帮我写个 REST 后端 API", first=True, session_id="main-1")
    _turn(env, "做个安全审查", first=False, session_id="main-1")
    assert env.mod._SESSIONS["main-1"].pending
    # 子 session: pre 轮被 P0 守卫挡住（打 guarded 标）, post 轮不得拉起
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r = _turn(env, "子任务", first=True, session_id="sub-1", platform="subagent")
    assert r is None
    assert env.mod._SESSIONS["sub-1"].guarded
    _post(env, "子任务", session_id="sub-1", platform="subagent")
    assert len(svc.launch_calls) == 0, "子 session 的 post 轮不得执行派发"
    assert env.mod._SESSIONS["sub-1"].pending == {}


def test_pre_mode_default_zero_change(env):
    """缺省（无 settings / 无 get_config 的旧宿主）= pre 模式, 行为与 v2.5 完全一致。"""
    svc = _FakeLifecycle()
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "做个安全审查", first=False)
    assert len(svc.launch_calls) == 1, "pre 模式当轮真派发（不变）"
    assert r is not None and "分工台账" in r["context"], "pre 模式当轮台账卡（不变）"
    evs = _events(env)
    assert not [e for e in evs if e["event"] in ("dispatch-pending", "skip-pending", "ledger-post-inject")], evs
    assert env.mod._SESSIONS["s1"].pending == {} and env.mod._SESSIONS["s1"].ledger_due == {}


def test_register_hooks_pre_and_post(env):
    """register() 必须同时挂 pre_llm_call + post_llm_call（post 面 fail-soft 由宿主保证）。"""
    ir = _load_plugin()
    ctx = _FakeCtx()
    ir.register(ctx)
    assert set(ctx.hooks) == {"pre_llm_call", "post_llm_call"}, ctx.hooks


def test_post_mode_terminal_polling_unaffected(env):
    """L3 面不变: post 模式 launch 后 SUCCEEDED → 下一 pre 轮出"子专家交接"卡。"""
    svc = _FakeLifecycle()
    _setup_post_mode(env, svc)
    _post(env, "做个安全审查")
    sid = env.mod._SESSIONS["s1"].running["engineering-security-engineer"][0]
    svc.status_script[sid] = "SUCCEEDED"
    svc.results[sid] = SimpleNamespace(summary="POST-MODE-FINDINGS")
    env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
    r = _turn(env, "那单元测试怎么写", first=False)
    assert r is not None and "POST-MODE-FINDINGS" in r["context"]
    assert "交接" in r["context"] and "主专家下一步" in r["context"]
    assert [e for e in _events(env) if e["event"] == "subagent-result-recovered"]
    assert env.mod._SESSIONS["s1"].lifecycle == {}


# ── F4/F5 review-fix（reviewer 复盘 2026-11-17） ──────────────────────────

def test_success_channel_without_id_is_fail_soft(env, monkeypatch):
    """F5: 成功通道缺关联 id（契约违反）→ fail-soft 记 dispatch-anomaly:
    不 raise（生产断言在 -O 下会被剥离, 不能靠 assert 守不变量）、
    不出台账卡、不占 ledger_due/fallback_due。pre/post 两个入口都钉。"""
    mod = env.mod
    monkeypatch.setattr(mod, "_launch_via_channels",
                        lambda *a, **k: (None, "subagent_lifecycle"))
    st = mod._get_state("s1")
    r = mod._dispatch_sub_expert(env.ctx, st, "s1", "msg", [], "engineering",
                                 "engineering-security-engineer", 0.9)
    assert r == (None, None), "pre 入口: 契约违反不得 raise、不得出卡"
    mod._launch_pending(env.ctx, st, "s1", "engineering-security-engineer",
                        "engineering", 0.9, "label", "goal", "ctx-text",
                        mod._EXPERTS_DIR / "engineering-security-engineer.md")
    assert st.ledger_due == {} and st.fallback_due == {}, "post 入口: 异常不得污染记账面"
    assert [e for e in _events(env) if e["event"] == "dispatch-anomaly"], _events(env)


def test_post_success_lands_ledger_due(env):
    """F4: post 模式 launch 成功记账 st.ledger_due（3 元组 (id, label, channel)）,
    下一 pre 轮消费交付 post 变体台账卡——写/读循环钉死。"""
    svc = _FakeLifecycle()
    _setup_post_mode(env, svc)
    _post(env, "做个安全审查")
    st = env.mod._SESSIONS["s1"]
    deleg_id = st.ledger_due["engineering-security-engineer"][0]
    assert st.ledger_due["engineering-security-engineer"] == (
        deleg_id, "做个安全审查", "subagent_lifecycle")
    env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
    r3 = _turn(env, "继续", first=False)
    assert r3 is not None and "分工台账" in r3["context"]
    assert st.ledger_due == {}, "消费后须清空"
