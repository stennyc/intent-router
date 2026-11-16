"""intent-router v2.3 — anchor_inject 配置开关（TDD suite）。

Owner 裁决（2026-11-17）: profile 的 SOUL.md 已含专家 persona 时（域 profile），
第一轮自动锚点注入是纯重复 + 注意力稀释 → per-profile 配置关闭:
  plugins.entries.intent-router.settings.anchor_inject: false
- false = 首轮跳过分类+注入（省 aux 调用），anchor 保持 None → 后续轮无锚点模式
  （新域照旧派子专家；压缩补注不触发）
- /expert 手动切换不受开关影响（显式指令 > 隐式自动）
- 缺省 / 配置读取异常 = true（fail-soft 保守默认，行为与 v2.2 一致）

Run: python -m pytest tests/ -v   (scratch venv, pytest + stdlib only, no network)
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_v23", root / "__init__.py")
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
    """与 v2/v2.2 fixture 同 + get_config（模拟 plugins.entries.intent-router.settings）。"""

    def __init__(self):
        self.llm = _FakeLLM()
        self.dispatch_calls = []
        self.hooks = {}
        self._n = 0
        self.settings: dict = {}
        self.get_config_calls = []

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def get_config(self, key, default=None):
        self.get_config_calls.append(key)
        return self.settings.get(key, default)

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


def _events(env):
    return [json.loads(line)["event"] for line in env.log.read_text(encoding="utf-8").splitlines()]


def _sessions(env):
    return env.mod._SESSIONS


# ── 开关关闭（SOUL.md 已有专家的域 profile 场景） ─────────────────────────

def test_off_skips_first_turn_classification_and_injection(env):
    env.ctx.settings = {"anchor_inject": False}
    r = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r is None
    assert env.ctx.llm.calls == []                      # 不烧 aux 分类
    assert _sessions(env)["s1"].anchor is None
    assert "anchor-disabled" in _events(env)


def test_off_followup_no_anchor_dispatch_still_works(env):
    """无锚点模式: 新域任务照旧派子专家（深度隔离不丢），无补注（无锚点）。"""
    env.ctx.settings = {"anchor_inject": False}
    _turn(env, "帮我写个 REST 后端 API", first=True)    # 被跳过
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r = _turn(env, "查一下这个 API 的 SQL 注入", first=False, history=[_user_msg("x")])
    assert len(env.ctx.dispatch_calls) == 1
    assert r is None                                     # 无锚点 → 无补注
    assert _sessions(env)["s1"].anchor is None


def test_off_expert_command_still_anchors(env):
    """/expert 手动切换不受开关影响（显式 > 隐式）；切换后下一轮补全文。"""
    env.ctx.settings = {"anchor_inject": False}
    _turn(env, "hi", first=True)
    r = _turn(env, "/expert security", first=False, history=[_user_msg("x")])
    assert isinstance(r, dict) and r.get("action") == "block"
    assert _sessions(env)["s1"].anchor == "engineering-security-engineer"
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r2 = _turn(env, "查注入", first=False, history=[_user_msg("x")])
    assert r2 is not None and "SECURITY-EXPERT-BODY" in r2["context"]


# ── 缺省行为回归 ─────────────────────────────────────────────────────────

def test_default_on_unchanged(env):
    """无配置 → 行为与 v2.2 完全一致（现有 38 用例 + 本用例是回归网）。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
    ]
    r = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r is not None and "BACKEND-EXPERT-BODY" in r["context"]
    assert _sessions(env)["s1"].anchor == "engineering-backend-architect"
    assert "get_config" not in str(type(env.ctx))         # sanity: ctx 有 get_config
    assert env.ctx.get_config_calls                        # 开关确实在每首轮被查询
