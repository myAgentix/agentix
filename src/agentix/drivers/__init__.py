"""agentix.drivers — the kernel's abstraction for external-system I/O.

Public surface grows per phase of the v0.5 re-founding; import from here,
not from submodules. Canonical doc: ``docs/drivers.md``.
"""

from agentix.drivers.base import (
    KNOWN_MODALITIES,
    KNOWN_SOURCES,
    Driver,
    DriverDescriptor,
    DriverError,
    DriverInvalidRequest,
    DriverRateLimited,
    DriverUnavailable,
)
from agentix.drivers.chat import (
    ChatDriver,
    ChatRequest,
    ChatResponse,
    ToolSpec,
    tool_to_spec,
)
from agentix.drivers.cost import CostRecordingChatDriver
from agentix.drivers.embedding import (
    CachedEmbeddingDriver,
    EmbeddingCache,
    EmbeddingDriver,
    EmbeddingError,
    EmbeddingResult,
    HubleEmbeddingDriver,
)
from agentix.drivers.factory import (
    build_drivers,
    register_credentialed_factory,
    register_driver_factory,
)
from agentix.drivers.file_store import FileStoreDriver
from agentix.drivers.limiter import (
    configure_driver_capacity,
    current_limit,
    driver_capacity,
)
from agentix.drivers.object_store import ObjectNotFound, ObjectStoreDriver
from agentix.drivers.registry import DriverConflict, DriverLease, DriverRegistry
from agentix.drivers.relational import ExecuteResult, RelationalDriver
from agentix.drivers.router import (
    ChatFailoverChain,
    FailoverCallback,
    NoDriversAvailable,
)
from agentix.drivers.session import (
    bind_session,
    bind_turn,
    current_session_id,
    current_turn_id,
    session_scope,
    unbind_session,
    unbind_turn,
)
from agentix.drivers.speech import AudioSource, SttDriver, Transcript

__all__ = [
    "KNOWN_MODALITIES",
    "KNOWN_SOURCES",
    "AudioSource",
    "CachedEmbeddingDriver",
    "ChatDriver",
    "ChatFailoverChain",
    "ChatRequest",
    "ChatResponse",
    "CostRecordingChatDriver",
    "Driver",
    "DriverConflict",
    "DriverDescriptor",
    "DriverError",
    "DriverInvalidRequest",
    "DriverLease",
    "DriverRateLimited",
    "DriverRegistry",
    "DriverUnavailable",
    "EmbeddingCache",
    "EmbeddingDriver",
    "EmbeddingError",
    "EmbeddingResult",
    "ExecuteResult",
    "FailoverCallback",
    "FileStoreDriver",
    "HubleEmbeddingDriver",
    "NoDriversAvailable",
    "ObjectNotFound",
    "ObjectStoreDriver",
    "RelationalDriver",
    "SttDriver",
    "ToolSpec",
    "Transcript",
    "bind_session",
    "bind_turn",
    "build_drivers",
    "configure_driver_capacity",
    "current_limit",
    "current_session_id",
    "current_turn_id",
    "driver_capacity",
    "register_credentialed_factory",
    "register_driver_factory",
    "session_scope",
    "tool_to_spec",
    "unbind_session",
    "unbind_turn",
]
