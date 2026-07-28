"""The scaffold must generate stubs that actually import and conform.

`agentix scaffold driver` is the advertised on-ramp for seam #13, so a
template that references a module the kernel doesn't have (or a verb the
protocol doesn't declare) breaks a driver author on their very first command.
These tests exec the rendered source and isinstance-check the result against
the real protocol, so template drift fails here rather than in a consumer repo.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentix.drivers.chat import ChatDriver
from agentix.drivers.embedding import EmbeddingDriver
from agentix.drivers.speech import SttDriver
from agentixd.scaffold.driver_tpl import render_driver

#: (modality, expected protocol, expected verb)
_MODALITIES = [
    ("chat", ChatDriver, "complete"),
    ("embedding", EmbeddingDriver, "embed"),
    ("stt", SttDriver, "transcribe"),
]


def _load(modality: str) -> tuple[str, dict[str, Any]]:
    """Render a stub and exec it, returning (filename, module namespace)."""
    filename, source = render_driver("acme-thing", modality)
    namespace: dict[str, Any] = {"__name__": filename[:-3]}
    # exec (not import) so the generated source is exercised without writing
    # to disk; a bad import line raises right here.
    exec(compile(source, filename, "exec"), namespace)
    return filename, namespace


@pytest.mark.parametrize(("modality", "protocol", "verb"), _MODALITIES)
def test_rendered_stub_imports_and_conforms(modality: str, protocol: type, verb: str) -> None:
    """The regression: stubs used to import `agentix.drivers.types`, which
    has never existed, so every scaffolded driver died at import."""
    filename, ns = _load(modality)
    assert filename == "acme_thing.py"

    cls = ns["AcmeThingDriver"]
    assert ns["__all__"] == ["AcmeThingDriver"]

    driver = cls()
    assert isinstance(driver, protocol), f"{modality} stub does not satisfy {protocol.__name__}"
    assert hasattr(driver, verb)


@pytest.mark.parametrize(("modality", "protocol", "verb"), _MODALITIES)
def test_rendered_stub_descriptor_is_a_property(modality: str, protocol: type, verb: str) -> None:
    """`Driver.descriptor` is a property, not a class attribute."""
    _, ns = _load(modality)
    driver = ns["AcmeThingDriver"]()
    desc = driver.descriptor
    assert desc.name == "acme-thing"
    assert desc.type == "model"
    assert desc.modality == modality


@pytest.mark.parametrize(("modality", "protocol", "verb"), _MODALITIES)
def test_rendered_verb_raises_not_implemented(modality: str, protocol: type, verb: str) -> None:
    """The stub is a stub — the verb exists and tells you to implement it."""
    _, ns = _load(modality)
    driver = ns["AcmeThingDriver"]()
    with pytest.raises(NotImplementedError, match="AcmeThingDriver"):
        import asyncio

        asyncio.run(getattr(driver, verb)(None))


@pytest.mark.parametrize(("modality", "protocol", "verb"), _MODALITIES)
def test_rendered_stub_closes_cleanly(modality: str, protocol: type, verb: str) -> None:
    """`aclose()` is required by the base Driver protocol; the stub had none."""
    import asyncio

    _, ns = _load(modality)
    assert asyncio.run(ns["AcmeThingDriver"]().aclose()) is None


@pytest.mark.parametrize(("modality", "protocol", "verb"), _MODALITIES)
def test_rendered_stub_names_no_phantom_kernel_module(modality: str, protocol: type, verb: str) -> None:
    """Guard the exact defect: `agentix.drivers.types` is not a real module."""
    _, source = render_driver("acme-thing", modality)
    assert "agentix.drivers.types" not in source
    # The registration snippet must match DriverFactory's (spec, cfg) signature.
    assert "lambda spec, cfg:" in source
    assert "lambda spec, **kw" not in source


def test_spec_model_seeds_the_default() -> None:
    """Stubs accept the DriverSpec the factory hands them."""
    from agentix.config import DriverSpec

    _, ns = _load("chat")
    spec = DriverSpec(name="acme", driver="acme-thing", modality="chat", model="acme-1")
    assert ns["AcmeThingDriver"](spec=spec).default_model == "acme-1"


def test_unknown_modality_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown modality"):
        render_driver("acme-thing", "telepathy")
