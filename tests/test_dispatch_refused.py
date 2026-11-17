"""intent-router v2.4 (A) — delegate_task tool_error 识别 + 降级注入。

Owner 裁决（2026-11-17）：宿主只读、改动仅落插件层。gateway 模式下
dispatch_tool 拿不到 parent agent → 宿主 delegate_task 返回 tool_error JSON
（tools/registry.py: tool_error → '{"error": "..."}'）。旧行为把这个拒绝误记为
"dispatch"（delegation_id=null 假派发，今天 23 条实证）。本套件钉死：

- C1 观测性: 拒绝必须记 "dispatch-refused"（带 error 文本）；不得出现 "dispatch"
  事件；st.running 不被污染。
- C2 降级: 双通道全拒时，若专家正文可得 → in-session 注入（programming value
  不丢，owner 标准：先问"对编程有什么用"）→ "dispatch-fallback-injection"；
  正文不可得 → 纯拒绝，hook 返回 None。

Run: C:/tmp/cmap-venv/Scripts/python -m pytest tests/ -q   (pytest + stdlib only)
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_v24_refused", root / "__init__.py")
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


class _FakeCtx:
    """无 subagent_lifecycle 的旧宿主形态（A 阶段只走 dispatch_tool 通道）。"""

    def __init__(self):
        self.llm = _FakeLLM()
        self.dispatch_calls = []
        self.hooks = {}
        self._n = 0
        self.dispatch_result = None  # 覆盖默认成功 JSON

    def register_hook(self, name, fn):
        self.hooks[name] = fn

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
    mod._fetch_expert_from_github = lambda slug: None  # no network in tests
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


def _refused_script():
    return [{"category": "engineering", "confidence": 0.9},
            {"category": "security", "confidence": 0.9}]


_TOOL_ERROR = json.dumps({"error": "delegate_task requires a parent agent context."})


# ── C1: 拒绝观测性 ────────────────────────────────────────────────────────────

def test_tool_error_records_refused_not_dispatch(env):
    env.ctx.dispatch_result = _TOOL_ERROR
    env.ctx.llm.scripts = _refused_script()
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "做个安全审查", first=False)
    evs = _events(env)
    refused = [e for e in evs if e["event"] == "dispatch-refused"]
    assert refused, f"no dispatch-refused event; got: {evs}"
    assert "parent agent" in refused[0].get("error", "")
    assert not [e for e in evs if e["event"] == "dispatch"], evs
    assert env.mod._SESSIONS["s1"].running == {}


def test_dispatch_tool_exception_still_records_dispatch_error(env):
    """旧契约保留: dispatch_tool 抛异常 → dispatch-error（不回归）。"""
    env.ctx.llm.scripts = _refused_script()

    def _raise(tool_name, args, **kw):
        raise RuntimeError("registry gone")

    env.ctx.dispatch_tool = _raise
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "做个安全审查", first=False)
    evs = _events(env)
    assert [e for e in evs if e["event"] == "dispatch-error"], evs
    assert not [e for e in evs if e["event"] == "dispatch"], evs
    assert r is None or "context" not in (r or {})


# ── C2: 全拒降级 in-session 注入 ──────────────────────────────────────────────

def test_total_refusal_degrades_to_in_session_injection(env):
    env.ctx.dispatch_result = _TOOL_ERROR
    env.ctx.llm.scripts = _refused_script()
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "做个安全审查", first=False)
    assert r is not None and "SECURITY-EXPERT-BODY" in r.get("context", "")
    evs = _events(env)
    assert [e for e in evs if e["event"] == "dispatch-fallback-injection"], evs
    assert not [e for e in evs if e["event"] == "dispatch"], evs


def test_total_refusal_without_expert_body_returns_no_context(env):
    env.ctx.dispatch_result = _TOOL_ERROR
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "devops", "confidence": 0.9},  # engineering-devops-automator: 本地无正文, github 已禁用
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "搭个 CI", first=False)
    assert r is None
    evs = _events(env)
    assert [e for e in evs if e["event"] == "dispatch-refused"], evs
    assert not [e for e in evs if e["event"] == "dispatch-fallback-injection"], evs


def test_success_path_unchanged_no_refused_events(env):
    """零回归: 正常 dispatch_tool 成功 → 无 refused/fallback 事件, dispatch 照记。"""
    env.ctx.llm.scripts = _refused_script()
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "做个安全审查", first=False)
    evs = _events(env)
    assert not [e for e in evs if e["event"] in ("dispatch-refused", "dispatch-fallback-injection")], evs
    d = [e for e in evs if e["event"] == "dispatch"]
    assert len(d) == 1 and d[0]["delegation_id"].startswith("deleg_")
    assert r is None
