# Drivers

**Status:** living doc · **Scope:** Agentix kernel `[K]` (app-agnostic)

![Driver families — Chat, Embedding, STT, Storage](assets/driver-families.svg)

**Single source of truth for the driver framework in `docs/`.** Sections 1–8 document
the landed subsystem (code: `src/agentix/drivers/`); section 9 is **DIRECTION**.
Neighbouring SSoTs are referenced, never restated (CRIE rule): which model serves a
request — chain order and the routing-policy direction — is [`routing.md`](routing.md);
cost recording and the money budget are [`budgets.md`](budgets.md); the capacity
limiter's isolation invariant is [`isolation.md`](isolation.md) §3 I5.

**A driver is the kernel's first-class unit of external-system I/O** — modular and
developer-programmable. The first family is AI models of any modality (chat,
embedding, stt landed; vision/tts/timeseries designed) from any source (provider API,
gateway, huggingface, local runtime). The base contract is deliberately
system-agnostic: the storage family (§5) is the first landed non-model type, and a
future queue or database driver registers through the same descriptor + lifecycle +
error taxonomy with zero kernel change — modularity is the expandability mechanism.

---

## 1. The core contract (`drivers/base.py`)

- **`DriverDescriptor`** (frozen): `name` (unique in the registry), `type` — an
  **open string vocabulary** (`"model"` today; `"storage"` for the storage family;
  `"database"`, `"queue"`, … later — no kernel enum to amend), `modality`
  (chat|embedding|vision|tts|stt|timeseries for model-type; None otherwise; validated:
  model-type requires one), `source` (api|gateway|huggingface|local), `capabilities:
  frozenset[str]`, `default_model`, `pricing_ref` (key into the operator pricing table;
  **None = this driver's spend is not token-priced** — the machine-readable marker the
  cost story reads, §7).
- **`Driver`** protocol (@runtime_checkable): `descriptor` property + `async aclose()`.
  **Deliberately verb-free** — identity and lifecycle only.
- **Per-type typed protocols** add the verbs — `ChatDriver.complete(ChatRequest) ->
  ChatResponse`, `EmbeddingDriver.embed(list[str]) -> list[EmbeddingResult]`,
  `SttDriver.transcribe(AudioSource) -> Transcript`. **Rejected alternative:** one
  generic `infer(Any) -> Any` — it erases the typing mypy enforces and forces
  isinstance dances on every caller. Expandability lives in the open `type`/protocol
  pattern instead (§5 storage family, §8 worked example).
- **Error taxonomy** — `DriverError(message, *, driver, retryable=False)`;
  `DriverRateLimited` / `DriverUnavailable` (retryable) vs `DriverInvalidRequest`
  (not). Classification happens once, in the adapter; everything upstream (failover
  chain, Retry middleware) just branches on `retryable`. The legacy `Llm*` names and every
  `agentix.llm.*` / `agentix.embeddings` shim were **removed in 0.5.0**; the rename
  table ships in `CHANGELOG.md`.

## 2. Chat driver family (`drivers/chat.py`, `drivers/adapters/`)

- Canonical wire types: `ChatRequest`/`ChatResponse` (ex-`LlmRequest`/`LlmResponse`,
  field-identical) + `ToolSpec`/`tool_to_spec`.
- Adapters use vendor SDKs **directly** (a locked decision — no translation-layer
  dependency): each translates `ChatRequest` into its vendor-specific shape and back.

### Vendor adapters

| Driver class | Factory key | Source | Notes |
|---|---|---|---|
| `AnthropicChatDriver` | `"anthropic"` | `api` | API-key + OAuth token sources; tokens re-read per-request (§2.1); capabilities: `tools`, `thinking`, `cache_control` |
| `OpenAIChatDriver` | `"openai"` | `api` | Official `openai` SDK; tool-use + `reasoning_effort` |
| `GroqChatDriver` | `"groq"` | `api` | Official `groq` SDK; OpenAI-compatible shape; default model `moonshotai/kimi-k2` |
| `GeminiChatDriver` | `"gemini"` | `api` | Subclasses `OpenAIChatDriver`; Google's `.../v1beta/openai/` compat endpoint; key from `GEMINI_API_KEY`/`GOOGLE_API_KEY`; `tool_choice="any"` → `"required"` may be rejected, use `"auto"` |
| `OllamaChatDriver` | `"ollama"` | `local` | Subclasses `OpenAIChatDriver`; host's `<url>/v1`; `base_url` required; auth ignored; first local-SLM adapter for the OT direction ([`sync.md`](sync.md) §2) |
| `GrokChatDriver` | `"grok"` | `api` | Subclasses `OpenAIChatDriver`; xAI endpoint; `base_url` or `GROK_*` env |
| `NvidiaChatDriver` | `"nvidia"` | `api` | Subclasses `OpenAIChatDriver`; NVIDIA NIM endpoint |
| `MeliousChatDriver` | `"melious"` | `api` | Vendor gateway; configured via `KernelConfig.melious` block + `MELIOUS_*` env |

OpenAI-compatible subclasses (`GeminiChatDriver`, `OllamaChatDriver`, `GrokChatDriver`,
`NvidiaChatDriver`) inherit tool-use + `usage` parsing for free. Pricing is deployment
config, not per-model source (the `__unknown__` fallback in `cost_tracking.py` covers
them; operators set real rates in `llm_pricing:`).

### Intrinsic gateway adapter

- **`HubleChatDriver`** (`"huble"`) — `source="gateway"`, proxies to its configured
  upstream; reports its own billed cost in `raw["cost_usd"]` (the
  `CostRecordingChatDriver` prefers this over locally-computed cost, see §7 /
  [`budgets.md`](budgets.md) §3).

### Failover and cost wrappers

- `ChatFailoverChain` (`drivers/router.py`, ex-`ProviderRouter`) — ordered
  first-success failover, itself `ChatDriver`-compatible; semantics canonical in
  [`routing.md`](routing.md) §2.
- `CostRecordingChatDriver` (`drivers/cost.py`) — the chat cost decorator; recording
  semantics canonical in [`budgets.md`](budgets.md) §3.
- The dispatcher consumes a `ChatDriver` (constructor kwarg `driver=`).

### 2.1 Anthropic token sources (`drivers/adapters/intrinsic/anthropic_auth.py`)

Claude Code rotates OAuth tokens every ~hour. A static token captured at init time
would expire mid-session. The provider receives a **`TokenSource`** it re-reads on
every `complete()` call:

| Source class | Description |
|---|---|
| `StaticTokenSource` | Literal API key or OAuth token |
| `EnvTokenSource` | Re-reads a named env var on every call |
| `FileTokenSource` | Re-reads `~/.claude/.credentials.json` on every call |
| `KeychainTokenSource` | macOS Keychain via `security find-generic-password` on every call |
| `ChainTokenSource` | Tries sources in order; returns first non-raising |

`resolve_token_source(api_key, credentials_path, keychain_service)` is the factory
used by `AnthropicChatDriver`. Priority: explicit `api_key` → `CLAUDE_CODE_OAUTH_TOKEN`
/ `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` env vars → keychain (if configured) →
credentials file (`~/.claude/.credentials.json` by default). Tokens prefixed
`sk-ant-oat` are treated as OAuth (sent as `Authorization: Bearer`); all others as API
keys (`x-api-key`). OAuth billing header override: `AGENTIX_ANTHROPIC_BILLING_HEADER`.

## 3. Embedding driver family (`drivers/embedding.py`)

`EmbeddingDriver` protocol + `OpenAIEmbeddingDriver` / `HubleEmbeddingDriver`,
fronted by `CachedEmbeddingDriver` over the SQLite `EmbeddingCache`
(sha256(model‖text) keys — swapping backends can't return stale vectors). Cosine
ranking is **not** a driver concept: `CosineIndex` lives in
`agentix.storage.vector_index`. What gets embedded is the memory layer's decision
([`memory.md`](memory.md) §4).

- **`OpenAIEmbeddingDriver`** (factory key `"openai-embedding"`) — `text-embedding-3-small`
  by default (1536 dims); re-uses the `openai` SDK already shipped for chat; key from
  `OPENAI_API_KEY` or `api_key`. `base_url` accepted for compatible endpoints.
- **`HubleEmbeddingDriver`** (factory key `"huble-embedding"`) — POSTs to the HUBLE
  gateway (`{base_url}{embeddings_path}`; default path `/api/v2/embeddings`) with
  OpenAI-shape body; 404 on the path → clear error directing to fallback or config.
- `EmbeddingCache` — SQLite-backed, packed little-endian float32 blobs; async-safe
  (SQLite per-row locking). Parallel cache lookups via `asyncio.gather` before the
  upstream call; only misses are batched upstream.

## 4. STT — the proof modality (`drivers/speech.py`, `adapters/hf.py`)

`AudioSource` (raw bytes + MIME type) in, `Transcript` out — a request shape that
**cannot be smuggled through `ChatRequest`**, proving the base abstraction isn't
secretly chat-shaped. `HfSttDriver` speaks the HuggingFace Inference API
(`source="huggingface"`, default `openai/whisper-large-v3`, `HF_TOKEN` env or
`api_key_env`; factory key `"hf-stt"`): one POST per call, 503-with-`estimated_time`
(cold model loading) classified retryable, `transport=` kwarg as the no-network test
seam. Its pricing is per-second — `pricing_ref=None`, see §7.

## 5. Storage driver family (`drivers/object_store.py`, `drivers/relational.py`, `drivers/file_store.py`)

The first non-model driver type — `type="storage"` — born of a two-layer split of
the kernel stores: the **store** stays the semantic layer (`MinioStore`: JSON/JSONL
encoding, stream composition, the `key_*` prefix discipline — `storage/README.md`),
while the raw **transport** underneath becomes a driver. Swapping the physical
backend means writing a new driver; the store and every consumer stay untouched.

- **`ObjectStoreDriver`** (`modality="object"`) — transport verbs only:
  `ensure_bucket` / `put_bytes` / `put_file` / `get_bytes` / `get_stream` /
  `list_objects` / `delete_object` / `exists` / `copy_object` / `presigned_get`.
  Anything expressible as composition over these (`put_json`, `put_stream`
  accumulation) deliberately stays in the store.
- **`MinioObjectStoreDriver`** (`adapters/intrinsic/minio.py`) — the landed backend
  (S3-compatible; the `minio.Minio` client + thread offloading moved here from
  `storage/minio_store.py`). Error classification happens once, here:
  `NoSuchKey`/`NoSuchBucket` → `ObjectNotFound` (a `DriverError`, not retryable,
  carries `.key`); `SlowDown` → `DriverRateLimited`; 5xx/connectivity →
  `DriverUnavailable`; the rest → `DriverInvalidRequest` — so the Retry middleware
  works for storage exactly as it does for chat. Factory key `"minio-object-store"`:
  endpoint from `spec.base_url`, `bucket`/`access_key`/`secure`/`region` from
  `spec.options`, secret via `api_key_env`.
- **`LocalObjectStoreDriver`** (`adapters/intrinsic/local_fs_object.py`) — the same
  protocol against a plain directory, for embedded integrators (the
  [`agentix.sync`](sync.md) hosts) that should not need a MinIO server:
  `MinioStore(driver=LocalObjectStoreDriver(root))`. Keys map to contained
  relative paths (escapes rejected, symlinks followed); writes are atomic
  (temp + `os.replace`). Degradations by contract: `presigned_get` returns a
  `file://` URI (no signing/expiry, same-host only), `content_type` is
  ignored, delete-on-missing is a no-op (S3 semantics). `FileNotFoundError` →
  `ObjectNotFound`; other `OSError` → `DriverUnavailable`. Single-node. Factory key
  `"local-object-store"` (root from `spec.base_url` or `spec.options["root"]`, no secret).
- **Wiring**: `MinioStore(config)` builds the MinIO driver internally (zero consumer
  churn); `MinioStore(driver=...)` injects an alternate backend. Registry accessors
  `object_store()` / `object_store_or_none()`.
- **`RelationalDriver`** (`modality="relational"`, `drivers/relational.py`) —
  `connect` / `execute -> ExecuteResult(lastrowid, rowcount)` / `query` /
  `query_one` (rows as plain dicts, backend-neutral) / `commit`. Landed backends:
  - **`SqliteRelationalDriver`** (`adapters/intrinsic/sqlite.py`, factory key
    `"sqlite-relational"`) — owns the connection + PRAGMAs (WAL, `busy_timeout=30000`:
    isolation.md I2's home now); lock/busy → `DriverUnavailable`, constraint/malformed
    SQL → `DriverInvalidRequest`. `SqliteStore(path)` unchanged; `SqliteStore(driver=...)`
    injects. The adapter-only `raw` property is the sqlite escape hatch for seam-#10
    subclass migrations. Honesty: the kernel store's DDL is sqlite-dialect (FTS5,
    PRAGMA) — a Postgres adapter satisfies the protocol, but porting the *store schema*
    is dialect work, stated not hidden.
  - **`PostgresRelationalDriver`** (`adapters/intrinsic/postgresql.py`, factory key
    `"postgresql-relational"`) — same `RelationalDriver` protocol over PostgreSQL.
    Endpoint from `spec.base_url`; secret via `api_key_env`.
- **`FileStoreDriver`** (`modality="file"`, `drivers/file_store.py`) —
  `read_text` / `write_text` / `append_text` / `list_files` / `exists` /
  `lock(name, timeout)` / `head_ref()`. Two boundaries designed in: **locking is a
  verb** (fcntl locally, WebDAV LOCK remotely — timeout raises a `TimeoutError`
  subclass, locally `MemoryLockTimeout` so existing handlers keep working), and
  **the version pin degrades** (`head_ref()` = git HEAD sha locally, None on
  backends without a repo — callers already treat None as "no pin, no drift
  check"). Landed backend: `LocalFileStoreDriver` (`adapters/intrinsic/local_fs.py`,
  factory key `"local-file-store"`, capabilities `{"git-pin", "fcntl-lock"}`) — owns
  path containment, fcntl locks under `.locks/`, the git pin. `MemoryStore(root)`
  unchanged; `MemoryStore(driver=...)` injects (NextCloud/WebDAV, SMB — app-side).
  Local I/O errors propagate as-is; remote adapters classify connectivity into the
  taxonomy.

## 6. Registry, config, factory — seam #13

- **`DriverRegistry`** (`drivers/registry.py`, ToolRegistry house style): `register`
  (strict, `DriverConflict`) / `try_register` (lenient, log+skip); lookup by `name`
  or the typed accessors `chat()` / `embedding()` / `embedding_or_none()` / `stt()` /
  `file_store()` / `relational()` / `object_store()` / `object_store_or_none()`.
  Default-per-modality is **pure lookup, explicitly not routing policy**: first
  registered wins unless `default=True` says otherwise. `aclose_all()` closes
  everything (including any outstanding session leases), logging instead of raising —
  shutdown must complete.
- **`DriverSpec`** (`config.py`) — one declared instance: `name`, `driver` (builtin
  factory key or dotted path `pkg.mod:Class`), `type`, `modality`, `model`,
  `base_url`, `api_key_env` (**the env-var NAME, never a secret** — 12-factor),
  `default`, `scope` (`"process"` default or `"session"`, see below), `options`.
  `KernelConfig.drivers: tuple[DriverSpec, ...]`; empty →
  `derive_driver_specs(cfg)` maps the legacy anthropic/huble/melious blocks (via
  `enabled_providers` — the activation SSoT is unchanged). Collapsing those blocks
  into `drivers:` is the v0.6 config migration
  ([`kernel-config-reference.md`](kernel-config-reference.md)).
- **`build_drivers(cfg, sqlite=None, model_override=None, always_chain=False)`**
  (`drivers/factory.py`) — the one composition entry: chat specs compose into one
  registered chat entry (bare driver when single — no chain overhead — else a
  `ChatFailoverChain` in spec order; each wrapped in `CostRecordingChatDriver` when
  `sqlite` is passed); embedding specs build behind the cache (skipped when sqlite is
  absent); `scope="session"` specs register as leasables (never instantiated here);
  everything else builds strictly — an unknown factory key **fails loud** (a
  misconfigured driver must not be silently skipped).
- **Seam #13 — how developers add drivers** (three explicit paths; entry-points
  discovery **rejected**: ambient import side effects defeat the purity gates):
  1. `register_driver_factory("mysql", build_mysql_driver)` at app startup, then
     declare `DriverSpec(driver="mysql", ...)` in config;
  2. `DriverSpec(driver="my_pkg.drivers:MySqlDriver")` — dotted path; constructor
     contract `__init__(*, spec: DriverSpec, api_key: str | None)`;
  3. build the instance yourself and `registry.register(my_driver)`.

### Session-scoped credential leases — seam #13, lease path

The three paths above assume **process-static** config: specs at startup, the secret
via `api_key_env`, one instance for the process, closed by `aclose_all()`. An app
whose external-system credentials arrive **per session** (per-job vault decryption,
multi-tenant remote systems) cannot use a static instance — and must not put secrets
on a `DriverSpec`. The lease path covers this:

- **`DriverSpec.scope`** — `"process"` (default, everything above unchanged) or
  `"session"`. A session-scoped spec is declared like any other (name, dotted path or
  factory key, `base_url`, `options`, optionally `api_key_env` for a static part of
  the credential) but `build_drivers` never instantiates it: it registers a
  **leasable entry** via `registry.register_leasable(name, builder)` instead. Still
  no secret on the spec, ever.
- **`registry.lease(name, credentials)`** — async context manager handing out a
  fresh instance bound to the caller-supplied credentials mapping:

  ```python
  async with registry.lease("erp-target", creds) as driver:
      await driver.execute(...)
  ```

  Lease lifetime ≤ session lifetime. A leased instance **never enters the name
  table**: `get()` / typed accessors / defaults cannot see it, so cross-session
  leakage is impossible by construction. The instance is attributed to
  `current_session_id` at lease time (`driver.lease` / `driver.lease_closed` log
  lines carry it); the capacity limiter (I5) wraps leased calls exactly as static
  ones (the adapter's call path acquires `driver_capacity()` either way).
- **`registry.leasable_names()`** — returns the sorted list of registered leasable
  names (distinct from the `_drivers` name table).
- **Teardown backstops** — the context manager is the primary lifetime; two
  backstops catch leaks: `registry.aclose_session_leases(session_id)` closes every
  lease still open for that session, and `aclose_all()` drains all outstanding leases
  at shutdown (logging, never raising).
- **Construction contracts** — dotted-path classes take
  `__init__(*, spec, api_key, credentials)` (the leased extension of the seam-#13
  contract); or register `register_credentialed_factory("erp", fn)` where
  `fn(spec, cfg, credentials) -> Driver`. Unknown keys fail loud, as always.

## 7. Cross-cutting — honest v0.5 boundaries

- **Cost**: recorded spend = **chat spend** (`CostRecordingChatDriver`, canonical in
  [`budgets.md`](budgets.md)). Embedding and STT calls are NOT written to the session
  cost ledger — `ModelPricing` is strictly per-token and fake per-second numbers
  would corrupt budget enforcement. They emit a structured `driver.usage` log line
  (type, modality, driver, model, units, bound session id) so the spend stays
  visible. The type-agnostic recorder keyed on `pricing_ref` + unit normalization is
  DIRECTION (budgets.md).
- **Capacity**: one process-global semaphore (`drivers/limiter.py`,
  `driver_capacity()`, default 8, per event loop — isolation.md I5) now covers chat
  AND stt calls (embedding wrapping is DIRECTION with per-type limits). This is
  **gate A — model calls only**. Vendor transport drivers own their per-target
  concurrency with their own semaphore (**gate B**) and must NOT wrap gate A:
  routing bulk external-system I/O through the model-call ceiling would couple two
  unrelated throughputs and starve both (seams.md, division-of-responsibilities).
- **Attribution**: `current_session_id` / `bind_session` / `session_scope` and
  `current_turn_id` / `bind_turn` live in `drivers/session.py` — modality-agnostic.
  The runner binds the session id; the engine binds the turn id around each
  middleware-chain run. Vendor drivers READ these ContextVars for log/usage
  attribution — they never define their own.

## 8. Worked example — a custom driver (dotted-path seam)

The `pkg.mod:Class` dotted-path form of seam #13 path 2. The class receives the
`DriverSpec` and the resolved `api_key` string (the value from the `api_key_env`
var, not the env-var name):

```python
class MySqlDriver:
    def __init__(self, *, spec: DriverSpec, api_key: str | None) -> None:
        self._pool = ...  # dsn from spec.base_url, secret from api_key
        self._name = spec.name

    @property
    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            name=self._name,
            type="storage",
            modality="relational",
            source="local",
        )

    async def query_one(self, sql: str, params=()) -> dict | None: ...
    async def aclose(self) -> None: ...
```

Declared as:

```yaml
drivers:
  - name: mysql-main
    driver: "my_pkg.drivers:MySqlDriver"
    type: storage
    modality: relational
    base_url: "mysql://10.0.99.1:3306/app"
    api_key_env: MYSQL_PASSWORD
```

The registry, lifecycle, error taxonomy (`DriverError(retryable=...)` for deadlocks
vs syntax errors) and config discipline all apply unchanged; only the verb protocol
is new — defined beside the driver, not in the kernel.

---

*Everything below is DIRECTION — converged design, not the code today.*

## 9. DIRECTION

- **Routing policy over drivers** — cost-aware/capability-aware candidate ordering,
  escalation tiers, health breakers: [`routing.md`](routing.md) §4/§6–7 (none of it
  landed in v0.5 — the registry default is a lookup, never a choice).
- **Non-chat cost recording** — unit-pricing schema (per-second stt, per-text
  embedding) + a type-agnostic `CostRecordingDriver` keyed on `pricing_ref`
  ([`budgets.md`](budgets.md)).
- **Per-type / per-driver capacity limits** (today: one shared semaphore).
- **Remaining modalities** — vision, tts, timeseries protocols + adapters (the stt
  proof establishes the pattern); a type-generic failover chain (don't generalize
  before a second consumer exists).
- **Config collapse** — fold `anthropic:`/`huble:`/`melious:` into `drivers:` (v0.6).
- **Lifecycle verbs** — `health()` / `warmup()` for local-runtime drivers.
- **Second storage backends** — MySQL/Postgres behind `RelationalDriver` for the
  kernel store schema (the `PostgresRelationalDriver` satisfies the protocol; porting
  the kernel's sqlite-dialect DDL is the remaining work), NextCloud/WebDAV behind
  `FileStoreDriver` (WebDAV LOCK; no git pin), S3/Azure/GCS behind
  `ObjectStoreDriver` — all app-side via the driver seam, zero kernel change.
