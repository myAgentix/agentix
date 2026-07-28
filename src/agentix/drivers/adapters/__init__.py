"""Concrete driver adapters — intrinsic (open-source infra) and OpenAI-compatible.

Intrinsic adapters ship with the kernel; adapters speaking the OpenAI-compatible
wire need the ``openai-compat`` extra (that SDK is used purely as an HTTP client):

    pip install agentix[openai-compat]   # nvidia, melious

Commercial first-party providers (Anthropic, OpenAI, Groq, Grok, …) are NOT
shipped by the kernel. Supply them out-of-tree via seam #13 — either
``register_driver_factory("my-provider", …)`` or the dotted path
``DriverSpec(driver="my_pkg.drivers:MyChatDriver")``. See docs/drivers.md.
"""

from agentix.drivers.adapters.intrinsic.huble import HubleChatDriver
from agentix.drivers.adapters.vendor.melious import MeliousChatDriver
from agentix.drivers.adapters.vendor.nvidia import NvidiaChatDriver
from agentix.drivers.adapters.vendor.openai_compat import OpenAIChatDriver

__all__ = [
    "HubleChatDriver",
    "MeliousChatDriver",
    "NvidiaChatDriver",
    "OpenAIChatDriver",
]
