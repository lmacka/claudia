"""
Anthropic SDK wrapper with prompt caching.

Single user, single model (Sonnet 4.6). No cost governor — billing is visible
in the Anthropic console; this app trusts the user to notice runaway spend.
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic
import structlog

from app.context import SystemPromptBlocks

log = structlog.get_logger()

SONNET = "claude-sonnet-4-6"

# Pricing table kept for the report cost estimate (USD per 1M tokens, Apr 2026).
PRICING: dict[str, dict[str, float]] = {
    SONNET: {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    def cost_usd(self, model: str) -> float:
        p = PRICING.get(model, PRICING[SONNET])
        return (
            (self.input_tokens / 1_000_000) * p["input"]
            + (self.output_tokens / 1_000_000) * p["output"]
            + (self.cache_read_tokens / 1_000_000) * p["cache_read"]
            + (self.cache_write_tokens / 1_000_000) * p["cache_write"]
        )


@dataclass
class Reply:
    text: str
    usage: Usage
    model: str
    stop_reason: str | None


class ClaudeClient:
    def __init__(self, api_key: str) -> None:
        self._c = anthropic.Anthropic(api_key=api_key)

    @staticmethod
    def _build_system(blocks: SystemPromptBlocks) -> list[dict]:
        out: list[dict] = []
        if blocks.block1:
            out.append({"type": "text", "text": blocks.block1, "cache_control": {"type": "ephemeral"}})
        if blocks.block2:
            out.append({"type": "text", "text": blocks.block2, "cache_control": {"type": "ephemeral"}})
        if blocks.block3:
            out.append({"type": "text", "text": blocks.block3})
        return out

    def single_turn(
        self,
        model: str,
        user_content: list[dict],
        system: str | None = None,
        max_tokens: int = 2048,
    ) -> Reply:
        """
        One-shot call with arbitrary user content blocks (text or image).
        No history, no caching. Used by the library extractor for vision OCR
        and verification spot-checks.
        """
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_content}],
        }
        if system:
            kwargs["system"] = system
        raw = self._c.messages.create(**kwargs)
        u = raw.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        text_parts = [b.text for b in raw.content if hasattr(b, "text")]
        text = "\n\n".join(text_parts).strip()
        log.info(
            "claude.single_turn",
            model=model,
            in_tokens=usage.input_tokens,
            out_tokens=usage.output_tokens,
        )
        return Reply(text=text, usage=usage, model=model, stop_reason=raw.stop_reason)

    def reply(
        self,
        model: str,
        blocks: SystemPromptBlocks,
        history: list[dict],
        max_tokens: int = 2048,
    ) -> Reply:
        raw = self._c.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=self._build_system(blocks),
            messages=history,
        )
        u = raw.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        text_parts = [b.text for b in raw.content if hasattr(b, "text")]
        text = "\n\n".join(text_parts).strip()
        log.info(
            "claude.reply",
            model=model,
            in_tokens=usage.input_tokens,
            out_tokens=usage.output_tokens,
            cache_read=usage.cache_read_tokens,
        )
        return Reply(text=text, usage=usage, model=model, stop_reason=raw.stop_reason)
