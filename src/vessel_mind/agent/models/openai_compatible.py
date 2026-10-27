"""The OpenAI-compatible adapter: POST /chat/completions with stream: true, over httpx.

It serves the free-tier hosted provider (Gemini's OpenAI-compatible endpoint) and
llama-server. No provider SDK.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal, cast

import httpx

from vessel_mind.core.chat import (
    AssistantMessage,
    ChatEvent,
    ChatModelError,
    ChatRequest,
    Stop,
    StopReason,
    TextDelta,
    ToolCall,
    ToolMessage,
    Usage,
    UserMessage,
)
from vessel_mind.core.credentials import Credential

_FINISH_REASONS: dict[str, StopReason] = {
    "stop": "end",
    "length": "max_tokens",
    "tool_calls": "tool_use",
}

# Rate limited, or overloaded: worth asking again after a pause.
_RETRYABLE = {429, 503}

# The request's reasoning_effort, as Gemini's and Ollama's endpoints accept it.
ReasoningEffort = Literal["none", "low", "medium", "high"]


class OpenAICompatibleModel:
    def __init__(
        self,
        *,
        id: str,
        base_url: str,
        credential: Credential | None,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> None:
        self.id = id
        self.reasoning_effort = reasoning_effort
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._sleep = sleep
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._credential = credential
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    def __repr__(self) -> str:  # never shows the key
        return f"OpenAICompatibleModel(id={self.id!r}, url={self._url!r})"

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        headers = {"Accept-Encoding": "identity"}
        if self._credential is not None:
            # Applied last, so no other header can shadow it.
            headers |= self._credential.headers()
        body = self._body(req)
        attempt = 0
        while True:
            # Until the first event reaches the caller, a failed attempt can be
            # retried without the caller seeing anything twice.
            started = False
            try:
                async with self._client.stream(
                    "POST", self._url, json=body, headers=headers
                ) as response:
                    if response.status_code == 200:
                        async for event in self._events(response):
                            started = True
                            yield event
                        return
                    retryable = response.status_code in _RETRYABLE
                    problem = f"HTTP {response.status_code}"
                    retry_after = response.headers.get("retry-after", "")
            except (httpx.TimeoutException, httpx.ConnectError) as error:
                # A first-byte timeout or a refused connection: worth asking again.
                retryable = not started
                problem = (
                    "timed out" if isinstance(error, httpx.TimeoutException) else ("cannot connect")
                )
                retry_after = ""
            except httpx.HTTPError as error:
                raise ChatModelError(f"{self.id}: {type(error).__name__}") from None
            delay = self._retry_delay(retryable, retry_after, attempt)
            if delay is None:
                raise ChatModelError(f"{self.id}: {problem}")
            attempt += 1
            await self._sleep(delay)

    def _retry_delay(self, retryable: bool, retry_after: str, attempt: int) -> float | None:
        """Seconds to wait before the next attempt, or None to give up."""
        if not retryable or attempt >= self._max_retries:
            return None
        delay = self._backoff_base * 2**attempt
        if retry_after.isdigit():
            delay = float(retry_after)
        return delay if delay <= self._backoff_max else None

    async def _events(self, response: httpx.Response) -> AsyncIterator[ChatEvent]:
        finish_reason: str | None = None
        usage = Usage()
        calls = _ToolCallAssembler()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("usage"):
                # Cumulative on every chunk (Gemini): the last one counts, never a sum.
                usage = _usage(chunk["usage"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    yield TextDelta(delta=content)
                for fragment in delta.get("tool_calls") or []:
                    calls.add(fragment)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
        # A call is emitted only when the stream has ended, so its input is complete.
        tool_calls = calls.finish(self.id)
        for call in tool_calls:
            yield call
        # Gemini says "stop" even when it called tools.
        reason = "tool_use" if tool_calls else _FINISH_REASONS.get(finish_reason or "stop", "end")
        yield Stop(reason=reason, usage=usage)

    def _body(self, req: ChatRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        for message in req.messages:
            match message:
                case UserMessage(text=text):
                    messages.append({"role": "user", "content": text})
                case AssistantMessage(text=text, tool_calls=[]):
                    messages.append({"role": "assistant", "content": text})
                case AssistantMessage(text=text, tool_calls=calls):
                    messages.append(
                        {
                            "role": "assistant",
                            "content": text or None,
                            "tool_calls": [_wire_call(call) for call in calls],
                        }
                    )
                case ToolMessage(tool_call_id=call_id, content=content):
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        body: dict[str, Any] = {
            "model": self.id,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in req.tools
            ]
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        return body


def _wire_call(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.input)},
        # Written back unchanged: Gemini refuses a step without its thought signature.
        **(call.provider_data or {}),
    }


class _PendingCall:
    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments: list[str] = []
        # Keys the provider sent beside the call (Gemini: extra_content), kept as they came.
        self.provider_data: dict[str, Any] = {}


# The keys of a tool-call fragment that the adapter reads; any other key is provider data.
_FRAGMENT_KEYS = {"index", "id", "type", "function"}


class _ToolCallAssembler:
    """Joins streamed tool-call fragments into whole calls.

    Two dialects exist. OpenAI and llama-server key each fragment by `index` and
    split the arguments across chunks. Gemini sends no `index` and each call whole.
    So: an `index` selects its call; without one, a new `id` starts a new call, and
    a fragment with neither continues the last call.
    """

    def __init__(self) -> None:
        self._calls: list[_PendingCall] = []
        self._by_index: dict[int, _PendingCall] = {}

    def add(self, fragment: dict[str, Any]) -> None:
        call = self._select(fragment)
        if fragment.get("id") and not call.id:
            call.id = fragment["id"]
        function = fragment.get("function") or {}
        call.name += function.get("name") or ""
        call.arguments.append(function.get("arguments") or "")
        for key, value in fragment.items():
            if key not in _FRAGMENT_KEYS and value is not None:
                call.provider_data[key] = value

    def _select(self, fragment: dict[str, Any]) -> _PendingCall:
        index, call_id = fragment.get("index"), fragment.get("id")
        if index is not None:
            call = self._by_index.get(index)
            if call is None:
                call = self._by_index[index] = self._new()
            return call
        if self._calls and (not call_id or call_id == self._calls[-1].id):
            return self._calls[-1]
        return self._new()

    def _new(self) -> _PendingCall:
        call = _PendingCall()
        self._calls.append(call)
        return call

    def finish(self, model_id: str) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for number, pending in enumerate(self._calls):
            text = "".join(pending.arguments).strip() or "{}"
            try:
                arguments = json.loads(text)
            except json.JSONDecodeError:
                arguments = None
            if not isinstance(arguments, dict):
                raise ChatModelError(
                    f"{model_id}: tool call {pending.name!r} has arguments that are not a JSON object"
                )
            calls.append(
                ToolCall(
                    id=pending.id or f"call_{number}",
                    name=pending.name,
                    input=cast(dict[str, Any], arguments),
                    provider_data=pending.provider_data or None,
                )
            )
        return calls


def _usage(raw: dict[str, Any]) -> Usage:
    prompt = int(raw.get("prompt_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    total = int(raw.get("total_tokens") or 0)
    return Usage(
        input_tokens=prompt,
        output_tokens=completion,
        thinking_tokens=max(total - prompt - completion, 0),
    )
