"""intent-router v3.0 — 委派提示卡（owner 裁决 2026-11-18: 弃插件确定性 spawn 栈,
改模型驱动委派——插件只给指示, 模型自己调 delegate_task, 宿主自动回传）。

设计:
- 后续轮新域分类过 gate（≠ 锚点, conf ≥ 0.7）→ 注入 **委派提示卡**（append-only）:
  告知新域/专家 slug + delegate_task 调用模板（goal/context 建议, 含专家文件路径
  与 filemem/kbs 自取指引 + git 归主进程约束）+ "结果宿主自动重入, 不委派亦可自答"。
- 每 (session, 域) 提示一次（幂等）；压缩后标记丢失 → 再补一次（与 v2.1 卡补注同构）。
- 锚点域 / 低 conf / null → 零动作（v1 缓存不变量: 后续轮唯一注入面 = 提示卡/压缩补注）。
- 退役面（v2.4–v2.8 全栈）: lifecycle spawn、轮询三态清账、G3 捞回、台账卡、
  launch_timing/post hook、降级注入、终态重派旗标 —— 全部删除, 无事件词汇。
- 保留面: 锚点专家（v1 字节）/ /expert / anchor_inject / P0 子 session 守卫 /
  G1 短消息护栏 / 问句门 / 压缩补注 / 完成消息轮跳过检测（循环守卫）。
- 宿主零改动。

Run: python run_tests.py   （自动检测活体 hermes 解释器）
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


# ── 提示卡行为 ──────────────────────────────────────────────────────────────

def test_new_domain_injects_hint_card(env):
    """新域过 gate → 委派提示卡（slug + delegate_task 模板 + 专家路径 + git 约束）。"""
    r, _h = _anchor_and_new_domain(env)
    assert r is not None and "委派提示" in r["context"], r
    ctx_text = r["context"]
    assert "engineering-security-engineer" in ctx_text
    assert "delegate_task" in ctx_text
    assert str(env.experts / "engineering-security-engineer.md") in ctx_text
    assert "git" in ctx_text                       # git 归主进程约束
    assert env.ctx.dispatch_calls == [], "v3 插件面不得调宿主 spawn（模型驱动）"
    hints = [e for e in _events(env) if e["event"] == "hint"]
    assert len(hints) == 1 and hints[0]["expert"] == "engineering-security-engineer"


def test_hint_idempotent_per_domain(env):
    """同域后续轮不再出卡（每 session 每域一次）——hint 轮必须持久化进历史
    （宿主语义: 该轮 user message = 原话 + 注入, 标记随其留存 → 标记可见不再补）。"""
    r, history = _anchor_and_new_domain(env)
    assert r is not None and "委派提示" in r["context"]
    history.append({"role": "user", "content": "做个安全审查\n" + r["context"]})
    history.append({"role": "assistant", "content": "ok-hint"})
    # 同域再来两轮（标记在历史 → 幂等静默）
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}] * 2
    for i in range(2):
        r2 = _turn(env, f"安全审查继续 {i}", first=False, history=history)
        assert r2 is None, f"同域第 {i+2} 轮不得再出卡: {r2}"
        history.append({"role": "user", "content": f"安全审查继续 {i}"})
        history.append({"role": "assistant", "content": f"ok-{i}"})
    assert len([e for e in _events(env) if e["event"] == "hint"]) == 1


def test_anchor_domain_no_hint(env):
    """锚点域后续轮 → 零动作（v1 缓存不变量）。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "engineering", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    assert _turn(env, "再加个单元测试", first=False) is None
    assert env.ctx.dispatch_calls == []
    assert [e for e in _events(env) if e["event"] == "hint"] == []


def test_no_v2_dispatch_event_vocabulary(env):
    """退役面钉死: v3 日志词汇表不得出现 v2.x 派发/轮询事件。"""
    r, _h = _anchor_and_new_domain(env)
    assert r is not None
    events = [e["event"] for e in _events(env)]
    banned = {"dispatch", "dispatch-refused", "dispatch-error", "skip-running",
              "lifecycle-absent", "lifecycle-unavailable", "subagent-result-recovered",
              "subagent-result-failed", "subagent-result-lost", "dispatch-pending",
              "pending-stuck", "ledger-post-inject", "dispatch-fallback-injection"}
    assert not (set(events) & banned), f"退役事件词汇泄漏: {set(events) & banned}"


def test_hint_reinject_after_compaction(env):
    """压缩事件（hint 标记消失 + 历史非空）→ 同域再补一次卡（v2.1 卡补注同构）。"""
    r, history = _anchor_and_new_domain(env)
    assert "委派提示" in r["context"]
    # 模拟压缩: 历史保留但 hint 标记被剪掉（只剩无标记的 user/assistant 对）
    compacted = [m for m in history if "委派提示" not in str(m.get("content", ""))][:1]
    compacted.append({"role": "assistant", "content": "done"})
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r2 = _turn(env, "安全审查继续", first=False, history=compacted)
    assert r2 is not None and "委派提示" in r2["context"], "压缩后必须再补一次"
    hints = [e for e in _events(env) if e["event"] == "hint"]
    assert len(hints) == 2 and hints[1].get("note") == "compaction-rehint"


# ── 护栏保留面（G1 + 问句门 + 循环守卫, 现只治"误提示"） ─────────────────

def test_g1_short_message_skips_silently(env):
    """G1: 后续轮 <4 字插话 → 静默跳过 aux（零分类、零事件、零提示）。"""
    _anchor_and_new_domain(env)
    calls_before = len(env.ctx.llm.calls)
    evs_before = len(_events(env))
    r = _turn(env, "修", first=False)
    assert r is None
    assert len(env.ctx.llm.calls) == calls_before, "短消息不得进 aux 分类"
    assert len(_events(env)) == evs_before, "G1 必须静默零事件"


def test_question_gate_skips_silently(env):
    """问句门: 状态/元问句 → 静默跳过（零分类、零事件、零提示）。"""
    _anchor_and_new_domain(env)
    calls_before = len(env.ctx.llm.calls)
    evs_before = len(_events(env))
    r = _turn(env, "完成了吗", first=False)
    assert r is None
    assert len(env.ctx.llm.calls) == calls_before
    assert len(_events(env)) == evs_before


def test_imperative_task_still_hints(env):
    """回归网: 非问句任务消息照旧分类出提示（护栏不误拦）。"""
    _anchor_and_new_domain(env)   # security 已提示（幂等, 后续 security 轮无新卡）
    env.ctx.llm.scripts = [{"category": "devops", "confidence": 0.9}]
    r = _turn(env, "部署一下 CI", first=False,
              history=[{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}])
    assert r is not None and "委派提示" in r["context"]
    assert [e for e in _events(env) if e["event"] == "hint"
            and e["expert"] == "engineering-devops-automator"]


def test_completion_reentry_turn_skips_detection(env):
    """循环守卫保留: [ASYNC DELEGATION] 合成轮跳过检测（不烧分类、不出卡）。"""
    _anchor_and_new_domain(env)
    calls_before = len(env.ctx.llm.calls)
    msg = ("[ASYNC DELEGATION BATCH COMPLETE — deleg_0001]\n"
           "A background subagent you dispatched earlier has finished.\n"
           "Status: completed\n--- RESULT ---\nfindings...")
    r = _turn(env, msg, first=False)
    assert r is None
    assert len(env.ctx.llm.calls) == calls_before


# ── P0 子 session 守卫回归 ──────────────────────────────────────────────────

def test_subagent_guard_still_short_circuits(env):
    """子 agent session（模型 delegate_task 拉起的）回合仍被守卫短路。"""
    env.ctx.llm.scripts = [{"category": "coding", "confidence": 0.9}]
    r = _turn(env, "作为 coding 领域专家执行以下任务", first=True,
              session_id="sub-1", platform="subagent")
    assert r is None
    assert env.ctx.llm.calls == []
    assert [e for e in _events(env) if e["event"] == "subagent-guard"]
    assert not [e for e in _events(env) if e["event"] in ("anchor", "hint")]


# ── 前缀缓存不变量（提示卡 = 纯尾部加法） ────────────────────────────────

def test_prefix_cache_stable_with_hint(env):
    """宿主语义: 每轮 user message = 原话 + 注入（若有）；每轮完整前缀必须是
    前一轮前缀的纯字节扩展（startswith）。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
        None,
    ]
    turns = [("帮我写个 REST 后端 API", True),
             ("再加个单元测试", False),
             ("做个安全审查", False),
             ("嗯", False)]
    system = "SYSTEM PROMPT (constant)"
    stored, prev_stream, injection_turns = [], None, []

    def _stream(msgs):
        return "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs)

    for i, (user, first) in enumerate(turns):
        r = _turn(env, user, first, history=stored, session_id="s-pc")
        injected = (r or {}).get("context")
        if injected:
            injection_turns.append(i)
        eff_user = user + ("\n" + injected if injected else "")
        request_msgs = [{"role": "system", "content": system}] + stored + \
            [{"role": "user", "content": eff_user}]
        stream = _stream(request_msgs)
        if prev_stream is not None:
            assert stream.startswith(prev_stream), f"turn {i} 篡改了已发送字节"
        prev_stream = stream
        stored.append({"role": "user", "content": eff_user})
        stored.append({"role": "assistant", "content": f"reply-{i}"})
    assert injection_turns == [0, 2], f"注入点漂移: {injection_turns}"


# ── 单 hook 面（post_llm_call 已退役） ─────────────────────────────────────

def test_only_pre_hook_registered(env):
    """v3 只注册 pre_llm_call（post 分序面随 launch_timing 退役）。"""
    assert set(env.ctx.hooks.keys()) == {"pre_llm_call"}
