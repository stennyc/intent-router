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
- 分工台账（v2.5, owner 裁决 2026-11-17 "动手"）: 治"主/子专家各做各的"
  结构性缺口——派发发生在主 LLM 处理消息之前, 主专家零感知子专家在做什么。
  全部 append-only 尾部加法（缓存不变量不变）:
  L1 派发成功同轮注入台账卡（slug+任务+delegation_id 审计键, "勿重复"）;
  L2 lifecycle RUNNING 每轮一行提醒（字节稳定; UNKNOWN 重试轮静默——
     结果去向由 lost 交接卡一次性交代）; L3 终态全升级"## 子专家交接"
     语义（recovered/failed/lost 均带"主专家下一步"指令）; L4 台账/提醒带
     delegation 审计键 → 跨重启可经 jsonl/filemem 重新锚定。
  折入: F-2（lifecycle 3 元组兼容分支删除, 生产不可达死代码）+
  F-4（lost 诊断字段测试断言收紧为 "unavailable" 精确值）。
- 平台守卫（v2.5.1 P0, owner 指认"代码侧问题"查实 2026-11-17）: 宿主
  turn_context.py:906 对每个 agent 回合无条件触发 pre_llm_call（只排除 detached
  fork）→ 子 agent session 自己的回合也跑完整路由流水线。活体实证: 池 jsonl 42 条
  子 session anchor-disabled 事件（coder profile anchor_inject=false 短路, 仅噪声）；
  默认 profile 下 = 错误专家全文注入子 agent + 孙代级联派发。修复: hook 入口
  `parent_session_id or platform == "subagent"` 短路（OR 语义, 宿主零改动），
  每 session 记一次 subagent-guard 审计事件。

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
import contextvars
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
_UNKNOWN_RETRY_CAP = 3    # v2.4.3: UNKNOWN 连续 N 次才判 lost（宿主 gate 瞬时不匹配给重试窗口）

# 宿主 async-delegation 完成消息的重入标记。两种形态（live probe 2026-11-16 实证）:
#   单任务:   "[ASYNC DELEGATION COMPLETE — <delegation_id>]"   (_format_async_delegation)
#   批量/fan-out: "[ASYNC DELEGATION BATCH COMPLETE — <delegation_id>]"  (实际交付形态，
#   即便 tasks 只有 1 条也按 fan-out unit 交付 —— 活体探针 deleg_21ecff95 证实)
# 公共前缀 "[ASYNC DELEGATION " 匹配两种；id 正则兼容 BATCH 变体。
_COMPLETION_MARKER = "[ASYNC DELEGATION "
_COMPLETION_ID_RE = re.compile(r"\[ASYNC DELEGATION (?:BATCH )?COMPLETE[^\]]*?([A-Za-z0-9_\-]+)\]")


class _SessionState:
    __slots__ = ("anchor", "running", "last_seen", "full_on_next", "lifecycle",
                 "guard_logged", "guarded", "pending", "ledger_due", "fallback_due")

    def __init__(self) -> None:
        self.anchor: Optional[str] = None
        # slug -> (delegation_id, dispatched_at)
        self.running: Dict[str, Tuple[str, float]] = {}
        self.last_seen: float = time.time()
        # v2.2: /expert 手动切换后，若专家标记不在上下文 → 下一次补注事件给全文（不给卡）
        self.full_on_next: bool = False
        # v2.4: slug -> lifecycle entry（宿主 subagent-lifecycle 通道派发的子专家；
        # 结果经轮询恢复）。v2.4.3 5 元组: (subagent_id, handle_dict, dispatched_at,
        # frozen_contextvars, unknown_count)。v2.5 6 元组: 尾部 + task_label
        # （L1 台账 / L2 提醒的任务标签, 同 running 窗口内字节稳定）；
        # _lifecycle_entry() 归一 6 元组（F-2: 旧 3 元组兼容分支已删, 生产不可达）。
        self.lifecycle: Dict[str, tuple] = {}
        # v2.5.1 P0: subagent-guard 审计节流（每 session 记一次, 防多 API-call 子专家刷 jsonl）
        self.guard_logged: bool = False
        # v2.6 P1（launch_timing=post）: 子 agent session 标记（pre 守卫打标, post 面双信号之一）
        self.guarded: bool = False
        # v2.6 P1: post 模式 pending 派发 {slug: (category, confidence, task_label,
        # goal, context_text, expert_path)} —— pre 轮只记账, post_llm_call 真 launch
        self.pending: Dict[str, tuple] = {}
        # v2.6 P1: post 模式 launch 成功后, 台账卡顺延到下一 pre 轮注入
        # {slug: (delegation_id, task_label, channel)}
        self.ledger_due: Dict[str, tuple] = {}
        # v2.6 P1: post 模式全拒, 降级专家全文注入顺延到下一 pre 轮
        # {slug: (confidence, task_label, category)}
        self.fallback_due: Dict[str, tuple] = {}


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
    """调用方须持有 _LOCK。完成消息丢失时的兜底：running/lifecycle 过期即放行重派。"""
    now = time.time()
    for slug in [s for s, (_d, ts) in st.running.items() if now - ts > _RUNNING_TTL]:
        del st.running[slug]
    for slug in [s for s, e in st.lifecycle.items() if now - (_lifecycle_entry(e)[2] or 0.0) > _RUNNING_TTL]:
        del st.lifecycle[slug]


def _lifecycle_entry(entry):
    """lifecycle entry 解包 → 归一 6 元组 (sid, hdict, ts, frozen, unknown_count, task_label)。
    生产写侧恒 6 元组；进程内旧 5 元组尾部补 None（task_label 侧用 `or ""` 兜底）。
    F-2（owner 裁决 2026-11-17）: 旧 3 元组兼容分支删除——生产不可达的防御死代码。"""
    e = tuple(entry)
    if len(e) < 6:
        e = e + (None,) * (6 - len(e))
    return e


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


def _join_ctx(*pieces: Optional[str]) -> Optional[str]:
    """合并注入片（append-only，保持字节序稳定：reinject 在前、lifecycle findings 次之、
    派发轮上下文（v2.5 台账卡 / 降级注入）最后）。"""
    out = "\n\n".join(p for p in pieces if p)
    return out or None


# ── 分工台账（v2.5, owner 裁决 2026-11-17）: 主专家全程知道子专家在做什么 ────

def _task_label(user_message: str, cap: int = 80) -> str:
    """L1/L2 卡片用的单行任务标签（同 running 窗口内字节稳定 → 缓存友好）。"""
    return " ".join(str(user_message or "").split())[:cap]


def _ledger_card(slug: str, task_label: str, deleg_id: str, channel: str,
                 post: bool = False) -> str:
    """L1+L4: 派发成功同轮台账卡——主专家从开工第一秒知道分工（append-only）。
    v2.6 post 模式: launch 发生在主专家回复之后 → 台账卡顺延一轮, 文案标明
    "上轮回复后已启动"（分序倒转的显性化, 主专家知道时序而非误以为并行）。"""
    timing_note = "上轮回复后已启动; " if post else ""
    return (
        f"## 分工台账（进行中）\n"
        f"子专家 {slug} 已接手: “{task_label}”（{timing_note}delegation_id={deleg_id}, channel={channel}）\n"
        f"- 此项由子专家处理中: 主进程勿重复该项工作\n"
        f"- 完成后 findings 自动送达（下一轮自动追加 / 完成消息重入）"
    )


def _running_reminder(slug: str, task_label: str, sid: str) -> str:
    """L2+L4: RUNNING 期间每轮尾部一行提醒（同 entry 字节稳定；UNKNOWN 轮静默——
    结果去向由 lost 交接卡一次性交代）。"""
    return (f"（子专家 {slug} 处理中: “{task_label}”; 主进程勿重复该项"
            f"（delegation_id={sid}）, 完成后 findings 自动送达）")


def _failed_handoff(slug: str, task_label: str, sid: str, state: str, err: str) -> str:
    """L3: 失败终态交接卡——主专家知道"子专家没干成"，自行处理或重派。"""
    return (
        f"## 子专家交接 ({slug}) — {state}\n"
        f"子专家 {slug} 未正常完成 “{task_label}”（{state}: {err}）。\n"
        f"主专家下一步: 主进程自行处理该项, 或发新任务触发自动重派（审计键 delegation_id={sid}）。"
    )


def _lost_pointer(slug: str, sid: str) -> str:
    """v2.4.3: UNKNOWN 重试耗尽的诚实指针卡——主进程至少"接"到交代（append-only）。
    v2.4.4 F4: 加 filemem 持久副本检索指引 + 采纳不重做（F2 指示子专家落盘后跨重启可捞回）。
    v2.5 L3: 升级"交接"语义 + 主专家下一步指令。"""
    return (
        f"## 子专家交接 ({slug}) — 结果丢失\n"
        f"Host subagent {sid}: result registry entry unavailable after "
        f"{_UNKNOWN_RETRY_CAP} polls (gateway restart cleared the in-process host registry, "
        f"or the host lifecycle context gate rejected the poll). Findings are NOT recoverable "
        f"via the lifecycle API; the sub-expert ran in its own host subagent session.\n"
        f"主专家下一步: 若其成果已落盘, filemem 检索 slug={slug} + 时间窗（审计键 delegation_id={sid}, "
        f"宿主 agent.log/supercompress 亦可查）——找到请采纳/核验而非重做; 未找到请主进程自行处理该项。"
    )


def _get_lifecycle_service(ctx: Any) -> Tuple[Any, Optional[str]]:
    """v2.4 通道①: 宿主 PluginContext.subagent_lifecycle。
    两种宿主形态都支持（鸭子类型, 零宿主耦合）:
    - 属性直接是服务实例（宿主 @cached_property 形态, 2026-11-17 活体实证:
      attr=SubagentLifecycleService）→ 直接用
    - 属性是工厂方法/可调对象 → 调用得到服务
    返回 (service, absent_reason): 可用 → (svc, None)；不可用 → (None, 原因)。"""
    try:
        factory = getattr(ctx, "subagent_lifecycle", None)
    except Exception as e:
        return None, f"getattr-error: {str(e)[:200]}"
    if factory is None:
        return None, "attr-missing"
    # 宿主实证形态: 属性本身即服务实例（有 launch/status 面）→ 直接采用
    if hasattr(factory, "launch") and hasattr(factory, "status"):
        return factory, None
    if not callable(factory):
        # 既非服务又不可调用 → 带类型名定位
        return None, f"attr-not-callable (ctx={type(ctx).__name__}, attr={type(factory).__name__})"
    try:
        svc = factory()
    except Exception as e:
        return None, f"factory-error: {str(e)[:300]}"
    if svc is None:
        return None, "factory-returned-none"
    return svc, None


def _handle_to_dict(handle: Any) -> Optional[dict]:
    try:
        d = handle.to_dict()
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _lifecycle_dispatch(ctx: Any, st: _SessionState, session_id: str,
                       goal: str, context_text: str, category: str, slug: str,
                       confidence: float, expert_path: Path, task_label: str = "") -> Optional[str]:
    """通道①: ctx.subagent_lifecycle().launch(...)。宿主 turn facade 每轮把父 agent
    bind 进 contextvar（gateway 模式 _cli_ref=None 时 dispatch_tool 拿不到 parent, 此通道可用）。
    launch 接受 Mapping（宿主 _coerce_request）；返回 SubagentHandle(subagent_id, to_dict)。
    结果不产生 [ASYNC DELEGATION] 重入（宿主重入通知只在 delegate_task 派发层注册）
    → 轮询 _poll_lifecycle 是结果交付的唯一通道。成功返回 subagent_id, 不可用返回 None
    （不可用原因记 lifecycle-absent；launch 被拒记 lifecycle-unavailable）。
    v2.5: task_label 入 6 元组（L1 台账 / L2 提醒的任务标签）。"""
    svc, absent_reason = _get_lifecycle_service(ctx)
    if svc is None:
        _log(session_id, "lifecycle-absent", expert=slug, reason=absent_reason or "unknown")
        return None
    try:
        handle = svc.launch({"goal": goal, "context": context_text, "role": "leaf"})
        sid = str(getattr(handle, "subagent_id", "") or "")
        if not sid:
            raise RuntimeError("lifecycle handle missing subagent_id")
        # v2.4.3 (B): 冻结当前 contextvars 快照。宿主 lifecycle gate3 要求 poll 时刻
        # contextvar 里的父 agent session 与 handle 匹配；launch 成功即证明此刻
        # worker 快照里的 resolver 可用 → poll 在冻结快照里 replay 宿主 API。
        frozen = contextvars.copy_context()
    except Exception as e:
        _log(session_id, "lifecycle-unavailable", expert=slug, error=str(e)[:300])
        return None
    with _LOCK:
        _sweep_running(st)
        st.lifecycle[slug] = (sid, _handle_to_dict(handle), time.time(), frozen, 0, task_label)
        # v2.4.3 (D): lifecycle 通道也占 running 守卫 —— 否则同域后续轮每轮重派,
        # st.lifecycle[slug] 被新 sid 覆盖 → 旧子专家 handle 成孤儿（结果永不回收）
        st.running[slug] = (sid, time.time())
    _log(session_id, "dispatch", category=category, expert=slug, confidence=confidence,
         delegation_id=sid, channel="subagent_lifecycle", expert_path=str(expert_path))
    return sid


def _poll_lifecycle(ctx: Any, st: _SessionState, session_id: str) -> Optional[str]:
    """v2.4.3: 轮询 lifecycle 通道子专家, 完成即把 findings append 进本轮 context（缓存安全）。
    非终态保留; UNKNOWN（宿主 gate 不匹配/注册表失踪）不再一次清账: 连续
    _UNKNOWN_RETRY_CAP 次才判 lost（_RUNNING_TTL sweep 兜底），期间注册表恢复即
    recovered。宿主 gate3 依赖 contextvar → status/result 在 launch 时刻冻结的
    contextvars 快照里 replay（_lifecycle_dispatch 存的第 4 元）。
    v2.5: L2 RUNNING 每轮一行提醒（不再静默, 主专家持续知道分工）; L3 终态全升级
    "## 子专家交接" 语义（recovered/failed/lost 均带主专家下一步指令）。"""
    if not st.lifecycle:
        return None
    svc, _absent = _get_lifecycle_service(ctx)
    if svc is None:
        return None
    pieces: list[str] = []
    with _LOCK:
        _sweep_running(st)
        pending = list(st.lifecycle.items())
    for slug, raw_entry in pending:
        sid, hdict, ts, frozen, unk, task_label = _lifecycle_entry(raw_entry)
        sid = str(sid or "")
        task_label = str(task_label or "")
        unk = int(unk or 0)
        if hdict is None:
            continue

        def _in(fn):
            return frozen.run(fn) if frozen is not None else fn()

        status: Any = None
        try:
            status = _in(lambda: svc.status(hdict))
        except Exception:
            status = None
        state = str(getattr(status, "state", "UNKNOWN") or "UNKNOWN")
        if state in ("RUNNING", "PENDING", "STARTING", "CANCEL_REQUESTED"):
            # v2.5 L2: 运行期提醒（同 entry 字节稳定; UNKNOWN 轮不在此分支 → 保持静默）
            pieces.append(_running_reminder(slug, task_label, sid))
            continue
        terminal = state in ("SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELLED")
        result: Any = None
        if terminal:
            try:
                result = _in(lambda: svc.result(hdict))
            except Exception:
                result = None
        if terminal:
            with _LOCK:
                st.lifecycle.pop(slug, None)
                # v2.4.3 (D): 终态清 running 守卫（owner 策略 B: 完成后允许为新任务重派）;
                # _RUNNING_TTL sweep 兜底防漏清
                if st.running.get(slug, ("", 0.0))[0] == sid:
                    del st.running[slug]
            if state == "SUCCEEDED":
                summary = str(getattr(result, "summary", None) or "").strip()
                if summary:
                    _log(session_id, "subagent-result-recovered", expert=slug, delegation_id=sid)
                    # v2.4.4 F4 接入指引 + v2.5 L3 "交接" 语义: 主专家拿到成果知道怎么用——
                    # 采纳/核验, 不重做
                    pieces.append(
                        f"## 子专家交接 ({slug}) — 完成\n"
                        f"子专家 {slug} 已完成 “{task_label}”, findings:\n"
                        f"{summary}\n"
                        f"主专家下一步: 采纳/核验（而非重做）; 需深入时并入当前工作。\n"
                        f"持久副本（若已落盘）: filemem 检索 slug={slug} + 时间窗; 审计键 delegation_id={sid}"
                    )
                else:
                    _log(session_id, "subagent-result-recovered", expert=slug,
                         delegation_id=sid, note="empty-summary")
            else:
                err = str(getattr(result, "error_message", None)
                          or getattr(result, "error_classification", None) or state)
                _log(session_id, "subagent-result-failed", expert=slug, delegation_id=sid,
                     state=state, error=err[:300])
                # v2.5 L3: 失败也是交接——主专家知道子专家没干成, 自行处理或重派
                pieces.append(_failed_handoff(slug, task_label, sid, state, err[:300]))
            continue
        # UNKNOWN: 宿主 gate 不匹配或注册表已清 → 重试窗口（不动 running 守卫——守卫属于原 sid）
        unk += 1
        if unk < _UNKNOWN_RETRY_CAP:
            with _LOCK:
                st.lifecycle[slug] = (sid, hdict, ts, frozen, unk, task_label)
            continue
        with _LOCK:
            st.lifecycle.pop(slug, None)
            if st.running.get(slug, ("", 0.0))[0] == sid:
                del st.running[slug]
        probe = _probe_resolver(frozen)
        _log(session_id, "subagent-result-lost", expert=slug, delegation_id=sid,
             note="unknown-handle", unknown_count=unk, **probe)
        pieces.append(_lost_pointer(slug, sid))
    return "\n\n".join(pieces) if pieces else None


def _probe_resolver(frozen: Any) -> Dict[str, str]:
    """v2.4.3 (A): UNKNOWN 清账时的宿主 resolver 诊断（best-effort, 宿主零改动）。
    读宿主公开 API get_active_subagent_parent() —— 当前上下文 + 冻结上下文各探一次,
    直接写明 gate3 卡在哪: none=快照无绑定, 异 session=绑定漂移, 真实 session=注册表失踪。"""
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
    except Exception:
        return {"resolver_session": "unavailable", "frozen_session": "unavailable"}

    def _probe() -> str:
        try:
            p = get_active_subagent_parent()
        except Exception:
            return "probe-error"
        if p is None:
            return "none"
        return str(getattr(p, "session_id", "") or "") or "no-session"

    cur = _probe()
    try:
        frz = frozen.run(_probe) if frozen is not None else "no-frozen"
    except Exception:
        frz = "probe-error"
    return {"resolver_session": cur, "frozen_session": frz}


def _build_dispatch_inputs(st: _SessionState, slug: str, user_message: str,
                           conversation_history: Any, category: str):
    """v2.6: 组装 5 项 launch 输入 (expert_path, goal, context_text, task_label)——
    pre 模式（当轮派发）与 post 模式（pending 记账, post_llm_call 真 launch）共用,
    字节序单一来源（F3 边界感知 / F2 成果持久指令不变）。"""
    expert_path = _EXPERTS_DIR / f"{slug}.md"
    task_label = _task_label(user_message)      # v2.5: 台账/提醒卡的任务标签（单行截断）
    goal = f"作为 {category} 领域专家（{slug}）执行以下任务，只交付 findings/结果。"
    context_text = "\n".join([
        f"1. 先读专家文件（file 工具）: {expert_path}",
        "   若文件不存在，在结果中说明——不要从网络拉取。",
        f"2. 动手前自取持久上下文: 用 filemem_search 与 kb_search_central 查与本任务相关的 {category} 事实。",
        f'3. 任务（用户原话）: "{user_message}"',
        f"4. 最近对话语境（最近 2 轮 user 消息）:\n{_recent_history(conversation_history, 2)}",
        # v2.4.4 F3 边界感知（主→子）: 子专家知道主进程状态, 不各做各的
        f"5. 任务边界: 主会话锚点专家 = {st.anchor or '（无）'}（其 persona 已注入主进程上下文）。"
        f"你只交付 {category} 域 findings，不重复主进程已在做的工作；"
        f"主会话对话对你不可见，持久上下文以 filemem/kbs 自取为准。",
        # v2.4.4 F2 成果持久（跨重启）: 宿主注册表随进程蒸發, filemem 是共享持久面
        f"6. 成果持久化: 完成时把 findings 摘要（结论/关键证据/下一步）写入 filemem"
        f"（type=task, scope=day），内容必须含: 专家 slug={slug}、用户任务原话、结论摘要。"
        f"主进程/用户按 slug+时间窗检索复用——避免你的成果随宿主重启丢失。",
        "约束: 只交付 findings/结果，不做 git commit/push（git 归主进程）；"
        "你无法调用 delegate_task/clarify，用你手上有的工具完成。",
    ])
    return expert_path, goal, context_text, task_label


def _launch_via_channels(ctx: Any, st: _SessionState, session_id: str, slug: str,
                         category: str, confidence: float, goal: str, context_text: str,
                         expert_path: Path, task_label: str) -> Tuple[Optional[str], str]:
    """v2.6 P1: launch 通道序列核心（① lifecycle → ② delegate_task），pre/post 共用单一来源。
    返回 (delegation_id, channel):
      成功 → (id, "subagent_lifecycle"|"delegate_task")——通道内已做 running/lifecycle 记账 + dispatch 事件；
      全拒 → (None, "refused")——dispatch-refused 已记审计，由调用方决定降级面（pre=当轮注入 / post=fallback_due）；
      宿主工具面故障（异常）→ (None, "error")——dispatch-error 已记，不做降级（语义不变）。"""
    # ① subagent-lifecycle 通道（gateway 模式）: 成功即真子 agent 已起
    sid = _lifecycle_dispatch(ctx, st, session_id, goal, context_text,
                             category, slug, confidence, expert_path, task_label)
    if sid is not None:
        return sid, "subagent_lifecycle"
    # ② 旧通道 delegate_task（CLI 模式; gateway 模式返回 tool_error → 记 refused）
    args = {"tasks": [{"goal": goal, "context": context_text}]}
    try:
        raw = ctx.dispatch_tool("delegate_task", args)
    except Exception as e:
        # 宿主工具面本身故障（区别于"返回拒绝"）: 保留 dispatch-error 语义, 不做降级注入
        logger.error(f"intent-router: sub-expert dispatch failed: {e}")
        _log(session_id, "dispatch-error", expert=slug, error=str(e))
        return None, "error"
    parsed: Any = None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        parsed = None
    deleg_id: Optional[str] = None
    error_text: Optional[str] = None
    if isinstance(parsed, dict):
        if parsed.get("delegation_id"):
            deleg_id = str(parsed["delegation_id"])
        elif "error" in parsed:
            error_text = str(parsed.get("error"))
    if deleg_id is not None:
        with _LOCK:
            _sweep_running(st)
            st.running[slug] = (deleg_id, time.time())
        _log(session_id, "dispatch", category=category, expert=slug, confidence=confidence,
             delegation_id=deleg_id, channel="delegate_task", expert_path=str(expert_path))
        return deleg_id, "delegate_task"
    # 拒绝（tool_error JSON / 不可解析）→ 拒绝事实入审计；running 不动，避免假 running 卡住重派
    if error_text is None:
        error_text = f"unparseable result: {str(raw)[:200]}"
    _log(session_id, "dispatch-refused", expert=slug, channel="delegate_task", error=error_text[:300])
    return None, "refused"


def _dispatch_sub_expert(ctx: Any, st: _SessionState, session_id: str, user_message: str,
                         conversation_history: Any, category: str, slug: str,
                         confidence: float) -> Tuple[Optional[str], Optional[str]]:
    """派发 Y 域子专家（pre 模式, 当轮真派发）。v2.4 通道顺序（owner 裁决 2026-11-17: 宿主只读，
    改动仅落插件层）: ①②③ 语义见 _launch_via_channels + 本函数降级面。
    返回 (delegation_id, dispatch_turn_context): 成功 → (id, v2.5 L1 台账卡, 主专家
    开工第一秒知道分工); 全拒且有正文 → (None, 降级注入文本)。
    不传 toolsets → 子代理继承父工具面，含 filemem + knowledge-base-search
    （已核实 DELEGATE_BLOCKED_TOOLS 不覆盖这两组）。"""
    expert_path, goal, context_text, task_label = _build_dispatch_inputs(
        st, slug, user_message, conversation_history, category)
    deleg_id, channel = _launch_via_channels(ctx, st, session_id, slug, category, confidence,
                                              goal, context_text, expert_path, task_label)
    if channel in ("subagent_lifecycle", "delegate_task"):
        assert deleg_id is not None    # 成功通道必带关联 id（_launch_via_channels 契约）
        # v2.5 L1+L4: 成功同轮台账卡——主专家从开工第一秒知道分工（勿重复 + 审计键）
        return deleg_id, _ledger_card(slug, task_label, deleg_id, channel)
    if channel == "refused":
        # ③ 降级 in-session 注入（programming value 不丢）
        body = _load_expert_context(slug)
        if body:
            _log(session_id, "dispatch-fallback-injection", expert=slug, confidence=confidence)
            return None, _wrap_injection(category, slug, body)
        return None, None
    return None, None    # channel == "error": 宿主工具面故障, 不做降级（语义不变）


# ── v2.6 P1: post 模式顺延面（台账卡 / 降级注入在下一 pre 轮交付） ─────────

def _consume_ledger_due(st: _SessionState, session_id: str) -> Optional[str]:
    """v2.6: post 模式 launch 成功的台账卡（post_llm_call 记账, 下一 pre 轮交付;
    pre 模式恒空 → 零行为变化）。append-only, 卡片字节序稳定。"""
    if not st.ledger_due:
        return None
    with _LOCK:
        items = list(st.ledger_due.items())
        for slug, _v in items:
            st.ledger_due.pop(slug, None)
    pieces = []
    for slug, (deleg_id, task_label, channel) in items:
        _log(session_id, "ledger-post-inject", expert=slug,
             delegation_id=deleg_id, channel=channel)
        pieces.append(_ledger_card(slug, task_label, deleg_id, channel, post=True))
    return "\n\n".join(pieces) if pieces else None


def _consume_fallback_due(st: _SessionState, session_id: str) -> Optional[str]:
    """v2.6: post 模式全拒的降级专家全文注入（post_llm_call 记账, 下一 pre 轮交付;
    programming value 不丢, 时延一轮。pre 模式恒空 → 零行为变化）。"""
    if not st.fallback_due:
        return None
    with _LOCK:
        items = list(st.fallback_due.items())
        for slug, _v in items:
            st.fallback_due.pop(slug, None)
    pieces = []
    for slug, (confidence, task_label, category) in items:
        body = _load_expert_context(slug)
        if body:
            _log(session_id, "dispatch-fallback-injection", expert=slug,
                 confidence=confidence, note="post-deferred")
            pieces.append(_wrap_injection(category, slug, body))
    return "\n\n".join(pieces) if pieces else None


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


# ── launch_timing 分序开关（v2.6 P1, owner 裁决 2026-11-17 "时序做"）────────
# pre（缺省）: 现状——pre_llm_call 分类即真 launch（主/子并行, 延迟重叠）。
# post: 治"主专家还没准备好/分工, 子专家先被拉起"的时序倒置——pre 轮只记
# pending, 宿主 post_llm_call（主专家回复之后）才真 launch; 台账卡/降级注入
# 顺延到下一 pre 轮。全部 append-only, 前缀缓存不变量不变。

def _launch_timing(ctx: Any) -> str:
    """读 launch_timing 配置（"pre"|"post"）。缺省/异常 = "pre"（fail-soft,
    旧宿主无 get_config → 零行为变化）。"""
    try:
        v = str(ctx.get_config("launch_timing", "pre") or "pre").lower()
    except Exception:
        return "pre"
    return v if v in ("pre", "post") else "pre"


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

        # 0) P0 平台守卫（v2.5.1, owner 指认"代码侧问题"查实 2026-11-17）: 子 agent
        #    session 自己的回合不得进入路由流水线。宿主 turn_context.py:906 对每个
        #    agent 回合无条件触发 pre_llm_call（只排除 detached fork），kwargs 带
        #    platform（子 agent = "subagent"）+ parent_session_id —— 无守卫时默认
        #    profile（anchor_inject=true）会把错误专家全文注入子 agent（persona 冲突
        #    + 膨胀）、烧 aux 分类、并可能孙代级联派发（子专家拉子专家）。
        #    OR 语义: 任一信号即挡（宿主版本演进下双信号互为备份）。
        #    审计: 每 session 记一次 subagent-guard（多 API-call 子专家不刷 jsonl）。
        if (kwargs.get("parent_session_id") or platform == "subagent"):
            if not st.guard_logged:
                st.guard_logged = True
                _log(session_id, "subagent-guard")
            st.guarded = True   # v2.6 P1: 子 session 标记（post 面双信号之一, 防 pending 误派发）
            return None

        # 0.5) 循环守卫: 合成完成轮 → 清 running + 跳过检测
        if _handle_completion_reentry(msg, st, session_id):
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

        # ── 后续轮: 压缩补注（事件驱动）+ 新域检测（v2 零注入不变量不变） ──
        lifecycle_ctx = _poll_lifecycle(ctx, st, session_id)   # v2.4: lifecycle 通道子专家 findings
        reinject_ctx = _reinject_after_compaction(st, session_id, conversation_history)
        category, confidence = _classify(ctx, msg)
        slug = None
        if category is not None and confidence >= _CONFIDENCE_GATE:
            slug = CATEGORY_MAP.get(category)
        # v2.6 P1: 分序面——pre（缺省, 零变化）/ post（launch 顺延到 post_llm_call）；
        # post 模式下本轮先消费上轮 post 记账的顺延卡（台账 / 降级注入, 尾部 append-only）
        timing = _launch_timing(ctx)
        due_ctx = (_join_ctx(_consume_ledger_due(st, session_id),
                             _consume_fallback_due(st, session_id))
                   if timing == "post" else None)
        if slug is None or slug == st.anchor:
            # 仍在锚点域 / 无新域 → 不派发；压缩事件轮仍补注（尾部加法，缓存安全）
            ctx_out = _join_ctx(reinject_ctx, lifecycle_ctx, due_ctx)
            return {"context": ctx_out} if ctx_out else None
        # 走到这里 ⇒ slug 有效且 ≠ 锚点 ⇒ category 必非 None（slug 由它派生）
        assert category is not None
        if timing == "post":
            # v2.6 P1: post 模式 pre 轮不真派发——记账 pending（宿主 post_llm_call 真 launch）；
            # 台账卡 / 降级注入顺延到下一 pre 轮（due_ctx 已在上方消费）
            with _LOCK:
                _sweep_running(st)
                if slug in st.running:
                    _log(session_id, "skip-running", category=category, expert=slug)
                    ctx_out = _join_ctx(reinject_ctx, lifecycle_ctx, due_ctx)
                    return {"context": ctx_out} if ctx_out else None
                if slug in st.pending:
                    _log(session_id, "skip-pending", category=category, expert=slug)
                    ctx_out = _join_ctx(reinject_ctx, lifecycle_ctx, due_ctx)
                    return {"context": ctx_out} if ctx_out else None
            expert_path, goal, context_text, task_label = _build_dispatch_inputs(
                st, slug, msg, conversation_history, category)
            with _LOCK:
                st.pending[slug] = (category, confidence, task_label, goal, context_text,
                                    expert_path)
            _log(session_id, "dispatch-pending", category=category, expert=slug,
                 confidence=confidence)
            ctx_out = _join_ctx(reinject_ctx, lifecycle_ctx, due_ctx)
            return {"context": ctx_out} if ctx_out else None
        with _LOCK:
            _sweep_running(st)
            if slug in st.running:
                _log(session_id, "skip-running", category=category, expert=slug)
                ctx_out = _join_ctx(reinject_ctx, lifecycle_ctx)
                return {"context": ctx_out} if ctx_out else None
        _deleg_id, dispatch_ctx = _dispatch_sub_expert(ctx, st, session_id, msg,
                                                        conversation_history,
                                                        category, slug, confidence)
        # v2.5 L1: dispatch_ctx = 成功轮台账卡 / 全拒降级注入（_join_ctx 保持字节序:
        # reinject → lifecycle findings/提醒/交接 → 派发轮上下文）
        ctx_out = _join_ctx(reinject_ctx, lifecycle_ctx, dispatch_ctx)
        return {"context": ctx_out} if ctx_out else None

    return _intent_router_hook


def _launch_pending(ctx: Any, st: _SessionState, session_id: str, slug: str,
                    category: str, confidence: float, task_label: str, goal: str,
                    context_text: str, expert_path: Path) -> None:
    """v2.6 P1: post 模式真 launch（post_llm_call 内执行; 通道序列与 pre 共用
    _launch_via_channels 单一来源）。结果面顺延到下一 pre 轮:
    成功 → ledger_due（台账卡, post 变体文案"上轮回复后已启动"）;
    全拒 → fallback_due（降级专家全文注入, 时延一轮, programming value 不丢）;
    宿主工具面故障（error）→ 不降级（dispatch-error 语义, 与 pre 一致）。"""
    deleg_id, channel = _launch_via_channels(ctx, st, session_id, slug, category, confidence,
                                              goal, context_text, expert_path, task_label)
    if channel in ("subagent_lifecycle", "delegate_task"):
        assert deleg_id is not None
        st.ledger_due[slug] = (deleg_id, task_label, channel)
    elif channel == "refused":
        st.fallback_due[slug] = (confidence, task_label, category)


def _make_post_hook(ctx: Any):
    """v2.6 P1: post_llm_call hook——launch_timing=post 时主专家回复之后真 launch。

    宿主实证（2026-11-17 只读）: post_llm_call 每 turn 一次（turn_finalizer.py,
    tool loop 结束后）, kwargs 带 session_id/user_message/assistant_response/
    conversation_history/model/platform（无 parent_session_id）; worker 线程经
    copy_context 继承 → 父 agent contextvar 绑定与 pre 时刻等价（v2.4.3 B 冻结
    快照机制直接适用）; ∈ _HOOK_TIMEOUT_BOUNDED_HOOKS（30s 界内, fail-open）。

    三重 no-op 门（缺省 pre 模式零开销）:
      ① 子 agent session（platform=="subagent" 或 st.guarded——pre 守卫打标）
         → 对称 P0: 子 session 的 post 轮不得执行 pending 派发
      ② launch_timing != "post"（缺省 pre / 旧宿主无 get_config）→ no-op
      ③ pending 空 → no-op
    """

    def _post_hook(session_id: str, user_message: Any = None,
                   assistant_response: Any = None, **kwargs: Any) -> Optional[Dict[str, str]]:
        st = _get_state(session_id)
        if str(kwargs.get("platform") or "") == "subagent" or st.guarded:
            return None
        if _launch_timing(ctx) != "post":
            return None
        with _LOCK:
            items = list(st.pending.items())
            for slug, _v in items:
                st.pending.pop(slug, None)
        if not items:
            return None
        for slug, (category, confidence, task_label, goal, context_text, expert_path) in items:
            _launch_pending(ctx, st, session_id, slug, category, confidence,
                            task_label, goal, context_text, expert_path)
        return None

    return _post_hook


def _manifest_version() -> str:
    """v2.4.2 收口：注册行版本号跟随 plugin.yaml manifest（不再硬编码字面量，
    避免活体跑新代码却打旧版本注册行 → 验收信号失真）。fail-soft：读不到给 '?'。"""
    try:
        p = Path(__file__).with_name("plugin.yaml")
        text = p.read_text(encoding="utf-8")
        m = re.search(r'^\s*version:\s*["\']?([\w.\-]+)', text, re.M)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def register(ctx) -> None:
    """插件注册入口（宿主加载时调用一次）"""
    ctx.register_hook("pre_llm_call", _make_hook(ctx))
    # v2.6 P1: post_llm_call（launch_timing=post 时主专家回复后真 launch;
    # 缺省 pre 模式三重 no-op 门, 零行为变化）
    ctx.register_hook("post_llm_call", _make_post_hook(ctx))
    logger.info("intent-router v%s registered (anchor + sub-expert dispatch via subagent-lifecycle + delegation-ledger + card reinjection + /expert + anchor_inject + refused-observability + lifecycle-absent diagnostics + launch-timing post)",
                _manifest_version())
