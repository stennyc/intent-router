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


def test_hint_inject_default_on_sends_card(env):
    """hint_inject 缺省（无 get_config → True）= 保留 hint 卡面（现状）。"""
    r, _h = _anchor_and_new_domain(env)
    assert r is not None and "委派提示" in r["context"]


def test_hint_inject_off_no_card(env):
    """hint_inject=false（显式关）→ 主 agent 不预设专家, 不发 hint 卡。
    供 owner 实测'派发前选 vs 派发后子 agent 自分类': 关 hint_inject 后专家全交给
    subagent_inject（子 agent 自分类）或主 agent 自答。"""
    env.ctx.settings["hint_inject"] = False
    r, _h = _anchor_and_new_domain(env)
    # 关后: 新域不再发 hint 卡（r 可能为 None 或 context 里无委派提示）
    ctx_text = (r or {}).get("context", "")
    assert "委派提示" not in ctx_text
    assert not [e for e in _events(env) if e["event"] == "hint"]


def test_hint_card_binding_not_advisory(env):
    """v3.3 绑定口吻: 卡指令模型采用模板, 旧弱化建议语（'仅为可选建议'）移除。
    v3.3.2 口吻升级为'复制粘贴'。"""
    r, _h = _anchor_and_new_domain(env)
    ctx_text = r["context"]
    assert "复制粘贴" in ctx_text, "v3.3.2 绑定语: 指示模型复制粘贴调用体"
    assert "可选建议" not in ctx_text, "弱化建议口吻必须移除"


def test_hint_card_single_task_discipline(env):
    """v3.3 调度纪律: 一次委派一个任务, 勿创建占位/备用子 agent。
    实证（2026-11-18 7ad9c4）: 模型同调用塞'备用占位'任务, 3 秒后自停——卡面无纪律约束。"""
    r, _h = _anchor_and_new_domain(env)
    ctx_text = r["context"]
    assert "一次只委派一个任务" in ctx_text
    assert "占位" in ctx_text


def test_hint_card_goal_no_repeat_boundary(env):
    """v3.3 F3 边界: goal 明写'勿重做主进程已在做的工作'（治'各做各的', 主→子方向缺口）。"""
    r, _h = _anchor_and_new_domain(env)
    assert "勿重做主进程" in r["context"]


_HINT_MARKER_PREFIX = "<!--ir-hint:"


def test_hint_card_directives_before_data(env):
    """v3.3.1 L2（reviewer 复盘 finding）: 指令句（纪律/自动重入）排在数据块之前——
    数据与指令分离, 模型照抄数据块时不会把指令句扫进子 agent context。"""
    r, _h = _anchor_and_new_domain(env)
    ctx_text = r["context"]
    discipline_i = ctx_text.index("一次只委派一个任务")
    reentry_i = ctx_text.index("自动重入")
    goal_i = ctx_text.index('"goal"')
    assert discipline_i < goal_i, "调度纪律句必须出现在数据块之前（指令前移）"
    assert reentry_i < goal_i, "自动重入句必须出现在数据块之前（指令前移）"


def test_hint_card_explicit_argument_block(env):
    """v3.3.1 L2: 数据块显式 JSON 化为 delegate_task(tasks=[{...}]) 参数块——
    data/directive 边界可辨, 且模型可直接粘贴整块作为调用参数。
    v3.3.2: 参数块外加 ```json 围栏。"""
    r, _h = _anchor_and_new_domain(env)
    ctx_text = r["context"]
    assert "delegate_task(tasks=[" in ctx_text, "数据块须显式参数化帧"
    assert '"goal":' in ctx_text and '"context":' in ctx_text, "JSON 式字段键"
    assert ctx_text.rstrip().endswith(
        "])\n```\n" + _HINT_MARKER_PREFIX + "engineering-security-engineer-->"), \
        "参数块闭合 + ```json 围栏关闭 + 审计标记行"


def test_no_advisory_wording_in_module_source(env):
    """v3.3.1 L1（reviewer 复盘 finding）: '可选建议' 字样在模块源码归零
    （含 docstring）——README 历史节与测试用例为有意留存, 不在此面。"""
    src = Path(__file__).resolve().parents[1] / "__init__.py"
    assert "可选建议" not in src.read_text(encoding="utf-8")


# ── v3.3.2 强化面 ──────────────────────────────────────────────────────────

def test_hint_card_json_fence(env):
    """v3.3.2: 数据块外加 ```json 围栏（fenced code block 照抄率高于裸文本）。"""
    r, _h = _anchor_and_new_domain(env)
    ctx_text = r["context"]
    assert "```json" in ctx_text, "须有 ```json 开围栏"
    assert "```" in ctx_text.split("```json")[-1], "须有 ``` 闭围栏"


def test_hint_card_copy_paste_tone(env):
    """v3.3.2: 口吻从'原样采用'升为'复制粘贴'（降低 LLM 自写概率）。"""
    r, _h = _anchor_and_new_domain(env)
    ctx_text = r["context"]
    assert "复制粘贴" in ctx_text, "v3.3.2 绑定语须为复制粘贴"
    assert "勿改写任何字段" in ctx_text, "复制粘贴口吻须带勿改写约束"


def test_hint_card_goal_context_local_vars(env):
    """v3.3.2: goal/context 提取为函数内局部变量（代码可读性, 行为不变）。"""
    import inspect
    mod = env.mod
    src = inspect.getsource(mod._hint_card)
    assert "goal_text" in src, "goal 须提取为局部变量 goal_text"
    assert "context_text" in src, "context 须提取为局部变量 context_text"


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


def test_question_now_classified_not_skipped(env):
    """v3.2 问句门退役: 问句不再静默跳过 —— aux 照跑（calls +1 即门消失的证据）;
    本案脚本耗尽（aux 返回 null）+ 该域已提示 → 无卡无新事件。"""
    _anchor_and_new_domain(env)
    calls_before = len(env.ctx.llm.calls)
    evs_before = len(_events(env))
    r = _turn(env, "完成了吗", first=False)
    assert r is None
    assert len(env.ctx.llm.calls) == calls_before + 1, "问句门已退役: aux 必须照跑"
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
    """subagent_inject=False（显式关, owner 2026-11-19 缺省翻 true 后）→ 子 agent 回合仍被守卫短路。"""
    env.ctx.settings["subagent_inject"] = False
    env.ctx.llm.scripts = [{"category": "coding", "confidence": 0.9}]
    r = _turn(env, "作为 coding 领域专家执行以下任务", first=True,
              session_id="sub-1", platform="subagent")
    assert r is None
    assert env.ctx.llm.calls == []
    assert [e for e in _events(env) if e["event"] == "subagent-guard"]
    assert not [e for e in _events(env) if e["event"] in ("anchor", "hint")]


# ── pre_tool_call 委派专家绑定（v3.5, owner 裁决 2026-11-19: 替换 hint 卡面） ──

def _tool_turn(env, tool_name, args, session_id="main-t", **kw):
    """驱动 pre_tool_call hook: 宿主按 kwargs 传 tool_name/args/session_id。"""
    hook = env.ctx.hooks.get("pre_tool_call")
    assert hook is not None, "v3.5 必须注册 pre_tool_call hook"
    return hook(tool_name=tool_name, args=args, session_id=session_id, **kw)


def test_delegate_task_goal_gets_expert_bound(env):
    """主 agent 手写纯 goal(delegate_task) → 插件强制把专家 slug+读文件指令绑进 goal/context。
    根治'主 agent 不照抄 hint 卡'这次 d5d159 失效场景。"""
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    args = {"tasks": [{"goal": "对 filemem-dsh 做 100% 行为等价审计, 全绿才推进 P2",
                       "context": ""}]}
    r = _tool_turn(env, "delegate_task", args, session_id="db-1")
    assert r is not None and r.get("action") == "modify"
    new_tasks = r["args"]["tasks"]
    assert "engineering-backend-architect" in new_tasks[0]["goal"]
    assert "读专家文件" in new_tasks[0]["goal"] or "experts/" in new_tasks[0]["goal"]
    assert "expert" in new_tasks[0]["context"].lower() or "专家绑定" in new_tasks[0]["context"]
    assert [e for e in _events(env) if e["event"] == "delegate-bind"]


def test_delegate_task_non_domain_goal_passthrough(env):
    """goal 分类不出域(conf<gate) → 原样放行, 不硬塞专家, 无 delegate-bind 事件。"""
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.2}]
    args = {"tasks": [{"goal": "随便查个小东西, 不涉及任何领域", "context": ""}]}
    r = _tool_turn(env, "delegate_task", args, session_id="db-2")
    assert r is None, "无域命中必须原样放行(None)"
    assert not [e for e in _events(env) if e["event"] == "delegate-bind"]


def test_pre_tool_call_ignores_other_tools(env):
    """非 delegate_task 工具 → hook 直接 None, 零改写零事件。"""
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r = _tool_turn(env, "terminal", {"command": "ls"}, session_id="db-3")
    assert r is None
    assert env.ctx.llm.calls == []   # 非 delegate_task 不烧分类


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
    """v3.5 注册 pre_llm_call(锚点/子agent注入/补注) + pre_tool_call(委派专家绑定);
    post 分序面已随 launch_timing 退役。"""
    assert set(env.ctx.hooks.keys()) == {"pre_llm_call", "pre_tool_call"}
