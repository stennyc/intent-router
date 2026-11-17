"""intent-router v2.4 (B) — 宿主 subagent-lifecycle 通道（gateway 模式真子 agent）。

宿主勘察结论（2026-11-17, 只读）:
- gateway 模式下 PluginManager._cli_ref=None → ctx.dispatch_tool 无法附 parent agent
  → delegate_task 返回 tool_error（见 test_dispatch_refused）。
- 宿主 PluginContext.subagent_lifecycle() 暴露 SubagentLifecycleService：
  父 agent 由宿主 turn facade 每轮 bind 进 contextvar（turn_facade.py:144），
  插件 hook 的 worker 线程经 copy_context 继承（plugins_dispatch.py:388/405）
  → launch() 在 pre_llm_call 时刻可解析出活的父 agent。
- launch(dict) 接受 Mapping（_coerce_request）；返回 SubagentHandle（subagent_id,
  to_dict 可序列化）。status/result 供轮询。lifecycle 子进程不注册
  process-registry → 不产生 [ASYNC DELEGATION] 重入 → 轮询是结果的唯一交付通道。

插件侧设计: 通道① lifecycle 优先（gateway 可用）；不可用/拒绝 → 通道②
dispatch_tool 旧路径；结果经后续 pre_llm_call 轮询恢复, findings append-only
注入（缓存安全: 只加不改前缀）。

Run: C:/tmp/cmap-venv/Scripts/python -m pytest tests/ -q   (pytest + stdlib only)
"""
import importlib.util
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "plugin.yaml"


def _manifest_version() -> str:
    m = re.search(r'^\s*version:\s*["\']?([\w.\-]+)', _MANIFEST_PATH.read_text(encoding="utf-8"), re.M)
    assert m is not None
    return m.group(1)


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_v24_lifecycle", root / "__init__.py")
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
    """宿主 SubagentLifecycleService 的 duck（只用 launch/status/result 面）。"""

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
    def __init__(self):
        self.llm = _FakeLLM()
        self.dispatch_calls = []
        self.hooks = {}
        self._n = 0
        self.lifecycle_svc = None        # 置 None = 旧宿主形态（无该 API）
        self.dispatch_result = None

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


def _dispatched(env, svc):
    """turn1 锚点 engineering → turn2 切 security 域触发 lifecycle 派发; 返回 sid。"""
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "做个安全审查", first=False)
    d = [e for e in _events(env) if e["event"] == "dispatch" and e.get("channel") == "subagent_lifecycle"]
    assert d, f"no lifecycle dispatch event; got: {_events(env)}"
    return d[0]["delegation_id"]


# ── 通道选择 ─────────────────────────────────────────────────────────────────

def test_lifecycle_channel_spawns_prefers_it_over_dispatch_tool(env):
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    assert sid.startswith("sub-")
    assert len(svc.launch_calls) == 1
    req = svc.launch_calls[0]
    assert "engineering-security-engineer" in req["goal"]
    assert "做个安全审查" in req["context"]          # 用户原话进子任务 context
    assert "expert" in str(req["context"]) or "专家" in str(req["context"])  # 专家文件路径指引
    assert req.get("role") == "leaf"
    assert env.ctx.dispatch_calls == []            # 旧通道未使用
    d = [e for e in _events(env) if e["event"] == "dispatch"][0]
    assert d["channel"] == "subagent_lifecycle" and d["delegation_id"] == sid
    assert env.mod._SESSIONS["s1"].lifecycle      # running 追踪在 lifecycle map
    r = _turn(env, "继续", first=False)           # 无脚本 → 不分类, 不应注入
    env.ctx.llm.scripts = []
    assert r is None


def test_lifecycle_unavailable_falls_back_to_delegate_task(env):
    svc = _FakeLifecycle()
    svc.raise_on_launch = RuntimeError("No active Hermes parent session is available.")
    sid = None
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "做个安全审查", first=False)
    assert len(env.ctx.dispatch_calls) == 1       # 旧通道接管
    evs = _events(env)
    assert [e for e in evs if e["event"] == "lifecycle-unavailable"], evs
    d = [e for e in evs if e["event"] == "dispatch" and e.get("channel") == "delegate_task"]
    assert d and d[0]["delegation_id"].startswith("deleg_")
    assert env.mod._SESSIONS["s1"].lifecycle == {}
    assert r is None


def test_no_lifecycle_service_keeps_legacy_path(env):
    """旧宿主（PluginContext 无 subagent_lifecycle 属性）→ 行为与 v2.3 完全一致; 且记
    lifecycle-absent(reason=attr-missing), 不再静默。"""

    class _BareCtx:
        def __init__(self):
            self.llm = _FakeLLM()
            self.dispatch_calls = []

        def dispatch_tool(self, tool_name, args, **kw):
            self.dispatch_calls.append((tool_name, args, kw))
            return json.dumps({"status": "dispatched", "delegation_id": "deleg_0001"})

    ctx = _BareCtx()
    ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    env.ctx = ctx
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "做个安全审查", first=False)
    assert len(ctx.dispatch_calls) == 1
    evs = _events(env)
    assert not [e for e in evs if e["event"] == "lifecycle-unavailable"], evs
    assert not [e for e in evs if e.get("event") == "dispatch" and e.get("channel") == "subagent_lifecycle"]
    assert r is None
    absent = [e for e in evs if e["event"] == "lifecycle-absent"]
    assert absent and "attr-missing" in absent[0].get("reason", ""), evs


def test_lifecycle_factory_error_records_absent(env):
    """factory() 抛异常（宿主 import 链故障等）→ 记 lifecycle-absent(reason=factory-error), 走旧通道。"""
    ctx = _FakeCtx()

    def _boom():
        raise RuntimeError("import chain broken")

    ctx.subagent_lifecycle = _boom
    env.ctx = ctx
    ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "做个安全审查", first=False)
    evs = _events(env)
    absent = [e for e in evs if e["event"] == "lifecycle-absent"]
    assert absent and "factory-error" in absent[0].get("reason", ""), evs
    assert [e for e in evs if e["event"] == "dispatch" and e.get("channel") == "delegate_task"], evs


def test_lifecycle_attr_not_callable_reason_includes_types(env):
    """属性既非服务又不可调用（纯占位符形态）→ reason 带 ctx/attr 类型名, 定位无需再重启盲猜。"""
    ctx = _FakeCtx()
    ctx.subagent_lifecycle = "sentinel-placeholder"   # 实例属性遮蔽方法, 模拟占位符
    env.ctx = ctx
    ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "做个安全审查", first=False)
    evs = _events(env)
    absent = [e for e in evs if e["event"] == "lifecycle-absent"]
    assert absent, evs
    r = absent[0].get("reason", "")
    assert r.startswith("attr-not-callable"), r
    assert "ctx=" in r and "attr=str" in r, r


def test_lifecycle_attr_is_service_instance_spawns_directly(env):
    """宿主实证形态（2026-11-17 18:11 活体诊断: ctx=PluginContext,
    attr=SubagentLifecycleService — 宿主把 subagent_lifecycle 定义为 @cached_property,
    属性访问直接得到服务实例）: 插件必须直接使用该实例, 不再走工厂调用。"""
    svc = _FakeLifecycle()
    ctx = _FakeCtx()
    ctx.subagent_lifecycle = svc      # 属性 = 服务实例（遮蔽工厂方法形态）
    env.ctx = ctx
    ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "做个安全审查", first=False)
    assert len(svc.launch_calls) == 1, "服务实例应被直接 launch"
    evs = _events(env)
    d = [e for e in evs if e["event"] == "dispatch" and e.get("channel") == "subagent_lifecycle"]
    assert d and d[0]["delegation_id"].startswith("sub-"), evs
    assert not [e for e in evs if e["event"] in ("lifecycle-absent", "lifecycle-unavailable")], evs
    assert not [e for e in evs if e["event"] == "dispatch-refused"], evs
    assert r is None


def test_lifecycle_service_none_records_absent(env):
    """factory() 返回 None → lifecycle-absent(reason=factory-returned-none)。"""
    ctx = _FakeCtx()
    ctx.subagent_lifecycle = lambda: None
    env.ctx = ctx
    ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "做个安全审查", first=False)
    evs = _events(env)
    absent = [e for e in evs if e["event"] == "lifecycle-absent"]
    assert absent and "factory-returned-none" in absent[0].get("reason", ""), evs


# ── 结果轮询: recovered / failed / lost ─────────────────────────────────────

def test_lifecycle_result_recovered_on_later_turn(env):
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "SUCCEEDED"
    svc.results[sid] = SimpleNamespace(
        summary="SEC-FINDINGS: fix SQLi in /orders; parameterize queries",
        error_message=None, error_classification=None)
    env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})  # 同域后续轮
    r = _turn(env, "那单元测试怎么写", first=False)
    assert r is not None and "SEC-FINDINGS: fix SQLi in /orders" in r.get("context", "")
    evs = _events(env)
    rec = [e for e in evs if e["event"] == "subagent-result-recovered"]
    assert rec and rec[0]["delegation_id"] == sid, evs
    assert env.mod._SESSIONS["s1"].lifecycle == {}      # 恢复后清账


def test_lifecycle_result_failed_is_cleared(env):
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "FAILED"
    svc.results[sid] = SimpleNamespace(summary=None, error_message="provider 500", error_classification="FAILED")
    env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
    r = _turn(env, "那单元测试怎么写", first=False)
    assert r is None or "SEC" not in (r or {}).get("context", "")
    evs = _events(env)
    f = [e for e in evs if e["event"] == "subagent-result-failed"]
    assert f and f[0]["delegation_id"] == sid and f[0]["state"] == "FAILED", evs
    assert env.mod._SESSIONS["s1"].lifecycle == {}


def test_lifecycle_unknown_handle_is_lost(env):
    """handle 失踪（如 gateway 重启, 注册表随进程丢失）→ 清账不卡死, 记 lost。"""
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "UNKNOWN"
    env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
    _turn(env, "那单元测试怎么写", first=False)
    evs = _events(env)
    lost = [e for e in evs if e["event"] == "subagent-result-lost"]
    assert lost and lost[0]["delegation_id"] == sid, evs
    assert env.mod._SESSIONS["s1"].lifecycle == {}


def test_lifecycle_running_still_tracked_next_turn_no_action(env):
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "RUNNING"
    env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
    r = _turn(env, "那单元测试怎么写", first=False)
    assert r is None
    assert env.mod._SESSIONS["s1"].lifecycle, "running 子进程保留到下一轮"
    assert not [e for e in _events(env) if e["event"].startswith("subagent-result-")]


def test_registration_line_version_follows_manifest(caplog):
    """v2.4.2 收口：注册行版本号跟随 plugin.yaml manifest（此前硬编码字面量漂移，
    活体 gateway 跑 v2.4.2 代码却打 v2.4.1 注册行 → 验收信号失真）。"""
    ir = _load_plugin()
    with caplog.at_level(logging.INFO):
        ir.register(_FakeCtx())
    ver = _manifest_version()
    recs = [r for r in caplog.records if "registered" in r.getMessage()]
    assert recs, "registration log line missing"
    assert f"intent-router v{ver} registered" in recs[-1].getMessage(), recs[-1].getMessage()
