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

## v2.1（2026-11-16，压缩后补注 — owner 裁决）

新增：宿主 compaction/剪枝消化锚点专家全文后，自动补注**蒸馏卡**（事件驱动、幂等、前缀缓存安全）：

| | v2 | v2.1 |
|---|---|---|
| 首轮锚点注入 | 全文注入一次 | 不变；wrapper 内嵌锚点标记 `<!--ir-expert:<slug>-->`（供压缩检测；仍属首轮尾部加法） |
| 后续轮 | 零注入 + 新域派发 | 不变；**仅压缩事件轮**在该轮 user 尾部补注专家卡 |
| 压缩后 | 专家全文被宿主消化，永久丢失 | 双标记缺失（+历史非空）→ 补注蒸馏卡（≤600 token）；卡缺失 → **全文回退**（裁决：专家不丢）；卡再丢再补（幂等） |

蒸馏卡（`cards/<slug>.md`）：离线静态资产进 git，**带判断标准不带模板**（模板/细节只留"取全文"指针）。
13 个大专家（6.5–24KB）出卡；15 个小专家（≤1.1KB）不出卡——补注走全文回退（本来就便宜）。
样例卡：`cards/product-product-manager.md`、`cards/engineering-code-reviewer.md`（owner 已审定）；
批量蒸馏的 11 张大专家卡（子代理分 3 片并行产出，父进程逐卡结构/字节核验后提交）：
`cards/` 现共 13 张，13 张大专家全覆盖。

审计日志新增事件：`card-reinject` / `full-reinject-fallback` / `reinject-failed`。

测试：`tests/test_card_reinjection.py`（10 用例：压缩事件 / 幂等 / 再丢再补 / 卡缺失回退 / 无锚点 / 空历史 / 合成完成轮 / 压缩+派发同轮 / 历史零改动）。

## v2.2（2026-11-17，/expert 手动覆盖 — owner 裁决"继续完成"）

新增：`/expert` 命令手动控制锚点专家（纯命令解析，不依赖 LLM 分类）：

| 指令 | 行为 |
|---|---|
| `/expert <category\|slug>`（如 `/expert security`、`/expert engineering-security-engineer`） | 切锚点；该轮走宿主 veto（`{"action":"block"}` + 确认消息）——**不进推理、不烧分类**。专家标记不在当前上下文 → 下一轮补注**全文**（手动切换是刻意行为）；标记已在上下文 → 只切锚点（专家已在场，零注入） |
| `/expert`（无参） | block：显示当前锚点 + 全部可用 category 列表（状态不动） |
| `/expert clear` | 清锚点，回无专家模式 |
| 未知参数 | block 报错 + 提示看列表（状态不动） |

命令轮与子专家 running 状态互不干扰；完成消息重入仍按 delegation id 清 running。

审计日志新增事件：`manual-anchor` / `manual-full-reinject` / `manual-list` / `manual-clear` / `manual-invalid`。

测试：`tests/test_expert_command.py`（9 用例：切换 / 下一轮补全文 / slug 形态 / 在场只切不注 / 列表 / clear / 未知参数 / 首轮命令 / 与 running 派发交互）。

## v2.3（2026-11-17，anchor_inject 配置开关 — owner 裁决）

背景：profile 的 `SOUL.md` 已含专家 persona（域 profile：security-engineer /
data-engineer / devops / product-manager / researcher / reviewer）时，第一轮
自动锚点注入 = 纯重复（5–7K token）+ 注意力稀释。插件运行时**看不见** SOUL.md
（`pre_llm_call` 契约不传 system prompt / context files；宿主源码实证），
故不做自动检测，改为 per-profile 显式开关（决策权归 owner）：

```yaml
# profile 的 config.yaml
plugins:
  entries:
    intent-router:
      settings:
        anchor_inject: false
```

| | 默认（true/缺省） | `anchor_inject: false` |
|---|---|---|
| 首轮 | 分类 + 锚点注入 | **跳过**（不烧 aux 调用），anchor 保持 None |
| 后续轮 | 检测 + 子专家派发 + 压缩补注 | 同左（无锚点模式：新域照旧派子专家，补注不触发） |
| `/expert` | 可用 | **不受影响**（显式指令 > 隐式自动） |

fail-soft：配置读取异常/宿主无 get_config → 按 true 处理（行为同 v2.2）。
配置经 `ctx.get_config`（宿主一等 API，读 profile `plugins.entries.intent-router.settings`），零硬编码路径，跨平台。

审计日志新增事件：`anchor-disabled`。

测试：`tests/test_anchor_inject_config.py`（4 用例：off 跳过首轮 / off 无锚点派发保留 / off 下 /expert 仍可锚定 / 缺省回归）。

## v2.4（2026-11-17，子专家真派发修复 — owner 裁决"宿主只读、改动仅落插件层"）

背景：2026-11-17 日志实证（coder profile）当日 23 条 `dispatch` 事件全部
`delegation_id=null` 且零子 agent 实际生成——**假派发**。根因（宿主只读勘察）：
gateway 模式下 `PluginManager._cli_ref=None`，`ctx.dispatch_tool("delegate_task")`
无法附 parent agent，宿主 `delegate_task` 返回 tool_error JSON
（`tools/registry.py::tool_error`），旧代码把拒绝误记为成功 `dispatch`。

修复（通道顺序，插件侧）：

| 通道 | 机制 | 适用 |
|---|---|---|
| ① subagent-lifecycle | `ctx.subagent_lifecycle().launch({goal, context, role})`（宿主 SubagentLifecycleService；父 agent 由宿主 turn facade 每轮 contextvar bind，hook 线程经 copy_context 继承——已实证）。返回 SubagentHandle(subagent_id) 作关联 id | **gateway 模式（桌面）** |
| ② delegate_task 旧通道 | `ctx.dispatch_tool("delegate_task")`，成功记 `dispatch(channel=delegate_task)`；拒绝记 `dispatch-refused`（不污染 running） | CLI 模式 |
| ③ 降级 in-session 注入 | 双通道全拒且专家正文可得 → 专家全文当轮注入，记 `dispatch-fallback-injection` | 兜底 |

结果交付：lifecycle 子进程不注册宿主重入通知（重入只在 delegate_task 派发层）
→ 后续 `pre_llm_call` 轮询 `status/result`，完成即 findings append 注入：

- `subagent-result-recovered`（SUCCEEDED，findings 进 context，缓存安全）
- `subagent-result-failed`（FAILED/INTERRUPTED/CANCELLED）
- `subagent-result-lost`（UNKNOWN handle，如 gateway 重启后注册表丢失；清账不卡死）

零回归保证：旧宿主/无 lifecycle API 的 ctx → 行为与 v2.3 完全一致（测试钉死）。
测试：`tests/test_dispatch_refused.py`（5 用例）+ `tests/test_subagent_lifecycle.py`（7 用例），全量 54 passed。
