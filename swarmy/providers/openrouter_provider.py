"""Non-Anthropic models via OpenRouter's chat-completions endpoint (raw HTTP with httpx)."""

from __future__ import annotations

import json
import os
import time
import uuid

import httpx

from ..catalog import ModelSpec
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
    with_retries,
)

FINISH_MAP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}
EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


class OpenRouterConversation(Conversation):
    def __init__(self, system: str, tools: list[ToolSpec]):
        super().__init__(system, tools)
        self.tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools
        ]
        self.messages: list[dict] = [{"role": "system", "content": system}]

    def add_user(self, text: str) -> None:
        self.chars += len(text)
        last = self.messages[-1]
        if last["role"] == "user" and isinstance(last.get("content"), str):
            last["content"] += "\n\n" + text
        else:
            self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            self.chars += len(r.content)
            content = ("ERROR: " + r.content) if r.is_error else r.content
            self.messages.append({"role": "tool", "tool_call_id": r.call_id, "content": content})

    def fork(self) -> "OpenRouterConversation":
        new = OpenRouterConversation.__new__(OpenRouterConversation)
        new.system, new.tool_specs, new.tools, new.chars = self.system, self.tool_specs, self.tools, self.chars
        new.messages = [dict(m) for m in self.messages]
        answered = {m.get("tool_call_id") for m in new.messages if m.get("role") == "tool"}
        for m in reversed(new.messages):
            if m.get("role") == "assistant":
                pending = [tc["id"] for tc in m.get("tool_calls") or [] if tc["id"] not in answered]
                if pending:
                    new.add_tool_results([ToolResult(i, PENDING_RESULT) for i in pending])
                break
        return new

    def add_assistant(self, message: dict) -> None:
        self.chars += len(json.dumps(message))
        self.messages.append(message)


class OpenRouterProvider(Provider):
    name = "openrouter"

    def __init__(self, retry: RetryPolicy | None = None, api_key: str | None = None):
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise FatalProviderError("OPENROUTER_API_KEY is not set (put it in .env).")
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(600, connect=30),
            headers={
                "Authorization": f"Bearer {key}",
                "X-Title": "swarmy",
            },
        )
        self.retry = retry or RetryPolicy()
        self.url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"

    def new_conversation(self, system: str, tools: list[ToolSpec]) -> OpenRouterConversation:
        return OpenRouterConversation(system, tools)

    async def step(self, conv, spec: ModelSpec, *, max_tokens: int, effort: str | None,
                   tool_choice: str | None = None) -> StepResult:
        assert isinstance(conv, OpenRouterConversation)
        body: dict = {
            "model": spec.model,
            "messages": conv.messages,
            "max_tokens": max_tokens,
            "usage": {"include": True},
        }
        if conv.tools:
            body["tools"] = conv.tools
            body["tool_choice"] = tool_choice or "auto"
            # Only route to upstream endpoints that actually support tool calling.
            body["provider"] = {"require_parameters": True}
        if effort and spec.supports_reasoning is not False:
            body["reasoning"] = {"effort": EFFORT_MAP.get(effort, "medium")}

        async def call():
            try:
                resp = await self.client.post(self.url, json=body)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                raise Retryable(f"network: {e}") from e
            if resp.status_code != 200:
                _raise_for_status(resp)
            data = resp.json()
            if "error" in data and not data.get("choices"):
                err = data["error"]
                code = err.get("code") if isinstance(err, dict) else None
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                if code in (429, 502, 503, 408) or (isinstance(code, int) and code >= 500):
                    raise Retryable(f"upstream {code}: {msg}")
                raise ProviderError(f"OpenRouter error {code}: {msg}")
            choice = (data.get("choices") or [None])[0]
            if choice is None:
                raise Retryable("empty choices")
            if choice.get("finish_reason") == "error" or choice.get("error"):
                raise Retryable(f"upstream error: {choice.get('error')}")
            return data

        t0 = time.monotonic()
        outcome = await with_retries(call, self.retry)
        latency = time.monotonic() - t0 - outcome.wait_s
        data = outcome.value
        choice = data["choices"][0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # some upstreams return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))

        calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls") or []
        for tc in raw_calls:
            if not tc.get("id"):
                tc["id"] = f"call_{uuid.uuid4().hex[:12]}"
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
                calls.append(ToolCall(id=tc["id"], name=fn.get("name", ""), arguments=args))
            except (ValueError, TypeError) as e:
                calls.append(ToolCall(id=tc["id"], name=fn.get("name", ""), arguments={}, error=f"invalid JSON arguments: {e}"))

        assistant = {"role": "assistant", "content": text}
        if raw_calls:
            assistant["tool_calls"] = raw_calls
        if message.get("reasoning_details"):
            # Reasoning models need their reasoning replayed across tool-use turns.
            assistant["reasoning_details"] = message["reasoning_details"]
        conv.add_assistant(assistant)

        u = data.get("usage") or {}
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        usage = Usage(
            input_tokens=max(0, (u.get("prompt_tokens") or 0) - cached),
            output_tokens=u.get("completion_tokens") or 0,
            cache_read_tokens=cached,
            reasoning_tokens=((u.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0,
            reported_cost=u.get("cost"),
        )
        stop = FINISH_MAP.get(choice.get("finish_reason") or "", "other")
        if calls and stop != "max_tokens":
            stop = "tool_use"
        return StepResult(
            text=text.strip(),
            tool_calls=calls,
            usage=usage,
            stop_reason=stop,
            latency_s=latency,
            rate_wait_s=outcome.wait_s,
            retries=outcome.retries,
            served_model=data.get("model", spec.model),
            thinking=message.get("reasoning") or "",
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def _raise_for_status(resp: httpx.Response) -> None:
    code = resp.status_code
    try:
        payload = resp.json()
        msg = (payload.get("error") or {}).get("message", resp.text)
    except Exception:
        msg = resp.text
    msg = str(msg)[:500]
    retry_after = None
    try:
        retry_after = float(resp.headers.get("retry-after"))
    except (TypeError, ValueError):
        pass
    if code == 429:
        low = msg.lower()
        if "per-day" in low or "per day" in low or "daily" in low:
            raise FatalProviderError(f"OpenRouter daily free-model quota exhausted: {msg}")
        raise Retryable(f"429: {msg}", retry_after)
    if code in (401, 403):
        raise FatalProviderError(f"OpenRouter auth error {code}: {msg}")
    if code == 402:
        raise FatalProviderError(f"OpenRouter: insufficient credits: {msg}")
    if code == 404:
        raise FatalProviderError(f"OpenRouter model/endpoint not found: {msg}")
    if code in (408, 409) or code >= 500:
        raise Retryable(f"HTTP {code}: {msg}", retry_after)
    raise ProviderError(f"OpenRouter HTTP {code}: {msg}")
