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

## v2（2026-11-16，owner 裁决记录）

行为变更（v1 首轮行为字节级保留）：

| | v1 | v2 |
|---|---|---|
| 首轮 | 分类 → 注入专家全文 | 不变（**锚点专家**，每 session 唯一注入点） |
| 后续轮 | **每轮重注入专家全文**（5–7K × 每轮） | **零注入**；检测到新领域 Y（≠锚点，conf≥0.7）→ 自动派发 Y **子专家** |
| 子专家 | — | `delegate_task`（background，立即返回）；child 自读 `experts/Y.md` + 自取 `filemem_search`/`kb_search_central`；结果以新消息 append 重入 |
| 前缀缓存 | 安全（注入只在 user 尾部） | 不变量收紧：注入只发生在首轮；子专家结果 = 追加新消息，system prompt 零改动 |

owner 裁决：
- **策略 B**：Y 子专家完成后允许为新任务重派（无每 session 上限）；running 期间不重派
- **跨平台**：零硬编码路径常量（pathlib 运行时解析 + 显式 utf-8），无 `sys.platform` 分支
- **循环守卫**：`[ASYNC DELEGATION COMPLETE — <id>]` 合成完成轮跳过检测 + 按 id 清 running 标志
- **git 归属**：子专家只交付 findings，commit/push 归主进程

审计日志：`<plugin 目录>/logs/dispatches.jsonl`（gitignored，fail-soft，每行一个事件：`anchor` / `dispatch` / `skip-running` / `subagent-result-reentered` / `dispatch-error`）。

测试：`tests/test_intent_router_v2.py`（OS-agnostic，scratch venv 跑 pytest；含前缀缓存字节级回归测试）。
