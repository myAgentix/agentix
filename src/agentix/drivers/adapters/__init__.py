"""Concrete driver adapters — intrinsic (open-source infra) and OpenAI-compatible.

Import adapters directly by submodule path; this package deliberately re-exports
nothing. A package ``__init__`` runs before any submodule, so any re-export here
would execute an optional adapter's module-level SDK import (e.g. ``openai``,
``minio``) on every consumer — including intrinsic-only ones that never touch it.

    from agentix.drivers.adapters.intrinsic.huble import HubleChatDriver
    from agentix.drivers.adapters.vendor.nvidia import NvidiaChatDriver   # needs openai-compat

Intrinsic adapters ship with the kernel. Adapters speaking the OpenAI-compatible
wire (``nvidia``, ``melious``) need the ``openai-compat`` extra (that SDK is used
purely as an HTTP client):

    pip install agentix[openai-compat]

Commercial first-party providers (Anthropic, OpenAI, Groq, Grok, …) are NOT
shipped by the kernel. Supply them out-of-tree via seam #13 — either
``register_driver_factory("my-provider", …)`` or the dotted path
``DriverSpec(driver="my_pkg.drivers:MyChatDriver")``. See docs/drivers.md.
"""
