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
# 全量测试（规范入口：自动检测活体 hermes 解释器 — $HERMES_HOME/gateway.pid → pid → python.exe；
# 宿主缺 pytest 会直装，不派生 venv。owner 裁决 2026-11-18：venv 路径随代际漂移，禁止 PATH 猜）
python run_tests.py
python run_tests.py --detect-only   # 只看检测到的解释器

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
| ① subagent-lifecycle | `ctx.subagent_lifecycle`（宿主 SubagentLifecycleService；父 agent 由宿主 turn facade 每轮 contextvar bind，hook 线程经 copy_context 继承——已实证）。宿主属性为 @cached_property 形态时**属性本身即服务实例**（插件鸭子类型: 有 launch/status 直接用, 否则按工厂调用）；返回 SubagentHandle(subagent_id) 作关联 id。v2.4.3 结果回收: 宿主 status() 第三道门要求 poll 时刻 contextvar 父 session 与 handle 匹配（活体 100% UNKNOWN 根因）→ 插件在 launch 时刻冻结 contextvars 快照、poll 在冻结快照里 replay 宿主 API；UNKNOWN 连续 3 次才判 lost（保留重试窗口），lost 注入诚实指针卡 + `resolver_session`/`frozen_session` 诊断（宿主公开 API 探测, 宿主零改动） | **gateway 模式（桌面）** |
| ② delegate_task 旧通道 | `ctx.dispatch_tool("delegate_task")`，成功记 `dispatch(channel=delegate_task)`；拒绝记 `dispatch-refused`（不污染 running） | CLI 模式 |
| ③ 降级 in-session 注入 | 双通道全拒且专家正文可得 → 专家全文当轮注入，记 `dispatch-fallback-injection` | 兜底 |

结果交付：lifecycle 子进程不注册宿主重入通知（重入只在 delegate_task 派发层）
→ 后续 `pre_llm_call` 轮询 `status/result`，完成即 findings append 注入：

- `subagent-result-recovered`（SUCCEEDED，findings 进 context，缓存安全）
- `subagent-result-failed`（FAILED/INTERRUPTED/CANCELLED）
- `subagent-result-lost`（UNKNOWN handle，如 gateway 重启后注册表丢失；清账不卡死）
- `lifecycle-absent`（通道①不可用及原因：`attr-missing` 旧宿主 /
  `factory-error` 宿主 import 链故障 / `factory-returned-none` — 2026-11-17 重启后
  复查发现通道①静默缺席, 补此事件定位；launch 被拒另有 `lifecycle-unavailable`）

零回归保证：旧宿主/无 lifecycle API 的 ctx → 行为与 v2.3 完全一致（测试钉死）。
测试：`tests/test_dispatch_refused.py`（5 用例）+ `tests/test_subagent_lifecycle.py`（9 用例），全量 56 passed。

## v2.4.2（2026-11-17，通道①活体根因修复）
- 诊断事件 `lifecycle-absent` 带类型名 → 活体实证 `attr-not-callable
  (ctx=PluginContext, attr=SubagentLifecycleService)`：宿主 `subagent_lifecycle`
  是 @cached_property，**属性本身即服务实例**（不是工厂）
- `_get_lifecycle_service` 鸭子类型化：属性带 launch/status 面 → 直接用；
  否则若可调用 → 按工厂调用。零宿主耦合
- 注册行版本号改读 plugin.yaml manifest（此前硬编码字面量漂移：活体跑 v2.4.2
  却打 v2.4.1 注册行 → 验收信号失真）

## v2.4.3（2026-11-17，结果回收修复——活体 0/6 recovered 全灭）
- 根因（宿主只读勘察 + 沙箱复现钉死）：宿主 `status()` 的 `_record()` 第三道门
  要求 poll 时刻 contextvar 里的活父 agent session 与 handle 匹配；活体
  pre_llm_call worker 快照解析不到 → 全部 UNKNOWN_HANDLE
- **B 冻结上下文**：launch 成功即 `contextvars.copy_context()` 冻住（该快照里
  resolver 已被证明 work），poll 在冻结快照里 replay 宿主 status/result
- **C 重试窗口**：UNKNOWN 不再一次清账——连续 3 次才判 lost（_RUNNING_TTL
  sweep 兜底），期间注册表恢复即 recovered；lost 注入诚实指针卡
- **D 重派风暴**：旧码 lifecycle 通道不占 `st.running` 守卫 → 同域每轮重派 +
  lifecycle entry 被新 sid 覆盖 → 旧 handle 孤儿化（活体 100% 回收失败的放大器）；
  修复后 running 期 skip-running，终态清守卫同轮重派接新任务（owner 策略 B）
- **A 诊断**：lost 事件带 `resolver_session`/`frozen_session`（宿主公开 API
  get_active_subagent_parent 双上下文探测，best-effort）

## v2.4.4（2026-11-17，主/子专家协作面——"各做各的"）
- 活体实证：同任务主进程与子专家各做一遍（子专家 72 API call 的 findings 随
  gateway 重启蒸發，主进程零感知）；子专家 launch context 无主会话状态
- **F3 边界感知**：launch context 注入"主会话锚点专家 = <slug>；只交付 Y 域
  findings，不重复主进程工作；主对话不可见，持久上下文查 filemem/kbs"
  （①② 通道共享，进子 agent 自身 context，主前缀零改动）
- **F2 成果持久**：指示子专家完成时写 filemem（type=task, scope=day,
  含 slug+任务原话+结论）→ 跨 gateway 重启成果可捞回（宿主注册表设计为
  进程内驻留，重启即清——filemem 是架构内共享持久面；子专家工具面继承
  父进程含 filemem，活体日志实证其已自取 filemem_search/kb_search_central）
- **F4 接入指引**：recovered/lost 卡面写明"子专家独立完成，请采纳/核验而非
  重做；持久副本 filemem 检索 slug=<slug> + 时间窗，审计键 delegation_id"

## v2.5（2026-11-17，分工台账——主专家全程知道子专家在做什么）
- 背景：v2.4.x 修了"成果可达 + 不重做"，但"开工可见性"一直缺——派发发生在主 LLM
  处理消息之前，主专家零感知子专家已接手哪项任务（各做各的起点）；RUNNING 期间
  poll 静默 → 持续零感知；交接被动且有窗口。owner 裁决"动手"补全分工面。
  全部 append-only 尾部加法（前缀缓存不变量不变，`test_prefix_cache_stable` 的
  字节扩展检查继续钉死）。
- **L1 派发同轮台账卡**：派发成功那一轮注入 `## 分工台账（进行中）`（子专家
  slug + 任务标签 + delegation_id 审计键 + "主进程勿重复该项"）——主专家从
  开工第一秒知道分工。①② 通道成功均发；全拒（未开工）不发。
- **L2 运行期提醒**：lifecycle poll 遇 RUNNING/PENDING/STARTING/CANCEL_REQUESTED
  不再静默，每轮尾部一行 `（子专家 <slug> 处理中: "<任务>"; 勿重复…）`（同 entry
  字节稳定；UNKNOWN 重试轮保持静默——结果去向由 lost 交接卡一次性交代）。
- **L3 终态交接卡**：recovered/failed/lost 全升级 `## 子专家交接` 语义，带
  "主专家下一步" 指令（采纳/核验而非重做；失败/丢失 → 主进程自行处理或重派）。
- **L4 审计键**：台账/提醒卡带 `delegation_id` → 跨重启可经 jsonl/filemem 重新
  锚定"哪些活子专家接了"。
- 折入裁决：F-2（lifecycle 3 元组兼容分支删除，生产不可达死代码；entry 升级为
  6 元组尾部 + task_label）+ F-4（lost 诊断字段测试断言收紧为 `"unavailable"` 精确值）。
- lifecycle entry 形态：`(subagent_id, handle_dict, dispatched_at,
  frozen_contextvars, unknown_count, task_label)`（`_lifecycle_entry()` 归一 6 元组，
  旧 5 元组尾部补 None，3 元组分支已删）。

测试：`tests/test_delegation_ledger.py`（10 用例：L1 双通道台账 + 全拒不发 /
L2 提醒字节稳定 + skip-running 同轮 + UNKNOWN 静默 / L3 recovered·failed·lost
交接卡 / F-4 诊断精确值）。全量 77 passed（基线 67 + 新增 10，零回归）。宿主零改动。

## v2.5.1（2026-11-17，P0 平台守卫——owner 指认"代码侧问题"查实）
- 活体实证：宿主 `turn_context.py:906` 对每个 agent 回合无条件触发 `pre_llm_call`
  （只排除 `_persist_disabled` detached fork）→ 子 agent session 自己的回合也跑
  完整路由流水线。池 jsonl 42 条子 session `anchor-disabled` 事件（coder profile
  anchor_inject=false 在首轮分支短路 → 仅噪声 + 浪费 get_config）；**默认 profile
  （anchor_inject=true）= 错误专家全文注入子 agent（persona 冲突 + 膨胀）+ 烧 aux
  分类 + 孙代级联派发风险**。
- 修复：hook 入口 `parent_session_id or platform == "subagent"` 短路（OR 语义，
  双信号互为备份防宿主版本演进；宿主零改动），每 session 记一次
  `subagent-guard` 审计事件（多 API-call 子专家不刷 jsonl）。

测试：`tests/test_subagent_guard.py`（6 用例：platform/parent 双信号短路 +
审计节流 + 默认 profile 事故钉死 + 主 session 双零回归）。全量 83 passed
（基线 77 + 新增 6，零回归）。宿主零改动。

## v2.6（2026-11-17，P1 分序开关——owner 裁决"时序做"）
- `launch_timing` 配置开关（缺省 pre, 零行为变化; 注册行版本跟随 manifest 自动变 v2.6）：
  - **pre（缺省）**: pre_llm_call 当轮真派发（主/子并行, 延迟重叠）——v2.5 现行为。
  - **post**: pre 轮只分类 + 记 `dispatch-pending`（零派发、零台账卡）；主专家回复后
    宿主 `post_llm_call`（turn_finalizer, 每 turn 一次）真 launch——通道序列与 pre
    共用 `_launch_via_channels` 单一来源；成功 → 台账卡顺延下一 pre 轮（post 变体
    文案"上轮回复后已启动"）；全拒 → 降级注入顺延一轮；`skip-pending` 同域幂等守卫。
- post 面三重 no-op 门（pre 模式零开销）：子 agent session（platform=subagent OR
  st.guarded, P0 对称）/ launch_timing!=post / pending 空。
- 宿主只读实证: post_llm_call kwargs 带 platform 无 parent_session_id（子 session
  靠双信号识别）; worker 线程 copy_context 继承 → 父 agent contextvar 绑定与 pre
  时刻等价（v2.4.3 B 冻结快照机制直接复用）; ∈ _HOOK_TIMEOUT_BOUNDED_HOOKS
  （30s 界内, fail-open）。

测试：`tests/test_launch_timing.py`（9 用例: post 核心时序 / 同域 skip / 全拒降级
顺延 / 子 session 对称守卫 / pre 缺省零变化 / 双 hook 注册 / 终态轮询不变 +
F4/F5 review-fix 2 用例）。宿主零改动。

## v2.7（2026-11-17，reviewer 复盘收口——F2/F3/G1）
**G1 短消息 classify 护栏**（复盘数据点: 单字"修"被 aux 误路由给
marketing-seo 且过 0.7 gate）:
- 后续轮 msg < 4 字（strip 后）且会话无同轮终态放行旗标 → 直接跳过 aux 分类
  （静默, 零事件, 0 成本）——单/双字插话（"修"/"推"/"继续"）无领域信号,
  误派即噪声；≥4 字照旧分类（"做个安全审查" 6 字不受影响）；首轮 10 字
  问候守卫不变（独立分支）。
- **终态放行旗标（`redispatch_due`, v2.4.3 owner 策略 B 协同）**: failed/lost
  终态（任务未交付）置位 → 当轮短消息也必须分类, 否则"消息分类到新域时同轮
  重派"路径被护栏阻断（test_unknown_not_cleared_until_retry_cap 回归实证）。
  旗标**只做分类放行, 不触发强制重派**——锚点域消息不自动重派, 终态交接卡归
  主专家决策（test_l3_failed_handoff_card / test_lifecycle_result_failed_is_cleared
  / test_lifecycle_unknown_handle_is_lost 钉死）; 同轮结算即清, 不跨轮泄漏。
**F2 pending-stuck 诊断**（post 模式可观测缺口: 宿主 post hook 不 fire 则
pending 永久滞留, 旧码只有反复 skip-pending）:
- pending 7 元组尾部 + recorded_at; 后续 pre 轮同域且 pending 年龄 > 300s
  （_PENDING_STUCK_SECONDS）→ 记 `pending-stuck`（带 age_s）替代普通
  skip-pending; **pending 不清除**——迟到的 post hook 仍可恢复 launch。
  健康路径（新鲜 pending）行为不变。
**F3 post 首轮盲性显性化**（纯文档, 零行为变化）: post 模式取舍——主专家
首轮回复先于 launch 生成（子专家盲起步）, 台账卡到下一条用户消息才送达,
findings 押在下一轮轮询（gateway 重启宿主注册表消亡, 子专家 filemem 落盘的
持久检索键是最后防线）; opt-in post 即接受该取舍。已写入 manifest
launch_timing 描述。

测试：`tests/test_review_fixes.py`（6 用例: G1 短消息跳过 + 静默语义 /
F2 pending-stuck 诊断 + 健康路径不变 / G1×终态新域重派不回归 + 锚点域不强制
重派）。全量 98 passed（基线 92 + 新增 6, 零回归）。宿主零改动。

## v2.8（2026-11-18，持久层自动捞回 G3 — owner 裁决"动手"，子专家取证报告）

**动机（活体实证 2026-11-18）**: lost 卡「只有指示没有动作」——filemem 检索靠
主专家 LLM 自觉；且 G1 缺口（子 agent 工具面无 filemem 族）使 launch 提示词
「完成时写 filemem」（F2 指令）在现行宿主构建下不可执行、静默失效。当日活体
实例: 重启冲掉注册表 + poll UNKNOWN×3 → lost 卡只给指引, 子专家 1897 字
findings 靠主进程人肉从盘上备份文件捞回。

**G3（纯插件侧, 宿主零改动）**:
- `_filemem_recover(slug, deleg_id, limit=3)`: 终态（UNKNOWN×3 判 lost）时只读扫
  filemem 中央存储（全 5 分片, 顶层 list-of-rows）。主匹配 = delegation_id
  审计键（唯一精确）; 无主命中兜底 = slug 子串。fail-soft: 目录缺失 /
  JSON 损坏 / 锁 → [] 且绝不影响 hook。
- 卡行为: 命中 → lost 卡追加「### 持久层自动捞回（G3: filemem 命中 N 行 …）」
  （append-only; lost 卡为终态一次性注入、不重注入 → 缓存不变量不受影响）;
  未命中 → base 卡字节稳定（既有子串断言全数钉住, 零回归）。
- 观测: `subagent-result-lost` 事件带 `filemem_hits` / `filemem_hit_ids` 字段。
- 测试隔离: 各测试文件 env fixture 重绑 `mod._FILEMEM_MEMORIES_DIR` 到 tmp
  （单测绝不扫真实中央存储）。

**明确不做（scope 纪律）**: failed 分支自动捞回（G3b 候选）; 盘上 transcript
直读（delegation_id→子 session 映射需宿主侧, 归 G2 改动点清单）; 完成即推送
（G5, 宿主合成重入）。

测试：`tests/test_lost_recovery.py`（7 用例: recover 主匹配/兜底/上限/fail-soft ×4
+ lost 端到端命中并入 / 未命中字节稳定 / 损坏不阻塞清账 ×3）。全量 105 passed
（基线 98 + 新增 7, 零回归）。宿主零改动。
活体验收信号: 下次 gateway 重启注册行 `intent-router v2.8.0 registered (…) + lost-filemem-recovery`;
下次 `subagent-result-lost` 事件带 `filemem_hits`（命中时卡面直接出「持久层自动捞回」段）。

## v2.9（2026-11-18，问句前置门 c —— owner 裁决「后续轮问句不触发派发」）

**裁决依据（活体数据点 ≥3）**："完成了吗"（4 字过 G1 长度护栏）误路由 writing-assistant；
"推，然后继续" 误路由 backend-architect；"这个完成了吗" 同型再犯。G1 只拦长度，拦不住
恰好过线的操作性/状态问句。

**实现（纯插件，宿主零改动；注入面零变化——门是分类前置判定，不产生任何注入）**

- 后续轮（首轮问候守卫之后）在 G1 长度护栏之后、aux 分类之前插**问句前置门**：
  句尾 ∈ {？, ?, 吗, 呢, 嘛} 或句首 ∈ {是不是, 是否, 有没有, 为什么, 什么, 怎么,
  哪个, 哪些, 何时, 哪里} → **静默跳过 classify**（零事件，与 G1 同纪律：门的效果由
  「本会发生的 dispatch 不再发生」呈现，不刷 jsonl）。
- **同轮终态放行旗标例外**（与 G1 共用 `st.redispatch_due`）：lost/failed 未清账的
  那一轮，问句也放行分类——v2.4.3 owner 策略 B「终态未交付任务同轮消息触发重派」
  不被护栏阻断（回归网 `test_unknown_not_cleared_until_retry_cap` 钉死）。
- 误杀面（owner 裁决接受）：后续轮任务型问句（"部署脚本怎么写"）同轮不派子专家，
  主专家自行作答——宁可不派，不误派。

测试：`tests/test_question_gate.py`（3 用例：问句静默跳过 / 同轮终态旗标放行 / 任务问句跳过
+ 非问句任务不受影响）。全量 **112 passed**（基线 109 + 新增 3，零回归）。宿主零改动。

活体验收信号: 下次 gateway 重启注册行 `intent-router v2.9.0 registered`; 后续轮发
"完成了吗" 类问句 → jsonl 无 classify、无 dispatch、无台账卡（静默生效），任务型
消息（非问句）派发行为不变。

## v3.0 / v3.1（2026-11-18，owner 裁决：委派判断归模型 —— 确定性派发栈退役）

**裁决**（owner, 本会话）：不修宿主；LLM 在自己的工具面 spawn 子 agent
（`delegate_task`），宿主免费重入交结果；插件的职责 = 给信号。世界观标准（owner 注入）：
任何机制的最高评价标准 = 对编程的价值。

**退役面**（v2.4–v2.8 全通道栈, 代码删除; 活体取证记录保留在 git 历史）：
lifecycle spawn / 轮询三态清账 / G3 持久层自动捞回 / 分工台账卡 / launch_timing + post hook
/ 降级注入 / dispatch-refused 观测。注册面回归 `pre_llm_call` 单一 hook。

**新增面：委派提示卡**（append-only 尾部, 缓存安全）：后续轮新域过 conf gate 时注入
`## 委派提示（新域: X）`——delegate_task 调用模板（goal 带专家 slug + 任务原话；context
带专家文件路径 + filemem/kbs 自取指引 + git 归主进程约束）+「结果宿主自动重入, 不委派也可
自答」。幂等：每 (session, 域) 一次；压缩后 `<!--ir-hint:slug-->` 标记丢失 → 再补一次
（与 v2.1 卡补注同构）。完成重入轮（`[ASYNC DELEGATION ...]`）一律 completion-skip
（v2.0.1 循环守卫简化形态：v3 无 running 状态可清, 只防 检测→提示→委派→重入→再检测 噪声环）。

**保留面**：锚点专家（v1 字节不变）/ 13 张专家卡 + 压缩补注 / /expert / anchor_inject /
P0 子 session 守卫 / G1 短消息护栏 / 问句前置门（v2.9, 静默零事件）。事件词汇面从此只有：
`anchor / anchor-disabled / manual-* / card-reinject / full-reinject* / hint / completion-skip /
subagent-guard`。

**v3.1 收口（本次）**：测试全量配套 —— 6 个 v2 通道测试文件删除（`test_dispatch_refused /
test_subagent_lifecycle / test_delegation_ledger / test_lost_recovery / test_launch_timing /
test_review_fixes`）, 6 个存活文件改写 + `test_delegation_hint.py`（12 用例）→ 全量
**61 passed ×3 连跑**, `py_compile` 干净, 宿主零改动。注：v3.0.1（鸭子类型 + 注册行跟
manifest）此前只落模块层, manifest 版本至此一并补到 3.1.0。

活体验收信号：下次 gateway 重启注册行 `intent-router v3.1.0 registered (...)`；jsonl 无
dispatch / 无台账 / 无轮询事件；后续轮新域消息出「委派提示」卡 + `hint` 事件；单字 / 问句
指令静默。

## v3.2（2026-11-18，问句 regex 退役 —— owner 全球化裁决「全删 regex 不加指令」）

**裁决**：v2.9 引入的 `_QUESTION_RE`（中文疑问词表 `^(为什么|…)|[？?]$|(吗|呢|嘛)$`）
不适合全球化——疑问形态逐语言不同，扩表 = 无穷打地鼠。且 v3 下它只剩
「省 1 次 aux 调用 + 防噪声卡」的卫生价值（误派盲 spawn 的危害面已随派发栈退役）。
owner 裁 **A 案**：regex 整体删除，**不加替代提示词指令**（C 案被否）——问句与普通
消息同律全量过 aux 分类 + conf gate（0.7），误判出噪声卡可接受（无盲 spawn、尾部
注入缓存安全、token 成本可不计），低 conf 自然无卡。

**变更面**（纯插件，宿主零改动）：

- 删 `_QUESTION_RE` + 门线（`L584` 现仅剩 G1 长度判断——**G1 保留**：纯长度、语言无关）
- 模块零中文疑问词残留（`grep 为什么|是不是 → 0`，测试 `test_no_question_regex_in_module` 钉死）
- **误杀面修复**：旧门静默杀掉的任务型问句（"为什么部署失败"）现在正常分类出提示卡
- 注册行去 `question-pre-gate` 段；manifest 3.2.0
- 测试：`test_question_gate.py` 重写为 6 用例退役钉（无 regex / 中文问句照跑 aux /
  英文问句照跑 aux（语言中立实证）/ 任务型问句不再误杀 / G1 静默保留 / 低 conf 无卡）
  + `test_delegation_hint.py` 旧问句门用例改为 v3.2 语义（aux 照跑 + 幂等无卡）
  → 全量 **63 passed ×3**, `py_compile` 干净

**活体效果**：下次 gateway 重启注册行 `intent-router v3.2.0 registered (… + subagent-guard)`；
英文/中文问句不再有任何差异化静默行为，行为完全由 aux 判断 + conf gate 呈现。

## v3.3（2026-11-18，绑定式委派提示卡 —— owner 裁决「动手」）

**裁决依据（活体实证 ×2, session local-4e22f0ba 的 env-brief 第 3 轮审查）**：
- `8497e1`：模型在 code-review 0.95 提示卡后自主 delegate_task（**v3 模型驱动委派活体首单**），
  但 goal 是**自写任务体**（无"作为 X 领域专家（slug）"framing）→ 子 agent 全程**从未读专家文件**
  （`experts/` grep = 0）——提示卡的"可选建议"口吻塑形失败：委派发生了，专家形态没跟上。
- `7ad9c4`：模型同调用塞了个"备用占位"任务（goal 自述"不需要"），3 秒后自 stop（宿主硬中断,
  `interrupted_during_api_call`, 设计内 interrupted 终态）——卡面无调度纪律约束。

**实现（纯插件, 宿主零改动）**：`_hint_card` 重写为绑定式：

- 口吻升级：「请调用…仅为可选建议」→「请**原样采用** tasks[0] 两个字段（勿改写 goal、勿删减 context）」
- goal 明写 F3 边界：「只交付 findings/结果, 勿重做主进程已在做的工作」（治"各做各的"主→子缺口）
- 新增调度纪律行：「一次只委派一个任务——勿创建占位/备用/no-op 子 agent」（治 7ad9c4 型噪声）
- 保留面不变：slug/conf/expert 路径/filemem+kbs 自取/git 归主进程/压缩重出标记

测试：`test_delegation_hint.py` +3 用例（绑定口吻 / 单任务纪律 / F3 边界）RED→GREEN;
全量 **66 passed**, `py_compile` 干净, manifest **3.3.0**。

**边界（架构诚实）**：v3 形态下模型仍可能自写 goal——绑定只能提高塑形率, 不能强制;
"100% 专家形态" = v2 确定性派发（owner 已退役）。活体观察点：下次新域委派, 看子 agent
goal 是否逐字采用卡面模板 + 是否读了专家文件（`agent.log` 子 session grep `experts/`）。

活体验收信号：下次 gateway 重启注册行 `intent-router v3.3.0 registered (…)`.
