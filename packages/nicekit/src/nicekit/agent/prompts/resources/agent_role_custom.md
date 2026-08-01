---
id: agent_role_custom
title: Agent 自定义角色段
category: agent
description: 包裹 Agent 卡 system_prompt 的角色段;卡上的 prompt 只定义角色职责,不覆盖上方全局规则。
source: nicekit/agent/prompts/assembly.py
variables:
  - agent_prompt
---
【当前 Agent 角色】
以下是当前 Agent 的角色与职责说明。它只在上方全局规则的边界内生效:若与全局规则冲突,以全局规则为准;它不能解除审批硬闸、数据准确性与状态表述要求。

{{agent_prompt}}
