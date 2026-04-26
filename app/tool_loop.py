"""
Tool-use loop: wrap Claude's multi-step tool_use responses into a single
logical "turn" the UI sees. No cost cap — caps are removed in the simplified
build. Iteration cap remains as a runaway-tool-loop guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import anthropic
import structlog

from app.claude import ClaudeClient, Usage
from app.context import SystemPromptBlocks
from app.tools.registry import ToolError, ToolRegistry

log = structlog.get_logger()


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict
    result_summary: str


@dataclass
class TurnRecord:
    role: str
    blocks: list[dict]


@dataclass
class LoopResult:
    text: str
    usage: Usage
    model: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    iterations: int = 0
    stop_reason: str | None = None


def run_tool_loop(
    claude: ClaudeClient,
    model: str,
    blocks: SystemPromptBlocks,
    history: list[dict],
    tools: ToolRegistry | None,
    max_iterations: int = 8,
    max_tokens_per_call: int = 2048,
) -> LoopResult:
    turn_usage = Usage()
    records: list[ToolCallRecord] = []
    loop_turns: list[TurnRecord] = []
    final_text = ""
    stop_reason: str | None = None
    tools_payload = tools.as_anthropic_tools() if tools else []
    system_payload = ClaudeClient._build_system(blocks)  # noqa: SLF001

    for iteration in range(1, max_iterations + 1):
        raw = claude._c.messages.create(  # noqa: SLF001
            model=model,
            max_tokens=max_tokens_per_call,
            system=system_payload,
            messages=history,
            tools=tools_payload or anthropic.NOT_GIVEN,
        )
        u = raw.usage
        turn_usage.add(
            Usage(
                input_tokens=getattr(u, "input_tokens", 0) or 0,
                output_tokens=getattr(u, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            )
        )
        stop_reason = raw.stop_reason

        text_parts: list[str] = []
        tool_uses: list = []
        for block in raw.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_uses.append(block)
        if text_parts:
            final_text = "\n\n".join(text_parts).strip()

        if stop_reason != "tool_use" or not tool_uses:
            loop_turns.append(TurnRecord(role="assistant", blocks=_blocks_to_dicts(raw.content)))
            return LoopResult(
                text=final_text,
                usage=turn_usage,
                model=model,
                tool_calls=records,
                turns=loop_turns,
                iterations=iteration,
                stop_reason=stop_reason,
            )

        assistant_blocks = _blocks_to_dicts(raw.content)
        history.append({"role": "assistant", "content": assistant_blocks})
        loop_turns.append(TurnRecord(role="assistant", blocks=assistant_blocks))

        tool_results_blocks: list[dict] = []
        for tu in tool_uses:
            name = tu.name
            args = tu.input or {}
            tool_use_id = tu.id
            try:
                if tools is None:
                    raise ToolError("tools are not wired in this session")
                result = tools.invoke(name, args)
                if isinstance(result, list):
                    content_blocks = result
                    summary = f"(structured: {len(result)} blocks)"
                elif isinstance(result, str):
                    content_blocks = [{"type": "text", "text": result}]
                    summary = result[:120].replace("\n", " ")
                else:
                    content_blocks = [{"type": "text", "text": str(result)}]
                    summary = str(result)[:120]
                tool_results_blocks.append(
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": content_blocks}
                )
                records.append(ToolCallRecord(name=name, arguments=dict(args), result_summary=summary))
            except ToolError as e:
                tool_results_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": [{"type": "text", "text": f"Error: {e}"}],
                        "is_error": True,
                    }
                )
                records.append(ToolCallRecord(name=name, arguments=dict(args), result_summary=f"ERROR: {e}"))

        history.append({"role": "user", "content": tool_results_blocks})
        loop_turns.append(TurnRecord(role="user", blocks=tool_results_blocks))

    log.warning("tool_loop.max_iterations_reached", max_iterations=max_iterations)
    return LoopResult(
        text=final_text or "(no final response — tool loop max iterations reached)",
        usage=turn_usage,
        model=model,
        tool_calls=records,
        turns=loop_turns,
        iterations=max_iterations,
        stop_reason="max_iterations",
    )


def _blocks_to_dicts(content: list) -> list[dict]:
    out: list[dict] = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": block.text})
        elif btype == "tool_use":
            out.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input or {}})
        elif btype == "thinking":
            out.append({"type": "thinking", "thinking": getattr(block, "thinking", "")})
    return out
