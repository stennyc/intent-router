"""intent-router v2.7 review-fixes — reviewer 复盘（2026-11-17）遗留三项。

G1 短消息 classify 护栏（"修" 单字被 aux 误路由给 marketing-seo 实证）:
   后续轮 msg < 4 字符直接跳过 aux 分类（0 成本、静默、无新事件）——单/双字
   插话无领域信号，误派即噪声；≥4 字符照旧分类（"做个安全审查" 6 字不受影响）。
   首轮 10 字问候守卫不变。
F2 pending-stuck 诊断（post 模式可观测缺口）:
   post 模式 pending 记账后若长期未 launch（宿主 post hook 未 fire / worker 崩）,
   后续 pre 轮同域 → 记 pending-stuck（带 age_s）替代普通 skip-pending；
   pending 不清除 —— 迟到的 post hook 仍可恢复 launch。健康路径（同轮 post 已
   launch）行为不变（skip-pending 语义由 test_launch_timing 既有用例钉死）。
F3 post 首轮盲性文档化: README + manifest launch_timing 描述显性化取舍——
   主专家首轮回复先于 launch 生成, 台账卡到下一条用户消息才送达, findings
   押在下一轮轮询（gateway 重启即丢）。纯文档, 零行为变化。

Run: C:/tmp/cmap-venv/Scripts/python -m pytest tests/ -q   (pytest + stdlib only)
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_v27_review", root / "__init__.py")
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

    def launch(self, request):
        self.launch_calls.append(dict(request))
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
        self.settings = {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def subagent_lifecycle(self):
        return self.lifecycle_svc

    def dispatch_tool(self, tool_name, args, **kw):
        self.dispatch_calls.append((tool_name, args, kw))
        if self.dispatch_result is not None:
            return self.dispatch_result
        self._n += 1
        return json.dumps({"delegation_id": f"deleg_{self._n:04d}"})

    def get_config(self, key, default=None):
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
    mod.register(ctx)
    return SimpleNamespace(mod=mod, ctx=ctx, experts=experts, log=tmp_path / "logs" / "dispatches.jsonl")


def _turn(env, user, first, history=None, session_id="s1", platform="cli"):
    mod, ctx = env.mod, env.ctx
    return mod._make_hook(ctx)(
        session_id=session_id, user_message=user, conversation_history=history or [],
        is_first_turn=first, model="m", platform=platform,
    )


def _post(env, user, session_id="s1", platform="cli", assistant="（主专家已回复）"):
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


def _anchor_and_domain_switch(env, svc):
    """turn1 engineering 锚点 + turn2 切 security 域（pre 侧记账/派发, 视模式而定）。"""
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    return _turn(env, "做个安全审查", first=False)


# ── G1 短消息 classify 护栏 ─────────────────────────────────────────────────

def test_short_subsequent_message_skips_classify(env):
    """G1: 后续轮单字 "修"（< 4 字符）→ 跳过 aux 分类（script 不消费）、
    零派发、零事件；≥4 字符消息（"做个安全审查" 6 字）照旧分类派发。"""
    svc = _FakeLifecycle()
    _anchor_and_domain_switch(env, svc)   # s1: 锚点 + pre 模式派发（基线）
    launches_before = len(svc.launch_calls)
    # 干净 session s2 验 G1（隔离 s1 的 running 状态）
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    _turn(env, "帮我写个 REST 后端 API", first=True, session_id="s2")
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    r = _turn(env, "修", first=False, session_id="s2")
    assert r is None
    assert len(svc.launch_calls) == launches_before, "短消息不得真派发"
    assert env.ctx.llm.scripts, "classify 未被调用 → script 仍留存"
    # ≥4 字符消息不受护栏影响: security 6 字 → 照旧分类派发
    _turn(env, "做个安全审查", first=False, session_id="s2")
    assert len(svc.launch_calls) == launches_before + 1, "≥4 字符消息必须照旧分类派发"


def test_short_message_gate_is_silent_no_event(env):
    """G1 静默语义: 短消息跳过不记任何事件（常见路径——owner 单字指令风格,
    不得刷 jsonl）。"""
    svc = _FakeLifecycle()
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    _turn(env, "推", first=False)
    evs = _events(env)
    assert not [e for e in evs if e["event"] in ("classify-skipped-short", "dispatch", "skip-pending")], evs


# ── F2 pending-stuck 诊断（post 模式） ─────────────────────────────────────

def test_post_mode_pending_stuck_diagnostic(env):
    """F2: post 模式 pending 滞留 > 300s（宿主 post hook 未 fire）→
    后续 pre 轮同域记 pending-stuck（带 age_s）而非普通 skip-pending；
    pending 不清除（迟到的 post hook 仍可恢复 launch）。"""
    svc = _FakeLifecycle()
    env.ctx.settings = {"launch_timing": "post"}
    _anchor_and_domain_switch(env, svc)
    st = env.mod._SESSIONS["s1"]
    assert st.pending, "前置: pre 轮已记 pending"
    slug = "engineering-security-engineer"
    entry = st.pending[slug]
    # 回拨 recorded_at 400s（> _PENDING_STUCK_SECONDS=300）, 模拟 post 未 fire
    st.pending[slug] = entry[:-1] + (entry[-1] - 400,)
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    _turn(env, "再做个安全审查", first=False)
    evs = _events(env)
    stuck = [e for e in evs if e["event"] == "pending-stuck"]
    assert stuck and stuck[0].get("age_s", 0) > 300, evs
    assert st.pending, "pending 必须保留 —— 迟到的 post hook 仍可恢复 launch"
    assert len(svc.launch_calls) == 0, "滞留期间不得假派发"


def test_post_mode_fresh_pending_stays_skip_pending(env):
    """F2 健康路径零变化: pending 新鲜（同轮 post 即将/已 launch 场景）→
    仍是普通 skip-pending, 不冒 pending-stuck。"""
    svc = _FakeLifecycle()
    env.ctx.settings = {"launch_timing": "post"}
    _anchor_and_domain_switch(env, svc)
    st = env.mod._SESSIONS["s1"]
    assert st.pending
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    _turn(env, "再做个安全审查", first=False)
    evs = _events(env)
    assert [e for e in evs if e["event"] == "skip-pending"], evs
    assert not [e for e in evs if e["event"] == "pending-stuck"], evs


# ── G1 × 终态协同（v2.4.3 owner 策略 B 不回归） ───────────────────────────

def test_terminal_short_new_domain_message_still_classifies_and_redispatches(env):
    """v2.7 G1×终态: 终态（failed）后, 短驱动消息因 redispatch_due 旗标被 G1 放行
    分类; 分类结果 = 新域（≠ 锚点）→ 走新域派发路径同轮重派（owner 策略 B,
    v2.4.3 语义）—— 这正是 G1 护栏不得回归的点。"""
    svc = _FakeLifecycle()
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)   # anchor = engineering
    _turn(env, "做个安全审查", first=False)            # 派发 security 子专家
    st = env.mod._SESSIONS["s1"]
    sid1 = st.lifecycle["engineering-security-engineer"][0]
    svc.status_script[sid1] = "FAILED"
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    _turn(env, "继续", first=False)   # 2 字 + 旗标在位 → G1 放行分类 → 新域 → 重派
    evs = _events(env)
    assert [e for e in evs if e["event"] == "subagent-result-failed"], evs
    entry = st.lifecycle.get("engineering-security-engineer")
    assert entry and entry[0] != sid1, "新域短消息同轮重派（owner 策略 B 不回归）"
    assert st.redispatch_due is None, "旗标同轮结算即清"


def test_terminal_short_anchor_domain_message_no_forced_redispatch(env):
    """v2.7 钉死: 终态后, 短驱动消息若分类到锚点域 → 不自动重派刚终态域
    （lifecycle 保持空）—— 终态交接卡归主专家决策, 插件不越权强制重派
    （test_l3_failed_handoff_card 等既有终态测试的语义）。"""
    svc = _FakeLifecycle()
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)   # anchor = engineering
    _turn(env, "做个安全审查", first=False)            # 派发 security 子专家
    st = env.mod._SESSIONS["s1"]
    sid1 = st.lifecycle["engineering-security-engineer"][0]
    svc.status_script[sid1] = "FAILED"
    env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
    _turn(env, "继续", first=False)   # 2 字 + 旗标 → 分类放行 → 结果 = 锚点域 → 不强制重派
    evs = _events(env)
    assert [e for e in evs if e["event"] == "subagent-result-failed"], evs
    assert st.lifecycle == {}, "锚点域短消息不得触发自动重派（交接卡归主专家）"
    assert st.redispatch_due is None, "旗标同轮结算即清"
