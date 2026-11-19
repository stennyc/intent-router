"""intent-router v3.0 — 锚点专家路由 + 委派提示（TDD suite）.

v3 owner ruling (2026-11-18): 确定性派发通道退役 → 模型自决委派。
- first-turn classify → 锚点专家注入 (context["expert"] + 尾部标记)
- follow-up 新域 (≠锚点, 过 conf gate) → append-only "委派提示" 卡:
  指示模型用宿主 delegate_task 工具自己拉子专家; 结果宿主自动重入
- 子 agent 会话 (subagent_session_id) → 整条流水线短路 (P0 守卫)
- 合成完成重入轮 ([ASYNC DELEGATION ...]) → 先于分类跳过
- 短后续消息 / 问句 → G1 + 问句门跳过分类 (静默)
- 压缩后锚点/专家文本消失 → 卡补注 (或全文回退)
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_v30", root / "__init__.py")
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeLLM:
    """complete_structured: 返回带 .parsed 的对象, 模拟宿主 LLMClient 契约."""

    def __init__(self):
        self.scripts = []
        self.calls = []

    def complete_structured(self, **kw):
        self.calls.append(kw)
        parsed = self.scripts.pop(0) if self.scripts else None
        return SimpleNamespace(parsed=parsed)


class _FakeCtx:
    """模拟宿主 PluginContext 的 duck-typed 接口面（含已退役的
    dispatch_tool —— v3 起插件不再调用, 测试靠它断言"零调用"）."""

    def __init__(self):
        self.llm = _FakeLLM()
        self.dispatch_calls = []
        self.hooks = {}
        self._n = 0

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def dispatch_tool(self, tool_name, args, **kw):
        self.dispatch_calls.append((tool_name, args, kw))
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
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "engineering-backend-architect.md").write_text("CARD-BACKEND-BODY", encoding="utf-8")
    mod._EXPERTS_DIR = experts
    mod._CARDS_DIR = cards
    mod._LOG_PATH = tmp_path / "logs" / "dispatches.jsonl"
    mod._fetch_expert_from_github = lambda slug: None  # no network in tests
    ctx = _FakeCtx()
    return SimpleNamespace(mod=mod, ctx=ctx, experts=experts, cards=cards,
                           log=tmp_path / "logs" / "dispatches.jsonl")


def _turn(env, user, first, history=None, session_id="s1"):
    mod, ctx = env.mod, env.ctx
    return mod._make_hook(ctx)(
        session_id=session_id, user_message=user, conversation_history=history or [],
        is_first_turn=first, model="m", platform="cli",
    )


def _user_msg(text):
    return {"role": "user", "content": text}


def _completion_msg(deleg_id, batch=True):
    marker = "[ASYNC DELEGATION BATCH COMPLETE — " if batch else "[ASYNC DELEGATION COMPLETE — "
    return (f"{marker}{deleg_id}]\n"
            "A background subagent you dispatched earlier has finished.\n"
            "Status: completed   API calls: 2   Duration: 41s\n"
            "--- RESULT ---\nSQL injection found in api/orders.")


def _first_turn_ctx(env):
    """首轮锚定并返回注入的 context（含锚点标记）。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
    ]
    r = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r is not None and "context" in r
    return r["context"]


def _events(env):
    return [json.loads(line)["event"] for line in env.log.read_text(encoding="utf-8").splitlines()]


# ── first-turn behavior ───────────────────────────────────────────────


def test_first_turn_injects_expert_context(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
    ]
    r = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r is not None, "first turn must inject expert context"
    assert "context" in r
    assert "BACKEND-EXPERT-BODY" in r["context"]
    assert "<!--ir-expert:engineering-backend-architect-->" in r["context"]
    # 尾部加法: 专家正文在前, 路由器标记在后
    assert r["context"].index("BACKEND-EXPERT-BODY") < r["context"].index("intent-router")


def test_first_turn_confidence_below_gate_no_expert(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.4},
    ]
    r = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r is None, "low confidence must NOT inject"


def test_first_turn_no_expert_available(env):
    env.ctx.llm.scripts = [
        {"category": "unknown-domain", "confidence": 0.95},
    ]
    r = _turn(env, "帮我看看这个", first=True)
    assert r is None


def test_first_turn_no_category_below_min_confidence(env):
    env.ctx.llm.scripts = [
        {"category": "", "confidence": 0.3},
    ]
    r = _turn(env, "hi", first=True)
    assert r is None


# ── follow-up behavior (v3: 新域只出委派提示) ─────────────────────────


def test_followup_same_expert_no_injection(env):
    ctx1 = _first_turn_ctx(env)
    env.ctx.llm.scripts = [{"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9}]
    r = _turn(env, "再加个单元测试", first=False, history=[_user_msg(ctx1)])
    assert r is None, "same expert already in context → no re-injection"
    assert env.ctx.dispatch_calls == []


def test_followup_different_category_no_injection(env):
    """v3.5.1: 新域过 gate 不再出 hint 卡（hint 卡面已删）—— 锚点标记仍可见的
    常规后续轮零注入; 锚点不被后续轮改写。"""
    ctx1 = _first_turn_ctx(env)
    env.ctx.llm.scripts = [
        {"category": "security", "confidence": 0.9},
    ]
    r = _turn(env, "查一下这个 API 的 SQL 注入", first=False, history=[_user_msg(ctx1)])
    assert r is None, "hint 卡面已删: 常规后续轮（锚点标记可见）必须零注入"
    assert env.ctx.dispatch_calls == []
    assert env.mod._SESSIONS["s1"].anchor == "engineering-backend-architect"  # 锚点不被改


def test_followup_card_injection_never_full_text(env):
    ctx1 = _first_turn_ctx(env)
    env.ctx.llm.scripts = [
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
    ]
    r = _turn(env, "再加个单元测试", first=False, history=[_user_msg("[压缩摘要]")])
    assert r is not None
    assert "CARD-BACKEND-BODY" in r["context"]
    assert "BACKEND-EXPERT-BODY" not in r["context"]


def test_lru_eviction_keeps_recent_sessions(env):
    """LRU: 超过 _SESSIONS_MAX 的创建淘汰最旧, 最近访问者留驻。"""
    mod = env.mod
    for i in range(mod._SESSIONS_MAX):
        mod._get_state(f"s{i}")
    mod._get_state("fresh")
    assert len(mod._SESSIONS) == mod._SESSIONS_MAX, "LRU cap must evict oldest"
    assert "s0" not in mod._SESSIONS
    assert "fresh" in mod._SESSIONS
    assert f"s{mod._SESSIONS_MAX - 1}" in mod._SESSIONS


# ── 合成完成重入轮（宿主 delegate_task 回传, v3 跳过一切检测）────────


def test_completion_reentry_turn_skips_detection_batch(env):
    """[ASYNC DELEGATION BATCH COMPLETE] 合成轮: 先于分类+提示跳过, 不烧 aux。"""
    _first_turn_ctx(env)
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    n_calls = len(env.ctx.llm.calls)
    r = _turn(env, _completion_msg("deleg_0001", batch=True), first=False,
              history=[_user_msg("x")])
    assert r is None
    assert len(env.ctx.llm.calls) == n_calls
    assert "completion-skip" in _events(env)


def test_completion_reentry_single_task_form(env):
    """单任务 [ASYNC DELEGATION COMPLETE — ...] 同律跳过（宿主两种形态）。"""
    _first_turn_ctx(env)
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    n_calls = len(env.ctx.llm.calls)
    r = _turn(env, _completion_msg("deleg_0002", batch=False), first=False,
              history=[_user_msg("x")])
    assert r is None
    assert len(env.ctx.llm.calls) == n_calls
    assert "completion-skip" in _events(env)


def test_no_redispatch_after_completion_in_v3(env):
    """v3.5.1: 完成轮后新域消息不再出 hint 卡（hint 卡面已删; 锚点标记可见 → 零注入）。
    完成轮本身被 completion-skip 循环守卫拦截。"""
    _first_turn_ctx(env)
    env.ctx.llm.scripts = [
        {"category": "security", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    r1 = _turn(env, "查一下注入", first=False, history=[_user_msg("x")])
    # 压缩补注（活逻辑）给卡/全文, 但 hint 事件必须归零
    assert "hint" not in _events(env)
    _turn(env, _completion_msg("deleg_0001"), first=False, history=[_user_msg("x")])
    assert "completion-skip" in _events(env)


# ── 缓存安全结构（尾部加法, 前缀不动）─────────────────────────────────


def test_prefix_cache_stable_across_turns(env):
    """v3.5.1: 只有事件轮（首轮锚注 / 压缩补注轮）注入 context; 常规轮字节不变。
    hint 卡面已删 → 新域常规轮（锚点标记可见）零注入, 不污染前缀。
    注: 历史必须包成 user 消息 dict —— _history_text 只消费 dict 消息,
    裸字符串会被跳过（标记检测随之失明）, 这是本用例踩过的坑。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    r1 = _turn(env, "帮我写个 REST 后端 API", first=True)
    hist = [_user_msg(r1["context"])]
    r2 = _turn(env, "再加个单元测试", first=False, history=hist)
    r3 = _turn(env, "查一下这个 API 的 SQL 注入", first=False, history=hist)
    # 常规后续轮（锚点标记在 hist）: r2/r3 必须零注入 → 不 append 进 hist
    assert r2 is None, "常规轮必须零注入"
    assert r3 is None, "hint 卡面已删: 新域常规轮零注入"
    r4 = _turn(env, "把注入修掉", first=False, history=hist)
    assert r4 is None, "无压缩事件 → 零注入"
    injection_turns = [i for i, r in enumerate([r1, r2, r3, r4]) if r is not None]
    assert injection_turns == [0], f"only event turns must inject: {injection_turns}"


def test_prefix_cache_stable_with_compression_reinject(env):
    """v3.5.1: 压缩补注（活逻辑）是常规轮唯一注入面 —— 标记缺失轮注入卡/全文,
    常规轮字节不变（前缀缓存安全）。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
    ]
    r1 = _turn(env, "帮我写个 REST 后端 API", first=True)
    # turn1: 锚点标记消失（[压缩摘要]）→ 压缩补注轮
    r2 = _turn(env, "查一下注入", first=False, history=[_user_msg("[压缩摘要]")])
    assert r2 is not None, "压缩补注轮必须注入（卡或全文）"
    # 补注后标记回历史 → 常规轮零注入
    hist = [_user_msg("[压缩摘要]"), _user_msg(r2["context"])]
    r3 = _turn(env, "继续查", first=False, history=hist)
    assert r3 is None, "标记可见后常规轮必须零注入"


# ── 观测面 ─────────────────────────────────────────────────────────────


def test_structured_log_written_utf8(env):
    """_log 仍活（anchor / card-reinject / completion-skip 等事件走 _log）;
    断言 _log UTF-8 写入能力（不依赖已删的 hint 事件）。"""
    ctx1 = _first_turn_ctx(env)
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    _turn(env, "查一下这个 API 的 SQL 注入", first=False, history=[_user_msg("[压缩摘要]")])
    assert env.log.exists()
    lines = [json.loads(line) for line in env.log.read_text(encoding="utf-8").splitlines()]
    assert lines, "至少应有 anchor + card-reinject 事件"
    # 首个事件必须是 anchor（_log UTF-8 写入验证面）
    assert lines[0]["event"] == "anchor"
    assert lines[0]["expert"] == "engineering-backend-architect"
    assert lines[0]["confidence"] == 0.9
    assert lines[0]["category"] == "engineering"


def test_log_failure_is_fail_soft(env, monkeypatch):
    """_log fail-soft: 日志路径不可写（父目录链挂普通文件 → mkdir 必失败）
    绝不影响 hook 注入 —— 走 _log 内部 try/except。用 anchor 事件验证（_log 仍活）。"""
    logs_dir = env.log.parent
    logs_dir.mkdir(parents=True, exist_ok=True)
    blocker = logs_dir / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(env.mod, "_LOG_PATH", blocker / "inner" / "dispatches.jsonl")
    env.ctx.llm.scripts = [
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
    ]
    r = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r is not None and "BACKEND-EXPERT-BODY" in r["context"], \
        "日志故障不得阻断 anchor 注入（_log fail-soft）"


def test_expert_path_is_native_absolute(env):
    """专家资产路径必须是「运行 OS 的原生绝对形态」—— 压缩补注回退全文路径
    （卡缺失时 _load_expert_context 读 experts/<slug>.md）, 不依赖已删的 hint 卡面。
    - Windows: 盘符绝对路径（junction 可解析, MSYS /c/ 形态会断）→ 断言 C:\\ 或 C:/
    - POSIX: 绝对路径以 / 开头且不含 C: 盘符 → 断言 is_absolute()

    按 os.name 自动检测运行平台, 不再硬编码单一 OS（此前 Linux 上必挂:
    env.experts 是 pytest tmp_path, 非 Windows 下无 C: 盘符）。
    v3.5.1: hint 卡面已删, 不再断言 hint 注入文本含路径字符串; 改走压缩补注
    回退全文路径验证 _load_expert_context（读 experts/<slug>.md）的活逻辑。
    """
    import os
    env.cards.joinpath("engineering-backend-architect.md").unlink()  # 强制回退全文路径
    _first_turn_ctx(env)
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r = _turn(env, "查一下注入", first=False, history=[_user_msg("[压缩摘要]")])
    ctx_text = r["context"]
    # 回退全文包装含 '压缩后补注' 标识 + 专家正文（_load_expert_context 读 experts/<slug>.md）
    assert "压缩后补注" in ctx_text and "BACKEND-EXPERT-BODY" in ctx_text, \
        "回退全文注入须含专家正文（_load_expert_context 读 experts/<slug>.md 的活逻辑验证）"
    if os.name == "nt":
        # Windows: 必须是盘符绝对路径
        assert "C:\\" in ctx_text or "C:/" in ctx_text, "Windows 下专家路径应为 C:\\ 盘符形态"
    else:
        # POSIX (Linux/macOS): 必须是绝对路径, 且不得出现 Windows 盘符形态
        p = Path(str(env.experts / "engineering-security-engineer.md"))
        assert p.is_absolute(), f"POSIX 下专家路径应为绝对路径: {p}"
        assert "C:\\" not in ctx_text and "C:/" not in ctx_text, "POSIX 下不应混入 Windows 盘符"


def test_no_hardcoded_paths_in_expert_text(env):
    ctx1 = _first_turn_ctx(env)
    for path in ("C:\\", "/c/Users/", "home/user"):
        assert path not in ctx1
