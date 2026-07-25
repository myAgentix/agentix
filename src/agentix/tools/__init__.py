"""Agentix tool layer — the kernel ``Tool`` protocol, ``ToolContext``, and registry.

Apps register their own tools (and the kernel's generic primitives) against a
``ToolRegistry``. The context carries the three stores + optional app-supplied remote
clients; tools declare ``mutates_target`` + a ``verifier`` so the safety gate can enforce
verify-then-rollback. The driver midlayer (``primitives``/``resilience``) supplies the
shared mechanisms app tools compose — batching, fingerprinting, JSON-from-LLM
extraction, transient retry, timeout halving, failure bisection.
"""

from agentix.tools.base import (
    Tool,
    ToolContext,
    ToolSpec,
    elapsed_ms,
    ensure_input,
)
from agentix.tools.factory import FunctionTool, tool
from agentix.tools.primitives import (
    aggregate_by_key,
    batched,
    chunk,
    extract_json_object,
    fingerprint_dict,
)
from agentix.tools.memory_tools import (
    MemoryRecallInput,
    MemoryRecallOutput,
    MemoryRegistryListInput,
    MemoryRegistryListOutput,
    MemorySearchInput,
    MemorySearchOutput,
    MemoryStoreInput,
    MemoryStoreOutput,
    RegistryEntry,
    memory_recall,
    memory_registry_list,
    memory_search,
    memory_store,
)
from agentix.tools.record_attempt import RecordAttemptInput, RecordAttemptOutput, record_attempt
from agentix.tools.registry import ToolConflict, ToolRegistry
from agentix.tools.resilience import (
    HalvingExhausted,
    TransientRetry,
    bisect_on_failure,
    halve_on_timeout,
)

__all__ = [
    "FunctionTool",
    "HalvingExhausted",
    "MemoryRecallInput",
    "MemoryRecallOutput",
    "MemoryRegistryListInput",
    "MemoryRegistryListOutput",
    "MemorySearchInput",
    "MemorySearchOutput",
    "MemoryStoreInput",
    "MemoryStoreOutput",
    "RecordAttemptInput",
    "RecordAttemptOutput",
    "RegistryEntry",
    "Tool",
    "ToolConflict",
    "ToolContext",
    "ToolRegistry",
    "ToolSpec",
    "TransientRetry",
    "aggregate_by_key",
    "batched",
    "bisect_on_failure",
    "chunk",
    "elapsed_ms",
    "ensure_input",
    "extract_json_object",
    "fingerprint_dict",
    "halve_on_timeout",
    "memory_recall",
    "memory_registry_list",
    "memory_search",
    "memory_store",
    "record_attempt",
    "tool",
]
