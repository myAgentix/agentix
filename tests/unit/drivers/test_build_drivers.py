"""build_drivers composition + parity with the legacy runtime factories."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentix.config import (
    DriverSpec,
    HubleConfig,
    KernelConfig,
    MeliousConfig,
    derive_driver_specs,
    enabled_providers,
)
from agentix.drivers import ChatFailoverChain, CostRecordingChatDriver
from agentix.drivers.factory import build_drivers, register_driver_factory
from agentix.storage import MinioConfig


def _cfg(**providers: object) -> KernelConfig:
    return KernelConfig(
        config_path=Path("/tmp/cfg.yaml"),
        minio=MinioConfig(endpoint="localhost:0", access_key="x", secret_key="x"),
        sqlite_path=Path("/tmp/db.sqlite"),
        memory_path=Path("/tmp/memory"),
        **providers,  # type: ignore[arg-type]
    )


class _FakeHuble:
    name = "huble"
    default_model = "glm-4.7"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    @property
    def descriptor(self) -> Any:
        from agentix.drivers.base import DriverDescriptor

        return DriverDescriptor(name=self.name, type="model", modality="chat", default_model=self.default_model)

    async def complete(self, request: Any) -> Any:  # pragma: no cover — never called
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class _FakeMelious(_FakeHuble):
    name = "melious"
    default_model = "deepseek-v4-flash"


class _FakeNvidia(_FakeHuble):
    name = "nvidia"
    default_model = "meta/llama-3.3-70b-instruct"


# ── derive parity with enabled_providers ──────────────────────────


def test_derive_specs_chat_matches_enabled_providers_order() -> None:
    cfg = _cfg(
        melious=MeliousConfig(enabled=True),
        huble=HubleConfig(enabled=True),
    )
    chat = [s for s in derive_driver_specs(cfg) if s.modality == "chat"]
    assert [s.name for s in chat] == [name for name, _ in enabled_providers(cfg)]
    assert chat[0].default is True


def test_derive_specs_last_resort_melious() -> None:
    chat = [s for s in derive_driver_specs(_cfg()) if s.modality == "chat"]
    assert [s.name for s in chat] == ["melious"]


def test_derive_specs_emit_no_ambient_embedding_fallback() -> None:
    """0.8: the kernel ships no ambient-credential embedding backend."""
    assert [s for s in derive_driver_specs(_cfg()) if s.modality == "embedding"] == []


# ── chat composition ──────────────────────────────────────────────


def test_single_chat_spec_registers_bare_driver() -> None:
    cfg = _cfg(huble=HubleConfig(enabled=True, base_url="https://h.example", api_key="k"))
    with patch("agentix.drivers.adapters.intrinsic.huble.HubleChatDriver", _FakeHuble):
        registry = build_drivers(cfg)
    chat = registry.chat()
    assert isinstance(chat, _FakeHuble)
    assert not isinstance(chat, ChatFailoverChain)


def test_always_chain_wraps_single_driver() -> None:
    cfg = _cfg(huble=HubleConfig(enabled=True, base_url="https://h.example", api_key="k"))
    with patch("agentix.drivers.adapters.intrinsic.huble.HubleChatDriver", _FakeHuble):
        registry = build_drivers(cfg, always_chain=True)
    assert isinstance(registry.chat(), ChatFailoverChain)


def test_multiple_chat_specs_compose_a_chain_in_priority_order() -> None:
    cfg = _cfg(
        huble=HubleConfig(enabled=True, base_url="https://h.example", api_key="k"),
        melious=MeliousConfig(enabled=True, base_url="https://m.example", api_key="k"),
    )
    with (
        patch("agentix.drivers.adapters.intrinsic.huble.HubleChatDriver", _FakeHuble),
        patch("agentix.drivers.adapters.vendor.melious.MeliousChatDriver", _FakeMelious),
    ):
        registry = build_drivers(cfg)
    chain = registry.chat()
    assert isinstance(chain, ChatFailoverChain)
    # Priority order: direct Melious first (no gateway hop), then HUBLE.
    assert [p.name for p in chain.providers] == ["melious", "huble"]


def test_sqlite_wraps_chat_drivers_in_cost_recorder() -> None:
    cfg = _cfg(huble=HubleConfig(enabled=True, base_url="https://h.example", api_key="k"))
    with patch("agentix.drivers.adapters.intrinsic.huble.HubleChatDriver", _FakeHuble):
        registry = build_drivers(cfg, sqlite=MagicMock())
    assert isinstance(registry.chat(), CostRecordingChatDriver)


def test_model_override_reaches_huble_not_other_drivers() -> None:
    """``model_override`` is scoped to the melious/huble specs only."""
    cfg = _cfg(
        huble=HubleConfig(enabled=True, base_url="https://h.example", api_key="k", model="glm-4.7"),
        drivers=(
            DriverSpec(name="huble", driver="huble", modality="chat", default=True),
            DriverSpec(name="nvidia", driver="nvidia", modality="chat", model="meta/llama-3.3-70b-instruct"),
        ),
    )
    huble_kwargs: dict[str, Any] = {}
    nvidia_kwargs: dict[str, Any] = {}

    class _CapturingHuble(_FakeHuble):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            huble_kwargs.update(kwargs)

    class _CapturingNvidia(_FakeNvidia):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            nvidia_kwargs.update(kwargs)

    with (
        patch("agentix.drivers.adapters.intrinsic.huble.HubleChatDriver", _CapturingHuble),
        patch("agentix.drivers.adapters.vendor.nvidia.NvidiaChatDriver", _CapturingNvidia),
    ):
        build_drivers(cfg, model_override="hermes-4-405b")
    assert huble_kwargs["model"] == "hermes-4-405b"
    assert nvidia_kwargs["model"] == "meta/llama-3.3-70b-instruct"


# ── declared specs + extension seam ───────────────────────────────


def test_unknown_factory_key_fails_loud() -> None:
    cfg = _cfg(drivers=(DriverSpec(name="x", driver="no-such-key", type="queue", modality="chat"),))
    with pytest.raises(ValueError, match="no-such-key"):
        build_drivers(cfg)


def test_registered_factory_builds_declared_spec() -> None:
    from agentix.drivers.base import DriverDescriptor

    class _TickerDriver:
        def __init__(self) -> None:
            self._descriptor = DriverDescriptor(name="ticker", type="timeseries-feed", source="local")

        @property
        def descriptor(self) -> DriverDescriptor:
            return self._descriptor

        async def aclose(self) -> None:
            pass

    register_driver_factory("test-ticker", lambda spec, cfg: _TickerDriver(), override=True)
    cfg = _cfg(
        melious=MeliousConfig(enabled=True, base_url="https://m.example", api_key="k"),
        drivers=(
            DriverSpec(name="melious", driver="melious", modality="chat", default=True),
            DriverSpec(name="ticker", driver="test-ticker", type="timeseries-feed", modality="other"),
        ),
    )
    with patch("agentix.drivers.adapters.vendor.melious.MeliousChatDriver", _FakeMelious):
        registry = build_drivers(cfg)
    assert registry.get("ticker").descriptor.type == "timeseries-feed"
    assert registry.chat().name == "melious"


def test_dotted_path_driver_construction() -> None:
    cfg = _cfg(
        melious=MeliousConfig(enabled=True, base_url="https://m.example", api_key="k"),
        drivers=(
            DriverSpec(name="melious", driver="melious", modality="chat", default=True),
            DriverSpec(
                name="dotted",
                driver="tests.unit.drivers.fake_dotted_driver:FakeDottedDriver",
                type="database",
                modality="other",
                api_key_env="AGENTIX_TEST_DOTTED_KEY",
            ),
        ),
    )
    with (
        patch("agentix.drivers.adapters.vendor.melious.MeliousChatDriver", _FakeMelious),
        patch.dict("os.environ", {"AGENTIX_TEST_DOTTED_KEY": "s3cr3t"}),
    ):
        registry = build_drivers(cfg)
    dotted = registry.get("dotted")
    assert dotted.descriptor.type == "database"
    assert dotted.api_key == "s3cr3t"  # type: ignore[attr-defined]
