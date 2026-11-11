"""Claude via the official Anthropic SDK (streaming, adaptive thinking, effort, prompt caching)."""

from __future__ import annotations

import json
import os
import time

import anthropic

from ..catalog import Endpoint, ModelSpec
from .base import (
    PENDING_RESULT,
    Conversation,
    FatalProviderError,
    Provider,
    ProviderError,
    RetryPolicy,
    Retryable,
    StepResult,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    clip_args,
    clip_text,
    with_retries,
)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
STOP_MAP = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal"}


THINKING = ("thinking", "redacted_thinking")


def _chars(block) -> int:
    try:
        return len(json.dumps(block.model_dump(exclude={"signature"}), default=str))
    except Exception:
        return 500


class AnthropicConversation(Conversation):
    """Native Messages-API history. Append-only between trims: assistant content is replayed
    unchanged (thinking blocks included), which keeps prompt caching and preserved thinking valid.
    Claude signs each thinking block against everything before it, so a trim, which edits earlier
    turns, also drops every thinking block (the documented way to edit a history)."""

    def __init__(self, system: str, tools: list[ToolSpec]):
        super().__init__(system, tools)
        self.tools = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
        ]
        self.messages: list[dict] = []

    def _append_user_blocks(self, blocks: list[dict]) -> None:
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["content"].extend(blocks)
        else:
            self.messages.append({"role": "user", "content": blocks})

    def add_user(self, text: str) -> None:
        self.chars += len(text)
        self._append_user_blocks([{"type": "text", "text": text}])

    def add_tool_results(self, results: list[ToolResult]) -> None:
        blocks = []
        for r in results:
            self.chars += len(r.content)
            block = {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content}
            if r.is_error:
                block["is_error"] = True
            blocks.append(block)
        self._append_user_blocks(blocks)

    def fork(self) -> "AnthropicConversation":
        new = AnthropicConversation.__new__(AnthropicConversation)
        new.system, new.tool_specs, new.tools, new.chars = self.system, self.tool_specs, self.tools, self.chars
        new.messages = [{"role": m["role"], "content": list(m["content"])} for m in self.messages]
        if new.messages and new.messages[-1]["role"] == "assistant":
            pending = [b.id for b in new.messages[-1]["content"] if getattr(b, "type", "") == "tool_use"]
            if pending:
                new.add_tool_results([ToolResult(i, PENDING_RESULT) for i in pending])
        return new

    def trim(self, keep_turns: int, keep_chars: int) -> int:
        turns = [i for i, m in enumerate(self.messages) if m["role"] == "assistant"]
        if len(turns) <= keep_turns:
            return 0
        boundary = turns[-keep_turns]
        saved = 0
        for i in range(1, boundary):  # messages[0] is the order
            m = self.messages[i]
            blocks = []
            for b in m["content"]:
                if isinstance(b, dict):
                    if b.get("type") == "tool_result" and isinstance(b.get("content"), str):
                        new = clip_text(b["content"], keep_chars)
                        saved += len(b["content"]) - len(new)
                        b = {**b, "content": new}
                    elif b.get("type") == "text":
                        new = clip_text(b.get("text", ""), keep_chars)
                        saved += len(b.get("text", "")) - len(new)
                        b = {**b, "text": new}
                else:  # SDK blocks from the model; thinking blocks are signed and stay as they are
                    kind = getattr(b, "type", "")
                    if kind == "text":
                        new = clip_text(b.text, keep_chars)
                        if new != b.text:
                            saved += len(b.text) - len(new)
                            b = b.model_copy(update={"text": new})
                    elif kind == "tool_use" and isinstance(getattr(b, "input", None), dict):
                        new = clip_args(b.input, keep_chars)
                        if new != b.input:
                            saved += len(json.dumps(b.input, default=str)) - len(json.dumps(new, default=str))
                            b = b.model_copy(update={"input": new})
                blocks.append(b)
            m["content"] = blocks
        if saved:
            for m in self.messages:
                if m["role"] == "assistant":
                    kept = [b for b in m["content"] if getattr(b, "type", "") not in THINKING]
                    saved += sum(_chars(b) for b in m["content"] if getattr(b, "type", "") in THINKING)
                    m["content"] = kept or [{"type": "text", "text": "(reasoning trimmed)"}]
        self.chars = max(0, self.chars - saved)
        return saved

    def add_assistant(self, content: list) -> None:
        self.chars += sum(_chars(b) for b in content)
        self.messages.append({"role": "assistant", "content": content})


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, endpoint: Endpoint | None = None, retry: RetryPolicy | None = None):
        # Retries are handled here (not by the SDK) so that time spent waiting on
        # rate limits can be measured separately from model latency.
        kwargs: dict = {"max_retries": 0, "timeout": endpoint.timeout_s if endpoint else 900}
        if endpoint is not None:
            if endpoint.base_url:
                kwargs["base_url"] = endpoint.base_url
            if endpoint.api_key_env and os.environ.get(endpoint.api_key_env):
                kwargs["api_key"] = os.environ[endpoint.api_key_env]
            if endpoint.headers:
                kwargs["default_headers"] = endpoint.headers
        try:
            self.client = anthropic.AsyncAnthropic(**kwargs)
        except Exception as e:  # e.g. no credentials found anywhere
            raise FatalProviderError(f"cannot create Anthropic client for endpoint "
                                     f"{endpoint.name if endpoint else 'anthropic'!r}: {e}") from e
        self.retry = retry or RetryPolicy()

    def new_conversation(self, system: str, tools: list[ToolSpec]) -> AnthropicConversation:
        return AnthropicConversation(system, tools)

    async def step(self, conv, spec: ModelSpec, *, max_tokens: int, effort: str | None,
                   tool_choice: str | None = None) -> StepResult:
        assert isinstance(conv, AnthropicConversation)
        params: dict = {
            "model": spec.model,
            "max_tokens": max_tokens,
            "system": conv.system,
            "messages": conv.messages,
            # Automatic caching: the breakpoint moves to the newest block each turn, so
            # every step re-reads the agent's history from cache.
            "cache_control": {"type": "ephemeral"},
        }
        if conv.tools:
            params["tools"] = conv.tools
            if tool_choice == "none":
                params["tool_choice"] = {"type": "none"}
        if spec.thinking == "adaptive":
            params["thinking"] = {"type": "adaptive", "display": spec.thinking_display}
        effort = spec.map_effort(effort)
        if effort:
            params["output_config"] = {"effort": effort}
        if spec.refusal_fallback:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"

        async def call():
            try:
                async with self.client.beta.messages.stream(**params) as stream:
                    return await stream.get_final_message()
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                raise FatalProviderError(f"Anthropic auth error: {e}") from e
            except anthropic.NotFoundError as e:
                raise FatalProviderError(f"Anthropic model not found ({spec.model}): {e}") from e
            except anthropic.RateLimitError as e:
                raise Retryable("429 rate limited", _retry_after(e)) from e
            except (anthropic.InternalServerError, anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
                raise Retryable(f"{type(e).__name__}: {e}") from e
            except anthropic.APIStatusError as e:
                if e.status_code in (408, 409, 429, 529) or e.status_code >= 500:
                    raise Retryable(f"HTTP {e.status_code}", _retry_after(e)) from e
                raise ProviderError(f"Anthropic HTTP {e.status_code}: {e.message}") from e

        t0 = time.monotonic()
        outcome = await with_retries(call, self.retry)
        latency = time.monotonic() - t0 - outcome.wait_s
        msg = outcome.value

        text_parts, thinking_parts, calls, fallback = [], [], [], False
        for block in msg.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "thinking":
                if getattr(block, "thinking", ""):
                    thinking_parts.append(block.thinking)
            elif btype == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
            elif btype == "fallback":
                fallback = True
        conv.add_assistant(list(msg.content))

        u = msg.usage
        details = getattr(u, "output_tokens_details", None)
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            reasoning_tokens=(getattr(details, "thinking_tokens", None) or getattr(details, "reasoning_tokens", None) or 0)
            if details else 0,
        )
        iterations = getattr(u, "iterations", None) or []
        if any(getattr(it, "type", "") == "fallback_message" for it in iterations):
            fallback = True
        return StepResult(
            text="\n".join(text_parts).strip(),
            tool_calls=calls,
            usage=usage,
            stop_reason=STOP_MAP.get(msg.stop_reason or "", "other"),
            latency_s=latency,
            rate_wait_s=outcome.wait_s,
            retries=outcome.retries,
            retry_errors=outcome.errors,
            served_model=getattr(msg, "model", spec.model) or spec.model,
            thinking="\n".join(thinking_parts),
            fallback=fallback or (getattr(msg, "model", spec.model) not in (spec.model, None)),
        )

    async def aclose(self) -> None:
        await self.client.close()


def _retry_after(e: anthropic.APIStatusError) -> float | None:
    try:
        return float(e.response.headers.get("retry-after"))
    except (TypeError, ValueError, AttributeError):
        return None
