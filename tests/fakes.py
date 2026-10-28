"""Shared fakes so no test ever touches a real provider (hard rule 5).

Two levels:
- `mock_http(handler)`: a real `Client` talking to an httpx MockTransport, for testing
  the client itself (status codes, headers, retries).
- `ScriptedClient`: replaces `Client.chat`/`Client.stream` with a Python function, for
  testing patterns, planner, executor, web UI, etc.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2 as httpx  # httpx2: the HTTP library the openai SDK is built on

from nathan.client import ChatResult, Client, ModelRef
from nathan.config import ModelSpec, Provider


def completion_json(text: str = "ok", model: str = "m", tool_calls: list | None = None,
                    prompt_tokens: int = 10, completion_tokens: int = 5) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = [
            {"id": tc.get("id", f"call_{i}"), "type": "function",
             "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments", {}))}}
            for i, tc in enumerate(tool_calls)
        ]
        message["content"] = text or None
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }


def mock_http(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def fake_providers(spec: dict[str, dict[str, list[str]]] | None = None,
                   keys: bool = True) -> dict[str, Provider]:
    """Build providers without providers.yaml.

    spec = {"groq": {"model-a": ["fast", "cheap"], ...}, ...} → provider → model → tags.
    """
    spec = spec or {
        "alpha": {"a-big": ["reasoning", "writing", "judge", "planner"], "a-small": ["fast", "cheap"]},
        "beta": {"b-big": ["writing", "reasoning", "code"]},
        "gamma": {"g-big": ["writing", "long_context", "judge"]},
        "delta": {"d-big": ["writing", "fast"]},
    }
    out = {}
    for name, models in spec.items():
        out[name] = Provider(
            name=name,
            base_url=f"https://{name}.test/v1",
            key_env=f"{name.upper()}_API_KEY",
            models={m: ModelSpec(tags=tags) for m, tags in models.items()},
            key_override=f"{name}-test-key" if keys else None,
        )
    return out


Responder = Callable[..., Any]


class ScriptedClient(Client):
    """A Client whose answers come from `responder(ref: str, messages, **kwargs)`.

    The responder may return a str, a ChatResult, or raise/return an Exception.
    """

    def __init__(self, responder: Responder, providers: dict[str, Provider] | None = None,
                 **kwargs: Any) -> None:
        super().__init__(providers if providers is not None else fake_providers(), **kwargs)
        self.responder = responder
        self.calls: list[tuple[str, list[dict]]] = []

    async def chat(self, ref, messages, **kwargs):  # type: ignore[override]
        r = ModelRef.parse(ref)
        self.provider(r.provider)
        if self.quota is not None:
            self.quota.check(r.provider, r.model)
        self.calls.append((str(r), messages))
        out = self.responder(str(r), messages, **kwargs)
        if hasattr(out, "__await__"):
            out = await out
        if isinstance(out, Exception):
            raise out
        if isinstance(out, ChatResult):
            result = out
        else:
            result = ChatResult(text=str(out), provider=r.provider, model=r.model,
                                prompt_tokens=10, completion_tokens=5)
        if self.quota is not None:
            self.quota.record(r.provider, r.model, result.total_tokens, {})
        return result

    async def stream(self, ref, messages, **kwargs):  # type: ignore[override]
        result = await self.chat(ref, messages, **kwargs)
        for word in result.text.split(" "):
            yield word + " "


def last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "user":
            return str(m["content"])
    return ""


def system_text(messages: list[dict]) -> str:
    return "\n".join(str(m["content"]) for m in messages if m["role"] == "system")
