"""
Intent Router Plugin v2 — 锚点专家 + 自动子专家派发

owner 裁决（2026-11-16，记录于代码）:
- 跨平台: Windows + Linux 自动适配，零硬编码路径常量（Windows canon 是
  %LOCALAPPDATA%\\hermes，绝不写死 ~/.hermes）；所有 I/O 用 pathlib 运行时解析
  + 显式 utf-8 编码，零 OS 分支代码。
- 前缀缓存: 唯一注入面是 pre_llm_call 返回 {"context"}（宿主追加到当前轮
  user message 尾部，永不进 system prompt）。v2 不新增任何注入面；子专家
  结果以新消息重入（append-only，缓存安全）。
- 派发策略 B: Y 子专家完成后允许为新任务重派（无每 session 上限）；
  running 期间不重派。
- git 归属: 子专家只交付 findings，git commit/push 归主进程。

设计:
- Turn 1（v1 现况不变）: 分类 → 锚点专家 X，X 全文注入一次。
- 之后每轮: aux 分类（temp 0, ~128 token，与主对话缓存无关）
    检测 == 锚点 / null / conf < 0.7 → 零动作
    检测 == Y ≠ 锚点 且 conf ≥ 0.7 → 立即派发 Y 子专家（background，
    立即返回；child 自读 experts/Y.md + 自取 filemem/kbs 持久上下文）
    后续轮次永不向主上下文注入（缓存不变量）
- 循环守卫: `[ASYNC DELEGATION COMPLETE — <id>]` 完成消息是合成轮 ——
  按 delegation id 清 running 标志，且该轮跳过检测（防 检测→派发→重入→再检测 风暴）
- 状态: 模块级 OrderedDict session_id → _SessionState（LRU 256，TTL 1h；
  零文件 I/O，天然跨平台）
- 审计: <plugin>/logs/dispatches.jsonl（utf-8，gitignored，fail-soft）
"""

import json
import logging
import re
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 专家映射：category -> expert_slug（v1 原样保留）
CATEGORY_MAP = {
    # 工程类
    "engineering": "engineering-backend-architect",
    "security": "engineering-security-engineer",
    "frontend": "engineering-frontend-developer",
    "code-review": "engineering-code-reviewer",
    "data": "engineering-data-engineer",
    "data-analysis": "support-analytics-reporter",  # 统计分析、数据洞察
    "statistics": "support-analytics-reporter",     # 统计学、假设检验、回归分析
    "devops": "engineering-devops-automator",
    # 设计类
    "design": "design-ui-designer",
    "ux": "design-ux-researcher",
    # 产品类
    "product": "product-manager",
    # 营销类
    "marketing": "marketing-seo-specialist",
    # 测试类
    "testing": "testing-api-tester",
    "performance": "testing-performance-benchmarker",
    # 写作类（aishort.top 新增）
    "writing": "writing-assistant",
    "content": "content-improver",
    "article": "article-title-generator",
    # 翻译类
    "translation": "translation-expert",
    "language": "language-detector",
    # 编程助手
    "coding": "stackoverflow-expert",
    "fullstack": "fullstack-developer",
    "frontend-dev": "frontend-developer",
    "ui-dev": "ux-ui-developer",
    # 学术类
    "math": "math-teacher",
    "mathematics": "mathematician",
    "history": "history-teacher",
    # 商业类
    "business": "ceo-strategist",
    "sales": "sales-expert",
}

# 意图分类 schema（v1 原样保留）
INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": list(CATEGORY_MAP.keys()),
            "description": "用户意图对应的领域分类"
        },
        "expert": {
            "type": "string",
            "description": "匹配的专家 slug 名称"
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "分类置信度"
        }
    },
    "required": ["category", "expert", "confidence"]
}

# 分类提示词（v1 原样保留 —— "第一轮固定"的含义：首轮行为与 v1 字节级一致）
_CLASSIFY_INSTRUCTIONS = """分析用户消息的意图，返回最匹配的专家类别。
只关注技术相关的开发任务。
如果与编程、开发、系统设计相关，返回 engineering。
如果与前端、Vue、React、CSS 相关，返回 frontend。
如果与代码审查、PR review 相关，返回 code-review。
如果与 UI/UX、视觉设计相关，返回 design。
如果与用户研究、交互设计相关，返回 ux。
如果与 SEO、营销、内容相关，返回 marketing。
如果与系统安全、渗透测试相关，返回 security。
如果与数据工程、ETL、数据分析相关，返回 data。
如果与统计分析、数据洞察、KPI 追踪相关，返回 data-analysis。
如果与统计学、假设检验、回归分析相关，返回 statistics。
如果与 DevOps、CI/CD、容器化相关，返回 devops。
如果与产品管理、需求分析相关，返回 product。
如果与 API 测试、单元测试相关，返回 testing。
如果与性能测试、压测相关，返回 performance。
如果与写作、文章编辑、内容优化相关，返回 writing。
如果与翻译、语言处理相关，返回 translation。
如果与 StackOverflow 问答、编程问题解答相关，返回 coding。
如果与数学教学、解题相关，返回 math。
如果与商业战略、CEO 视角相关，返回 business。
如果不确定或无关，返回 null。"""

# 跨平台: 所有路径运行时 pathlib 解析，模块内零硬编码路径常量（owner 裁决）
_EXPERTS_DIR = Path(__file__).resolve().parent / "experts"
_LOG_PATH = Path(__file__).resolve().parent / "logs" / "dispatches.jsonl"

_CONFIDENCE_GATE = 0.7    # v1 阈值，不变
_SESSION_TTL = 3600       # session 状态寿命（秒）
_SESSIONS_MAX = 256       # LRU 上限
_RUNNING_TTL = 1800       # running 标志兜底过期（秒）—— 完成消息丢失时防永久卡死

# 宿主 async-delegation 完成消息的重入标记。两种形态（live probe 2026-11-16 实证）:
#   单任务:   "[ASYNC DELEGATION COMPLETE — <delegation_id>]"   (_format_async_delegation)
#   批量/fan-out: "[ASYNC DELEGATION BATCH COMPLETE — <delegation_id>]"  (实际交付形态，
#   即便 tasks 只有 1 条也按 fan-out unit 交付 —— 活体探针 deleg_21ecff95 证实)
# 公共前缀 "[ASYNC DELEGATION " 匹配两种；id 正则兼容 BATCH 变体。
_COMPLETION_MARKER = "[ASYNC DELEGATION "
_COMPLETION_ID_RE = re.compile(r"\[ASYNC DELEGATION (?:BATCH )?COMPLETE[^\]]*?([A-Za-z0-9_\-]+)\]")


class _SessionState:
    __slots__ = ("anchor", "running", "last_seen")

    def __init__(self) -> None:
        self.anchor: Optional[str] = None
        # slug -> (delegation_id, dispatched_at)
        self.running: Dict[str, Tuple[str, float]] = {}
        self.last_seen: float = time.time()


_SESSIONS: "OrderedDict[str, _SessionState]" = OrderedDict()
_LOCK = Lock()


# ── 专家内容加载（v1 原样保留：本地优先，GitHub 兜底仅限锚点路径） ─────────

def _read_local_expert(expert_slug: str) -> Optional[str]:
    """从本地缓存读取专家内容"""
    expert_file = _EXPERTS_DIR / f"{expert_slug}.md"
    if expert_file.exists():
        return expert_file.read_text(encoding="utf-8")
    return None


def _fetch_expert_from_github(expert_slug: str) -> Optional[str]:
    """从 GitHub 仓库获取专家内容（首次使用）"""
    import urllib.request

    url = f"https://raw.githubusercontent.com/jnMetaCode/agency-agents-zh/main/{expert_slug.replace('-', '/', 1)}/{expert_slug}.md"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            _EXPERTS_DIR.mkdir(parents=True, exist_ok=True)
            (_EXPERTS_DIR / f"{expert_slug}.md").write_text(content, encoding="utf-8")
            return content
    except Exception as e:
        logger.warning(f"Failed to fetch expert {expert_slug}: {e}")
        return None


def _load_expert_context(expert_slug: str) -> Optional[str]:
    """加载专家上下文，优先本地缓存，其次 GitHub"""
    content = _read_local_expert(expert_slug)
    if content:
        return content
    return _fetch_expert_from_github(expert_slug)


# ── 状态管理（模块级，零文件 I/O，天然跨平台） ─────────────────────────────

def _get_state(session_id: str) -> _SessionState:
    with _LOCK:
        now = time.time()
        for sid in [s for s, st in _SESSIONS.items() if now - st.last_seen > _SESSION_TTL]:
            del _SESSIONS[sid]
        st = _SESSIONS.get(session_id)
        if st is None:
            st = _SessionState()
            _SESSIONS[session_id] = st
            if len(_SESSIONS) > _SESSIONS_MAX:
                _SESSIONS.popitem(last=False)  # evict least-recently-used
        else:
            _SESSIONS.move_to_end(session_id)
        st.last_seen = now
        return st


def _sweep_running(st: _SessionState) -> None:
    """调用方须持有 _LOCK。完成消息丢失时的兜底：running 过期即放行重派。"""
    now = time.time()
    for slug in [s for s, (_d, ts) in st.running.items() if now - ts > _RUNNING_TTL]:
        del st.running[slug]


# ── 结构化审计日志（fail-soft：日志故障绝不影响 hook） ────────────────────

def _log(session_id: str, event: str, **fields: Any) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "session_id": session_id, "event": event, **fields}
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── 分类 / 检测 ─────────────────────────────────────────────────────────────

def _classify(ctx: Any, user_message: str) -> Tuple[Optional[str], float]:
    """v1 的意图分类，抽出可测。返回 (category, confidence)，失败为 (None, 0.0)。"""
    try:
        result = ctx.llm.complete_structured(
            instructions=_CLASSIFY_INSTRUCTIONS,
            input=[{"type": "text", "text": user_message}],
            json_schema=INTENT_SCHEMA,
            purpose="intent.router.classify",
            temperature=0.0,
            max_tokens=128,
        )
    except Exception as e:
        logger.error(f"Intent classification failed: {e}")
        return None, 0.0
    parsed = getattr(result, "parsed", None)
    if not parsed:
        return None, 0.0
    category = parsed.get("category")
    try:
        confidence = float(parsed.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if category is None:
        return None, confidence
    return category, confidence


def _normalize_user_message(user_message: Any) -> str:
    """宿主契约: user_message 是 str 或 content-parts list（多模态轮）。"""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts = []
        for p in user_message:
            if isinstance(p, dict) and p.get("text"):
                parts.append(str(p["text"]))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(user_message or "")


# ── 子专家派发 ──────────────────────────────────────────────────────────────

def _recent_history(conversation_history: Any, n: int = 2, cap: int = 500) -> str:
    """最近 n 条 user 消息（截断），供子专家重建语境。fail-soft。"""
    try:
        users = [m for m in (conversation_history or [])
                 if isinstance(m, dict) and m.get("role") == "user"]
        parts = []
        for m in users[-n:]:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
            c = str(c or "")[:cap]
            if c:
                parts.append(c)
        return "\n---\n".join(parts)
    except Exception:
        return ""


def _dispatch_sub_expert(ctx: Any, st: _SessionState, session_id: str, user_message: str,
                         conversation_history: Any, category: str, slug: str,
                         confidence: float) -> Optional[str]:
    """派发 Y 域子专家。background 语义：立即返回，结果以新消息重入（append-only，
    缓存安全）。不传 toolsets → 子代理继承父工具面，含 filemem + knowledge-base-search
    （已核实 DELEGATE_BLOCKED_TOOLS 不覆盖这两组）。"""
    expert_path = _EXPERTS_DIR / f"{slug}.md"
    goal = f"作为 {category} 领域专家（{slug}）执行以下任务，只交付 findings/结果。"
    context_text = "\n".join([
        f"1. 先读专家文件（file 工具）: {expert_path}",
        "   若文件不存在，在结果中说明——不要从网络拉取。",
        f"2. 动手前自取持久上下文: 用 filemem_search 与 kb_search_central 查与本任务相关的 {category} 事实。",
        f'3. 任务（用户原话）: "{user_message}"',
        f"4. 最近对话语境（最近 2 轮 user 消息）:\n{_recent_history(conversation_history, 2)}",
        "约束: 只交付 findings/结果，不做 git commit/push（git 归主进程）；"
        "你无法调用 delegate_task/clarify，用你手上有的工具完成。",
    ])
    args = {"tasks": [{"goal": goal, "context": context_text}]}
    try:
        raw = ctx.dispatch_tool("delegate_task", args)
    except Exception as e:
        logger.error(f"intent-router: sub-expert dispatch failed: {e}")
        _log(session_id, "dispatch-error", expert=slug, error=str(e))
        return None
    deleg_id: Optional[str] = None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict):
            deleg_id = str(parsed.get("delegation_id") or "")
    except Exception:
        pass
    with _LOCK:
        _sweep_running(st)
        st.running[slug] = (deleg_id or "", time.time())
    _log(session_id, "dispatch", category=category, expert=slug, confidence=confidence,
         delegation_id=deleg_id or None, expert_path=str(expert_path))
    return deleg_id


# ── 完成消息重入处理（循环守卫） ────────────────────────────────────────────

def _handle_completion_reentry(user_message: str, st: _SessionState, session_id: str) -> bool:
    """子专家完成消息重入 = 合成 user 轮。按 delegation id 清 running 标志，
    且该轮跳过检测 —— 否则 检测(Y)→派发(Y)→重入→再检测(Y) 成风暴。"""
    if _COMPLETION_MARKER not in user_message:
        return False
    m = _COMPLETION_ID_RE.search(user_message)
    deleg_id = m.group(1) if m else None
    with _LOCK:
        if deleg_id:
            for slug in [s for s, (did, _ts) in st.running.items() if did == deleg_id]:
                del st.running[slug]
                _log(session_id, "subagent-result-reentered", expert=slug, delegation_id=deleg_id)
        else:
            _log(session_id, "subagent-result-reentered", delegation_id=None)
    return True


# ── 注入包装（v1 原样保留） ────────────────────────────────────────────────

def _wrap_injection(category: str, expert_content: str) -> str:
    return f"""【{category}领域专家上下文】
以下是当前会话的专家指导，请参照执行：

{expert_content}

---
*上下文由 intent-router 插件自动注入*
（注意：无论上述专家上下文使用什么语言，你的回复必须使用用户输入的语言。）"""


# ── hook ────────────────────────────────────────────────────────────────────

def _make_hook(ctx: Any):
    """构建 pre_llm_call hook（闭包持有 ctx；状态在模块级按 session_id 隔离）。"""

    def _intent_router_hook(
        session_id: str,
        user_message: Any,
        conversation_history: list,
        is_first_turn: bool,
        model: str,
        platform: str,
        **kwargs: Any,
    ) -> Optional[Dict[str, str]]:
        st = _get_state(session_id)
        msg = _normalize_user_message(user_message)

        # 0) 循环守卫: 合成完成轮 → 清 running + 跳过检测
        if _handle_completion_reentry(msg, st, session_id):
            return None

        if is_first_turn:
            # ── v1 现况不变: 首轮问候守卫 + 分类 + 锚点注入（唯一注入点） ──
            if len(msg.strip()) < 10:
                return None
            category, confidence = _classify(ctx, msg)
            if category is None or confidence < _CONFIDENCE_GATE:
                return None
            slug = CATEGORY_MAP.get(category)
            if not slug:
                return None
            content = _load_expert_context(slug)
            if not content:
                logger.warning(f"Expert content not found for {slug}")
                return None
            st.anchor = slug
            _log(session_id, "anchor", category=category, expert=slug, confidence=confidence)
            return {"context": _wrap_injection(category, content)}

        # ── 后续轮: 只检测，永不注入（前缀缓存不变量） ──
        category, confidence = _classify(ctx, msg)
        if category is None or confidence < _CONFIDENCE_GATE:
            return None
        slug = CATEGORY_MAP.get(category)
        if not slug:
            return None
        if slug == st.anchor:
            return None  # 仍在锚点域 → 专家已驻留历史，零动作
        with _LOCK:
            _sweep_running(st)
            if slug in st.running:
                _log(session_id, "skip-running", category=category, expert=slug)
                return None
        _dispatch_sub_expert(ctx, st, session_id, msg, conversation_history,
                             category, slug, confidence)
        return None

    return _intent_router_hook


def register(ctx) -> None:
    """插件注册入口（宿主加载时调用一次）"""
    ctx.register_hook("pre_llm_call", _make_hook(ctx))
    logger.info("intent-router v2 registered (anchor expert + auto sub-expert dispatch)")
