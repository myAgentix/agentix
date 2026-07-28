# Making LLM calls

**Status:** living doc · **Scope:** Agentix kernel `[K]` (app-agnostic)

**Single source of truth for *calling* models in `docs/` — the app/agent-programmer's
view.** How you obtain a driver, issue a chat call, target a model, and what happens on
failure. The *driver framework itself* (how a driver is built, the families, seam #13) is
[`drivers.md`](drivers.md); the *routing-policy direction* (choosing a model by cost /
capability) is [`routing.md`](routing.md); cost recording is [`budgets.md`](budgets.md);
config keys are [`kernel-config-reference.md`](kernel-config-reference.md). Neighbouring
SSoTs are referenced, never restated (CRIE rule).

**One rule:** never import a vendor SDK or hardcode a provider in a tool or agent. You call
one canonical verb on a driver you get from the registry; the kernel resolves the provider.

---

## 1. The one call

Every LLM call is the same two steps:

```python
driver = registry.chat()                         # get the default chat driver
response = await driver.complete(request)         # ChatRequest -> ChatResponse
```

- **Get a driver** — the registry's typed accessors (`drivers/registry.py`): `chat(name=None)`,
  `embedding(name=None)` / `embedding_or_none()`, `stt(name=None)`. With no `name` you get the
  **default** for that modality (declaration order, or `default=True`); pass a name to select a
  specific configured instance (§3). Accessors are **pure lookup, not routing policy**.
- **Call it** — `await driver.complete(ChatRequest) -> ChatResponse`. That is the whole chat
  verb (`drivers/chat.py`). Embedding drivers expose `embed(list[str])`, stt `transcribe(AudioSource)`.

**Agents don't call `complete()` themselves.** An agent turn runs through the
`AgentDispatcher`, which holds one `ChatDriver` (`driver=` kwarg) and calls `complete()` in the
tool-loop, building the request from its `request_defaults` template + assembled context + the
turn's tool specs (`core/agent_dispatcher.py`; engine wiring in [`engine.md`](engine.md)). App
code that talks to the daemon uses the SDK (`AgentixClient`,
[`contracts-consumer-guide.md`](contracts-consumer-guide.md)) — same contract, no second path.

## 2. The wire contract — `ChatRequest` / `ChatResponse`

Canonical pydantic types in `drivers/chat.py` (`extra="forbid"` — unknown fields raise). The
fields you set:

| `ChatRequest` field | Default | Purpose |
|---|---|---|
| `messages: list[Message]` | — | the conversation |
| `model: str \| None` | driver default | **which model** for this call (§3) |
| `max_tokens: int` | `16_384` | output budget — generous by design (a stingy default truncates structured output mid-JSON; spend control is the TokenBudget middleware, not truncation) |
| `temperature: float` | `1.0` | sampling |
| `reasoning_effort` | `None` | `"low"`/`"medium"`/`"high"` — reasoning depth |
| `thinking_enabled` / `thinking_budget_tokens` | `False` / `None` | extended thinking |
| `cache_control` | `False` | prompt caching where supported |
| `stop_sequences` | `None` | stop strings |
| `tools: list[ToolSpec]` / `tool_choice` | `None` | tool-use (`"auto"`/`"any"`/`"none"`) |
| `extra_params: dict` | `{}` | vendor passthrough |

Vendor-feature knobs are **best-effort**: an adapter that doesn't support one silently ignores it.

`ChatResponse`: `content: str`, `usage: TokenUsage`, `model: str` (the model that actually
answered), `finish_reason`, `tool_calls: list[ToolCall]` (non-empty ⇒ the dispatcher loops),
`raw: dict`.

**Errors** you must handle map into one taxonomy (`drivers/base.py`, canonical in
[`drivers.md`](drivers.md) §1): `DriverError(*, driver, retryable)` → `DriverRateLimited` /
`DriverUnavailable` (retryable) vs `DriverInvalidRequest` (not). Adapters classify once; you
branch on `.retryable`. Never catch raw vendor SDK exceptions — they don't cross the seam.

## 3. Targeting a model per inferencing need

Composable levers, most local first:

1. **Per-call model** — set `ChatRequest.model` (driver default when unset). Every adapter
   honours it. The primary lever.
2. **Per-call inference knobs** — `reasoning_effort`, `thinking_enabled`/`thinking_budget_tokens`
   for depth; `max_tokens` + `temperature` for latency/cost. A deep-reasoning step sets
   `reasoning_effort="high"`; a latency-sensitive classify sets `"low"` + a small `max_tokens`.
3. **Named driver by role** — register several `DriverSpec`s and select with `registry.chat("<name>")`.
   This routes *by inferencing need* at the driver level:
   ```yaml
   drivers:
     - {name: fast,     driver: melious, model: deepseek-v4-flash, default: true}
     - {name: reasoner, driver: melious, model: deepseek-v4-pro}
   ```
   ```python
   await registry.chat("reasoner").complete(request)
   ```
4. **Per-agent default** — an `AgentDispatcher` is built with a `request_defaults` `ChatRequest`
   (model + knobs), pinning an agent to a model.
5. **Discovery** — before you pin a `model:`, list what a provider actually serves:
   `agentix model list <provider>` (CLI) or `ChatDriver.list_models()` (code). See providers
   with `agentix driver providers`.

`DriverSpec` fields (`model`, `base_url`, `api_key_env` — the env-var *name*, never the secret)
are canonical in [`drivers.md`](drivers.md) §6 / [`kernel-config-reference.md`](kernel-config-reference.md).

## 4. Failover behaviour

When several chat specs are configured they compose into a `ChatFailoverChain`
(`drivers/router.py`) that is itself a `ChatDriver` — callers never know if they hold one
adapter or a chain.

- Tries drivers **in order; first success wins**.
- Fails over only on **retryable** errors (`DriverRateLimited`, `DriverUnavailable`);
  `DriverInvalidRequest` **re-raises immediately** (a malformed request won't improve on the
  next driver).
- Every hop can notify an async `FailoverCallback` `(failed, next, error)`; callback failures
  are swallowed (observability never takes down dispatch); not fired on the last attempt.
- Whole chain exhausted ⇒ `NoDriversAvailable(attempts)` where `attempts` is
  `list[(driver_name, error_str)]`.

Model-call concurrency is bounded by one process semaphore (`driver_capacity()`, default 8;
[`isolation.md`](isolation.md) §3 I5) — a ceiling, not a selection lever. The routing *policy*
that would choose a model by cost/capability/health is **direction, not landed** —
[`routing.md`](routing.md).

## 5. Where to go next

- **Author or register a driver** (the contract, families, seam #13, dotted-path, leases) —
  [`drivers.md`](drivers.md); compliance rules — [`driver-compliance.md`](driver-compliance.md).
- **Routing policy** (cost/capability/health-aware selection — direction) — [`routing.md`](routing.md).
- **Config** (the `drivers:` block, env vars, `DriverSpec`) — [`kernel-config-reference.md`](kernel-config-reference.md).
- **Cost + budgets** — [`budgets.md`](budgets.md); **per-turn window** — [`context.md`](context.md).
- **Talking to the daemon from an app** (SDK) — [`contracts-consumer-guide.md`](contracts-consumer-guide.md).
