"""
engine.agents — generic bounded agent runtime (R2.2).

Extracted from the monolithic AgenticEvaluator (engine/evaluator.py):
iteration/wall-clock/token caps, tool-call weighting, context compaction,
tool dedup, bounded file reads, sandbox scratch state, LLM tool calling.

Concrete agents (R2.3+) subclass or compose ``AgentRunner`` with their own
prompts, tools, and output schema.
"""

from engine.agents.budget import Budget
from engine.agents.runner import AgentRunError, AgentRunner, BudgetExceededError
from engine.agents.schemas import SchemaError, parse_response, schema_to_prompt
from engine.agents.tools import (
    Tool,
    ToolRegistry,
    ToolResult,
    is_data_file_path,
    make_read_file_tool,
    read_file_bounded,
    sandbox_tools,
)

__all__ = [
    "AgentRunner",
    "AgentRunError",
    "BudgetExceededError",
    "Budget",
    "Tool",
    "ToolResult",
    "ToolRegistry",
    "read_file_bounded",
    "make_read_file_tool",
    "sandbox_tools",
    "is_data_file_path",
    "schema_to_prompt",
    "parse_response",
    "SchemaError",
]
