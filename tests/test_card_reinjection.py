"""intent-router v2.1 — 压缩后专家卡补注（TDD suite）。

Owner rulings encoded (2026-11-16):
- 卡 = 离线蒸馏静态资产 cards/<slug>.md（≤600 token，带判断标准不带模板）；
  小专家（≤1.1KB）不蒸馏——它们的补注走全文回退（本来就便宜）。
- 卡缺失兜底 = 回退补注全文（成本退回但专家不丢）；卡缺失是资产 bug，日志记一笔。
- 压缩检测 = 标记消失：非空 conversation_history 中 锚点标记 与 卡标记 均不见
  = 专家文本被压缩/剪枝消化。空历史（测试工件/压缩前状态）永不触发。
- 缓存安全：补注只出现在事件轮 user message 尾部；其余轮字节不变。

Run: python -m pytest tests/ -v   (scratch venv, pytest + stdlib only, no network)
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_v21", root / "__init__.py")
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
        parsed = self.scripts.pop(0) if self.scripts else None
        return SimpleNamespace(parsed=parsed)


class _FakeCtx:
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
    return (
        f"{marker}{deleg_id}]\n"
        "A background subagent you dispatched earlier has finished.\n"
        "Status: completed   API calls: 2   Duration: 41s\n"
        "--- RESULT ---\n"
        "SQL injection found in api/orders; parameterized queries recommended."
    )


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


# ── 标记（压缩检测前提） ─────────────────────────────────────────────────────

def test_first_turn_injection_embeds_anchor_marker(env):
    ctx = _first_turn_ctx(env)
    assert "<!--ir-expert:engineering-backend-architect-->" in ctx
    assert "BACKEND-EXPERT-BODY" in ctx


# ── 压缩事件 → 卡补注 ───────────────────────────────────────────────────────

def test_compression_event_reinjects_card(env):
    ctx1 = _first_turn_ctx(env)
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},   # 标记还在 → 零动作
        {"category": "engineering", "confidence": 0.9},   # 标记消失 → 补卡
    ]
    r = _turn(env, "再加个单元测试", first=False, history=[_user_msg(ctx1), _user_msg("嗯")])
    assert r is None                                       # 专家可见：零注入（v2 不变量）
    r = _turn(env, "查一下注入", first=False,
              history=[_user_msg("[压缩摘要：此前讨论过 REST 后端 API]")])
    assert r is not None and "context" in r
    assert "CARD-BACKEND-BODY" in r["context"]
    assert "<!--ir-card:engineering-backend-architect-->" in r["context"]
    assert "card-reinject" in _events(env)


def test_card_reinjection_idempotent_while_visible(env):
    ctx1 = _first_turn_ctx(env)
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "engineering", "confidence": 0.9},
        {"category": "engineering", "confidence": 0.9},
    ]
    r = _turn(env, "查一下注入", first=False,
              history=[_user_msg("[压缩摘要]")])
    assert r is not None and "CARD-BACKEND-BODY" in r["context"]
    card_ctx = r["context"]
    r2 = _turn(env, "继续查", first=False,
               history=[_user_msg("[压缩摘要]"), _user_msg(card_ctx)])
    assert r2 is None                                      # 卡标记可见 → 不重复补


def test_card_compressed_again_reinjects_again(env):
    ctx1 = _first_turn_ctx(env)
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "engineering", "confidence": 0.9},
    ]
    r1 = _turn(env, "查一下注入", first=False, history=[_user_msg("[压缩摘要 v1]")])
    assert r1 is not None
    r2 = _turn(env, "继续查", first=False, history=[_user_msg("[压缩摘要 v2 —— 卡也被消化了]")])
    assert r2 is not None and "CARD-BACKEND-BODY" in r2["context"]  # 卡便宜，再丢再补


def test_missing_card_falls_back_to_full_text(env):
    """owner 裁决：卡缺失 → 回退补注全文（成本退回但专家不丢）。"""
    env.cards.joinpath("engineering-backend-architect.md").unlink()
    ctx1 = _first_turn_ctx(env)
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r = _turn(env, "查一下注入", first=False, history=[_user_msg("[压缩摘要]")])
    assert r is not None and "context" in r
    assert "BACKEND-EXPERT-BODY" in r["context"]
    assert "full-reinject-fallback" in _events(env)


# ── 不变量 / 边界 ────────────────────────────────────────────────────────────

def test_no_anchor_no_reinjection(env):
    env.ctx.llm.scripts = [
        None,                                             # 首轮分类 null → 无锚点
        {"category": "engineering", "confidence": 0.9},
    ]
    assert _turn(env, "随便聊聊", first=True) is None
    r = _turn(env, "查一下注入", first=False, history=[_user_msg("[压缩摘要]")])
    assert r is None


def test_empty_history_followup_never_reinjects(env):
    """空历史 = 测试工件/压缩前状态 → 永不触发（v2 既有 follow-up 测试依赖此不变量）。"""
    _first_turn_ctx(env)
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r = _turn(env, "再加个单元测试", first=False, history=[])
    assert r is None


def test_synthetic_completion_turn_never_reinjects(env):
    """合成完成轮被循环守卫先行拦截 —— 即使历史同时缺标记也不补注。"""
    ctx1 = _first_turn_ctx(env)
    env.ctx.llm.scripts = [
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "查一下这个 API 的 SQL 注入", first=False, history=[_user_msg(ctx1)])
    assert len(env.ctx.dispatch_calls) == 1
    r = _turn(env, _completion_msg("deleg_0001"), first=False,
              history=[_user_msg("[压缩摘要]")])
    assert r is None
    assert "card-reinject" not in _events(env)


def test_compression_and_dispatch_same_turn(env):
    """压缩轮用户同时问新域 → 补注 + 派发同轮完成（补注是尾部加法，派发是后台副作用）。"""
    ctx1 = _first_turn_ctx(env)
    env.ctx.llm.scripts = [
        {"category": "security", "confidence": 0.9},
    ]
    r = _turn(env, "查一下这个 API 的 SQL 注入", first=False,
               history=[_user_msg("[压缩摘要]")])
    assert len(env.ctx.dispatch_calls) == 1
    assert r is not None and "CARD-BACKEND-BODY" in r["context"]


def test_reinjection_does_not_mutate_history(env):
    """缓存安全结构保证：hook 对 conversation_history 只读。"""
    _first_turn_ctx(env)
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    hist = [_user_msg("[压缩摘要]")]
    snapshot = json.dumps(hist, ensure_ascii=False)
    r = _turn(env, "查一下注入", first=False, history=hist)
    assert r is not None
    assert json.dumps(hist, ensure_ascii=False) == snapshot
