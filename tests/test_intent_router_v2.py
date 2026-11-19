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


def test_followup_different_category_hints(env):
    """v3: 新域过 gate → 委派提示卡（指示模型自己 delegate_task），无确定性派发。"""
    ctx1 = _first_turn_ctx(env)
    env.ctx.llm.scripts = [
        {"category": "security", "confidence": 0.9},
    ]
    r = _turn(env, "查一下这个 API 的 SQL 注入", first=False, history=[_user_msg(ctx1)])
    assert r is not None, "new domain past gate must surface the hint card"
    assert "委派提示" in r["context"]
    assert "engineering-security-engineer" in r["context"]
    assert "delegate_task" in r["context"]
    assert env.ctx.dispatch_calls == []
    assert env.mod._SESSIONS["s1"].anchor == "engineering-backend-architect"  # 锚点不被 hint 改


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
    """v3: 无 running 状态可清 —— 完成轮后的新域消息不再出卡（幂等, 压缩后重置才重出）。"""
    _first_turn_ctx(env)
    env.ctx.llm.scripts = [
        {"category": "security", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    r1 = _turn(env, "查一下注入", first=False, history=[_user_msg("x")])
    assert r1 is not None and "委派提示" in r1["context"]
    _turn(env, _completion_msg("deleg_0001"), first=False, history=[_user_msg("x")])
    r2 = _turn(env, "再查下注入", first=False,
               history=[_user_msg("x"), _user_msg(r1["context"] + "\n" + _completion_msg("deleg_0001"))])
    assert r2 is None, "已提示过的域不再重出（幂等）"
    assert _events(env).count("hint") == 1


# ── 缓存安全结构（尾部加法, 前缀不动）─────────────────────────────────


def test_prefix_cache_stable_across_turns(env):
    """只有事件轮（首轮锚注 / 新域 hint）注入 context; 非事件轮字节不变。
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
    hist.append(_user_msg(r3["context"]))
    r4 = _turn(env, "把注入修掉", first=False, history=hist)
    injection_turns = [i for i, r in enumerate([r1, r2, r3, r4]) if r is not None]
    assert injection_turns == [0, 2], f"only event turns must inject: {injection_turns}"


# ── 观测面 ─────────────────────────────────────────────────────────────


def test_structured_log_written_utf8(env):
    _first_turn_ctx(env)
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    _turn(env, "查一下注入", first=False, history=[_user_msg("x")])
    assert env.log.exists()
    line = json.loads(env.log.read_text(encoding="utf-8").splitlines()[-1])
    assert line["event"] == "hint"
    assert line["expert"] == "engineering-security-engineer"
    assert line["confidence"] == 0.9
    assert line["category"] == "security"


def test_log_failure_is_fail_soft(env, monkeypatch):
    """日志故障（磁盘不可写）绝不影响 hook 注入 —— 走 _log 内部 try/except。
    模拟: 让 _LOG_PATH 的父目录链上挂一个普通文件 → mkdir 必失败。"""
    logs_dir = env.log.parent
    logs_dir.mkdir(parents=True, exist_ok=True)
    blocker = logs_dir / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(env.mod, "_LOG_PATH", blocker / "inner" / "dispatches.jsonl")
    _first_turn_ctx(env)
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r = _turn(env, "查一下注入", first=False, history=[_user_msg("x")])
    assert r is not None and "委派提示" in r["context"]


def test_expert_path_is_native_absolute(env):
    """专家资产路径必须是「运行 OS 的原生绝对形态」。
    - Windows: 盘符绝对路径（junction 可解析, MSYS /c/ 形态会断）→ 断言 C:\\ 或 C:/
    - POSIX: 绝对路径以 / 开头且不含 C: 盘符 → 断言 is_absolute()

    按 os.name 自动检测运行平台, 不再硬编码单一 OS（此前 Linux 上必挂:
    env.experts 是 pytest tmp_path, 非 Windows 下无 C: 盘符）。
    """
    import os
    _first_turn_ctx(env)
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r = _turn(env, "查一下注入", first=False, history=[_user_msg("x")])
    ctx_text = r["context"]
    # 路径字符串必须出现在注入文本里
    assert str(env.experts / "engineering-security-engineer.md") in ctx_text
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
