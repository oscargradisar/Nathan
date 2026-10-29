"""Tier 1: one `chat()` for every provider.

Every provider we use speaks the OpenAI chat-completions dialect, so one AsyncOpenAI
client per provider (just a different base_url + key) is enough. This module adds:

- retries with exponential backoff on 429 / 5xx / timeouts,
- typed errors (see errors.py) once retries run out,
- response headers kept on the result, so the quota manager can read live limits,
- optional hooks for the quota manager and the response cache (Tier 2 / Tier 6).

Why async? Network calls spend almost all their time waiting. With asyncio, Nathan can
wait on five providers at once instead of one after another.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx2 as httpx  # httpx2: the HTTP library the openai SDK is built on
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from nathan.config import NO_KEY, Provider, load_providers
from nathan.errors import (
    AuthError,
    BadRequest,
    ModelGone,
    NathanError,
    ProviderDown,
    QuotaExceeded,
)

Message = dict[str, Any]

# A 429 with a Retry-After longer than this is treated as "quota gone", not "wait a bit".
MAX_RETRY_WAIT_S = 20.0


@dataclass(frozen=True)
class ModelRef:
    """A model address like "groq/openai/gpt-oss-120b" → provider "groq", model the rest."""

    provider: str
    model: str

    @classmethod
    def parse(cls, ref: str | ModelRef) -> ModelRef:
        if isinstance(ref, ModelRef):
            return ref
        provider, sep, model = ref.partition("/")
        if not sep or not provider or not model:
            raise ValueError(f"model must look like provider/model, got {ref!r}")
        return cls(provider, model)

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass
class ChatResult:
    text: str
    provider: str
    model: str
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    cached: bool = False

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class QuotaHook(Protocol):
    """What the client needs from a quota manager (implemented in quota.py)."""

    def check(self, provider: str, model: str) -> None: ...  # raises QuotaExceeded
    def record(self, provider: str, model: str, tokens: int,
               headers: dict[str, str]) -> None: ...
    def block(self, provider: str, model: str, seconds: float, reason: str) -> None: ...
    def mark_gone(self, provider: str, model: str) -> None: ...


class CacheHook(Protocol):
    def get(self, key: str) -> ChatResult | None: ...
    def put(self, key: str, result: ChatResult) -> None: ...


def retry_after_seconds(headers: httpx.Headers | dict[str, str] | None) -> float | None:
    """Read Retry-After (seconds form). Returns None if absent or unparsable."""
    if not headers:
        return None
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None



TIMEOUT_BENCH_S = 300.0  # a model that timed out sits out this long

_THINKING = re.compile(r"<(think|thought|thinking)>.*?(</\1>|$)", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Some models (Gemma, DeepSeek R1, Qwen) put their private reasoning in tags."""
    return _THINKING.sub("", text).strip()

async def _drop_placeholder_key(request: httpx.Request) -> None:
    if request.headers.get("Authorization") == f"Bearer {NO_KEY}":
        del request.headers["Authorization"]

class Client:
    """Holds one AsyncOpenAI client per provider and adds retry + error typing."""

    def __init__(
        self,
        providers: dict[str, Provider] | None = None,
        *,
        quota: QuotaHook | None = None,
        cache: CacheHook | None = None,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 2,
        timeout: float = 60.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.providers = providers if providers is not None else load_providers()
        self.quota = quota
        self.cache = cache
        self.http_client = http_client  # tests inject a mock transport here
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep = sleep
        self._clients: dict[str, AsyncOpenAI] = {}

    # ---- plumbing -------------------------------------------------------------------

    def provider(self, name: str) -> Provider:
        try:
            return self.providers[name]
        except KeyError:
            raise NathanError(f"unknown provider {name!r}", provider=name) from None

    def _sdk(self, name: str) -> AsyncOpenAI:
        if name not in self._clients:
            p = self.provider(name)
            if not p.api_key:
                raise AuthError(f"no API key for {name} (set {p.key_env})", provider=name)
            http = self.http_client
            if p.api_key == NO_KEY:
                # Keyless gateways reject any Authorization header, but the SDK always adds one
                http = http or httpx.AsyncClient(timeout=self.timeout)
                if _drop_placeholder_key not in http.event_hooks["request"]:
                    http.event_hooks["request"].append(_drop_placeholder_key)
            self._clients[name] = AsyncOpenAI(
                base_url=p.resolved_base_url,
                api_key=p.api_key,
                timeout=self.timeout,
                max_retries=0,  # we retry ourselves so we can type the errors
                http_client=http,
            )
        return self._clients[name]

    async def close(self) -> None:
        for c in self._clients.values():
            await c.close()
        self._clients.clear()

    def _backoff(self, attempt: int) -> float:
        return min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.25)

    # ---- main entry point -------------------------------------------------------------

    async def chat(
        self,
        ref: str | ModelRef,
        messages: list[Message],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> ChatResult:
        r = ModelRef.parse(ref)
        self.provider(r.provider)  # fail early on unknown provider names
        params: dict[str, Any] = {"model": r.model, "messages": messages}
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if temperature is not None:
            params["temperature"] = temperature
        if tools:
            params["tools"] = tools
        if response_format:
            params["response_format"] = response_format

        cache_key = None
        if self.cache is not None and use_cache and not tools:
            from nathan.cache import make_key

            cache_key = make_key(str(r), params)
            hit = self.cache.get(cache_key)
            if hit is not None:
                return hit

        if self.quota is not None:
            self.quota.check(r.provider, r.model)

        sdk = self._sdk(r.provider)
        attempt = 0
        while True:
            start = time.perf_counter()
            try:
                raw = await sdk.chat.completions.with_raw_response.create(**params)
                completion = raw.parse()
                headers = {k.lower(): v for k, v in raw.headers.items()}
                result = _to_result(completion, r, headers, time.perf_counter() - start)
                if self.quota is not None:
                    self.quota.record(r.provider, r.model, result.total_tokens, headers)
                if cache_key is not None and result.text:
                    self.cache.put(cache_key, result)  # type: ignore[union-attr]
                return result

            except APIStatusError as e:
                status = e.status_code
                msg = f"{r}: HTTP {status} {_short(e)}"
                if status == 429:
                    wait = retry_after_seconds(e.response.headers)
                    if attempt < self.max_retries and (wait is None or wait <= MAX_RETRY_WAIT_S):
                        await self._sleep(wait if wait is not None else self._backoff(attempt))
                        attempt += 1
                        continue
                    if self.quota is not None:
                        self.quota.block(r.provider, r.model, wait or 60.0, "429")
                    raise QuotaExceeded(msg, provider=r.provider, model=r.model,
                                        retry_after=wait) from e
                if status >= 500:
                    if attempt < self.max_retries:
                        await self._sleep(self._backoff(attempt))
                        attempt += 1
                        continue
                    raise ProviderDown(msg, provider=r.provider, model=r.model) from e
                if status in (401, 403):
                    raise AuthError(msg, provider=r.provider, model=r.model) from e
                if status == 404 or "model_not_found" in str(e.body or ""):
                    if self.quota is not None:
                        self.quota.mark_gone(r.provider, r.model)
                    raise ModelGone(msg, provider=r.provider, model=r.model) from e
                raise BadRequest(msg, provider=r.provider, model=r.model) from e

            except APITimeoutError as e:
                # No answer within the timeout: retrying the same model would make the
                # whole task wait again. Bench it briefly and let the caller try the next one.
                if self.quota is not None:
                    self.quota.block(r.provider, r.model, TIMEOUT_BENCH_S, "timeout")
                raise ProviderDown(f"{r}: no answer within {self.timeout:.0f}s",
                                   provider=r.provider, model=r.model) from e

            except APIConnectionError as e:
                if attempt < self.max_retries:
                    await self._sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise ProviderDown(f"{r}: {type(e).__name__}", provider=r.provider,
                                   model=r.model) from e

    async def stream(
        self,
        ref: str | ModelRef,
        messages: list[Message],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Yield text pieces as they arrive (used by the web UI's live lanes).

        No retries mid-stream: if the stream breaks, the caller falls back to another model.
        """
        r = ModelRef.parse(ref)
        if self.quota is not None:
            self.quota.check(r.provider, r.model)
        params: dict[str, Any] = {"model": r.model, "messages": messages, "stream": True}
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if temperature is not None:
            params["temperature"] = temperature
        sdk = self._sdk(r.provider)
        chars = 0
        try:
            stream = await sdk.chat.completions.create(**params)
            async for chunk in stream:
                if not chunk.choices:
                    continue
                piece = chunk.choices[0].delta.content or ""
                if piece:
                    chars += len(piece)
                    yield piece
        except APIStatusError as e:
            if e.status_code == 429:
                raise QuotaExceeded(f"{r}: HTTP 429", provider=r.provider, model=r.model,
                                    retry_after=retry_after_seconds(e.response.headers)) from e
            if e.status_code == 404:
                if self.quota is not None:
                    self.quota.mark_gone(r.provider, r.model)
                raise ModelGone(f"{r}: HTTP 404", provider=r.provider, model=r.model) from e
            if e.status_code in (401, 403):
                raise AuthError(f"{r}: HTTP {e.status_code}", provider=r.provider,
                                model=r.model) from e
            raise ProviderDown(f"{r}: HTTP {e.status_code}", provider=r.provider,
                               model=r.model) from e
        except (APIConnectionError, APITimeoutError) as e:
            raise ProviderDown(f"{r}: {type(e).__name__}", provider=r.provider,
                               model=r.model) from e
        if self.quota is not None:
            # Streams don't always report usage; ~4 characters per token is a fair estimate.
            self.quota.record(r.provider, r.model, chars // 4, {})


def _short(e: APIStatusError) -> str:
    text = str(getattr(e, "message", "") or e)
    return text[:200]


def _to_result(completion: Any, r: ModelRef, headers: dict[str, str], seconds: float) -> ChatResult:
    choice = completion.choices[0] if completion.choices else None
    msg = choice.message if choice else None
    usage = getattr(completion, "usage", None)
    tool_calls = []
    for tc in (getattr(msg, "tool_calls", None) or []):
        tool_calls.append({
            "id": tc.id,
            "name": tc.function.name,
            "arguments": tc.function.arguments or "{}",
        })
    return ChatResult(
        text=strip_thinking((msg.content if msg else None) or ""),
        provider=r.provider,
        model=r.model,
        latency_ms=seconds * 1000,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        headers=headers,
        tool_calls=tool_calls,
    )


# ---- convenience function -------------------------------------------------------------

_default: Client | None = None


async def chat(provider: str, model: str, messages: list[Message], **kwargs: Any) -> ChatResult:
    """`await chat("groq", "llama-3.1-8b-instant", [{"role": "user", "content": "hi"}])`."""
    global _default
    if _default is None:
        _default = Client()
    return await _default.chat(ModelRef(provider, model), messages, **kwargs)
