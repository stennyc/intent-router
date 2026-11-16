"""intent-router v2 — TDD suite.

Owner rulings encoded here (2026-11-16):
- B: after a sub-expert completes it MAY be re-dispatched for a new task
  (no per-session cap); no re-dispatch while one is running.
- Cross-platform: zero hardcoded path constants; expert paths are pathlib-resolved
  at runtime; tests use only pathlib/tmp_path (no C:\\ or /root fixtures).
- Cache safety: injection happens on turn 1 only; every later turn's model prefix
  is a pure byte-extension of the previous one (regression test below).

Run: python -m pytest tests/ -v   (scratch venv, pytest + stdlib only, no network)
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── plugin loader (the plugin dir is 'intent-router' — hyphen, not importable by name) ──

def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("intent_router_v2", root / "__init__.py")
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeLLM:
    """Scripted stand-in for ctx.llm. Each test pops `scripts` in turn order.

    Each script entry: {"category": ..., "expert": ..., "confidence": ...} or None.
    """

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
    # hermetic experts dir + log path (module attributes, set fresh per test)
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


def _completion_msg(deleg_id, batch=True):
    """Exact host re-entry shapes (live probe deleg_21ecff95, 2026-11-16: batch/fan-out form
    is what even a 1-task dispatch delivers; single-task form per _format_async_delegation)."""
    marker = "[ASYNC DELEGATION BATCH COMPLETE — " if batch else "[ASYNC DELEGATION COMPLETE — "
    return (
        f"{marker}{deleg_id}]\n"
        "A background subagent you dispatched earlier has finished.\n"
        "Status: completed   API calls: 2   Duration: 41s\n"
        "--- RESULT ---\n"
        "SQL injection found in api/orders; parameterized queries recommended.\n"
        "Full live transcript (complete tool/assistant trace): C:\\tmp\\probe\\task-0.log"
    )


# ── turn 1: anchor (v1 behavior, unchanged) ─────────────────────────────────

def test_first_turn_anchors_and_injects_once(env):
    env.ctx.llm.scripts = [{"category": "engineering", "expert": "engineering-backend-architect", "confidence": 0.9}]
    r = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r is not None and "context" in r
    assert "BACKEND-EXPERT-BODY" in r["context"]
    assert env.mod._SESSIONS["s1"].anchor == "engineering-backend-architect"
    assert env.ctx.dispatch_calls == []


def test_first_turn_short_greeting_skips_classification(env):
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.9}]
    r = _turn(env, "hi", first=True)
    assert r is None
    assert env.ctx.llm.calls == []          # guard fires BEFORE the aux LLM call
    assert env.ctx.dispatch_calls == []


def test_first_turn_low_confidence_no_anchor(env):
    env.ctx.llm.scripts = [{"category": "engineering", "confidence": 0.5}]
    assert _turn(env, "帮我写个 REST 后端 API", first=True) is None
    assert env.mod._SESSIONS["s1"].anchor is None


def test_first_turn_null_category_no_anchor(env):
    env.ctx.llm.scripts = [None]
    assert _turn(env, "随便聊聊", first=True) is None
    assert env.mod._SESSIONS["s1"].anchor is None


# ── follow-up turns: detect-only, never inject (cache invariant) ────────────

def test_followup_same_domain_no_action(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "engineering", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "再加个单元测试", first=False)
    assert r is None                       # no injection on follow-ups, ever
    assert env.ctx.dispatch_calls == []


def test_followup_low_confidence_no_dispatch(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.5},   # below the 0.7 gate
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    assert _turn(env, "看看有没有注入", first=False) is None
    assert env.ctx.dispatch_calls == []


def test_followup_null_category_no_dispatch(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        None,
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    assert _turn(env, "嗯", first=False) is None
    assert env.ctx.dispatch_calls == []


def test_new_domain_dispatches_sub_expert_with_self_serve_context(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    r = _turn(env, "查一下这个 API 的 SQL 注入", first=False,
               history=[{"role": "user", "content": "帮我写个 REST 后端 API"},
                        {"role": "assistant", "content": "done"}])
    assert r is None                        # follow-ups NEVER inject
    assert len(env.ctx.dispatch_calls) == 1
    tool, args, kw = env.ctx.dispatch_calls[0]
    assert tool == "delegate_task"
    assert "toolsets" not in args           # inherit parent toolsets incl. filemem + knowledge-base-search
    assert "toolsets" not in kw
    task = args["tasks"][0]
    expert_path = str(env.experts / "engineering-security-engineer.md")
    assert expert_path in task["context"]                      # runtime-resolved absolute native path
    assert "filemem_search" in task["context"]                 # self-serve instructions
    assert "kb_search_central" in task["context"]
    assert "SQL 注入" in task["context"]                       # user's words carried
    assert "git commit" in task["context"] or "git" in task["context"]  # main process owns git
    # running flag recorded with the delegation id
    slug = "engineering-security-engineer"
    assert slug in env.mod._SESSIONS["s1"].running


def test_running_no_redispatch(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "查一下注入", first=False)
    _turn(env, "继续查注入", first=False)
    assert len(env.ctx.dispatch_calls) == 1   # while running: no re-dispatch


def test_completion_reentry_clears_running_and_skips_detection(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
        # NO script for the synthetic completion turn — detection must be skipped
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "查一下注入", first=False)
    calls_before = len(env.ctx.llm.calls)
    r = _turn(env, _completion_msg("deleg_0001"), first=False)
    assert r is None
    assert len(env.ctx.llm.calls) == calls_before            # no classification on synthetic turns (loop guard)
    assert env.mod._SESSIONS["s1"].running == {}             # running flag cleared by id


def test_completion_reentry_single_task_form_also_guarded(env):
    """单任务形态 '[ASYNC DELEGATION COMPLETE — ...]'（_format_async_delegation）同样被守卫。"""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "查一下注入", first=False)
    calls_before = len(env.ctx.llm.calls)
    r = _turn(env, _completion_msg("deleg_0001", batch=False), first=False)
    assert r is None
    assert len(env.ctx.llm.calls) == calls_before
    assert env.mod._SESSIONS["s1"].running == {}


def test_redispatch_allowed_after_completion(env):
    """Owner ruling B: a new Y-domain task after completion dispatches Y again."""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "查一下注入", first=False)
    _turn(env, _completion_msg("deleg_0001"), first=False)
    n_after_reentry = len(env.ctx.dispatch_calls)
    assert n_after_reentry == 1
    _turn(env, "把发现的注入修掉", first=False)
    assert len(env.ctx.dispatch_calls) == 2


# ── cache regression: N-turn byte-stable prefix ─────────────────────────────

def test_prefix_cache_stable_across_turns(env):
    """Host semantics: a turn's user message = original + injection (if any).
    Cache invariant: each turn's full prefix is a pure byte-extension of the previous
    turn's prefix, and injection happens on turn 1 only."""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},   # post-completion re-dispatch turn
    ]
    turns = [
        ("帮我写个 REST 后端 API", True),
        ("再加个单元测试", False),
        ("查一下 SQL 注入", False),
        (_completion_msg("deleg_0001"), False),
        ("把注入修掉", False),
    ]
    system = "SYSTEM PROMPT (constant)"
    stored = []
    prev_stream = None
    injection_turns = []

    def _stream(msgs):
        # The byte stream a provider sees: each message serialized, concatenated.
        # No list brackets — so "only extends, never mutates the head" == startswith.
        return "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs)

    for i, (user, first) in enumerate(turns):
        r = _turn(env, user, first, history=stored, session_id="s-pc")
        injected = (r or {}).get("context")
        if injected:
            injection_turns.append(i)
        eff_user = user + ("\n" + injected if injected else "")
        # request seen by the provider this turn: system + stored history + this user msg
        request_msgs = [{"role": "system", "content": system}] + stored + [{"role": "user", "content": eff_user}]
        stream = _stream(request_msgs)
        if prev_stream is not None:
            assert stream.startswith(prev_stream), f"turn {i} mutated a previously-sent byte"
        prev_stream = stream
        stored.append({"role": "user", "content": eff_user})
        stored.append({"role": "assistant", "content": f"reply-{i}"})
    assert injection_turns == [0]           # ONLY turn 1 injects


# ── structured audit log ─────────────────────────────────────────────────────

def test_structured_log_written_utf8(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True, session_id="s-log")
    _turn(env, "查一下注入", first=False, session_id="s-log")
    assert env.log.exists()
    lines = [json.loads(l) for l in env.log.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = [l["event"] for l in lines]
    assert "anchor" in events
    assert "dispatch" in events
    d = [l for l in lines if l["event"] == "dispatch"][0]
    assert d["session_id"] == "s-log"
    assert d["expert"] == "engineering-security-engineer"
    assert d["delegation_id"].startswith("deleg_")
    assert d["confidence"] == 0.9


def test_log_failure_is_fail_soft(env, tmp_path):
    """A broken log path must never break the hook (owner: fail-soft).

    Deterministic across OSes: make the log's PARENT a regular file →
    open() raises NotADirectoryError on Windows AND POSIX."""
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir", encoding="utf-8")
    env.mod._LOG_PATH = blocker / "dispatches.jsonl"
    r = _turn(env, "帮我写个 REST 后端 API", first=True)
    assert r is not None and "context" in r      # anchor injection still works
    assert _turn(env, "查一下注入", first=False) is None  # dispatch path still works
    assert len(env.ctx.dispatch_calls) == 1      # the dispatch itself succeeded


# ── cross-platform invariants ───────────────────────────────────────────────

def test_no_hardcoded_path_constants_in_source(env):
    """Owner ruling: zero hardcoded path constants (Windows canon or POSIX).

    A hardcoded constant would appear as a QUOTED string literal in code;
    prose in comments/docstrings may cite the ruling verbatim and is exempt.
    """
    import re as _re
    plugin_src = (Path(__file__).resolve().parents[1] / "__init__.py").read_text(encoding="utf-8")
    quoted_windows = _re.findall(r"""["'][A-Z]:[\\/][^"']*""", plugin_src)
    assert not quoted_windows, f"quoted Windows absolute path literals: {quoted_windows}"
    quoted_posix = _re.findall(r"""["']/(?:root|home)[^"']*""", plugin_src)
    assert not quoted_posix, f"quoted POSIX root/home path literals: {quoted_posix}"
    quoted_home = _re.findall(r"""["']~/[^"']*""", plugin_src)
    assert not quoted_home, f"quoted ~ home path literals: {quoted_home}"
    assert "sys.platform" not in plugin_src and "win32" not in plugin_src  # pathlib only, no OS branches


def test_expert_path_is_native_absolute(env):
    env.ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "查一下注入", first=False)
    ctx_text = env.ctx.dispatch_calls[0][1]["tasks"][0]["context"]
    p = Path(env.experts / "engineering-security-engineer.md")
    assert str(p) in ctx_text
    assert p.is_absolute()


# ── state lifecycle: LRU cap + running TTL ──────────────────────────────────

def test_session_lru_cap(env):
    mod = env.mod
    for i in range(mod._SESSIONS_MAX + 10):
        st = mod._get_state(f"sess-{i}")
        assert st is not None
    assert len(mod._SESSIONS) <= mod._SESSIONS_MAX
    assert "sess-0" not in mod._SESSIONS          # oldest evicted
    assert f"sess-{mod._SESSIONS_MAX + 9}" in mod._SESSIONS


def test_running_ttl_expiry(env):
    """A lost completion wake must not wedge the running flag forever."""
    import time as _t
    mod, ctx = env.mod, env.ctx
    ctx.llm.scripts = [
        {"category": "engineering", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
        {"category": "security", "confidence": 0.9},
    ]
    _turn(env, "帮我写个 REST 后端 API", first=True)
    _turn(env, "查一下注入", first=False)
    slug = "engineering-security-engineer"
    st = mod._SESSIONS["s1"]
    assert slug in st.running
    # age the running entry past _RUNNING_TTL
    st.running[slug] = ("deleg_0001", _t.time() - mod._RUNNING_TTL - 1)
    _turn(env, "继续查注入", first=False)
    assert len(ctx.dispatch_calls) == 2           # expired flag → re-dispatch permitted
