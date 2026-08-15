# Agentix Developer Cookbook

Copy-pasteable recipes for the most common integration patterns.
Each recipe is self-contained — pick the one that matches what you need.

> **Prerequisite**: a running `agentixd` daemon with at least one LLM driver
> configured in `~/.agentix/config.yaml`. See [quickstart.md](quickstart.md)
> for installation.

---

## Table of Contents

0. [End-to-end minimal agent](#0-end-to-end-minimal-agent)
1. [Your first turn (SDK)](#1-your-first-turn-sdk)
2. [Write a read-only tool (class-based)](#2-write-a-read-only-tool-class-based)
3. [Write a read-only tool (@tool decorator)](#3-write-a-read-only-tool-tool-decorator)
4. [Dependency injection for tools](#4-dependency-injection-for-tools)
5. [Write a mutating tool + verifier pair](#5-write-a-mutating-tool--verifier-pair)
6. [Implement rollback (SafetyGate subclass)](#6-implement-rollback-safetygate-subclass)
7. [Create a skill bundle](#7-create-a-skill-bundle)
8. [Package a plugin](#8-package-a-plugin)
9. [Add a driver tool surface](#9-add-a-driver-tool-surface)
10. [Wire sandbox allowlists](#10-wire-sandbox-allowlists)
11. [Forward events to your transport](#11-forward-events-to-your-transport)
12. [Verify your setup with the CLI](#12-verify-your-setup-with-the-cli)
13. [ToolContext cheat sheet](#13-toolcontext-cheat-sheet)
14. [Common mistakes (compliance gotchas)](#14-common-mistakes-compliance-gotchas)

---

## 0. End-to-end minimal agent

**When**: You're starting from scratch and want a complete working agent —
plugin, tool, skill, config, and SDK call — all in one place.

**Project layout**:

```
myagent/
  __init__.py
  plugin.py
  tools.py
  skills/
    greet/
      SKILL.md
run.py
```

**myagent/tools.py**:

```python
import time
from pydantic import BaseModel, Field
from agentix.tools.factory import tool
from agentix.tools.base import ToolContext, elapsed_ms

class GreetInput(BaseModel):
    name: str = Field(..., description="Who to greet")

class GreetOutput(BaseModel):
    message: str
    latency_ms: int = 0

@tool(mutates_target=False)
async def greet(params: GreetInput, ctx: ToolContext) -> GreetOutput:
    """Greet someone by name."""
    started = time.perf_counter_ns()
    return GreetOutput(
        message=f"Hello, {params.name}!",
        latency_ms=elapsed_ms(started),
    )
```

**myagent/skills/greet/SKILL.md**:

```markdown
---
name: greet
description: How to greet users appropriately
---

# Greet

When the user asks to be greeted, call the `greet` tool with their name.
If no name is given, use "World".
```

**myagent/plugin.py**:

```python
from pathlib import Path

def register(state, tool_registry):
    from myagent.tools import greet
    tool_registry.register(greet)

def skills_roots():
    return [str(Path(__file__).parent / "skills")]
```

**~/.agentix/config.yaml** (add your plugin):

```yaml
plugin_packages:
  - myagent
```

**run.py** (exercise it):

```python
import asyncio
from agentix_sdk.client import AgentixClient

async def main():
    async with AgentixClient() as client:
        session = await client.create_session(customer_id="demo")
        turn = await client.run_turn(session.id, message="Greet Alice")
        print(turn.content)

asyncio.run(main())
```

Start the daemon (`agentixd`), then run `python run.py`. The agent will see
the `greet` tool, consult the skill if needed, and call the tool.

**What's happening**: The plugin registers one tool and one skill root at
daemon startup. The SDK creates a session and sends a message. The kernel's
dispatcher sees `greet` in the tool menu, the LLM decides to call it, and
the result flows back as the turn content. That's the full loop.

---

## 1. Your first turn (SDK)

**When**: You want to talk to the kernel from your app without embedding it.

```python
import asyncio
from agentix_sdk.client import AgentixClient

async def main():
    async with AgentixClient() as client:
        session = await client.create_session(customer_id="acme")
        turn = await client.run_turn(session.id, message="What tools do you have?")
        print(turn.content)

asyncio.run(main())
```

**What's happening**: `AgentixClient` connects to the daemon over a Unix socket
(`~/.agentix/agentixd.sock`). `create_session` opens a resumable session;
`run_turn` sends a message through the full middleware chain (trajectory,
cost tracking, budget, safety gate) and returns the assistant's response.

**See also**: [quickstart.md](quickstart.md) — full SDK method reference.

---

## 2. Write a read-only tool (class-based)

**When**: You need full control over tool attributes, or your tool has complex
initialisation logic.

```python
import time
from pydantic import BaseModel, Field
from agentix.tools.base import Tool, ToolContext, elapsed_ms, ensure_input

class LookupOrderInput(BaseModel):
    order_id: str = Field(..., description="The order ID to look up")

class LookupOrderOutput(BaseModel):
    order_id: str
    status: str
    total: float
    latency_ms: int = 0

class LookupOrder(Tool):
    name = "lookup_order"
    description = "Look up an order by ID and return its status and total."
    input_schema = LookupOrderInput
    output_schema = LookupOrderOutput
    mutates_target = False
    verifier: str | None = None

    async def call(self, input: BaseModel, ctx: ToolContext) -> BaseModel:
        params = ensure_input(input, LookupOrderInput)
        started = time.perf_counter_ns()

        # Your domain logic here
        order = {"status": "shipped", "total": 42.50}

        return LookupOrderOutput(
            order_id=params.order_id,
            status=order["status"],
            total=order["total"],
            latency_ms=elapsed_ms(started),
        )
```

Register it in your plugin (see [Recipe 8](#8-package-a-plugin)):

```python
from agentix.tools.registry import ToolRegistry

registry = ToolRegistry()
registry.register(LookupOrder())
```

**What's happening**: The `Tool` protocol requires `name`, `description`,
`input_schema`, `output_schema`, `mutates_target`, `verifier`, and an async
`call()`. The registry, dispatcher, and safety gate all work through this
interface. `ensure_input` handles model coercion; `elapsed_ms` computes
latency from a `perf_counter_ns` mark.

**See also**: [tools.md](tools.md) §1–2 — full tool protocol.

---

## 3. Write a read-only tool (@tool decorator)

**When**: Your tool is a single async function with no complex init.

```python
import time
from pydantic import BaseModel, Field
from agentix.tools.factory import tool
from agentix.tools.base import ToolContext, elapsed_ms

class GreetInput(BaseModel):
    name: str = Field(..., description="Who to greet")

class GreetOutput(BaseModel):
    message: str
    latency_ms: int = 0

@tool(mutates_target=False)
async def greet(params: GreetInput, ctx: ToolContext) -> GreetOutput:
    """Greet someone by name."""
    started = time.perf_counter_ns()
    return GreetOutput(
        message=f"Hello, {params.name}!",
        latency_ms=elapsed_ms(started),
    )
```

The decorator infers `name` from the function name (`"greet"`), `description`
from the docstring, and I/O schemas from the type hints. Override any of them
explicitly:

```python
@tool(
    name="greet_user",
    description="Explicit description for the LLM",
    mutates_target=False,
    advertised=True,           # True = in LLM menu (default)
    required_provider=None,    # None = always available (default)
)
async def greet(params: GreetInput, ctx: ToolContext) -> GreetOutput:
    ...
```

**What's happening**: `@tool` returns a `FunctionTool` instance that satisfies
the `Tool` protocol. The registry, dispatcher, and safety gate treat it
identically to a class-based tool.

**See also**: [tools.md](tools.md) §2 — declarative construction.

---

## 4. Dependency injection for tools

**When**: Your tool needs a database client, API handle, or other dependency
that shouldn't be a global.

```python
from agentix.tools.factory import tool, FunctionTool
from agentix.tools.base import ToolContext

def build_search_tool(search_client) -> FunctionTool:
    @tool(name="search_docs", description="Search the docs index", mutates_target=False)
    async def _search(params: SearchInput, ctx: ToolContext) -> SearchOutput:
        # closes over search_client
        results = await search_client.query(params.query)
        return SearchOutput(hits=results)
    return _search

# In your plugin's register():
search_tool = build_search_tool(my_search_client)
tool_registry.register(search_tool)
```

**What's happening**: The inner function closes over the dependency. The builder
returns a `FunctionTool` instance. This is the standard pattern for tools
that need handles, providers, or configuration not available via `ToolContext`.

---

## 5. Write a mutating tool + verifier pair

**When**: Your tool changes external state (writes to a database, calls a
mutating API) and you want the safety gate to verify and rollback on drift.

```python
import time
from pydantic import BaseModel, Field
from agentix.tools.factory import tool
from agentix.tools.base import ToolContext, elapsed_ms

# ── The mutating tool ──

class CreateRecordInput(BaseModel):
    model: str = Field(..., description="Target model name")
    values: dict = Field(..., description="Field values to set")

class CreateRecordOutput(BaseModel):
    record_id: int
    model: str
    verify_scope: list[int]     # IDs for the verifier to check
    latency_ms: int = 0

# In practice, inject my_api using the builder pattern from Recipe 4:
#   def build_create_record(my_api) -> FunctionTool: ...

@tool(
    name="create_record",
    description="Create a record in the target system",
    mutates_target=True,
    verifier="verify_create",   # Required — must name a registered tool
)
async def create_record(params: CreateRecordInput, ctx: ToolContext) -> CreateRecordOutput:
    """Create a record and return its ID."""
    started = time.perf_counter_ns()
    record_id = await my_api.create(params.model, params.values)
    return CreateRecordOutput(
        record_id=record_id,
        model=params.model,
        verify_scope=[record_id],
        latency_ms=elapsed_ms(started),
    )

# ── The verifier ──

class VerifyCreateInput(BaseModel):
    model: str
    batch_scope: list[int] = Field(default_factory=list)

class VerifyCreateOutput(BaseModel):
    ok: bool
    findings: list[str] = Field(default_factory=list)

@tool(name="verify_create", description="Verify created records exist", mutates_target=False)
async def verify_create(params: VerifyCreateInput, ctx: ToolContext) -> VerifyCreateOutput:
    """Check that the created records actually exist."""
    missing = []
    for rid in params.batch_scope:
        if not await my_api.exists(params.model, rid):
            missing.append(f"record {rid} not found in {params.model}")
    return VerifyCreateOutput(ok=not missing, findings=missing)
```

**What's happening**: After `create_record` runs, the safety gate automatically:
1. Builds the verifier input by forwarding same-named fields (`model`) and
   `verify_scope` → `batch_scope`.
2. Calls `verify_create`.
3. If `ok=False`, calls your `SafetyGate.rollback()` (see Recipe 6), then
   raises `SafetyVerifyFailed`.

If `verify_scope` is an empty list `[]`, the safety gate skips verification
(the tool declared it mutated nothing this call).

**Error handling**: When a tool raises an exception, the dispatcher catches it
and records the error in the turn result — the agent sees the traceback and
can retry or adjust. You don't need to catch and wrap errors yourself. Just
let domain exceptions propagate naturally:

```python
async def call(self, input: BaseModel, ctx: ToolContext) -> BaseModel:
    params = ensure_input(input, LookupOrderInput)
    order = await my_api.get(params.order_id)
    if order is None:
        raise ValueError(f"order {params.order_id!r} not found")  # agent sees this
    return LookupOrderOutput(...)
```

The safety gate raises its own exceptions — you never need to catch these
in tool code:

| Exception | When | What happens |
|-----------|------|--------------|
| `SafetyGateBlocked` | `ctx.dry_run=True` and tool has `mutates_target=True` | Mutation blocked; agent informed |
| `SafetyInvariantViolated` | Mutating tool has no `verifier` declared | Startup / dispatch error |
| `SafetyVerifyFailed` | Verifier returned `ok=False` | `rollback()` already ran; agent informed |

**See also**: [tools.md](tools.md) §6 — safety gate flow; [seams.md](seams.md) §2.

---

## 6. Implement rollback (SafetyGate subclass)

**When**: Your app has mutating tools and needs to define how to undo them
when verification fails.

```python
from agentix.tools.safety import SafetyGate
from agentix.tools.base import Tool, ToolContext
from pydantic import BaseModel

class MyAppSafetyGate(SafetyGate):
    async def rollback(
        self,
        ctx: ToolContext,
        *,
        tool: Tool,
        input: BaseModel,
        model: str | None,
    ) -> None:
        """Undo a failed mutation. Called automatically by the gate."""
        if tool.name == "create_record":
            # input matches CreateRecordInput from Recipe 5 (model, values)
            target_model = getattr(input, "model", None)
            if target_model and model:
                await my_api.delete(target_model, model)  # model = the affected record

    def _resolve_contract(self, ctx, model):
        # Optional: 100% audit for critical models
        if model in {"account.move", "sale.order"}:
            return ({"count": None, "sample": None}, [])
        return (None, [])  # default: count + sample

    def _derive_verifier_fields(self, source, target_fields):
        # Optional: map field names between tool input and verifier input
        if "target_model" in target_fields and "model" in source:
            return {"target_model": source["model"]}
        return {}
```

Wire it into your plugin:

```python
def register(state, tool_registry):
    from agentix.storage import SqliteStore
    state.safety_gate = MyAppSafetyGate(sqlite=state.sqlite)
```

**What's happening**: The base `SafetyGate` owns the verify-then-rollback
flow. You override three hooks:
- `rollback()` (required if you have mutating tools) — domain-specific undo.
- `_resolve_contract()` (optional) — per-model verification contracts.
- `_derive_verifier_fields()` (optional) — field-name mapping between tool and verifier inputs.

**See also**: [seams.md](seams.md) §2 — safety gate hooks.

---

## 7. Create a skill bundle

**When**: You want to package procedural know-how (multi-step recipes,
domain procedures) that the agent can consult at runtime.

Directory layout:

```
skills/
  check-inventory/
    SKILL.md          # Required: frontmatter + procedure body
    tool.py           # Optional: skill-scoped tools
    resources/        # Optional: files referenced by the procedure
```

**SKILL.md**:

```markdown
---
name: check-inventory
description: How to check and reconcile inventory levels
allowed-tools:
  - lookup_order
  - read_file
---

# Check Inventory

Use this procedure when the user asks about stock levels or discrepancies.

## Steps

1. Call `lookup_order` with the order ID to get current status
2. Call `read_file` on the warehouse manifest at `data/manifest.csv`
3. Compare quantities — report any mismatches
4. If mismatches found, list them with expected vs actual values
```

**tool.py** (optional — skill-scoped tools):

```python
from agentix.tools.registry import ToolRegistry
from agentix.tools.factory import tool
from agentix.tools.base import ToolContext
from pydantic import BaseModel

class ReconcileInput(BaseModel):
    order_id: str

class ReconcileOutput(BaseModel):
    matched: bool
    discrepancies: list[str]

@tool(name="reconcile_inventory", description="Compare order vs warehouse", mutates_target=False)
async def reconcile_inventory(params: ReconcileInput, ctx: ToolContext) -> ReconcileOutput:
    """Skill-scoped tool — only available when check-inventory is activated."""
    return ReconcileOutput(matched=True, discrepancies=[])

def register(registry: ToolRegistry) -> None:
    """Called when the skill is activated via SkillCatalog.activate()."""
    registry.try_register(reconcile_inventory)
```

The agent consults the skill at runtime via the built-in `consult_skill` tool,
which reads the `SKILL.md` body.

**What's happening**: Skills are progressive-disclosure knowledge bundles.
At session start, the catalog cheaply surfaces `(name, description)` pairs.
When the agent needs a procedure, it calls `consult_skill("check-inventory")`
to load the full body. If the skill has a `tool.py`, its tools are registered
when the skill is activated.

**See also**: [skills.md](skills.md) — full skill standard.

---

## 8. Package a plugin

**When**: You want to ship tools, skills, drivers, and hooks as a single
installable package for the daemon.

**File**: `myagent/plugin.py`

```python
from __future__ import annotations
from pathlib import Path

def register(state, tool_registry) -> None:
    """Called at daemon startup after drivers + builtins are loaded."""

    # 1. Register tools
    from myagent.tools import LookupOrder, create_record, verify_create
    tool_registry.register(LookupOrder())
    tool_registry.register(create_record)       # FunctionTool from @tool
    tool_registry.register(verify_create)

    # 2. Set per-session engine factory (custom middleware chain)
    state._session_engine_factory = _make_engine

    # 3. Set pre-turn hook (per-turn setup/teardown)
    state._pre_turn_hook = _pre_turn_hook

def skills_roots() -> list[str]:
    """Return skill directories to merge into the catalog."""
    return [str(Path(__file__).parent / "skills")]

def _make_engine(state, session, app_meta):
    from agentix.core.engine import Engine
    return Engine(
        sqlite=state.sqlite,
        minio=state.minio,
        middlewares=[],   # add your middleware layers here
        dispatcher=state.dispatcher,
    )

def _pre_turn_hook(state, session):
    """Return an async context manager — the daemon calls ``async with hook(state, session):``."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        # Setup: open connections, populate session extras
        state._session_extras[session.id] = {"dry_run": False}
        try:
            yield
        finally:
            # Teardown: close connections, clean up
            state._session_extras.pop(session.id, None)

    return _ctx()
```

**Config**: declare the plugin in `~/.agentix/config.yaml`:

```yaml
plugin_packages:
  - myagent
```

**What's happening**: At daemon startup, the kernel imports `myagent.plugin`,
runs an AST compliance check (no kernel-internal imports, no direct LLM
calls, tools implement the protocol), then calls `register()`. Your plugin
wires tools, hooks, and skill roots through the declared seams — nothing else
is needed.

**See also**: [plugins.md](plugins.md) — plugin contract and compliance rules.

---

## 9. Add a driver tool surface

**When**: Your integration driver owns a set of tools that should only appear
in the LLM menu when the driver is active (leased for the current session).

```python
from agentix.tools.surface import SimpleToolSurface, qualify
from agentix.tools.factory import tool
from agentix.tools.base import ToolContext
from pydantic import BaseModel

# Tools MUST be named <namespace>__<local_name>

class DetectVersionInput(BaseModel):
    pass

class DetectVersionOutput(BaseModel):
    version: str

@tool(name="erp__detect_version", description="Detect the ERP version", mutates_target=False)
async def detect_version(params: DetectVersionInput, ctx: ToolContext) -> DetectVersionOutput:
    """Detect the version of the connected ERP system."""
    return DetectVersionOutput(version="17.0")


class ErpToolSurface(SimpleToolSurface):
    """Only advertises tools when the ERP driver is leased."""

    def __init__(self, driver_registry):
        self._driver_registry = driver_registry
        super().__init__(
            namespace="erp",
            tools=[detect_version],
        )

    def active(self) -> bool:
        try:
            self._driver_registry.leased("erp-target")
            return True
        except KeyError:
            return False
```

Mount it in your plugin:

```python
def register(state, tool_registry):
    surface = ErpToolSurface(driver_registry=state.registry)
    tool_registry.mount(surface)
```

**What's happening**: `SimpleToolSurface` validates at construction that every
tool name carries the namespace prefix. When `active()` returns `False`, the
tools vanish from the LLM menu but stay resolvable by exact name (for
verifiers and compiled recipes). The registry routes `erp__detect_version`
lookups to the surface automatically.

**See also**: [tools.md](tools.md) §5 — driver surfaces (spec 001).

---

## 10. Wire sandbox allowlists

**When**: Your agent needs to fetch URLs beyond the kernel defaults
(`github.com`, `raw.githubusercontent.com`), run binaries beyond the defaults
(`python`, `pytest`, `ruff`, etc.), or commit under a custom git identity.

```python
from agentix.tools.spike.web_fetch import register_allowed_hosts
from agentix.tools.spike.run_command import register_allowed_binaries
from agentix.tools.spike.git_ops import register_agent_git_identity, AgentGitIdentity

# In your plugin's register():
def register(state, tool_registry):
    # Allow fetching from your API docs
    register_allowed_hosts(["docs.mycompany.com", "api.mycompany.com"])

    # Allow running your CLI tool
    register_allowed_binaries(["mycompany-cli", "node"])

    # Set agent git identity for commits
    register_agent_git_identity(AgentGitIdentity(
        branch_prefix="myagent/work-",
        name="my-agent",
        email_domain="mycompany.com",
    ))
```

**What's happening**: The kernel ships restrictive defaults — the agent can
only fetch from code-hosting sites and run generic verifier binaries. These
seams let your app extend the sandbox without modifying kernel code. The
allowlists are module-level sets; `register_*` calls extend them at startup.

**See also**: [seams.md](seams.md) §7 — sandbox allowlists.

---

## 11. Forward events to your transport

**When**: You want to stream session lifecycle events (turn started, tool
called, cost recorded, etc.) to your own infrastructure (NATS, Kafka,
webhooks).

```python
from agentix.events import bus, SessionEvent

async def forward_to_nats(event: SessionEvent) -> None:
    """Global sink — receives every event from every session."""
    await nats_client.publish(
        subject=f"agentix.sessions.{event.session_id}",
        payload=event.model_dump_json().encode(),
    )

# In your plugin's register():
def register(state, tool_registry):
    bus.add_sink(forward_to_nats)
```

**What's happening**: The kernel publishes `SessionEvent` objects (frozen
Pydantic models with `session_id`, `type`, `payload`, `at`, `schema_version`)
onto the in-process bus. Global sinks see every event regardless of session.
The bus is fire-and-forget — events are ephemeral, not persisted.

**Event types** (`agentix.event_types.EventType`):

| Type | When emitted |
|------|-------------|
| `session_started` | Session created |
| `session_end` | Session completed |
| `turn_started` | Turn begins (user message received) |
| `turn_completed` | Turn ends (assistant response ready) |
| `job_started` | Job begins (a session decomposes into N jobs) |
| `job_completed` | Job ends successfully |
| `job_failed` | Job ends with an error |
| `model_started` | Per-model processing begins |
| `model_completed` | Per-model processing ends |
| `safety_event` | Safety gate action (dry-run block, verify fail, rollback) |
| `verify_stage` | Per-rung verification progress |
| `checkpoint_requested` | Operator review milestone (reserved) |

**See also**: [seams.md](seams.md) §11 — events out.

---

## 12. Verify your setup with the CLI

**When**: After wiring your plugin, tools, or skills, confirm everything
loaded correctly.

```bash
# Check config is valid
agentix config validate

# List registered tools (flat + surfaces)
agentix tool list

# List available skills
agentix skill list

# Show a specific skill's body
agentix skill show check-inventory

# Check driver status
agentix driver list

# Inspect a session
agentix session list
agentix session status <session-id>

# Full system status
agentix status
```

If a tool or skill doesn't appear, check the daemon logs — the compliance
checker logs warnings for skipped registrations (conflicts, missing verifiers,
protocol violations).

---

## 13. ToolContext cheat sheet

**When**: You're writing a tool and want to know what's available on `ctx`.

Every tool receives a `ToolContext` instance as its second argument. Here's
what you can do with it:

```python
from agentix.tools.base import ToolContext

async def call(self, input: BaseModel, ctx: ToolContext) -> BaseModel:
    # ── Session state ──
    ctx.session.id               # current session ID
    ctx.session.customer_id      # who this session belongs to
    ctx.session.app_meta         # opaque app-specific metadata dict

    # ── Progress reporting ──
    await ctx.progress(percent=0.5, message="halfway done")
    # Writes to tool_progress table; best-effort, never throws

    # ── Storage ──
    ctx.sqlite                   # SqliteStore — operational DB
    ctx.minio                    # MinioStore — blob checkpoints
    ctx.memory                   # MemoryStore — episodic pages + learnings

    # ── Memory (store findings for future turns) ──
    await ctx.memory.write_section(
        path="ep/discoveries.md",
        section="New finding",
        body="The API returns dates in ISO 8601 format",
    )

    # ── Look up other tools ──
    if ctx.registry:
        other = ctx.registry.get("some_tool")
        result = await other.call(some_input, ctx)

    # ── Embeddings (semantic search) ──
    if ctx.embeddings:
        vectors = await ctx.embeddings.embed(["search query"])

    # ── Flags ──
    ctx.dry_run                  # True = mutations blocked (safety gate)
    ctx.activated_skill_names    # skills activated for this session
    ctx.skills_root              # str | list[str] — skill catalog roots
```

**See also**: `src/agentix/tools/base.py:50` — `ToolContext` definition.

---

## 14. Common mistakes (compliance gotchas)

**When**: Your plugin fails to load, or the daemon refuses to start.

The kernel runs an AST compliance scan on every plugin at startup
(`agentix.compliance.enforce_plugin_compliance`). Here are the rules and
how to avoid breaking them:

### 1. No shadowing kernel classes

```python
# BAD — redefines a kernel concept
class Session:
    pass

class ToolContext:
    pass
```

Forbidden class names: `Session`, `Turn`, `WorkingMemory`, `ToolContext`,
`ToolRegistry`, `SkillCatalog`, `Dispatcher`, `KernelState`, `MemoryRegistry`.

**Fix**: Import and use the kernel's class instead of redefining it.

### 2. Tools must use the kernel protocol

```python
# BAD — async call() without importing agentix.tools
class MyTool:
    async def call(self, input, ctx):
        ...
```

**Fix**: Import from `agentix.tools.base` or use `@tool` from `agentix.tools.factory`.

### 3. No raw file writes in memory modules

```python
# BAD — in any file with "memory" in the path
open("memories/data.md", "w").write(...)
Path("memories/data.md").write_text(...)
```

**Fix**: Use `ctx.memory.write_section()` instead.

### 4. No private kernel imports

```python
# BAD — underscore-prefixed internal modules
from agentix.core._internal import something
from agentix.storage._engine import pool
```

Allowed exceptions: `agentix.storage.memory`, `agentix.storage.registry`,
`agentix.core.middleware`.

**Fix**: Use the public API surface only.

### 5. No direct LLM access

```python
# BAD — the kernel is the sole LLM caller
import openai
import anthropic
from agentix.drivers.chat import ChatDriver
```

This includes all provider SDKs (`openai`, `anthropic`, `mistralai`, `cohere`,
`ollama`, `litellm`, `transformers`, `google.generativeai`) and the kernel's
chat adapters.

**Fix**: The kernel calls the LLM for you. Your plugin supplies tools; the
dispatcher handles prompting.

### 6. Skills must have SKILL.md

Every directory under your `skills/` root must contain a `SKILL.md` file
(warning severity — won't block startup, but the skill won't load).

### 7. Plugin must define `register(state, tool_registry)`

The `plugin.py` file must have a module-level `register` function with at
least 2 parameters. Without it, the daemon crashes with `AttributeError` at
startup.

**Run compliance in CI**: Call `check_driver_compliance(src_root)` from
`agentix.compliance` in your own test suite to catch violations before
deployment.

---

## Quick reference: what goes where

| I want to...                        | Seam    | Recipe |
|-------------------------------------|---------|--------|
| See a complete working agent        | All     | [0](#0-end-to-end-minimal-agent) |
| Talk to the kernel from my app      | SDK     | [1](#1-your-first-turn-sdk) |
| Give the agent a new capability     | Tool    | [2](#2-write-a-read-only-tool-class-based), [3](#3-write-a-read-only-tool-tool-decorator) |
| Inject deps into a tool             | Tool    | [4](#4-dependency-injection-for-tools) |
| Safely mutate external systems      | Safety  | [5](#5-write-a-mutating-tool--verifier-pair), [6](#6-implement-rollback-safetygate-subclass) |
| Teach the agent a procedure         | Skill   | [7](#7-create-a-skill-bundle) |
| Package everything for the daemon   | Plugin  | [8](#8-package-a-plugin) |
| Namespace tools under a driver      | Surface | [9](#9-add-a-driver-tool-surface) |
| Extend the sandbox                  | Sandbox | [10](#10-wire-sandbox-allowlists) |
| Stream events out                   | Events  | [11](#11-forward-events-to-your-transport) |
| Verify the setup                    | CLI     | [12](#12-verify-your-setup-with-the-cli) |
| Use ToolContext effectively         | Tool    | [13](#13-toolcontext-cheat-sheet) |
| Debug compliance failures           | Plugin  | [14](#14-common-mistakes-compliance-gotchas) |
