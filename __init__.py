"""
Intent Router Plugin v3 — 锚点专家 + 委派提示（模型驱动, owner 裁决 2026-11-18）

owner 裁决史（代码内留痕）:
- v2.0–v2.8（2026-11-16/17）: 插件确定性 spawn 栈（宿主 subagent-lifecycle 通道
  + 结果轮询三态清账 + 台账卡 + G3 持久层捞回 + launch_timing 分序）。
- v3.0（2026-11-18, owner 裁决「设计过度」）: 弃插件确定性 spawn —— 委派判断归
  模型（它有全上下文, 路由器只有 aux 分类）; 宿主 delegate_task 自带 [ASYNC
  DELEGATION] 合成重入（回传免费, 零宿主改动）; 插件只给**指示**。
  删除面: lifecycle spawn / 轮询 / 三态清账 / G3 捞回 / 台账卡 / launch_timing /
  post hook / 降级注入 / 终态重派旗标（代码留 git 史, 可考古）。
- 保留面: 锚点专家（v1 首轮字节不变）/ 13 专家卡 + 压缩后补注（v2.1）/
  /expert 手动覆盖（v2.2）/ anchor_inject 开关（v2.3）/ P0 子 session 平台
  守卫（v2.5.1）/ G1 短消息护栏（v2.7; v3.2 起唯一静默门, 问句 regex 已退役）/ 完成消息轮
  跳过检测（循环守卫, v2.0.1 简化保留: 无 running 状态, 纯跳过）。
- 平台不变: 跨平台零硬编码路径（pathlib 运行时解析 + 显式 utf-8）; 唯一注入面
  是 pre_llm_call 返回 {"context"}（宿主追加到当前轮 user message 尾部, 永不进
  system prompt, 缓存安全）; 审计 <plugin>/logs/dispatches.jsonl（fail-soft）。

设计（v3）:
- Turn 1: 分类 → 锚点专家 X 全文注入一次（v1 字节级不变; anchor_inject=false
  的域 profile 跳过; /expert 手动切换不受开关影响）。
- 之后每轮: G1 短消息护栏（静默跳过 aux）→ aux 分类:
    == 锚点 / null / conf < 0.7 → 零动作
    == Y ≠ 锚点 且 conf ≥ 0.7 → 注入**委派提示卡**（append-only, 每 session
    每域一次; 压缩后标记丢失 → 再补一次）: 告知新域/专家 slug + delegate_task
    调用模板（goal/context 建议含专家文件路径 + filemem/kbs 自取 + git 归主
    进程）+ "结果宿主自动重入, 不委派亦可自答"。
- [ASYNC DELEGATION] 合成完成轮 → 跳过检测（循环守卫; 无 running 状态需清）。
- 状态: 模块级 OrderedDict session_id → _SessionState（LRU 256, TTL 1h, 零文件 I/O）
- 审计事件（v3 词汇表）: anchor / anchor-disabled / subagent-guard / manual-* /
  card-reinject / full-reinject* / hint / completion-skip
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

# 压缩检测标记（v2.1）: 埋进注入 wrapper。压缩摘要/剪枝永远不会原样复述
# 这两个 HTML 注释。历史里任一标记在 = "专家可见"；双缺失 + 历史非空 = 压缩事件。
_ANCHOR_MARKER_FMT = "<!--ir-expert:{}-->"
_CARD_MARKER_FMT = "<!--ir-card:{}-->"
# v3: 委派提示卡标记（压缩后 hint 再补检测, 与卡补注同构）
_HINT_MARKER_FMT = "<!--ir-hint:{}-->"

_CONFIDENCE_GATE = 0.7    # v1 阈值，不变
# G1（v2.7 保留）: 后续轮短消息护栏——单/双字插话（"修"/"推"/"继续"）无领域
# 信号, 低于此长度直接跳过 aux 分类（静默, 零事件, 0 成本）。语言无关（纯长度判断）。
# 问句门（v2.9-c 中文 regex）已随 v3.2 owner 裁决「全删 regex 不加指令」退役:
# 全球化批评成立（疑问形态逐语言不同, 扩表 = 打地鼠）+ v3 下误提示无害
# （无盲 spawn, 尾部注入缓存安全, token 成本可不计）→ 问句与普通消息同律过
# aux + conf gate; 误杀面（任务型问句被静默跳杀）同时修复。
_MIN_SUBSEQ_LEN = 4

_SESSION_TTL = 3600       # session 状态寿命（秒）
_SESSIONS_MAX = 256       # LRU 上限

# 宿主 async-delegation 完成消息的合成轮标记（模型调 delegate_task 后宿主重入
# 交付的 user 轮）。v3 无 running 状态需清 —— 纯循环守卫: 该轮跳过 aux 检测
# （防 检测→提示→模型委派→重入→再检测 噪声环）。两种形态公共前缀匹配,
# id 正则兼容 BATCH 变体（live probe 2026-11-16 deleg_21ecff95）。
_COMPLETION_MARKER = "[ASYNC DELEGATION "
_COMPLETION_ID_RE = re.compile(r"\[ASYNC DELEGATION (?:BATCH )?COMPLETE[^\]]*?([A-Za-z0-9_\-]+)\]")


class _SessionState:
    __slots__ = ("anchor", "last_seen", "full_on_next", "guard_logged", "hinted")

    def __init__(self) -> None:
        self.anchor: Optional[str] = None
        self.last_seen: float = time.time()
        # v2.2: /expert 手动切换后，若专家标记不在上下文 → 下一次补注事件给全文（不给卡）
        self.full_on_next: bool = False
        # v2.5.1 P0: subagent-guard 审计节流（每 session 记一次, 防多 API-call 子专家刷 jsonl）
        self.guard_logged: bool = False
        # v3: 已出过委派提示卡的域（每 session 每域一次; 压缩后标记丢失可再补）
        self.hinted: set = set()


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


def _join_ctx(*pieces: Optional[str]) -> Optional[str]:
    """合并注入片（append-only，保持字节序稳定：压缩补注在前、委派提示卡最后）。"""
    out = "\n\n".join(p for p in pieces if p)
    return out or None


# ── 委派提示卡（v3.0, owner 裁决 2026-11-18: 委派判断归模型, 插件只给指示） ─

def _task_label(user_message: str, cap: int = 80) -> str:
    """提示卡 goal 模板用的单行任务标签（用户原话截断）。"""
    return " ".join(str(user_message or "").split())[:cap]


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


def _hint_visible(st: _SessionState, conversation_history: Any) -> bool:
    """v3: 委派提示卡标记是否仍在当前历史里（压缩丢失检测, 与 v2.1 卡补注同构）。"""
    if not st.hinted:
        return True
    blob = _history_text(conversation_history)
    return any(_HINT_MARKER_FMT.format(slug) in blob for slug in st.hinted)


def _hint_card(slug: str, category: str, confidence: float, user_message: str) -> str:
    """v3 委派提示卡——模型驱动委派：告知新域 + delegate_task 调用模板。
    模型自行决定是否委派（宿主 delegate_task 自带 [ASYNC DELEGATION] 合成重入,
    回传免费）; 不委派亦可自答。append-only 尾部加法, 缓存安全。"""
    expert_path = _EXPERTS_DIR / f"{slug}.md"
    task = _task_label(user_message)
    return (
        f"## 委派提示（新域: {category}）\n"
        f"检测到新领域 {category}（专家 slug={slug}, conf={confidence}）。\n"
        f"如值得委派给子专家, 请调用 delegate_task:\n"
        f"- goal: 作为 {category} 领域专家（{slug}）执行以下任务: {task}; 只交付 findings/结果\n"
        f"- context: 1) 先读专家文件 {expert_path}（file 工具; 若文件不存在则在结果中说明, "
        f"勿从网络拉取）; 2) 动手前自取持久上下文: 用 filemem_search 与 kb_search_central "
        f"查与本任务相关的 {category} 域事实; 3) 只交付 findings, 不做 git commit/push"
        f"（git 归主进程）。\n"
        f"结果由宿主自动重入（无需轮询）。不委派也可自行作答——本提示仅为可选建议。\n"
        f"{_HINT_MARKER_FMT.format(slug)}"
    )


def _completion_skip(user_message: str, st: _SessionState, session_id: str) -> bool:
    """v3 循环守卫（v2.0.1 简化保留）: [ASYNC DELEGATION] 合成完成轮跳过检测。
    v3 无 running/lifecycle 状态需清 —— 只防 检测→提示→委派→重入→再检测 噪声环。"""
    if _COMPLETION_MARKER not in user_message:
        return False
    m = _COMPLETION_ID_RE.search(user_message)
    _log(session_id, "completion-skip",
         delegation_id=m.group(1) if m else None)
    return True


# ── 压缩后补注（v2.1 保留） ────────────────────────────────────────────────

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
    - 触发: 锚点已设 + 历史非空 + 双标记均缺失（= 专家被压缩/剪枝消化；空历史永不触发）
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


# ── anchor_inject 配置开关（v2.3 保留, owner 裁决 2026-11-17） ────────────
# profile 的 SOUL.md 已含专家 persona（域 profile）时，第一轮自动锚点是纯重复
# + 注意力稀释。per-profile 配置（plugins.entries.intent-router.settings）:
#   anchor_inject: false → 首轮跳过分类+注入，/expert 手动切换不受影响
# 缺省 / 读取异常 = true（fail-soft 保守默认）。

def _anchor_inject_enabled(ctx: Any) -> bool:
    try:
        return bool(ctx.get_config("anchor_inject", True))
    except Exception:
        return True   # 宿主无 get_config / 配置损坏 → 保守保持自动锚点


# ── /expert 手动覆盖（v2.2 保留, owner 裁决 2026-11-17） ──────────────────
# 宿主 veto 通道（{"action":"block"} → 该轮终止、不进推理、不烧分类）:
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

        # 0) P0 平台守卫（v2.5.1 保留）: 子 agent session（模型 delegate_task 拉起的）
        #    自己的回合不得进入路由流水线。宿主对每个 agent 回合无条件触发
        #    pre_llm_call（只排除 detached fork）, kwargs 带 platform（子 agent =
        #    "subagent"）+ parent_session_id —— 无守卫时默认 profile 会把错误专家
        #    全文注入子 agent（persona 冲突）+ 烧 aux 分类。OR 语义: 任一信号即挡。
        #    审计: 每 session 记一次 subagent-guard（多 API-call 子专家不刷 jsonl）。
        if (kwargs.get("parent_session_id") or platform == "subagent"):
            if not st.guard_logged:
                st.guard_logged = True
                _log(session_id, "subagent-guard")
            return None

        # 0.5) 循环守卫（v3 简化）: 宿主 [ASYNC DELEGATION] 合成完成轮 → 跳过检测
        #      （防 检测→提示→委派→重入→再检测 噪声环; v3 无 running 状态需清）
        if _completion_skip(msg, st, session_id):
            return None

        # 1) /expert 手动覆盖（v2.2）: 命令轮走宿主 veto，不进推理、不烧分类
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

        # ── 后续轮: 压缩补注（事件驱动）+ 新域检测 → 委派提示卡（v3 模型驱动） ──
        reinject_ctx = _reinject_after_compaction(st, session_id, conversation_history)
        # G1 短消息护栏（v3.2 起唯一静默门: 纯长度判断, 语言无关）: 单/双字插话
        # 无新域任务信号 → 跳过 aux（零事件）。问句不再特殊化（v2.9 regex 退役,
        # owner 裁决 2026-11-18 全球化）—— 与普通消息同律过 aux + conf gate。
        if len(msg.strip()) < _MIN_SUBSEQ_LEN:
            category, confidence = None, 0.0
        else:
            category, confidence = _classify(ctx, msg)
        slug = None
        if category is not None and confidence >= _CONFIDENCE_GATE:
            slug = CATEGORY_MAP.get(category)
        hint_ctx: Optional[str] = None
        if slug is not None and slug != st.anchor:
            # 新域（≠ 锚点）过 gate → 委派提示卡: 每 session 每域一次（幂等）;
            # 压缩后标记丢失 → 再补一次（v2.1 卡补注同构, 卡便宜）
            if slug not in st.hinted:
                st.hinted.add(slug)
                _log(session_id, "hint", category=category, expert=slug,
                     confidence=confidence)
                hint_ctx = _hint_card(slug, category, confidence, msg)
            elif not _hint_visible(st, conversation_history):
                _log(session_id, "hint", category=category, expert=slug,
                     confidence=confidence, note="compaction-rehint")
                hint_ctx = _hint_card(slug, category, confidence, msg)
        ctx_out = _join_ctx(reinject_ctx, hint_ctx)
        return {"context": ctx_out} if ctx_out else None

    return _intent_router_hook


def _manifest_version() -> str:
    """v2.4.2 收口保留：注册行版本号跟随 plugin.yaml manifest（不再硬编码字面量，
    避免活体跑新代码却打旧版本注册行 → 验收信号失真）。fail-soft：读不到给 '?'。"""
    try:
        p = Path(__file__).with_name("plugin.yaml")
        text = p.read_text(encoding="utf-8")
        m = re.search(r'^\s*version:\s*["\']?([\w.\-]+)', text, re.M)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def register(ctx) -> None:
    """插件注册入口（宿主加载时调用一次）。v3: 只注册 pre_llm_call（post 分序面
    随 launch_timing 退役——无插件 spawn 即无需 post 时点）。"""
    ctx.register_hook("pre_llm_call", _make_hook(ctx))
    logger.info("intent-router v%s registered (anchor + delegation-hint (model-driven delegate_task) + card reinjection + /expert + anchor_inject + subagent-guard)",
                _manifest_version())
