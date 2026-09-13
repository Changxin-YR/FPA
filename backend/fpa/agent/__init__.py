"""Agent 侧：工具面、网关、确认闸门。

与 kernel/ 的分工：内核定义"一个能力是什么、怎么受控执行"，本包负责
"把能力暴露给 DeepSeek Harness，并在模型与业务之间做管控"。

对应需求的 AI Agent Gateway 与 Tool Registry 两条。
"""

from __future__ import annotations

from fpa.agent.gateway import AgentToolGateway, ToolSpec, TurnOutcome

__all__ = ["AgentToolGateway", "ToolSpec", "TurnOutcome"]
