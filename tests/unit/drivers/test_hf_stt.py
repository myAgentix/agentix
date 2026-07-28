"""HfSttDriver — MockTransport suite, no network. The proof-modality tests:
bytes-in/text-out through the driver abstraction, source="huggingface"."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agentix.drivers import (
    AudioSource,
    Driver,
    DriverInvalidRequest,
    DriverRateLimited,
    DriverUnavailable,
    SttDriver,
    Transcript,
)
from agentix.drivers.adapters.intrinsic.hf import HfSttDriver

# ───────────────────── helpers ─────────────────────


def _driver(handler: Any, **kw: Any) -> HfSttDriver:
    return HfSttDriver(api_key="hf-test-token", transport=httpx.MockTransport(handler), **kw)


def _audio(**kw: Any) -> AudioSource:
    return AudioSource(data=b"RIFF....WAVEfmt fake-audio", **kw)


# ───────────────────── construction ─────────────────────


def test_missing_token_fails_loud_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(DriverInvalidRequest, match="HF_TOKEN"):
        HfSttDriver()


def test_descriptor_is_huggingface_stt() -> None:
    drv = _driver(lambda request: httpx.Response(200, json={"text": "x"}))
    desc = drv.descriptor
    assert desc.type == "model"
    assert desc.modality == "stt"
    assert desc.source == "huggingface"
    assert desc.pricing_ref is None  # per-second pricing — not cost-recorded in v0.5
    assert desc.default_model == "openai/whisper-large-v3"


def test_protocol_conformance() -> None:
    drv = _driver(lambda request: httpx.Response(200, json={"text": "x"}))
    assert isinstance(drv, Driver)
    assert isinstance(drv, SttDriver)


# ───────────────────── happy path ─────────────────────


@pytest.mark.asyncio
async def test_transcribe_posts_audio_and_parses_text() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "hallo welt"})

    drv = _driver(handler)
    result = await drv.transcribe(_audio(mime_type="audio/flac", language="de"))
    assert isinstance(result, Transcript)
    assert result.text == "hallo welt"
    assert result.model == "openai/whisper-large-v3"
    assert result.language == "de"
    assert seen["url"].endswith("/models/openai/whisper-large-v3")
    assert seen["auth"] == "Bearer hf-test-token"
    assert seen["content_type"] == "audio/flac"
    assert seen["body"] == b"RIFF....WAVEfmt fake-audio"


@pytest.mark.asyncio
async def test_per_call_model_override() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/models/openai/whisper-small" in str(request.url)
        return httpx.Response(200, json={"text": "ok"})

    drv = _driver(handler)
    result = await drv.transcribe(_audio(model="openai/whisper-small"))
    assert result.model == "openai/whisper-small"


# ───────────────────── error mapping ─────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "exc_cls", "retryable"),
    [
        (429, {"error": "rate limited"}, DriverRateLimited, True),
        (
            503,
            {"error": "Model openai/whisper-large-v3 is currently loading", "estimated_time": 20.0},
            DriverUnavailable,
            True,
        ),
        (500, {"error": "internal"}, DriverUnavailable, True),
        (400, {"error": "bad audio"}, DriverInvalidRequest, False),
        (401, {"error": "unauthorized"}, DriverInvalidRequest, False),
    ],
)
async def test_http_status_mapping(status: int, body: dict[str, Any], exc_cls: type, retryable: bool) -> None:
    drv = _driver(lambda request: httpx.Response(status, json=body))
    with pytest.raises(exc_cls) as excinfo:
        await drv.transcribe(_audio())
    assert excinfo.value.retryable is retryable
    assert excinfo.value.driver == "hf-stt"


@pytest.mark.asyncio
async def test_cold_start_message_carries_estimated_time() -> None:
    body = {"error": "loading", "estimated_time": 17.5}
    drv = _driver(lambda request: httpx.Response(503, json=body))
    with pytest.raises(DriverUnavailable, match=r"17\.5"):
        await drv.transcribe(_audio())


@pytest.mark.asyncio
async def test_network_errors_map_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    drv = _driver(handler)
    with pytest.raises(DriverUnavailable, match="unreachable"):
        await drv.transcribe(_audio())


@pytest.mark.asyncio
async def test_non_json_and_missing_text_are_invalid() -> None:
    drv = _driver(lambda request: httpx.Response(200, content=b"<html>oops</html>"))
    with pytest.raises(DriverInvalidRequest, match="non-JSON"):
        await drv.transcribe(_audio())

    drv2 = _driver(lambda request: httpx.Response(200, json={"nope": 1}))
    with pytest.raises(DriverInvalidRequest, match="missing 'text'"):
        await drv2.transcribe(_audio())


def test_empty_audio_rejected_locally() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        AudioSource(data=b"")


# ───────────────────── registry + factory wiring ─────────────────────


def test_declared_spec_lands_in_registry_as_stt() -> None:
    from pathlib import Path
    from unittest.mock import patch

    from agentix.config import DriverSpec, KernelConfig, MeliousConfig
    from agentix.drivers.factory import build_drivers
    from agentix.storage import MinioConfig

    cfg = KernelConfig(
        config_path=Path("/tmp/cfg.yaml"),
        minio=MinioConfig(endpoint="localhost:0", access_key="x", secret_key="x"),
        sqlite_path=Path("/tmp/db.sqlite"),
        memory_path=Path("/tmp/memory"),
        melious=MeliousConfig(enabled=True, base_url="https://m.example", api_key="k"),
        drivers=(
            DriverSpec(name="melious", driver="melious", modality="chat", default=True),
            DriverSpec(
                name="hf-stt",
                driver="hf-stt",
                modality="stt",
                model="openai/whisper-small",
                api_key_env="AGENTIX_TEST_HF_TOKEN",
                default=True,
            ),
        ),
    )

    class _FakeMelious:
        name = "melious"
        default_model = "deepseek-v4-flash"

        from agentix.drivers.base import DriverDescriptor as _DD

        descriptor = _DD(name="melious", type="model", modality="chat")

        def __init__(self, **kwargs: Any) -> None:
            pass

        async def complete(self, request: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

        async def aclose(self) -> None:
            pass

    with (
        patch("agentix.drivers.adapters.vendor.melious.MeliousChatDriver", _FakeMelious),
        patch.dict("os.environ", {"AGENTIX_TEST_HF_TOKEN": "hf-x"}),
    ):
        registry = build_drivers(cfg)

    stt = registry.stt()
    assert isinstance(stt, HfSttDriver)
    assert stt.default_model == "openai/whisper-small"


@pytest.mark.asyncio
async def test_transcribe_respects_driver_capacity() -> None:
    """The STT call runs inside the shared capacity semaphore."""
    from agentix.drivers import configure_driver_capacity, current_limit

    previous = current_limit()
    configure_driver_capacity(1)
    try:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append("hit")
            return httpx.Response(200, json={"text": "ok"})

        drv = _driver(handler)
        result = await drv.transcribe(_audio())
        assert result.text == "ok"
        assert calls == ["hit"]
    finally:
        configure_driver_capacity(previous)


@pytest.mark.asyncio
async def test_transcript_raw_preserved() -> None:
    body = {"text": "ok", "chunks": [{"timestamp": [0.0, 1.0], "text": "ok"}]}
    drv = _driver(
        lambda request: httpx.Response(
            200, content=json.dumps(body).encode(), headers={"content-type": "application/json"}
        )
    )
    result = await drv.transcribe(_audio())
    assert result.raw["chunks"][0]["text"] == "ok"
