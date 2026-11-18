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
import contextvars

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
    # G3 隔离: filemem 指向不存在目录（fail-soft []）, 单测绝不扫真实中央存储
    mod._FILEMEM_MEMORIES_DIR = tmp_path / "filemem_missing"
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
    r = _turn(env, "继续", first=False)           # v2.5 L2: RUNNING 提醒行（尾部加法, 无脚本不分类）
    env.ctx.llm.scripts = []
    assert r is not None and "处理中" in r["context"]


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
    assert r is not None and "分工台账" in r["context"]      # v2.5 L1: 通道② 成功也出台账卡


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
    assert r is not None and "分工台账" in r["context"]      # v2.5 L1
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
    assert r is not None and "分工台账" in r["context"]      # v2.5 L1


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
    """v2.4.3: handle 失踪（如 gateway 重启, 宿主注册表随进程丢失）→ 3 连 UNKNOWN 后
    清账不卡死, 记 lost（1-2 连保留重试窗口）。"""
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "UNKNOWN"
    for _ in (1, 2):
        env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
        _turn(env, "那单元测试怎么写", first=False)
    assert env.mod._SESSIONS["s1"].lifecycle, "3 连窗口内不得清账"
    env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
    _turn(env, "那单元测试怎么写", first=False)
    evs = _events(env)
    lost = [e for e in evs if e["event"] == "subagent-result-lost"]
    assert lost and lost[0]["delegation_id"] == sid, evs
    assert env.mod._SESSIONS["s1"].lifecycle == {}


def test_lifecycle_running_now_injects_reminder(env):
    """v2.5 L2 行为变更（owner 拍板）: RUNNING 不再静默——每轮一行提醒,
    主专家持续知道子专家在做什么（取代旧 spec test_..._next_turn_no_action）。"""
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "RUNNING"
    env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
    r = _turn(env, "那单元测试怎么写", first=False)
    assert r is not None and "处理中" in r["context"]
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


# ── v2.4.3: 结果回收修复（活体 0/6 recovered，全部 unknown-handle）────────
# 宿主 SubagentLifecycleService.status() 的 _record() 第三道门要求 poll 时刻
# contextvar 里活着的父 agent session 与 handle 匹配；活体 pre_llm_call worker
# 快照解析不到 → 全灭。插件侧修复（宿主零改动）:
# B: launch 时刻冻结 contextvars（launch 成功 = 该上下文 resolver 已被证明 work），
#    poll 在冻结上下文里 replay → 宿主读到的 contextvar 值与 launch 时刻一致
# C: UNKNOWN 不再一次清账 —— 3 连 UNKNOWN 才判 lost（_RUNNING_TTL sweep 兜底），
#    lost 注入诚实指针卡（产物去哪找），主进程至少"接"到交代
# A: lost 事件带 resolver_session/frozen_session 诊断（宿主公开 API 探测，best-effort）

_TEST_CTXVAR: "contextvars.ContextVar[str]" = contextvars.ContextVar("ir_test_ctx", default=None)


def test_poll_replays_launch_context(env):
    """B: status() 必须在 launch 时刻冻结的上下文里执行（宿主 gate3 依赖 contextvar）。"""
    import contextvars
    svc = _FakeLifecycle()
    seen: dict = {}
    orig = svc.status

    def spy(hdict):
        seen["at_status"] = _TEST_CTXVAR.get()
        return orig(hdict)

    svc.status = spy
    launcher = contextvars.copy_context()

    def do_launch():
        _TEST_CTXVAR.set("launch-bound")
        return _dispatched(env, svc)

    sid = launcher.run(do_launch)
    # 主线程（poll 侧）该 contextvar = 默认 None；只有冻结上下文里是 "launch-bound"
    svc.status_script[sid] = "SUCCEEDED"
    svc.results[sid] = SimpleNamespace(summary="FROZEN-OK")
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    r = _turn(env, "继续", first=False)
    assert seen.get("at_status") == "launch-bound", f"status 未在冻结上下文执行: {seen}"
    assert r and "FROZEN-OK" in r["context"]


def test_unknown_not_cleared_until_retry_cap(env):
    """C+A: UNKNOWN 保留重试（3 连才 lost）；lost 事件带诊断字段；lost 注入指针卡。"""
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "UNKNOWN"
    for i in (1, 2):
        env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
        r = _turn(env, "继续", first=False)
        assert r is None, f"UNKNOWN #{i} 不该注入任何东西"
        assert env.mod._SESSIONS["s1"].lifecycle, f"UNKNOWN #{i} 不得清账（保留重试窗口）"
        assert not [e for e in _events(env) if e["event"] == "subagent-result-lost"], \
            "3 连之前不得判 lost"
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    r3 = _turn(env, "继续", first=False)
    lost = [e for e in _events(env) if e["event"] == "subagent-result-lost"]
    assert lost and lost[0]["delegation_id"] == sid
    # A+F-4: 诊断字段收紧为精确值——测试环境宿主模块不可 import → 确定性 "unavailable"
    assert lost[0]["resolver_session"] == "unavailable", lost[0]
    assert lost[0]["frozen_session"] == "unavailable", lost[0]
    # C: 指针卡 append-only 注入（主进程至少接诚实交代）
    assert r3 and "context" in r3 and sid in r3["context"]
    # D: lost 清守卫后同轮立即重派新子专家接任务（owner 策略 B），旧 entry 被新 sid 取代
    assert env.mod._SESSIONS["s1"].lifecycle, "lost 后同轮重派（新 sid 接任务）"
    assert env.mod._SESSIONS["s1"].lifecycle["engineering-security-engineer"][0] != sid


def test_recovered_after_unknown_via_frozen_context(env):
    """B+C: 一次 UNKNOWN 后注册表恢复 → 下一轮即 recovered（活体故障的修复路径）。"""
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "UNKNOWN"
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    r1 = _turn(env, "继续", first=False)
    assert env.mod._SESSIONS["s1"].lifecycle, "UNKNOWN 后条目必须保留"
    svc.status_script[sid] = "SUCCEEDED"
    svc.results[sid] = SimpleNamespace(summary="RECOVERED-AFTER-UNKNOWN")
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    r2 = _turn(env, "继续", first=False)
    assert r2 and "RECOVERED-AFTER-UNKNOWN" in r2["context"], \
        f"UNKNOWN 后恢复失败: {r2} events={_events(env)}"


def test_lifecycle_dispatch_holds_running_guard(env):
    """v2.4.3 (D): lifecycle 通道占 running 守卫 → 同域后续轮 skip-running 不重派
    （旧行为: 每轮重派 + st.lifecycle[slug] 被覆盖 → 旧 handle 孤儿化, 结果永不回收）。
    终态清守卫 → 完成后允许为新任务重派（owner 策略 B）。"""
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    _turn(env, "再做个安全审查", first=False)
    assert len(svc.launch_calls) == 1, "同域第 2 次派发必须被 running 守卫拦下"
    sk = [e for e in _events(env) if e["event"] == "skip-running"]
    assert sk and sk[0]["expert"] == "engineering-security-engineer"
    assert env.mod._SESSIONS["s1"].lifecycle["engineering-security-engineer"][0] == sid, \
        "原 handle 不得被重派覆盖（孤儿化）"
    # 终态清守卫 → 同轮立即重派接新任务（owner 策略 B）
    svc.status_script[sid] = "SUCCEEDED"
    svc.results[sid] = SimpleNamespace(summary="DONE")
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    r = _turn(env, "继续下一个任务", first=False)
    assert r and "DONE" in r["context"]
    assert len(svc.launch_calls) == 2, "完成后同轮立即重派（策略 B: 新任务接上）"
    assert env.mod._SESSIONS["s1"].running["engineering-security-engineer"][0] == "sub-0002"
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    _turn(env, "同域再来一个任务", first=False)
    assert len(svc.launch_calls) == 2, "running 期间不重派（守卫生效）"


# ── v2.4.4: 主/子专家协作面（各做各的 → 边界感知 + 成果持久 + 接入指引）────
# 活体实证（2026-11-17）: 本聊天"请排查"同任务被主进程与子专家各做一遍,
# 子专家 72 个 API call 的 findings 随 gateway 重启蒸發, 主进程零感知。
# F3 边界感知: launch context 注入锚点专家 slug + "只交付 findings 不重复主进程工作"
# F2 成果持久: 指示子专家完成时写 filemem（slug+时间窗检索键）→ 跨重启可捞回
# F4 接入指引: recovered/lost 卡面写明"采纳/核验而非重做 + filemem 检索键"

def test_launch_context_carries_boundary_and_persistence(env):
    """F3+F2: lifecycle 通道 launch 的 context 含任务边界（锚点 slug）+ filemem 持久化指令。"""
    svc = _FakeLifecycle()
    _dispatched(env, svc)
    ctx_text = svc.launch_calls[0]["context"]
    st = env.mod._SESSIONS["s1"]
    assert "engineering-backend-architect" in ctx_text, "边界行须带主会话锚点专家 slug"
    assert "任务边界" in ctx_text and "不重复" in ctx_text
    assert "filemem" in ctx_text and "slug=engineering-security-engineer" in ctx_text


def test_channel_two_context_also_enriched(env):
    """F3+F2 是共享 context_text: 通道②（delegate_task, CLI 模式）同样受益。
    通道① 被拒（raise_on_launch）→ 走通道②。不走 _dispatched（它断言 lifecycle 派发
    事件, 与通道② 语义矛盾）; 直接驱动通道② 并校验共享 context_text 携带任务边界+持久化。"""
    svc = _FakeLifecycle()
    svc.raise_on_launch = RuntimeError("No active Hermes parent session is available.")
    env.ctx.lifecycle_svc = svc
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "做个安全审查", first=False)
    assert env.ctx.dispatch_calls, "通道① 不可用 → 通道② 接管"
    ctx_text = env.ctx.dispatch_calls[0][1]["tasks"][0]["context"]
    assert "任务边界" in ctx_text and "filemem" in ctx_text
    evs = _events(env)
    assert [e for e in evs if e["event"] == "lifecycle-unavailable"], evs
    d = [e for e in evs if e["event"] == "dispatch" and e.get("channel") == "delegate_task"]
    assert d and d[0]["delegation_id"].startswith("deleg_"), evs


def test_recovered_card_directs_adopt_not_redo(env):
    """F4: recovered 卡面指引主专家"采纳/核验而非重做" + filemem 持久副本检索键。"""
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "SUCCEEDED"
    svc.results[sid] = SimpleNamespace(summary="FINDINGS-BODY")
    env.ctx.llm.scripts.append({"category": "security", "confidence": 0.9})
    r = _turn(env, "继续", first=False)
    assert r and "FINDINGS-BODY" in r["context"]
    assert "而非重做" in r["context"] and "filemem" in r["context"], r["context"]


def test_lost_pointer_directs_filemem_search(env):
    """F4: lost 指针卡指引 filemem 检索（slug 键）——F2 持久化后跨重启成果可捞回。"""
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "UNKNOWN"
    for _ in range(3):
        env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
        _turn(env, "继续", first=False)
    lost = [e for e in _events(env) if e["event"] == "subagent-result-lost"]
    assert lost and lost[0]["delegation_id"] == sid
    # 卡文案断言（_lost_pointer 输出, 清账轮经 context 注入）: 直接调函数验证
    card = env.mod._lost_pointer("engineering-security-engineer", sid)
    assert "filemem" in card and "engineering-security-engineer" in card and "而非重做" in card, card
