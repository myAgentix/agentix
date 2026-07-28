"""Anthropic (Claude) provider.

Two auth modes, both transparent to callers:

* **API key** — any plain ``sk-ant-api...`` key works via the standard
  ``x-api-key`` header.
* **OAuth (Claude Code subscription)** — keys prefixed ``sk-ant-oat``
  are sent as ``Authorization: Bearer <token>`` with an ``anthropic-beta``
  hint and the billing header Anthropic expects for Claude Code clients.
  Credentials are resolved per-request via a :class:`TokenSource` so
  Claude Code's background refresh of the OAuth token lands in the next
  API call without a restart. Supported sources: env vars
  (``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_AUTH_TOKEN`` /
  ``ANTHROPIC_API_KEY``), the macOS Keychain (``security
  find-generic-password -s 'Claude Code-credentials'``), or a plain JSON
  file (``~/.claude/.credentials.json``).

OAuth is the primary path — it rides on the operator's Claude
subscription with zero per-token API cost.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anthropic
import structlog

from agentix.core.types import Message, TokenUsage, ToolCall
from agentix.drivers.adapters.intrinsic.anthropic_auth import TokenSource, resolve_token_source
from agentix.drivers.base import (
    DriverDescriptor,
    DriverInvalidRequest,
    DriverRateLimited,
    DriverUnavailable,
)
from agentix.drivers.chat import ChatRequest, ChatResponse

log = structlog.get_logger(__name__)

_OAUTH_ANTHROPIC_BETA = "oauth-2025-04-20,claude-code-20250219"

# The OAuth "billing" header Anthropic's Claude Code flow expects.
# The version string inside this header will drift as Claude Code updates;
# operators who hit OAuth auth failures after a Claude Code update can
# override via ``AGENTIX_ANTHROPIC_BILLING_HEADER`` without a code change.
# Default matches a recent-enough Claude Code version to keep existing
# setups working.
#
# Precedence: ``AGENTIX_ANTHROPIC_BILLING_HEADER`` > default.
# (The legacy branded env name was removed in agentix 0.3.)
_DEFAULT_BILLING_HEADER = "cc_version=2.1.85.351; cc_entrypoint=cli; cch=6c6d5;"


def _billing_header() -> str:
    """Return the OAuth billing header — env-override first, then default."""
    return os.environ.get("AGENTIX_ANTHROPIC_BILLING_HEADER") or _DEFAULT_BILLING_HEADER


_MODEL_AUTO_MAX = "claude-opus-4-7"
_MODEL_AUTO_PRO = "claude-sonnet-4-6"


class AnthropicChatDriver:
    """Claude via the official SDK — API-key and OAuth both supported."""

    name = "anthropic"

    @property
    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            name=self.name,
            type="model",
            modality="chat",
            source="api",
            capabilities=frozenset({"tools", "thinking", "cache_control"}),
            default_model=self.default_model,
            pricing_ref=self.default_model,
        )

    def __init__(
        self,
        *,
        api_key: str | None = None,
        oauth_credentials_path: str | Path | None = None,
        keychain_service: str | None = None,
        token_source: TokenSource | None = None,
        model: str | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        if token_source is None:
            token_source = resolve_token_source(
                api_key=api_key,
                credentials_path=oauth_credentials_path,
                keychain_service=keychain_service,
            )
        self._token_source = token_source
        # Probe once at init to pin auth-mode + default model — picking
        # Opus vs Sonnet depends on whether we're OAuth or API-key, and
        # that doesn't change mid-session. Raises loudly if no source
        # can produce a token, so operators see the config gap immediately.
        token, is_oauth = token_source.get_token()
        self._is_oauth = is_oauth
        self.default_model = model or _infer_default_model(is_oauth)

        if is_oauth:
            # OAuth auth lives in ``Authorization: Bearer <token>``. Using
            # the SDK's ``auth_token`` parameter sets that header cleanly
            # and — critically — does NOT also inject ``x-api-key``, which
            # Anthropic's server rejects with a 401 when a bogus value
            # like ``dummy-oauth`` lands there. Per-request refresh uses
            # ``with_options(auth_token=<fresh>)`` in complete().
            default_headers = {
                "anthropic-beta": _OAUTH_ANTHROPIC_BETA,
                "x-anthropic-billing-header": _billing_header(),
            }
            self._client = anthropic.AsyncAnthropic(
                auth_token=token,
                timeout=timeout_seconds,
                default_headers=default_headers,
            )
        else:
            self._client = anthropic.AsyncAnthropic(
                api_key=token,
                timeout=timeout_seconds,
            )
        log.info(
            "anthropic.provider_ready",
            auth_mode="oauth" if is_oauth else "api_key",
            default_model=self.default_model,
            token_source=type(token_source).__name__,
        )

    async def complete(self, request: ChatRequest) -> ChatResponse:
        model = request.model or self.default_model
        system_text, turns = _split_system(request.messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_tokens,
            "messages": turns,
        }
        # OAuth flow rejects prompt caching; for API-key mode we promote the
        # system prompt to the block form with a cache_control marker so
        # Anthropic actually hits the 5-minute ephemeral cache.
        caching_enabled = request.cache_control and not self._is_oauth
        if system_text:
            if caching_enabled:
                kwargs["system"] = [
                    {
                        "type": "text",
                        "text": system_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                kwargs["system"] = system_text
        if request.temperature != 1.0:
            kwargs["temperature"] = request.temperature
        if request.stop_sequences:
            kwargs["stop_sequences"] = request.stop_sequences
        if request.thinking_enabled:
            budget = request.thinking_budget_tokens or max(1024, request.max_tokens // 4)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # Tool-use. Anthropic's ``tools`` takes
        # ``[{name, description, input_schema}, ...]`` — ToolSpec's shape
        # matches directly. ``tool_choice`` under OAuth can't be ``any``
        # (Anthropic's OAuth flow disallows forced tool selection), so we
        # normalise ``any`` → ``auto`` in that mode.
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                }
                for spec in request.tools
            ]
        if request.tool_choice is not None:
            effective = request.tool_choice
            if self._is_oauth and effective == "any":
                effective = "auto"
            kwargs["tool_choice"] = {"type": effective}
        if caching_enabled:
            kwargs.setdefault("extra_headers", {})["anthropic-beta"] = "prompt-caching-2024-07-31"
        kwargs.update(request.extra_params)

        # OAuth mode: rebind ``auth_token`` on a per-call sub-client so
        # Claude Code's in-flight refresh is picked up. ``with_options``
        # is a shallow copy + header merge in the SDK — cheap per call.
        # API-key mode keeps using ``self._client`` since the SDK already
        # owns the header via ``api_key`` at init.
        if self._is_oauth:
            fresh_token, _ = self._token_source.get_token()
            client = self._client.with_options(auth_token=fresh_token)
        else:
            client = self._client

        try:
            response = await client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise DriverRateLimited(str(e), driver=self.name) from e
        except anthropic.APIStatusError as e:
            if e.status_code and e.status_code >= 500:
                raise DriverUnavailable(str(e), driver=self.name) from e
            raise DriverInvalidRequest(str(e), driver=self.name) from e
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise DriverUnavailable(str(e), driver=self.name) from e

        return _from_anthropic_response(response, model)

    async def list_models(self) -> list[str]:
        try:
            resp = await self._client.models.list()
        except anthropic.RateLimitError as e:
            raise DriverRateLimited(str(e), driver=self.name) from e
        except anthropic.APIStatusError as e:
            if e.status_code and e.status_code >= 500:
                raise DriverUnavailable(str(e), driver=self.name) from e
            raise DriverInvalidRequest(str(e), driver=self.name) from e
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise DriverUnavailable(str(e), driver=self.name) from e
        return sorted(m.id for m in resp.data)

    async def aclose(self) -> None:
        await self._client.close()


# ────────────────────────── helpers ────────────────────────────────────────


def _infer_default_model(is_oauth: bool) -> str:
    # When OAuth is active we trust whatever the subscription exposes —
    # Max plans get Opus, Pro gets Sonnet. Explicit model override always
    # wins; this is just a reasonable guess for the auto-detect default.
    return _MODEL_AUTO_MAX if is_oauth else _MODEL_AUTO_PRO


def _split_system(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Anthropic wants ``system`` as a top-level string, not in messages."""
    system_parts: list[str] = []
    turns: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            if m.content:
                system_parts.append(m.content)
            continue
        turns.append(_message_to_anthropic(m))
    return "\n\n".join(system_parts), turns


def _message_to_anthropic(m: Message) -> dict[str, Any]:
    role = "assistant" if m.role == "assistant" else "user"
    if not m.tool_calls and m.role != "tool":
        return {"role": role, "content": m.content}
    blocks: list[dict[str, Any]] = []
    if m.role == "tool":
        blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id or "",
                "content": m.content,
            }
        )
        return {"role": "user", "content": blocks}
    if m.content:
        blocks.append({"type": "text", "text": m.content})
    for tc in m.tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.arguments,
            }
        )
    return {"role": role, "content": blocks}


def _from_anthropic_response(response: Any, model: str) -> ChatResponse:
    """Collapse an Anthropic response into the kernel's canonical ChatResponse.

    Anthropic returns a list of content blocks:

      * ``text`` — the assistant's free-text reply.
      * ``thinking`` — internal reasoning (skipped, not billed as output).
      * ``tool_use`` — a tool invocation the agent should run.

    The AgentDispatcher loops while ``tool_calls`` is non-empty.
    We also expose ``stop_reason`` faithfully — ``tool_use`` vs
    ``end_turn`` — so callers can short-circuit without inspecting the
    content.
    """
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in getattr(response, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", ""))
        elif btype == "thinking":
            continue
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=str(getattr(block, "id", "")),
                    name=str(getattr(block, "name", "")),
                    arguments=dict(getattr(block, "input", {}) or {}),
                )
            )
    usage = TokenUsage(
        input_tokens=int(getattr(response.usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(response.usage, "output_tokens", 0) or 0),
        cached_tokens=int(getattr(response.usage, "cache_read_input_tokens", 0) or 0),
    )
    return ChatResponse(
        content="".join(text_parts),
        usage=usage,
        model=getattr(response, "model", model),
        finish_reason=getattr(response, "stop_reason", None),
        tool_calls=tool_calls,
        raw={"id": getattr(response, "id", None)},
    )
