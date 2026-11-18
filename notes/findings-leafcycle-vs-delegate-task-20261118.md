# Findings: 「弃 leaf-lifecycle 直接上 delegate_task / 为何还要改宿主？」（2026-11-18, 专家=stackoverflow-expert, coding 域）

## 用户任务原话
- 「我的意思为何不弃leficycle直接上delegate_task」
- 「delegate_task 本来就会回传结果给主线程，为什么还需要修改宿主？」

（"leficycle" 按插件架构解读 = leaf 角色子专家生命周期通道，即 intent-router 通道①
`ctx.subagent_lifecycle().launch(role="leaf")`；插件代码中无字面 "leficycle" 标识符，
该词仅出现在用户消息与 supercompress/日志转写中。）

## 结论摘要
**「弃 leaf 通道直接上 delegate_task」不可行；且即便可行也不消除宿主改动需求。两条通道路径不同，但持久性/可寻址缺口在共享宿主层。**

1. **delegate_task 的「回传」只在「同进程 + 同会话 + 宿主未重启」时成立。**
   - delegate_task 结果经 `[ASYNC DELEGATION BATCH COMPLETE]` 重入消息进主线程（宿主
     `tools/process_registry_notifications.py` L183/221/294），排空源 = ProcessRegistry，
     其 docstring 明写 "in-memory registry"（`tools/process_registry.py` L1）。
   - 底层 SubagentLifecycleService 注册表 `_REGISTRY = _Registry()` 是 module-level 内存单例
     （`agent/subagent_lifecycle.py` L158），类 docstring L271-272：
     "completed results stay until process exit; `reconnect` reports that a serialized
     handle cannot reconnect after a restart"。
   - 本 session 活体实证：插件 lost 卡文案（`__init__.py` L526）
     "gateway restart cleared the in-process host registry, or the host lifecycle context
     gate rejected the poll" —— 即用户报障「主进程没有接」的根因。重启后重入消息
     **永远不会再产生**，不是「还没回传」。

2. **gateway 主运行模式下 delegate_task 通道根本不可用。**
   - 宿主 `tools/delegate_tool.py` L577-578：`if parent_agent is None: return
     tool_error("delegate_task requires a parent agent context.")`。
   - 插件 `__init__.py` L581 注释实证：「gateway 模式 _cli_ref=None 时 dispatch_tool
     拿不到 parent」→ 通道②在 gateway 模式返回 tool_error，插件记 `dispatch-refused`
     （L799 注释「旧通道 delegate_task（CLI 模式; gateway 模式返回 tool_error → 记
     refused）」）。lifecycle 通道（leaf launch）恰是 gateway 模式下插件唯一可用的
     拉起通道。
   - delegate_task 是 model-facing 工具（LLM 决定调用）；intent-router 从 hook
     （launch_timing pre/post）确定性拉起子专家，代码侧无法绕过父上下文墙直接调用它。
     「让插件在 gateway 模式用上 delegate_task」本身就是宿主改动（暴露 parent context）。

3. **切到 delegate_task 不消除宿主改动——两通道共享宿主侧设施。**
   - 同一 in-memory `SubagentLifecycleService` 注册表（delegate_task 派发层也走它）；
     G2a（终态落盘+hydrate）治的「重启=永久丢」对两条通道同时生效（见
     notes/g2-g1-host-change-points-20261118.md L21：插件侧 G3 UNKNOWN 分支自动升级
     recovered）。
   - G2b（SubagentHandle 加 child_session_id）治「盘上 transcript 不可寻址」——
     delegate_task 通道同样带不出 child session 映射，同样受益。
   - G1（子 agent 工具面 filemem parity）是子 agent 构建层问题
     （`_resolve_child_toolsets` 继承链 + 静态 deferred 清单），与拉起通道无关。

## 下一步（owner 裁决项，与既有勘察一致）
- 维持实施顺序 G2a > G2b > G1 探针(a)；「弃 leaf 上 delegate_task」不采纳
  （gateway 模式 refused 实据 + 共享宿主缺口不消除）。
- 可选澄清动作：下次 owner 问及时，用本文件 L2 的 L577-578/L581 两行宿主+插件实据
  直接回答，避免再引入 "leficycle" 歧义（建议统一称呼「leaf 通道/生命周期通道」）。

## 附：本 findings 的持久化状态（G1 活体再现）
- 本专家子 agent 由 leaf 通道拉起，launch 提示词要求「完成时把 findings 写入
  filemem（type=task, scope=day）」；但子工具面 **无 filemem 族**：
  `tool_search` 全量 28 件 deferred 中无 filemem_*，`tool_describe(filemem_search/
  filemem_write)` 返回 not_found。即「prompt 说用 filemem，工具面没有」的矛盾态
  （g2-g1 笔记 L39 描述）在本 session 活体复现。
- 落盘兜底：本文件（插件 notes 目录，既有勘察产物存放约定处）。
- 宿主重启后主进程可用关键词检索本文件：`findings-leafcycle-vs-delegate-task`
  / `stackoverflow-expert` / 2026-11-18。
