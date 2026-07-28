"""NVIDIA NIM provider — OpenAI-compatible adapter for NVIDIA Inference Microservices.

NVIDIA NIM exposes an OpenAI-compatible endpoint at
``https://integrate.api.nvidia.com/v1``. This adapter points
``OpenAIChatDriver`` at that endpoint so any NIM-hosted model
(Llama, Mistral, Nemotron, etc.) is reachable without new machinery.

Auth: ``NVIDIA_API_KEY`` env var or explicit ``api_key``.
ToS: https://www.nvidia.com/en-us/data-center/products/ai-enterprise/eula/
     consumer must accept independently.
"""

from __future__ import annotations

import os

from agentix.drivers.adapters.vendor.openai_compat import OpenAIChatDriver
from agentix.drivers.base import DriverDescriptor, DriverInvalidRequest

__all__ = ["NvidiaChatDriver"]

_NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
_DEFAULT_MODEL = "meta/llama-3.3-70b-instruct"


class NvidiaChatDriver(OpenAIChatDriver):
    """NVIDIA NIM chat via the OpenAI-compatible endpoint."""

    name = "nvidia"

    @property
    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            name=self.name,
            type="model",
            modality="chat",
            source="api",
            capabilities=frozenset({"tools"}),
            default_model=self.default_model,
            pricing_ref=self.default_model,
        )

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 300.0,
        base_url: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("NVIDIA_API_KEY")
        if not key:
            raise DriverInvalidRequest(
                "no NVIDIA API key (set NVIDIA_API_KEY or pass api_key)",
                driver=self.name,
            )
        super().__init__(
            api_key=key,
            model=model or _DEFAULT_MODEL,
            timeout_seconds=timeout_seconds,
            base_url=base_url or _NVIDIA_NIM_BASE_URL,
        )
