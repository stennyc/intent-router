"""intent-router v2.8 G3 — lost 时持久层自动捞回（filemem 中央存储只读扫描并入卡）。

owner 裁决（2026-11-18「动手」，子专家取证报告 G3 原文）:
- 根因: lost 卡只有「指示」没有「动作」——filemem 检索靠主专家 LLM 自觉；
  且 G1 缺口（子 agent 工具面无 filemem 族）使 launch 提示词「完成时写 filemem」
  在现行宿主构建下不可执行、静默失效 → 自动捞回必须发生在主进程侧（插件）终态时刻。
- 设计（纯插件侧，宿主零改动）:
  * _filemem_recover(slug, deleg_id): 只读扫描 filemem 中央存储（全分片，顶层 list of row dict），
    主匹配 = delegation_id 审计键（唯一精确）；无主命中兜底 = slug 子串；上限 3 行。
    fail-soft: 目录缺失 / JSON 损坏 / 锁 —— 一律返回 []，绝不影响 hook。
  * lost 分支（UNKNOWN 重试耗尽）: 命中并入卡「### 持久层自动捞回」段
    （append-only；lost 卡为终态一次性注入，不重注入 → 缓存不变量不受影响）；
    未命中 → base 卡字节稳定（既有子串断言 test 全数钉住）。
  * subagent-result-lost 事件带 filemem_hits / filemem_hit_ids 观测字段。
- 隔离: env fixture 把 mod._FILEMEM_MEMORIES_DIR 重绑到 tmp（防单测扫真实中央存储）。

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
    spec = importlib.util.spec_from_file_location("intent_router_v28_lost_recovery", root / "__init__.py")
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
        self.lifecycle_svc = None
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
    # G3 隔离: filemem 中央存储指向 tmp 空目录（单测绝不扫真实存储）
    mem = tmp_path / "filemem" / "memories"
    mem.mkdir(parents=True)
    mod._FILEMEM_MEMORIES_DIR = mem
    ctx = _FakeCtx()
    return SimpleNamespace(mod=mod, ctx=ctx, experts=experts,
                           log=tmp_path / "logs" / "dispatches.jsonl",
                           filemem=mem)


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
    """turn1 锚点 engineering → turn2 切 security 域触发 lifecycle 派发; slug=engineering-security-engineer。"""
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


def _write_rows(env, shard, rows):
    (env.filemem / f"{shard}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _make_lost(env, svc):
    """UNKNOWN ×3 → 第 3 轮清账判 lost; 返回 (sid, r3)。"""
    sid = _dispatched(env, svc)
    svc.status_script[sid] = "UNKNOWN"
    r3 = None
    for _ in range(3):
        env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
        r3 = _turn(env, "那单元测试怎么写", first=False)
    return sid, r3


# ── _filemem_recover 直接面 ─────────────────────────────────────────────────

def test_recover_primary_delegation_id_wins(env):
    """主匹配 = delegation_id 审计键（唯一精确）；slug 兜底行不得抢主命中。"""
    sid = "sub-0001"
    _write_rows(env, "task", [
        {"id": "task_g3a", "type": "task",
         "content": f"子专家 findings: 审计键 delegation_id={sid}, 结论=foo bar"},
    ])
    _write_rows(env, "preference", [
        {"id": "pref_g3b", "type": "preference",
         "content": "engineering-security-engineer 的历史行（无审计键, 只该兜底命中）"},
    ])
    hits = env.mod._filemem_recover("engineering-security-engineer", sid)
    assert len(hits) == 1, hits
    assert hits[0]["id"] == "task_g3a" and hits[0]["shard"] == "task", hits[0]
    assert "foo bar" in hits[0]["content_excerpt"]


def test_recover_fallback_slug_when_no_deleg_match(env):
    """无 delegation_id 命中 → slug 子串兜底（仍带 shard/截断）。"""
    _write_rows(env, "fact", [
        {"id": "fact_g3c", "type": "fact",
         "content": "engineering-security-engineer 曾处理过 X（旧行, 审计键已 lapsed）"},
    ])
    hits = env.mod._filemem_recover("engineering-security-engineer", "sub-9999")
    assert len(hits) == 1 and hits[0]["id"] == "fact_g3c", hits


def test_recover_limit_caps_at_three(env):
    _write_rows(env, "task", [
        {"id": f"task_g3_{i}", "type": "task", "content": f"delegation_id=sub-0001 row {i}"}
        for i in range(5)
    ])
    hits = env.mod._filemem_recover("engineering-security-engineer", "sub-0001")
    assert len(hits) == 3, [h["id"] for h in hits]


def test_recover_failsoft_missing_dir_and_corrupt_shard(env):
    """目录缺失 → []（不抛）；分片 JSON 损坏 → 跳过该分片（不抛）。"""
    assert env.mod._filemem_recover("a-slug", "sub-0001") == []   # 空目录
    _write_rows(env, "task", [{"id": "task_ok", "content": "delegation_id=sub-0001 好行"}])
    (env.filemem / "corrupt.json").write_text("{not valid json", encoding="utf-8")
    hits = env.mod._filemem_recover("a-slug", "sub-0001")
    assert len(hits) == 1 and hits[0]["id"] == "task_ok", hits
    env.mod._FILEMEM_MEMORIES_DIR = env.filemem.parent / "no_such_dir"
    assert env.mod._filemem_recover("a-slug", "sub-0001") == []


# ── lost 分支端到端（轮询 3 连 UNKNOWN → 清账轮）────────────────────────────

def test_lost_card_merges_filemem_hits(env):
    """G3 核心: lost 卡自动并入 filemem 命中（append-only）+ 事件带观测字段。"""
    svc = _FakeLifecycle()
    sid = _dispatched(env, svc)
    # 模拟 F2 落盘: 子专家（或主进程代写）完成时 findings 进 filemem, 审计键 = 真实 sid
    _write_rows(env, "task", [
        {"id": "task_g3live", "type": "task",
         "content": f"SUBAGENT-FINDINGS: 结论=磁盘实证; 审计键 delegation_id={sid}"},
    ])
    svc.status_script[sid] = "UNKNOWN"
    r3 = None
    for _ in range(3):
        env.ctx.llm.scripts.append({"category": "engineering", "confidence": 0.9})
        r3 = _turn(env, "那单元测试怎么写", first=False)
    lost = [e for e in _events(env) if e["event"] == "subagent-result-lost"]
    assert lost and lost[0]["delegation_id"] == sid, lost
    assert lost[0]["filemem_hits"] == 1, lost[0]
    assert lost[0].get("filemem_hit_ids") == ["task_g3live"], lost[0]
    ctx_out = r3["context"]
    assert "持久层自动捞回" in ctx_out, ctx_out
    assert "task_g3live" in ctx_out and "SUBAGENT-FINDINGS" in ctx_out, ctx_out
    # base 卡指令面保留（G3 是尾部加法, 不删原有指引）
    assert "交接" in ctx_out and "而非重做" in ctx_out, ctx_out


def test_lost_no_hit_keeps_base_card_byte_stable(env):
    """未命中 → base 卡与 2 参调用字节一致（既有子串断言的钉住面）; 事件 filemem_hits=0。"""
    svc = _FakeLifecycle()
    sid, r3 = _make_lost(env, svc)
    lost = [e for e in _events(env) if e["event"] == "subagent-result-lost"]
    assert lost and lost[0]["delegation_id"] == sid
    assert lost[0]["filemem_hits"] == 0, lost[0]
    assert "持久层自动捞回" not in r3["context"], r3["context"]
    base = env.mod._lost_pointer("engineering-security-engineer", sid)
    assert base == env.mod._lost_pointer("engineering-security-engineer", sid, None)
    assert "交接" in r3["context"] and sid in r3["context"]
    assert "filemem" in r3["context"] and "而非重做" in r3["context"]


def test_lost_failsoft_corrupt_shard_still_settles(env):
    """分片损坏不得阻塞清账: lost 正常判 + 卡退化为 base 指引面。"""
    (env.filemem / "bad.json").write_text("{broken", encoding="utf-8")
    svc = _FakeLifecycle()
    sid, r3 = _make_lost(env, svc)
    lost = [e for e in _events(env) if e["event"] == "subagent-result-lost"]
    assert lost and lost[0]["delegation_id"] == sid
    assert lost[0]["filemem_hits"] == 0
    assert env.mod._SESSIONS["s1"].lifecycle == {}
    assert "交接" in r3["context"]
