"""intent-router v2.2 — /expert 手动覆盖（TDD suite）。

Owner 裁决（2026-11-17 "继续完成" backlog /expert）:
- /expert <category|slug> 切锚点；该轮走宿主 veto（{"action":"block"}）不进推理、不烧分类
- 专家标记不在当前上下文 → 切换后下一轮补注全文（刻意行为给全文不给卡）；
  标记已在上下文 → 只切锚点，零注入（专家已在场）
- /expert 无参 = 列表；/expert clear = 清锚点；未知参数 = 报错不动状态

Run: python -m pytest tests/ -v   (scratch venv, pytest + stdlib only, no network)
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_v22", root / "__init__.py")
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


def _completion_msg(deleg_id):
    return (f"[ASYNC DELEGATION BATCH COMPLETE — {deleg_id}]\n"
            "A background subagent you dispatched earlier has finished.\n"
            "--- RESULT ---\nSQL injection found in api/orders.")


def _events(env):
    return [json.loads(line)["event"] for line in env.log.read_text(encoding="utf-8").splitlines()]


def _sessions(env):
    return env.mod._SESSIONS


def _first_turn_ctx(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
    ]
    r = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r is not None and "context" in r
    return r["context"]


# ── 切换语义 ────────────────────────────────────────────────────────────────

def test_expert_switch_blocks_and_switches_anchor(env):
    r = _turn(env, "/expert security", first=False, history=[_user_msg("x")])
    assert isinstance(r, dict) and r.get("action") == "block"
    assert "engineering-security-engineer" in r["message"]
    assert _sessions(env)["s1"].anchor == "engineering-security-engineer"
    assert env.ctx.llm.calls == []                    # 命令轮不烧分类
    assert "manual-anchor" in _events(env)


def test_expert_switch_next_turn_full_text(env):
    """标记不在上下文 → 切换后下一轮补注全文（不是卡）。"""
    r1 = _turn(env, "/expert security", first=False, history=[_user_msg("x")])
    assert r1["action"] == "block"
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r2 = _turn(env, "查一下这个 API 的 SQL 注入", first=False, history=[_user_msg("x")])
    assert r2 is not None and "context" in r2
    assert "SECURITY-EXPERT-BODY" in r2["context"]
    assert "<!--ir-expert:engineering-security-engineer-->" in r2["context"]
    assert "manual-full-reinject" in _events(env)


def test_expert_slug_form(env):
    r = _turn(env, "/Expert ENGINEERING-SECURITY-ENGINEER", first=False, history=[_user_msg("x")])
    assert isinstance(r, dict) and r.get("action") == "block"
    assert _sessions(env)["s1"].anchor == "engineering-security-engineer"


def test_expert_switch_visible_marker_only_switches_anchor(env):
    """专家标记已在上下文 → 只切锚点（专家已在场），后续压缩事件用卡不用全文。"""
    ctx1 = _first_turn_ctx(env)
    r = _turn(env, "/expert engineering-backend-architect", first=False, history=[_user_msg(ctx1)])
    assert r["action"] == "block"
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r2 = _turn(env, "再加个单元测试", first=False, history=[_user_msg("[压缩摘要]")])
    assert r2 is not None and "CARD-BACKEND-BODY" in r2["context"]   # 卡，不是全文（flag 被消费）
    assert "card-reinject" in _events(env)


# ── 列表 / 清空 / 错误 ────────────────────────────────────────────────────

def test_expert_no_arg_lists_categories(env):
    _first_turn_ctx(env)
    r = _turn(env, "/expert", first=False, history=[_user_msg("x")])
    assert r["action"] == "block"
    assert "engineering-backend-architect" in r["message"]   # 当前锚点
    assert "engineering" in r["message"] and "security" in r["message"]
    assert _sessions(env)["s1"].anchor == "engineering-backend-architect"  # 列表不动状态


def test_expert_clear_resets_anchor(env):
    _first_turn_ctx(env)
    r = _turn(env, "/expert clear", first=False, history=[_user_msg("x")])
    assert r["action"] == "block"
    assert _sessions(env)["s1"].anchor is None
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r2 = _turn(env, "再加个单元测试", first=False, history=[_user_msg("y")])
    assert r2 is not None and "委派提示" in r2["context"]   # v3: 无锚点 → 无补注（意图不变）; hint 轮出提示卡


def test_expert_invalid_no_state_change(env):
    _first_turn_ctx(env)
    r = _turn(env, "/expert nosuchthing", first=False, history=[_user_msg("x")])
    assert r["action"] == "block"
    assert _sessions(env)["s1"].anchor == "engineering-backend-architect"
    assert "manual-invalid" in _events(env)


def test_expert_on_first_turn(env):
    r = _turn(env, "/expert security", first=True, history=[])
    assert r["action"] == "block"
    assert _sessions(env)["s1"].anchor == "engineering-security-engineer"
    assert env.ctx.llm.calls == []


# ── 与派发/重入的交互 ─────────────────────────────────────────────────────

def test_expert_switch_after_hint_and_completion_skip(env):
    """v3: security 出过提示后切锚点到 product: 完成重入轮仍被跳过, 提示状态不受影响。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "查一下注入", first=False, history=[_user_msg("x")])
    assert "engineering-security-engineer" in _sessions(env)["s1"].hinted   # 提示态不变
    r = _turn(env, "/expert product", first=False, history=[_user_msg("x")])
    assert r["action"] == "block"
    assert _sessions(env)["s1"].anchor == "product-manager"
    r2 = _turn(env, _completion_msg("deleg_0001"), first=False, history=[_user_msg("x")])
    assert r2 is None
    assert "completion-skip" in _events(env)
