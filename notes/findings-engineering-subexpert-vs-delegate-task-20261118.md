# Findings: 「intent-router 子专家是否设计过度？/ 只指示模型调 delegate_task 是否即正确做法？」（2026-11-18, 专家=engineering-backend-architect, engineering 域）

## 用户任务原话
- 「我觉得这个intent-router 子专家功能有点设计过度，我认为只需要给模型指示让模型拉起子专家，也就是让模型调用 delegate_task 的方式才是正确做法」
- 前序语境：「delegate_task 本来就会回传结果给主线程，为什么还需要修改宿主？」

## 数据实证（logs/dispatches.jsonl，185 事件，2026-11-17~18 约 21h 窗口；宿主源码复核 2026-11-18）
1. **lifecycle 通道 45 次真 launch → 0 次 recovered / 0 次 reentered，15 次 lost（33%）**；G3 filemem 捞回仅命中 2 次（因 G1 缺口：子 agent 工具面无 filemem 族，launch 提示词要求写 filemem 不可执行）。
   → 子专家流水线在窗口内**没有一次记录在案的成果回收**；v2.4.3~v2.8 的冻结 contextvar / 台账 / 捞回机制的实际交付价值 ≈ 0。
2. **delegate_task 旧通道 13 次全部 refused**，随后 13 次 `dispatch-fallback-injection`（专家全文注入主 session，= v1 行为）。
   → gateway 桌面模式下，今天**实际扛活的是降级注入**，不是子专家通道。
3. P0 平台守卫处理 71 条子 session 噪声回合（subagent-guard 20 + anchor-disabled 51）。

## 宿主侧结构墙（自行复核，非转引）
- `tools/delegate_tool.py` L577-578：`if parent_agent is None: return tool_error("delegate_task requires a parent agent context.")`——gateway 模式 `PluginManager._cli_ref=None`，**模型调 delegate_task 也会拿到 tool_error**：「指示模型调 delegate_task」在桌面模式下对模型本身同样不可用（不是绕过插件就能绕过的）。
- `tools/process_registry.py` docstring：「in-memory registry」；`agent/subagent_lifecycle.py` 类 docstring：「completed results stay until process exit; reconnect reports that a serialized handle cannot reconnect after a restart」。
   → 对「delegate_task 本来就会回传结果给主线程」的纠偏：**回传 = 同进程内存重入消息；宿主重启后永远不会再产生**（15 条 lost 事件即活体后果）。delegate_task 与 lifecycle 两通道共享这一持久性缺口。

## 结论（engineering 域 findings）
1. **用户「设计过度」判断：实证支持（数据面）。** 1321 行插件 + 112 测试，但窗口内子专家通道 0 回收 / 15 lost / 13 全降级注入——复杂度远大于交付。过度部分集中在 lifecycle 通道的可靠性补丁（冻结快照、UNKNOWN×3、台账 L1-L3、G3 捞回），它们的根因是**宿主层缺口在插件层打补丁**。
2. **用户「只指示模型调 delegate_task 即正确做法」：分模式裁决，不能一刀切。**
   - **CLI 模式（parent context 在场）：同意，且是最小正确设计。** 静态指示块（domain 任务 → delegate_task，launch context 含：读 experts/<slug>.md + filemem/kbs 自取 + 成果写 filemem(type=task,scope=day) + 只交付 findings 不 git + 不重复主进程活）可完整替代 F2/F3 确定性注入；可退役：通道①、冻结 contextvar、UNKNOWN×3、台账 L1-L3、launch_timing、subagent-guard 大部分、question gate（模型侧自然不派问句）。保留：锚点注入 + 压缩蒸馏卡补注（v2.1，与派发无关）+ /expert + G3 薄捞回。
   - **gateway/桌面模式：不同意该方案可独立成立。** delegate_task 有结构墙（L577），模型也调不动；且即便调得动，结果重启即灭（15 lost 实证）。gateway 下只有两条路：(a) 保留 lifecycle 通道但**先修宿主 G2a（终态落盘+hydrate，1 函数）+ G2b（child_session_id 映射）**，之后插件可大幅瘦身（丢冻结快照/UNKNOWN 重试/台账，留 launch+poll+G3）；(b) 放弃 gateway 子专家，接受现状降级注入（= v1 行为，数据证明它是当前实际工作路径，代价：主 session 上下文膨胀 + 每域全文注入 token）。
3. **纠正前序误区**：「delegate_task 天然回传」≠「结果持久」。选 delegate_task 简化方案时，成果持久性要靠 launch 指示里的 filemem 落盘——而现行宿主构建下子 agent 无 filemem 工具面（G1），指示不可执行 → 简化方案必须先补 G1（三探针之一）否则成果仍随宿主重启蒸发。

## 下一步（owner 裁决项）
1. 定运行时：主跑 CLI 还是 gateway 桌面？（决定走 2a 还是 2b）
2. CLI → 授权瘦身：退役通道①+台账+冻结快照+launch_timing，注入静态指示块，保留锚点/卡补注//expert/G3；测试面 112 → 预计 ~40。
3. gateway → 宿主 G2a+G2b 落地（只读勘察清单见 notes/g2-g1-host-change-points-20261118.md）后瘦身；或直接接受降级注入为 gateway 主路径。
4. 无论哪条路：补 G1（filemem 工具面进子 agent）——否则「成果写 filemem」指示在子 agent 侧不可执行，持久性承诺落空。

## 附：持久化状态（G1 活体再现 #2）
- 本子 agent 会话 tool_search 全量 28 件 deferred 中无 filemem 族（检索 3 轮确认），launch 提示词「写 filemem(type=task,scope=day)」不可执行。
- 落盘兜底：本文件（插件 notes/，与 stackoverflow-expert 同日产物同一约定）。
- 主进程检索键：`findings-engineering-subexpert-vs-delegate-task` / `engineering-backend-architect` / 2026-11-18。
