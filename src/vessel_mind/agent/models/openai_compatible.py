"""The OpenAI-compatible adapter: POST /chat/completions with stream: true, over httpx.

It serves the free-tier hosted provider (Gemini's OpenAI-compatible endpoint) and
llama-server. No provider SDK.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from vessel_mind.core.chat import (
    AssistantMessage,
    ChatEvent,
    ChatModelError,
    ChatRequest,
    Stop,
    StopReason,
    TextDelta,
    Usage,
    UserMessage,
)

_FINISH_REASONS: dict[str, StopReason] = {
    "stop": "end",
    "length": "max_tokens",
    "tool_calls": "tool_use",
}

# Rate limited, or overloaded: worth asking again after a pause.
_RETRYABLE = {429, 503}


class OpenAICompatibleModel:
    def __init__(
        self,
        *,
        id: str,
        base_url: str,
        api_key: str | None,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.id = id
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._sleep = sleep
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    def __repr__(self) -> str:  # never shows the key
        return f"OpenAICompatibleModel(id={self.id!r}, url={self._url!r})"

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        headers = {"Accept-Encoding": "identity"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = self._body(req)
        attempt = 0
        while True:
            async with self._client.stream(
                "POST", self._url, json=body, headers=headers
            ) as response:
                if response.status_code == 200:
                    async for event in self._events(response):
                        yield event
                    return
                delay = self._retry_delay(response, attempt)
                if delay is None:
                    raise ChatModelError(f"{self.id}: HTTP {response.status_code}")
            attempt += 1
            await self._sleep(delay)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float | None:
        """Seconds to wait before the next attempt, or None to give up."""
        if response.status_code not in _RETRYABLE or attempt >= self._max_retries:
            return None
        delay = self._backoff_base * 2**attempt
        retry_after = response.headers.get("retry-after", "")
        if retry_after.isdigit():
            delay = float(retry_after)
        return delay if delay <= self._backoff_max else None

    async def _events(self, response: httpx.Response) -> AsyncIterator[ChatEvent]:
        finish_reason: str | None = None
        usage = Usage()
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
                content = (choice.get("delta") or {}).get("content")
                if content:
                    yield TextDelta(delta=content)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
        yield Stop(reason=_FINISH_REASONS.get(finish_reason or "stop", "end"), usage=usage)

    def _body(self, req: ChatRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        for message in req.messages:
            match message:
                case UserMessage(text=text):
                    messages.append({"role": "user", "content": text})
                case AssistantMessage(text=text):
                    messages.append({"role": "assistant", "content": text})
        body: dict[str, Any] = {
            "model": self.id,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        return body


def _usage(raw: dict[str, Any]) -> Usage:
    prompt = int(raw.get("prompt_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    total = int(raw.get("total_tokens") or 0)
    return Usage(
        input_tokens=prompt,
        output_tokens=completion,
        thinking_tokens=max(total - prompt - completion, 0),
    )
