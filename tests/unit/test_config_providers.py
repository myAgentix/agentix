"""Provider-selection helpers — the single source of truth for activation."""

from __future__ import annotations

from pathlib import Path

from agentix.config import (
    HubleConfig,
    KernelConfig,
    MeliousConfig,
    enabled_providers,
    select_enabled_provider,
)
from agentix.storage import MinioConfig


def _cfg(**providers: object) -> KernelConfig:
    return KernelConfig(
        config_path=Path("/tmp/cfg.yaml"),
        minio=MinioConfig(endpoint="localhost:0", access_key="x", secret_key="x"),
        sqlite_path=Path("/tmp/db.sqlite"),
        memory_path=Path("/tmp/memory"),
        **providers,  # type: ignore[arg-type]
    )


def test_enabled_providers_priority_order() -> None:
    cfg = _cfg(melious=MeliousConfig(enabled=True), huble=HubleConfig(enabled=True))
    assert [name for name, _ in enabled_providers(cfg)] == ["melious", "huble"]


def test_enabled_providers_skips_inactive() -> None:
    cfg = _cfg(huble=HubleConfig(enabled=True))
    assert [name for name, _ in enabled_providers(cfg)] == ["huble"]


def test_select_primary_is_first_by_priority() -> None:
    cfg = _cfg(huble=HubleConfig(enabled=True))
    name, pc = select_enabled_provider(cfg)
    assert name == "huble"
    assert isinstance(pc, HubleConfig)


def test_select_falls_back_to_melious_when_nothing_active() -> None:
    cfg = _cfg()  # no provider configured
    assert enabled_providers(cfg) == []
    name, pc = select_enabled_provider(cfg)
    assert name == "melious"
    assert isinstance(pc, MeliousConfig)


def test_kernel_config_carries_no_first_party_provider_block() -> None:
    """0.8: anthropic/openai/groq/grok are out-of-tree drivers (seam #13)."""
    fields = {f.name for f in KernelConfig.__dataclass_fields__.values()}
    assert fields & {"anthropic", "openai", "groq", "grok"} == set()
