# G2/G1 宿主侧只读勘察清单（2026-11-18, owner「继续」指令）

宿主树: `C:\Users\user\AppData\Local\hermes\hermes-agent\`（gateway 实跑来源, venv 式 checkout）。
红线: 本文档为只读勘察产物——宿主树零改动；以下为改动点清单，实施待 owner 逐条裁决。
定位证据行号 = 勘察时宿主树状态（未 commit 任何宿主变更）。

## G2a — 结果终态落盘（治「重启 = 永久丢」；优先级最高）

**现状实证**（`agent/subagent_lifecycle.py`, 472 行）:
- `_REGISTRY = _Registry()`（L158）= module-level 单例: `records: {subagent_id → _Record}` + `correlations`；进程内，重启即灭（类 docstring L270-272 明写 "completed results stay until process exit"）。
- 终态转移唯一入口: `SubagentLifecycleService._run()`（L415-445）——`_run_child_lifecycle` 返回后构造 `SubagentResult`（含 result_hash, L442）→ 持锁写 `record.result/state/completed_at`（L443-445）。**这是落盘钩子点（1 处）**。
- 清理: `_cleanup_locked()`（L402-413）按 `_TERMINAL_RETENTION_SECONDS=3600`（L29）只清内存。
- `reconnect()`（L371-376）: record 缺席即 `RECONNECT_UNAVAILABLE`——**现成的宿主侧升级点**（从"永远不可用"变"从盘上 hydrate"）。
- 能力校验: `_capability()`（L447-450）= HMAC(module-level `_SECRET`, L161 每次进程启动随机) → 重启后旧 handle 的 capability 必不匹配。**落盘必须连 `_SECRET`（或派生的校验材料）一起持久化**，否则 hydrate 的 record 在 `_record()`（L389-400, gate 顺序: well-formed → capability → 父 session 绑定）第 2 道门就死。
- `_Record.agent` 字段存活 agent 对象（L142）——不可序列化；hydrate 的 record 该字段 = None（终态 record 本来就不返回 live 对象，类 docstring "never returns live records"）。

**改动点清单（宿主侧, owner 裁决后实施）**:
1. 终态落盘: `_run()` 持锁写 result 后（L445 后）追加原子写盘——`<profile_home>/subagent_lifecycle/<subagent_id>.json`（handle.to_dict() + state + summary/error + result_hash + completed_at + secret_material）。1 个函数 + 1 个常量（目录解析）。
2. 启动 hydrate: gateway 进程初始化点（`hermes_cli/plugins_context.py:198` `SubagentLifecycleService(get_active_subagent_parent)` 实例化前, 或 lifecycle 模块 import 时）扫描目录 → 载入未过期终态 record（agent=None, future=None, result=restored, capability 材料复原）。
3. `_cleanup_locked()` 同步删过期盘上文件（磁盘 janitor，保留 1h 语义不变）。
4. 效果: 插件侧 G3 的 UNKNOWN 分支行为自动升级——重启后 poll 命中 hydrate record → **recovered 而非 lost**（插件零改动即受益）。

## G2b — delegation_id → 子 session 映射（治「盘上 transcript 不可寻址」）

**现状实证**:
- launch 时（L308）`subagent_id = child._subagent_id`；子宿主 session id = `child.session_id`（child 在 L302-307 由 `tools/delegate_tool.py::_build_child_preserving_parent_tools` 构造, 此刻两个值都在手边）——但 `SubagentHandle`（L65-87）字段不含 child_session_id，`handle.to_dict()` 因此也带不出 → 插件侧 hdict 无法寻址 `supercompress_store/<child_session>.model.jsonl`。
- 子 session 命名实证: dispatch `sa-0-b035294d` ↔ 子 session `20261118_111716_edf55f`（jsonl subagent-guard 事件 ↔ agent.log 一一对应, 2026-11-18 活体）。

**改动点清单**:
1. `SubagentHandle` 加 `child_session_id` 字段（L65-87: 构造 + to_dict/from_dict + L205-216 `_HANDLE_FIELD_CHECKS` 同步加一项 shape 校验；contract_version 语义兼容——旧 handle 缺字段 → 校验面放行 or 升 PUBLIC_CONTRACT_VERSION，二选一 owner 定）。
2. launch 组装点（L312-316）填 `getattr(child, "session_id", None)`。
3. 效果: 插件 G3-v2 = lost/UNKNOWN 时直接读盘上子 transcript（`profiles/<p>/supercompress_store/<child_session_id>.model.jsonl` 末条 P0 model 段 = findings 全文），**不依赖 filemem 落盘、不依赖宿主注册表**——三重兜底闭环（注册表 → 落盘 hydrate → transcript 直读 → filemem 检索键）。

## G1（P1）— 子 agent 工具面 filemem parity

**根因定位（比子专家报告更精确一层）**:
- `tools/tool_search.py:58/147`: **deferred 工具目录 = 静态配置**——`_DEFAULT_DEFERRED_TOOLS = frozenset(DEFAULT_CONFIG["tools"]["tool_search"]["defer"])`，agent 未设 `defer_tools` 时全进程统一。子专家看到的 28 件 deferred（kbs 9 / code_map 4 / desktop_ui 8 / …）就是这个静态清单。filemem_* 对主 agent 是**一等工具**（不在 defer 清单里 → 不出现在 tool_search 面）→ 子专家 tool_search 查不到，属设计形态而非遗漏。
- 子工具面真继承链: `tools/delegate_tool_toolsets.py::_resolve_child_toolsets`（L77+, 语义 L80-85 docstring）: 显式 toolsets 与父复合展开集求交；未显式 → 继承父 enabled 集；`DELEGATE_BLOCKED_TOOLS`（L14-22: delegate_task/clarify/memory/send_message/cronjob_manage）**不含 filemem**。
- 活体形态（本宿主 2026-11-18）: 主 agent `enabled_toolsets=None`（= all tools，L86-88 分支）→ 走 `parent_agent.valid_tool_names` 反查 toolset（L90-94）——**插件注册的文件工具若 toolset 归属缺失/不进 valid_tool_names 反查面，即从子继承面消失**；而 filemem 插件的 prompt 段（路由规矩）按 profile 配置照常注入子会话 → 出现"prompt 说用 filemem_*，工具面没有"的矛盾态（子专家正确判"不可执行"并落盘备份）。

**探针（需 owner 定实施面，三选一）**:
- (a) 配置面: 宿主 `tools/tool_search/defer` 静态清单不动，把 filemem 工具面显式挂进子 toolset 继承（`_resolve_child_toolsets` 的 parent 反查面补插件 tool 归属）——最小改动，但依赖"插件工具 toolset 归属"这一宿主侧语义确认；
- (b) 清单面: filemem 族进 `defer` 静态清单（tool_search 可查）——子专家 tool_search 即达，无需改继承链；但主 agent 面也变 deferred（主面行为变化，需 owner 裁决）；
- (c) launch 面: intent-router 的 `ctx.subagent_lifecycle().launch(request)` 请求带 `allowed_toolsets`（宿主 L463-472 已有 toolset 白名单 + "不得拓宽父权限" 校验）——插件侧可传，但白名单内容仍取决于 (a)/(b) 的宿主语义。
- 探针第一步（只读）: 确认 filemem 插件注册工具的 toolset 归属（`tools/registry` 注册面 + 插件 manifest 的 toolsets 声明）与 `model_tools.get_toolset_for_tool` 对 filemem_* 的返回值——两者都空 = 根因坐实（工具无 toolset 归属 → 不进父反查面 → 子继承面消失）。

## 实施顺序建议（owner 裁决）

G2a（落盘+hydrate，重启可恢复）> G2b（映射字段，transcript 直读）> G1 探针 (a)（工具面 parity）。
三者皆为宿主改动（红线内只读勘察完成）；插件侧 G3（已交付 78e0389）对 G2a/G2b 零耦合——宿主任一落地，插件自动受益或叠加，不冲突。
