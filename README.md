# Intent Router Plugin

动态专家上下文路由插件。根据用户意图自动加载对应领域专家技能，避免全局加载 277 个 agent 的 token 浪费。

## 功能

- 分析用户消息意图
- 自动加载对应领域专家技能
- 注入完整专家内容（无截断）
- 按需加载，避免全局浪费

## 工作原理

1. 每轮对话前，`pre_llm_call` hook 触发
2. 使用 LLM 分类用户意图（轻量调用，temperature=0）
3. 置信度 >= 0.7 时加载对应专家内容
4. 专家内容注入到用户消息末尾（不影响 prompt cache）

## 支持的专家领域

| 分类 | Slug | 大小 |
|------|------|------|
| engineering | engineering-backend-architect | ~8KB |
| security | engineering-security-engineer | ~16KB |
| frontend | engineering-frontend-developer | ~8KB |
| code-review | engineering-code-reviewer | ~7KB |
| data | engineering-data-engineer | ~14KB |
| devops | engineering-devops-automator | ~11KB |
| design | design-ui-designer | ~12KB |
| ux | design-ux-researcher | ~11KB |
| product | product-manager | ~24KB |
| marketing | marketing-seo-specialist | ~13KB |
| testing | testing-api-tester | ~11KB |
| performance | testing-performance-benchmarker | ~7KB |

## Token 开销对比

| 方式 | 每轮开销 | 节省 |
|------|----------|------|
| 全局加载 277 个 agent | ~500,000+ | - |
| intent-router | +650 + 2000-8000 | **99%+** |

## 缓存机制

- **首次运行**：从 GitHub 拉取专家内容并缓存到本地
- **后续运行**：直接读本地缓存（零网络请求）
- **缓存位置**：`~/.hermes/plugins/intent-router/experts/`

## 添加新专家

```bash
# 1. 下载专家内容
curl -L "https://raw.githubusercontent.com/jnMetaCode/agency-agents-zh/main/<category>/<slug>.md" \
  -o ~/.hermes/plugins/intent-router/experts/<slug>.md

# 2. 编辑 __init__.py 添加映射
CATEGORY_MAP["<category>"] = "<slug>"

# 3. 重启 Hermes
hermes plugins disable intent-router
hermes plugins enable intent-router
```

## 验证命令

```bash
# 查看插件状态
hermes plugins list | grep intent-router

# 查看日志
grep -i "intent-router\|Injected" ~/.hermes/logs/agent.log

# 测试特定意图
hermes chat -q "帮我 review 代码"
hermes chat -q "帮我设计 UI"
hermes chat -q "帮我写后端 API"
```
