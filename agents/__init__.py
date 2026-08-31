"""
ReliefAI Agent package.

The orchestrator is a tool-using agent: it selects and calls tools from the MCP
tool server rather than dispatching to a fixed roster of prompt agents.

The remaining specialist agents own genuine LLM domain logic (multilingual
translation, narrative report writing, supply assessment, shift-gap filling).
They each back a dedicated API route, and the two that the orchestrator needs
are exposed to it as MCP tools — there is no separate copy of their logic.
"""

from agents.orchestrator import run_orchestrator
from agents.supply_agent import run_supply_agent
from agents.comms_agent import run_comms_agent
from agents.shift_agent import run_shift_agent
from agents.report_agent import run_report_agent

__all__ = [
    "run_orchestrator",
    "run_supply_agent",
    "run_comms_agent",
    "run_shift_agent",
    "run_report_agent",
]
