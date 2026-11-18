"""intent-router v2.5.1 P0 — 平台守卫：子 agent session 自己的回合不得进入路由流水线。

活体实证（2026-11-17, coder profile, owner 指认"代码侧问题"查实）:
- 宿主 turn_context.py:906 对每个 agent 回合无条件触发 pre_llm_call
  （只排除 _persist_disabled 的 detached fork），kwargs 带 platform（子 agent =
  "subagent"）+ parent_session_id。
- 池 jsonl：42 条子 agent session 的 anchor-disabled 事件（coder profile
  anchor_inject=false 在首轮分支短路 → 当前无实际伤害，只有噪声 + 浪费的
  get_config 调用）。
- 默认 profile（anchor_inject=true）下是真事故：子专家 goal 文本（"作为 X 领域
  专家…执行…"）被 aux 重新分类 → 错误专家全文注入子 agent 上下文（persona
  冲突 + 膨胀）+ 子 session 锚点 → 后续轮再分类出新域 → 孙代级联派发。

修复（纯插件侧，宿主零改动）: hook 入口 `parent_session_id or platform ==
"subagent"` → 子 agent 回合直接短路（不进分类/注入/派发/命令/重入任何一支）；
每 session 记一次 subagent-guard 审计事件（防 72-API-call 子专家刷 jsonl）。

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
    spec = importlib.util.spec_from_file_location("intent_router_v251_guard", root / "__init__.py")
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
    def __init__(self):
        self.llm = _FakeLLM()
        self.dispatch_calls = []
        self.hooks = {}
        self._n = 0
        self.dispatch_result = None

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
    mod._fetch_expert_from_github = lambda slug: None
    ctx = _FakeCtx()
    return SimpleNamespace(mod=mod, ctx=ctx, experts=experts, log=tmp_path / "logs" / "dispatches.jsonl")


def _turn(env, user, first, history=None, session_id="s1", platform="cli",
          parent_session_id=""):
    mod, ctx = env.mod, env.ctx
    return mod._make_hook(ctx)(
        session_id=session_id, user_message=user, conversation_history=history or [],
        is_first_turn=first, model="m", platform=platform,
        parent_session_id=parent_session_id,
    )


def _events(env):
    events = []
    if env.log.exists():
        for line in env.log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


_GOAL = "作为 coding 领域专家（stackoverflow-expert）执行以下任务，只交付 findings/结果。"


# ── 守卫行为 ────────────────────────────────────────────────────────────────

def test_subagent_turns_short_circuit_platform_signal(env):
    """platform=subagent → 直接短路: 零分类、零派发、零注入；只记 subagent-guard。"""
    env.ctx.llm.scripts = [
        {"category": "coding", "confidence": 0.9},
    ]
    r = _turn(env, _GOAL, first=True, session_id="sub-1", platform="subagent")
    assert r is None, f"子 agent 回合不得注入任何东西, got: {r}"
    assert env.ctx.llm.calls == [], "守卫必须短在 aux 分类之前（不烧分类）"
    assert env.ctx.dispatch_calls == []
    evs = _events(env)
    assert [e for e in evs if e["event"] == "subagent-guard"], evs
    assert not [e for e in evs if e["event"] in ("anchor", "anchor-disabled", "dispatch", "dispatch-refused")], evs


def test_subagent_turns_short_circuit_parent_signal(env):
    """parent_session_id 非空（宿主另一信号面）同样触发守卫 —— OR 语义, 单信号即挡。"""
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r = _turn(env, _GOAL, first=True, session_id="sub-2",
              platform="cli", parent_session_id="local-main-1")
    assert r is None
    assert env.ctx.llm.calls == []
    assert [e for e in _events(env) if e["event"] == "subagent-guard"]


def test_guard_logs_once_per_session(env):
    """审计节流: 同一子 session 多回合只记一次 subagent-guard（72-API-call 子专家不刷 jsonl）。"""
    for i in range(3):
        _turn(env, "继续干活", first=(i == 0), session_id="sub-3",
              platform="subagent", history=[{"role": "user", "content": _GOAL}])
    guards = [e for e in _events(env) if e["event"] == "subagent-guard"]
    assert len(guards) == 1, guards
    assert guards[0]["session_id"] == "sub-3"


def test_default_profile_subagent_first_turn_no_wrong_expert_injection(env):
    """事故钉死（owner 指认的"代码侧问题"本体）: 默认 profile（无 get_config →
    anchor_inject 缺省 true）下, 子 agent 首轮（goal 文本 >10 字）旧码会 aux 分类 +
    注入错误专家全文 + 设子 session 锚点 → 后续轮孙代级联。守卫后全灭。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},   # 子 agent 自己的 goal 文本被分类
    ]
    r = _turn(env, _GOAL, first=True, session_id="sub-4", platform="subagent")
    assert r is None
    assert "BACKEND-EXPERT-BODY" not in json.dumps(r or {}, ensure_ascii=False)
    assert env.ctx.llm.calls == [], "守卫后子 agent 回合不烧 aux 分类"
    evs = _events(env)
    assert not [e for e in evs if e["event"] == "anchor"], "不得给子 agent 设锚点"
    assert not [e for e in evs if e["event"] in ("dispatch", "lifecycle-absent")], evs
    st = env.mod._SESSIONS.get("sub-4")
    assert st is None or st.anchor is None


# ── 主 session 零回归 ───────────────────────────────────────────────────────

def test_main_session_pipeline_unaffected(env):
    """主 session（无 parent, 非 subagent platform）全流水线照旧: 锚点 + 委派提示（v3）。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    r1 = _turn(env, "帮我写个 REST 后端 API", first=True, session_id="main-1")
    assert r1 is not None and "BACKEND-EXPERT-BODY" in r1["context"]
    r2 = _turn(env, "做个安全审查", first=False, session_id="main-1")
    h = [e for e in _events(env) if e["event"] == "hint"]
    assert h, "主 session hint 面不得被守卫误伤"
    assert not [e for e in _events(env) if e["event"] == "subagent-guard"], "主 session 不得记 guard 事件"


def test_expert_command_still_works_on_main_session(env):
    """/expert 命令只挡子 agent, 主 session 命令面照旧（veto 轮）。"""
    r = _turn(env, "/expert security", first=False, session_id="main-2")
    assert isinstance(r, dict) and r.get("action") == "block"
    assert env.mod._SESSIONS["main-2"].anchor == "engineering-security-engineer"
