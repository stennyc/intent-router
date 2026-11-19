"""intent-router v3.5.1 — 委派专家通道收敛为 subagent_inject（owner 裁决 2026-11-19）。

v3.5.1 删除面: hint 卡面（委派提示卡 + hint/delegate-bind 事件 + hint_inject 开关）
+ pre_tool_call 白通道（delegate_task goal 改写）——实测（deleg_6ef27712）确认
pre_tool_call 改写传不到 background/async 子 agent。委派专家改由 subagent_inject
（子 agent 自分类注入专家全文, 治本通道）承担。

本文件保留的回归网:
- P0 子 session 守卫（subagent_inject=false 全拦）/ subagent_inject 注入路径
- 完成消息轮跳过检测（循环守卫, completion-skip）
- 后续轮非压缩轮零注入不变量（无锚点标记可见时不重复注入）
- 锚点首轮注入 + 锚点域后续轮幂等
- G1 短消息护栏（唯一静默门, 语言无关）
- 单 hook 面: v3.5.1 起只注册 pre_llm_call（pre_tool_call 已退役）

已删除（测 v3.0–v3.5 委派提示卡/白通道, v3.5.1 功能整体退役）:
- 提示卡内容断言（复制粘贴/调度纪律/参数块/围栏/幂等/压缩补 hint 等全组）
- pre_tool_call 委派专家绑定组（delegate_task goal 改写/delegate-bind 审计）
- hint_inject 开关组（配置面已删）
- 前缀缓存多轮 hint 注入点用例（hint 注入面已删, 后续轮注入只剩压缩补注）

Run: python -m pytest tests/ -q   （scratch venv, pytest + stdlib only, no network）
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_v3_hint", root / "__init__.py")
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
        self.settings = {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def dispatch_tool(self, tool_name, args, **kw):
        # v3: 插件不得再调宿主工具面做 spawn——记录了调用即测试失败
        self.dispatch_calls.append((tool_name, args, kw))
        return json.dumps({"error": "v3 must not spawn"})

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


@pytest.fixture()
def env(tmp_path):
    mod = _load_plugin()
    experts = tmp_path / "experts"
    experts.mkdir()
    for slug in ("engineering-backend-architect", "engineering-security-engineer"):
        (experts / f"{slug}.md").write_text(f"{slug.upper()}-BODY", encoding="utf-8")
    mod._EXPERTS_DIR = experts
    mod._LOG_PATH = tmp_path / "logs" / "dispatches.jsonl"
    mod._fetch_expert_from_github = lambda slug: None
    ctx = _FakeCtx()
    mod.register(ctx)
    return SimpleNamespace(mod=mod, ctx=ctx, experts=experts,
                           log=tmp_path / "logs" / "dispatches.jsonl")


def _turn(env, user, first, history=None, session_id="s1", platform="cli",
          parent_session_id=""):
    mod, ctx = env.mod, env.ctx
    return ctx.hooks["pre_llm_call"](
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


def _anchor_and_new_domain(env):
    """turn1 锚点 engineering → turn2 切 security 新域（宿主语义: 历史含 turn1 注入）。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    r1 = _turn(env, "帮我写个 REST 后端 API", first=True)
    turn1 = f"帮我写个 REST 后端 API\n{r1['context']}" if r1 else "帮我写个 REST 后端 API"
    history = [{"role": "user", "content": turn1}, {"role": "assistant", "content": "done"}]
    r = _turn(env, "做个安全审查", first=False, history=history)
    return r, history

def test_new_domain_followup_no_injection(env):
    """v3.5.1: hint 卡面已删 —— 新域后续轮（锚点标记仍在历史）→ 零动作零事件。"""
    r, _h = _anchor_and_new_domain(env)
    assert r is None
    assert not [e for e in _events(env) if e["event"] == "hint"]


def test_followup_non_compression_turn_zero_injection(env):
    """v3.5.1 后续轮零注入不变量: 锚点标记仍可见的常规轮 → None（不重复注入, 前缀缓存安全）。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "engineering", "confidence": 0.9},
    ]
    r1 = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r1 is not None and "ENGINEERING-BACKEND-ARCHITECT-BODY" in r1["context"]
    history = [{"role": "user", "content": f"帮我写个 REST 后端 API\n{r1['context']}"},
               {"role": "assistant", "content": "done"}]
    assert _turn(env, "再加个单元测试", first=False, history=history) is None
    assert env.ctx.dispatch_calls == []
    assert [e for e in _events(env) if e["event"] == "hint"] == []


def test_no_v2_dispatch_event_vocabulary(env):
    """退役面钉死: v3 日志词汇表不得出现 v2.x 派发/轮询事件 + v3.5 白通道事件。"""
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r is not None
    events = [e["event"] for e in _events(env)]
    banned = {"dispatch", "dispatch-refused", "dispatch-error", "skip-running",
              "lifecycle-absent", "lifecycle-unavailable", "subagent-result-recovered",
              "subagent-result-failed", "subagent-result-lost", "dispatch-pending",
              "pending-stuck", "ledger-post-inject", "dispatch-fallback-injection",
              "delegate-bind", "hint", "compaction-rehint"}
    assert not (set(events) & banned), f"退役事件词汇泄漏: {set(events) & banned}"


# ── 护栏保留面（G1 + 循环守卫 + hint 事件归零钉死, 现只治"误提示"） ─────

def test_g1_short_message_skips_silently(env):
    """G1: 后续轮短插话（"嗯" 2 字）→ 静默跳过 aux（零分类、零事件、零注入）。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    r1 = _turn(env, "帮我写个 REST 后端 API", first=True)
    history = [{"role": "user", "content": f"帮我写个 REST 后端 API\n{(r1 or {}).get('context', '')}"},
               {"role": "assistant", "content": "done"}]
    calls_before = len(env.ctx.llm.calls)
    evs_before = len(_events(env))
    r = _turn(env, "嗯", first=False, history=history)
    assert r is None
    assert len(env.ctx.llm.calls) == calls_before, "短消息不得进 aux 分类"
    assert len(_events(env)) == evs_before, "G1 必须静默零事件"


def test_followup_no_hint_event_leak(env):
    """v3.5.1: hint 卡面已删 —— 压缩补注触发面（活逻辑）仍工作, 但 hint 事件必须归零。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "查一下注入", first=False, history=[{"role": "user", "content": "[压缩摘要]"}])
    assert not [e for e in _events(env) if e["event"] == "hint"]


def test_completion_reentry_turn_skips_detection(env):
    """循环守卫保留: [ASYNC DELEGATION] 合成轮跳过检测（不烧分类、零注入）。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    r1 = _turn(env, "帮我写个 REST 后端 API", first=True)
    history = [{"role": "user", "content": f"帮我写个 REST 后端 API\n{(r1 or {}).get('context', '')}"},
               {"role": "assistant", "content": "done"}]
    calls_before = len(env.ctx.llm.calls)
    msg = ("[ASYNC DELEGATION BATCH COMPLETE — deleg_0001]\n"
           "A background subagent you dispatched earlier has finished.\n"
           "Status: completed\n--- RESULT ---\nfindings...")
    r = _turn(env, msg, first=False, history=history)
    assert r is None
    assert len(env.ctx.llm.calls) == calls_before
    assert [e for e in _events(env) if e["event"] == "completion-skip"]


# ── P0 子 session 守卫回归 ──────────────────────────────────────────────────

def test_subagent_guard_still_short_circuits(env):
    """subagent_inject=False（显式关, owner 2026-11-19 缺省翻 true 后）→ 子 agent 回合仍被守卫短路。"""
    env.ctx.settings["subagent_inject"] = False
    env.ctx.llm.scripts = [{"category": "coding", "confidence": 0.9}]
    r = _turn(env, "作为 coding 领域专家执行以下任务", first=True,
              session_id="sub-1", platform="subagent")
    assert r is None
    assert env.ctx.llm.calls == []
    assert [e for e in _events(env) if e["event"] == "subagent-guard"]
    assert not [e for e in _events(env) if e["event"] in ("anchor", "hint")]


# ── 单 hook 面（v3.5.1: pre_tool_call 白通道退役, 只留 pre_llm_call） ─────

def test_only_pre_llm_hook_registered(env):
    """v3.5.1 注册面收敛: 只注册 pre_llm_call（锚点/子agent注入/压缩补注）;
    pre_tool_call 委派专家绑定面已随白通道退役（实测传不到子 agent）。"""
    assert set(env.ctx.hooks.keys()) == {"pre_llm_call"}
    assert "pre_tool_call" not in env.ctx.hooks
