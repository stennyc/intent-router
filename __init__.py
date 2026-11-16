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
- /expert 手动覆盖（v2.2, owner 裁决 2026-11-17）: /expert <category|slug>
  切锚点（专家不在场 → 下一轮补全文；已在场 → 只切不注）；/expert 列表；
  /expert clear 清锚点。命令轮走宿主 veto（{"action":"block"}）——不进推理、
  不烧分类；命令解析不依赖 LLM。
- anchor_inject 开关（v2.3, owner 裁决 2026-11-17）: profile 的 SOUL.md 已含
  专家 persona 时（域 profile），配置 plugins.entries.intent-router.settings
  .anchor_inject=false → 首轮跳过分类+注入（免重复+免稀释），/expert 手动切换
  不受影响；缺省/异常 = true（fail-soft，行为同 v2.2）。

设计:
- Turn 1（v1 现况不变）: 分类 → 锚点专家 X，X 全文注入一次。
  v2.1: 注入 wrapper 内埋锚点标记 `<!--ir-expert:<slug>-->`（压缩检测用；
  仍属首轮尾部加法，缓存不变量不变）。
- 之后每轮: aux 分类（temp 0, ~128 token，与主对话缓存无关）
    检测 == 锚点 / null / conf < 0.7 → 零动作
    检测 == Y ≠ 锚点 且 conf ≥ 0.7 → 立即派发 Y 子专家（background，
    立即返回；child 自读 experts/Y.md + 自取 filemem/kbs 持久上下文）
    后续轮次永不向主上下文注入（缓存不变量）
- 压缩后补注（v2.1, owner 裁决 2026-11-16）: 宿主 compaction/剪枝把锚点专家
  消化掉后（非空历史中双标记均缺失），当轮 user 尾部补注该专家的**蒸馏卡**
  （cards/<slug>.md，≤600 token，离线蒸馏静态资产）；卡缺失 → 全文回退
  （成本退回但专家不丢）；卡标记幂等（再丢再补）。空历史永不触发。
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
_CARDS_DIR = Path(__file__).resolve().parent / "cards"

# 压缩检测标记（v2.1, owner 裁决 2026-11-16）: 埋进注入 wrapper。压缩摘要/剪枝永远不会
# 原样复述这两个 HTML 注释。历史里任一标记在 = "专家可见"；双缺失 + 历史非空 = 压缩事件。
_ANCHOR_MARKER_FMT = "<!--ir-expert:{}-->"
_CARD_MARKER_FMT = "<!--ir-card:{}-->"

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
    __slots__ = ("anchor", "running", "last_seen", "full_on_next")

    def __init__(self) -> None:
        self.anchor: Optional[str] = None
        # slug -> (delegation_id, dispatched_at)
        self.running: Dict[str, Tuple[str, float]] = {}
        self.last_seen: float = time.time()
        # v2.2: /expert 手动切换后，若专家标记不在上下文 → 下一次补注事件给全文（不给卡）
        self.full_on_next: bool = False


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


def _read_card(expert_slug: str) -> Optional[str]:
    """专家卡（v2.1: 离线蒸馏静态资产 cards/<slug>.md，≤600 token，带判断标准不带模板）。
    缺失返回 None —— 由调用方走全文回退（owner 裁决：卡缺失，专家不丢）。"""
    card_file = _CARDS_DIR / f"{expert_slug}.md"
    try:
        if card_file.exists():
            return card_file.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to read expert card {expert_slug}: {e}")
    return None


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


# ── 压缩后补注（v2.1） ──────────────────────────────────────────────────────

def _history_text(conversation_history: Any) -> str:
    """拼接各消息 content（str 或 content-parts list）。只读，fail-soft。"""
    try:
        parts = []
        for m in conversation_history or []:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
            if c:
                parts.append(str(c))
        return "\n".join(parts)
    except Exception:
        return ""


def _expert_visible(st: _SessionState, conversation_history: Any) -> bool:
    """锚点专家的标记（全文标记 或 卡标记）是否仍在当前历史里。"""
    if not st.anchor:
        return True
    blob = _history_text(conversation_history)
    return (_ANCHOR_MARKER_FMT.format(st.anchor) in blob
            or _CARD_MARKER_FMT.format(st.anchor) in blob)


def _reinject_after_compaction(st: _SessionState, session_id: str,
                               conversation_history: Any) -> Optional[str]:
    """压缩后补注（owner 裁决 2026-11-16；v2.2 加 /expert 手动切换语义）:
    - 触发: 锚点已设 + 历史非空 + 双标记均缺失（= 专家被压缩/剪枝消化；空历史是
      测试工件/压缩前状态，永不触发 —— v2 既有不变量）
    - 内容: 专家卡优先（cards/<slug>.md）；卡缺失 → 全文回退（成本退回但专家不丢）
    - v2.2: st.full_on_next（/expert 手动切换且专家不在场）→ 本次给全文不给卡，消费后置回
    - 幂等: 卡注入后其标记留在历史里；再次被压缩才再补卡（卡便宜）
    - 缓存安全: 返回值只 append 到当前轮 user message 尾部，历史零改动。
    """
    if not st.anchor or not conversation_history:
        return None
    if _expert_visible(st, conversation_history):
        st.full_on_next = False     # 专家已在场：消费切换标志，避免迟到的全文注入
        return None
    slug = st.anchor
    if st.full_on_next:
        st.full_on_next = False     # 手动切换（刻意行为）：本次补全文，不给卡
        full = _load_expert_context(slug)
        if full:
            body = (
                f"【{slug} 专家全文 · 手动切换补注（/expert）】\n\n"
                f"{_ANCHOR_MARKER_FMT.format(slug)}\n{full}\n"
                "---\n*intent-router 手动切换专家（owner 指令 /expert；"
                "后续压缩事件仍按卡优先规则补注）*"
            )
            _log(session_id, "manual-full-reinject", expert=slug)
            return body
        # 全文也缺失 → 落入卡/回退路径
    card = _read_card(slug)
    if card:
        body = (
            f"【{slug} 专家卡 · 压缩后补注】\n"
            "专家全文已被上下文压缩消化，以下为蒸馏卡（判断标准）；模板与细节以卡内"
            "「取全文」指针为准：\n\n"
            f"{_CARD_MARKER_FMT.format(slug)}\n{card}\n"
            "---\n*intent-router 压缩后补卡（每次压缩事件补一次；卡再丢再补）*"
        )
        _log(session_id, "card-reinject", expert=slug, source="card")
        return body
    full = _load_expert_context(slug)
    if full:
        body = (
            f"【{slug} 专家全文 · 压缩后补注（卡缺失，回退全文）】\n\n"
            f"{_ANCHOR_MARKER_FMT.format(slug)}\n{full}\n"
            "---\n*intent-router 压缩后补全文（owner 裁决：卡缺失时回退全文；"
            "卡缺失是资产 bug，应重新蒸馏）*"
        )
        _log(session_id, "full-reinject-fallback", expert=slug, source="full-text")
        return body
    _log(session_id, "reinject-failed", expert=slug, reason="card-and-full-text-both-missing")
    return None


# ── 注入包装（v1 原样保留 + v2.1 埋锚点标记） ──────────────────────────────

def _wrap_injection(category: str, slug: str, expert_content: str) -> str:
    """v2.1: 埋锚点标记（供压缩检测）；其余字节保持 v1 原样。"""
    return f"""【{category}领域专家上下文】
以下是当前会话的专家指导，请参照执行：

{_ANCHOR_MARKER_FMT.format(slug)}
{expert_content}

---
*上下文由 intent-router 插件自动注入*
（注意：无论上述专家上下文使用什么语言，你的回复必须使用用户输入的语言。）"""


# ── anchor_inject 配置开关（v2.3, owner 裁决 2026-11-17） ─────────────────
# profile 的 SOUL.md 已含专家 persona（域 profile）时，第一轮自动锚点是纯重复
# + 注意力稀释。per-profile 配置（plugins.entries.intent-router.settings）:
#   anchor_inject: false → 首轮跳过分类+注入，/expert 手动切换不受影响
# 缺省 / 读取异常 = true（fail-soft 保守默认，与 v2.2 行为一致）。

def _anchor_inject_enabled(ctx: Any) -> bool:
    try:
        return bool(ctx.get_config("anchor_inject", True))
    except Exception:
        return True   # 宿主无 get_config / 配置损坏 → 保守保持自动锚点


# ── /expert 手动覆盖（v2.2, owner 裁决 2026-11-17 "继续完成"） ─────────────
# 宿主 veto 通道（turn_context.py: {"action":"block"} → 该轮终止、不进推理、不烧分类）:
#   /expert <category|slug>  切锚点（专家不在场 → 下一轮补全文；已在场 → 只切不注）
#   /expert                  显示当前锚点 + 可用 category 列表
#   /expert clear            清锚点（回无专家模式）
_EXPERT_CMD_RE = re.compile(r"^\s*/expert(?:\s+(\S+))?\s*$", re.IGNORECASE)
_EXPERT_SLUGS = frozenset(CATEGORY_MAP.values())


def _handle_expert_command(msg: str, st: _SessionState, session_id: str,
                           conversation_history: Any) -> Optional[Dict[str, str]]:
    """/expert 命令 → 返回 veto dict；非命令返回 None（放行给常规流程）。"""
    m = _EXPERT_CMD_RE.match(msg)
    if not m:
        return None
    arg = (m.group(1) or "").lower()
    if not arg:
        current = st.anchor or "（无）"
        listing = ", ".join(sorted(CATEGORY_MAP.keys()))
        _log(session_id, "manual-list")
        return {"action": "block", "message": (
            f"intent-router: 当前锚点专家 = {current}\n"
            f"可用 category: {listing}\n"
            f"用法: /expert <category|expert slug>（如 /expert security）或 /expert clear")}
    if arg == "clear":
        st.anchor = None
        st.full_on_next = False
        _log(session_id, "manual-clear")
        return {"action": "block", "message": "intent-router: 锚点专家已清除，回到无专家模式。"}
    slug = CATEGORY_MAP.get(arg)
    if slug is None and arg in _EXPERT_SLUGS:
        slug = arg
    if slug is None:
        _log(session_id, "manual-invalid", arg=arg)
        return {"action": "block", "message": (
            f"intent-router: 未知专家/category '{arg}' —— 输入 /expert 查看有效列表")}
    st.anchor = slug
    # 专家标记已在当前上下文 → 只切锚点（专家已在场，零注入）；否则下一轮补全文
    st.full_on_next = not _expert_visible(st, conversation_history)
    _log(session_id, "manual-anchor", expert=slug, source=arg,
         full_on_next=st.full_on_next)
    note = "（下一轮注入全文）" if st.full_on_next else "（专家已在上下文中，即时生效）"
    return {"action": "block", "message": f"intent-router: 锚点专家已切换为 {slug} {note}"}


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

        # 0.5) /expert 手动覆盖（v2.2）: 命令轮走宿主 veto，不进推理、不烧分类
        expert_block = _handle_expert_command(msg, st, session_id, conversation_history)
        if expert_block is not None:
            return expert_block

        if is_first_turn:
            # v2.3: profile 配置 anchor_inject=false → 跳过首轮自动锚点（SOUL.md 已有
            # 专家 persona；/expert 手动切换在上方命令分支，不受此开关影响）
            if not _anchor_inject_enabled(ctx):
                _log(session_id, "anchor-disabled")
                return None
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
            return {"context": _wrap_injection(category, slug, content)}

        # ── 后续轮: 压缩补注（事件驱动）+ 新域检测（v2 零注入不变量不变） ──
        reinject_ctx = _reinject_after_compaction(st, session_id, conversation_history)
        category, confidence = _classify(ctx, msg)
        slug = None
        if category is not None and confidence >= _CONFIDENCE_GATE:
            slug = CATEGORY_MAP.get(category)
        if slug is None or slug == st.anchor:
            # 仍在锚点域 / 无新域 → 不派发；压缩事件轮仍补注（尾部加法，缓存安全）
            return {"context": reinject_ctx} if reinject_ctx else None
        # 走到这里 ⇒ slug 有效且 ≠ 锚点 ⇒ category 必非 None（slug 由它派生）
        assert category is not None
        with _LOCK:
            _sweep_running(st)
            if slug in st.running:
                _log(session_id, "skip-running", category=category, expert=slug)
                return {"context": reinject_ctx} if reinject_ctx else None
        _dispatch_sub_expert(ctx, st, session_id, msg, conversation_history,
                             category, slug, confidence)
        return {"context": reinject_ctx} if reinject_ctx else None

    return _intent_router_hook


def register(ctx) -> None:
    """插件注册入口（宿主加载时调用一次）"""
    ctx.register_hook("pre_llm_call", _make_hook(ctx))
    logger.info("intent-router v2.3 registered (anchor + sub-expert dispatch + card reinjection + /expert + anchor_inject switch)")
