"""intent-router v2.9/v3.0 — 问句前置门（TDD suite）。

Owner 裁决（2026-11-18 复盘数据点: "完成了吗" 误路由 writing,
"推，然后继续" 误路由 eng）: 后续轮以问句模式开头的消息先于 aux 分类
静默跳过（零事件、零 aux 烧钱、无提示）—— 问句/状态句的价值在主进程
整合, 不是再拉一个盲子会话。

v3 注: "跳过"语义在 v3 下等价于"跳过提示"（确定性派发已退役,
问句门保护的是 aux 分类调用本身）。
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_qgate", root / "__init__.py")
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
    mod._fetch_expert_from_github = lambda slug: None
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


def _anchor(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9},
    ]
    r = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r is not None and "context" in r
    return r["context"]


def _events(env):
    return [json.loads(line)["event"] for line in env.log.read_text(encoding="utf-8").splitlines()]


def test_question_pattern_table(env):
    """门 = 纯模式表（owner 钉死, 无关键词白名单）: 表内即跳, 表外过。
    样例必须与模块 _QUESTION_RE 的现行表对齐（v2.9 定稿, 不凭记忆扩表）。"""
    re_mod = env.mod
    assert re_mod._QUESTION_RE is not None
    for q in ("完成了吗", "都完成了吗", "是不是搞定了", "为什么不行", "是否可行",
              "下一步做什么", "可以推吗", "行吗"):
        assert re_mod._QUESTION_RE.search(q.strip()), f"漏判: {q!r}"
    for t in ("把注入修掉", "推送", "开始写", "帮我写个 REST 后端 API", "继续查"):
        assert not re_mod._QUESTION_RE.search(t.strip()), f"误杀: {t!r}"


def test_question_ending_skips_classify_silently(env):
    """后续轮问句结尾 → 跳过分类+提示: 零 aux、零事件、零注入。"""
    ctx1 = _anchor(env)
    n_calls = len(env.ctx.llm.calls)
    r = _turn(env, "完成了吗", first=False, history=[_user_msg(ctx1)])
    assert r is None
    assert len(env.ctx.llm.calls) == n_calls, "问句门必须先于 aux 分类"
    assert _events(env) == ["anchor"], "静默 = 零新事件"


def test_question_lead_word_skips_classify(env):
    """领字式问句（'是不是…'）同律静默跳过。"""
    ctx1 = _anchor(env)
    n_calls = len(env.ctx.llm.calls)
    r = _turn(env, "是不是搞定了", first=False, history=[_user_msg(ctx1)])
    assert r is None
    assert len(env.ctx.llm.calls) == n_calls


def test_imperative_task_still_classifies(env):
    """陈述式任务指令不受门影响（该过 gate 就过, 出提示）。"""
    ctx1 = _anchor(env)
    env.ctx.llm.scripts = [
        {"category": "security", "confidence": 0.9},
    ]
    r = _turn(env, "帮我写个 SQL 注入检测脚本", first=False, history=[_user_msg(ctx1)])
    assert len(env.ctx.llm.calls) == 2, "陈述句必须正常烧一次分类"
    assert r is not None and "委派提示" in r["context"]
    assert env.ctx.dispatch_calls == []
