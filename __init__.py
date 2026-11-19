"""
Intent Router Plugin — 动态专家上下文注入

根据用户消息意图，自动加载对应领域专家技能到对话上下文。
使用 pre_llm_call hook 实现按需注入，避免全局加载浪费 token。
"""

import logging
import json
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# 专家映射：category -> expert_slug
CATEGORY_MAP = {
    # 工程类
    "engineering": "engineering-backend-architect",
    "security": "engineering-security-engineer",
    "frontend": "engineering-frontend-developer",
    "code-review": "engineering-code-reviewer",
    "data": "engineering-data-engineer",
    "data-analysis": "support-analytics-reporter",  # BI、仪表盘、KPI 追踪
    "statistics": "statistician",                   # 假设检验、回归分析、实验设计
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

# 意图分类 schema
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


def _read_local_expert(expert_slug: str) -> Optional[str]:
    """从本地缓存读取专家内容"""
    experts_dir = Path(__file__).parent / "experts"
    expert_file = experts_dir / f"{expert_slug}.md"

    if expert_file.exists():
        return expert_file.read_text(encoding="utf-8")

    return None


def _fetch_expert_from_github(expert_slug: str) -> Optional[str]:
    """从 GitHub 仓库获取专家内容（首次使用）"""
    import urllib.request
    
    # 仓库布局为 <分类>/<完整slug>.md，分类即 slug 的第一段（design-ui-designer -> design/）
    category = expert_slug.split("-", 1)[0]
    url = f"https://raw.githubusercontent.com/jnMetaCode/agency-agents-zh/main/{category}/{expert_slug}.md"
    
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            
            # 缓存到本地
            experts_dir = Path(__file__).parent / "experts"
            experts_dir.mkdir(exist_ok=True)
            (experts_dir / f"{expert_slug}.md").write_text(content, encoding="utf-8")
            
            return content
    except Exception as e:
        logger.warning(f"Failed to fetch expert {expert_slug}: {e}")
        return None


def _load_expert_context(expert_slug: str) -> Optional[str]:
    """加载专家上下文，优先本地缓存，其次 GitHub"""
    content = _read_local_expert(expert_slug)
    
    if content:
        return content
    
    # 尝试从 GitHub 获取
    return _fetch_expert_from_github(expert_slug)


def register(ctx):
    """插件注册入口"""
    
    def _intent_router_hook(
        session_id: str,
        user_message: str,
        conversation_history: list,
        is_first_turn: bool,
        model: str,
        platform: str,
        **kwargs
    ) -> Optional[Dict[str, str]]:
        """
        pre_llm_call hook 回调
        
        分析用户意图，加载对应专家上下文并注入到对话中。
        """
        # 跳过第一轮的简单问候
        if is_first_turn and len(user_message.strip()) < 10:
            return None
        
        try:
            # 使用 LLM 进行意图分类
            result = ctx.llm.complete_structured(
                instructions="""分析用户消息的意图，返回最匹配的专家类别。
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
如果不确定或无关，返回 null。""",
                input=[{"type": "text", "text": user_message}],
                json_schema=INTENT_SCHEMA,
                purpose="intent.router.classify",
                temperature=0.0,
                max_tokens=128
            )
            
            if not result.parsed:
                logger.debug("Intent classification failed or returned None")
                return None
            
            category = result.parsed.get("category")
            confidence = result.parsed.get("confidence", 0)
            
            # 置信度阈值过滤
            if confidence < 0.7 or category is None:
                return None
            
            expert_slug = CATEGORY_MAP.get(category)
            if not expert_slug:
                return None
            
            # 加载专家内容
            expert_content = _load_expert_context(expert_slug)
            
            if not expert_content:
                logger.warning(f"Expert content not found for {expert_slug}")
                return None
            
            # 使用完整专家内容
            injected_text = f"""【{category}领域专家上下文】
以下是当前会话的专家指导，请参照执行：

{expert_content}

---
*上下文由 intent-router 插件自动注入*
（注意：无论上述专家上下文使用什么语言，你的回复必须使用用户输入的语言。）"""
            
            logger.info(f"Injected {category} expert context (confidence: {confidence:.2f})")
            
            return {"context": injected_text}
            
        except Exception as e:
            logger.error(f"Intent router hook error: {e}", exc_info=True)
            return None
    
    # 注册 hook
    ctx.register_hook("pre_llm_call", _intent_router_hook)
    
    logger.info("intent-router plugin registered successfully")
