"""intent-router v3.2 — 问句门退役（TDD suite）+ v3.5.1 hint 卡面删除后的行为收敛。

Owner 裁决（2026-11-18「全删 regex 不加指令」, 全球化批评）:
- _QUESTION_RE 中文硬编码表整体删除（不适合全球化: 疑问形态逐语言不同,
  扩表 = 无穷打地鼠; 且 v3 下误提示无害——无盲 spawn, 尾部注入缓存安全,
  token 成本可不计）
- 不加替代提示词指令（C 案被否）: 问句消息与普通消息同律, 全量过
  aux 分类 + conf gate（0.7）—— 误判出噪声卡可接受, 低 conf 自然无卡
- 误杀面修复: 旧门静默跳过的任务型问句（"为什么部署失败"）现在正常分类出卡

v3.5.1（2026-11-19）删除面: hint 卡面（委派提示卡）+ pre_tool_call 白通道。
本文件测试用例随之收敛: 常规后续轮（锚点标记可见）不再触发 hint 注入 ——
v3.5.1 下后续轮唯一注入面 = 压缩补注（reinject, 锚点已设 + 双标记缺失 + 历史非空）;
问句/常规消息不再进 aux 分类（外层 G1 短消息分类已随 hint 卡面一并删除）。
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_v32", root / "__init__.py")
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


def test_no_question_regex_in_module(env):
    """A 红线钉死: 模块无 _QUESTION_RE, 源码无中文疑问词表（语言中立）。"""
    assert not hasattr(env.mod, "_QUESTION_RE")
    src = (Path(__file__).resolve().parents[1] / "__init__.py").read_text(encoding="utf-8")
    for word in ("为什么", "是不是", "吗|呢|嘛"):
        assert word not in src, f"模块仍残留中文问句模式: {word}"


def test_status_question_now_classified(env):
    """v3.5.1: 问句门退役保留（无前置 regex 拦截）; 但 hint 卡面已删 ——
    锚点标记仍在历史（_anchor 注入的 ctx1 带 <!--ir-expert: 标记）的常规后续轮
    → 零注入（后续轮唯一注入面 = 压缩补注, 标记可见时不触发）。"""
    ctx1 = _anchor(env)
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r = _turn(env, "完成了吗", first=False, history=[_user_msg(ctx1)])
    assert r is None, "锚点标记可见的常规轮必须零注入（hint 卡面已删）"


def test_english_question_now_classified(env):
    """语言中立实证: 英文问句不被特殊拦截; v3.5.1 下锚点标记可见轮 → 零注入。"""
    ctx1 = _anchor(env)
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r = _turn(env, "did it finish?", first=False, history=[_user_msg(ctx1)])
    assert r is None


def test_task_question_no_longer_mis_skipped(env):
    """误杀面语义保留: 任务型问句（句首'为什么'）不再被前置 regex 静默跳杀;
    v3.5.1 下锚点标记可见轮 → 零注入（hint 卡面已删, 不再出委派提示卡）。"""
    ctx1 = _anchor(env)
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.9}]
    r = _turn(env, "为什么部署失败", first=False, history=[_user_msg(ctx1)])
    assert r is None
    assert "hint" not in _events(env), "hint 事件必须归零（v3.5.1 卡面已删）"


def test_g1_short_message_still_silent(env):
    """G1 长度护栏（语言无关）保留: 短插话（'修' 1 字 < _MIN_SUBSEQ_LEN=4）
    后续轮静默零事件。注: 历史必须带锚点标记（宿主语义）, 否则触发合法的压缩补注。"""
    ctx1 = _anchor(env)
    n_calls = len(env.ctx.llm.calls)
    r = _turn(env, "修", first=False, history=[_user_msg(ctx1)])
    assert r is None
    assert len(env.ctx.llm.calls) == n_calls, "G1 必须先于 aux（不烧分类）"
    assert _events(env) == ["anchor"], "静默 = 零新事件"


def test_low_conf_message_no_card(env):
    """v3.5.1: 低 conf 消息不出 hint 卡（hint 卡面已删, 全量无 hint 事件）;
    锚点标记可见轮 → 零注入（后续轮唯一注入面 = 压缩补注）。"""
    ctx1 = _anchor(env)
    env.ctx.llm.scripts = [{"category": "security", "confidence": 0.3}]
    r = _turn(env, "还有别的坑吗", first=False, history=[_user_msg(ctx1)])
    assert r is None, "conf < gate → 无 hint 卡（v3.5.1 全量无 hint 卡面）"
