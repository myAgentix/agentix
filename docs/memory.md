# Memory

**Status:** living doc · **Scope:** Agentix kernel `[K]` (app-agnostic), with a
labelled reference-app DIRECTION part

**Single source of truth for memory in `docs/`.** Sections 1–6 document the landed
kernel memory subsystem (code: `src/agentix/core/working_memory.py`,
`storage/memory.py`, `drivers/embedding.py`, `storage/vector_index.py`); sections 7–8 are **DIRECTION** — the memory
doctrine an app builds on top. Two disambiguations up front: the model *window*
(what enters a single LLM call) is [`context.md`](context.md) — memory is one of
its retrieval sources, not the window itself. And **data vs memory never cross**:
data = the records an app processes (bulk store + target system); memory = what
the system *learnt*. Reference-app physical layout: `ludo-agent/arch.md` §7.

---

## 1. The memory model — three tiers, one rule

Everything the system remembers falls into three tiers by lifetime and scope:

| Tier | Lifetime | Scope | Kernel carrier |
|---|---|---|---|
| **Transient** | one run | the session | `WorkingMemory` on `Session` (§2) |
| **Episodic** | survives runs | per-tenant / per-context | markdown pages via `MemoryStore` (§3) |
| **Learnings** | permanent | general, cross-tenant | markdown pages via `MemoryStore` (§3) |

The kernel/app split: the kernel ships the **substrate** — the working-memory log,
the section-preserving page store with locks, semantic recall — plus the middleware
slot the app's maintain loop plugs into (§6). The app owns the memory *tools*
(consult, record, diagnose) and the maintain workflow (what reconciles into what).

One rule binds all tiers: **memory holds conclusions, never payload data.** Bulk
artifacts go to the object store; operational state goes to SQLite; memory is the
markdown layer (`storage/README.md` has the what-goes-where table).

## 2. Working memory — the Transient tier

`core/working_memory.py`. A structured **tried / failed / learned** log per
session, on `Session.working_memory`.

- `AttemptRecord`: `target` (what it was aimed at) · `approach` (the strategy) ·
  `outcome` (`success`/`failed`) · `lesson` (why it failed, or what worked and
  under which conditions) · `turn_index` · optional `tool_name`.
- `WorkingMemory` adds `blocked_paths` (dead ends as `"<target> via <approach>"`,
  auto-added on every failed attempt) and `active_strategy` (the current
  one-sentence plan). `is_blocked(target, approach)` is the guard.
- `render_for_system_prompt()` produces a markdown block injected as a **system
  message** each turn: active strategy, blocked paths ("do NOT retry"), attempts
  log (last 12 full, older elided to one line), capped at ~6000 chars. Because
  compression preserves system messages verbatim, the log **survives context
  compression** where tool-result history collapses.

Three write surfaces:

1. **The app's record tool** (e.g. `record_attempt`) — the model writes lessons
   deliberately.
2. **Auto-record on tool failure** (`agent_dispatcher._auto_record_attempt`) — a
   failed dispatch becomes a failed attempt + blocked path, domain-neutrally
   (subject from generic arg keys, bulky args skipped by size).
3. **Auto-record on recovery** (`_auto_record_recovery`) — a success on a
   currently-blocked target records the overturn and **unblocks** the path.

The dispatcher also bumps its throttled checkpoint whenever working memory gains
an attempt — lessons are never lost to a crash. Tests:
`tests/unit/core/test_working_memory.py`.

## 3. The markdown memory store — Episodic + Learnings substrate

`storage/memory.py`. Disk primitives over a directory of markdown files with YAML
frontmatter.

### Construction

`MemoryStore` accepts either a root path or an injected driver (since v0.5.3):

```python
MemoryStore(root="/path/to/memory")          # local filesystem via LocalFileStoreDriver
MemoryStore(driver=some_driver)              # alternate backend (WebDAV, SMB, …)
```

The physical medium lives behind the `agentix.drivers.file_store.FileStoreDriver`
protocol — the default backend is the local filesystem
(`drivers/adapters/intrinsic/local_fs.py`: fcntl locks, git pin). A `driver`
property exposes the transport for registry wiring.

`MemoryPage` is the parsed representation: `frontmatter` (dict) · `preamble`
(content before the first H2) · `sections` (ordered `{heading: body}` map). Its
`render()` method reassembles the page to round-trip-stable markdown.

### Page API

- **Section-preserving writes** — callers mutate **one H2 section at a time**
  (`write_section`); other sections and the frontmatter stay byte-identical.
- Full-page methods: `read_page` / `write_page` / `create_page` / `update_frontmatter`.
- `list_pages(relative_dir)` — returns every `.md` file under the dir, sorted.
- `path_log()` / `path_index()` — resolved `Path` helpers for `log.md` and
  `index.md` under the memory root.

### Log append

`append_to_log(type, subject, body)` serialises writes to `log.md` behind an
asyncio lock so concurrent calls cannot corrupt the ordering. Each entry is
formatted as:

```
## [YYYY-MM-DD] <type> | <subject>
<body>
```

### Advisory locks

`lock(name, *, timeout_seconds=10.0)`: exponential backoff up to a timeout,
`MemoryLockTimeout` on failure. The mechanism is the file-store driver's
(locally: non-blocking `fcntl.flock` on `.locks/<name>.lock` under the memory
root). Covers both same-process (`asyncio.gather`) and cross-process contention.
Namespaced names by convention (`customer-<id>`, `reconcile-<key>`);
`lock_for_customer(id)` is the readable wrapper for the common case.
**Single-node only** — multi-node needs a DB advisory lock (arch.md §10.2).

### Git pin

`head_sha()` lets a session record which memory state it ran against (the store
is expected to be git-backed). Returns `None` when outside a git repo or on a
backend that carries no version pin — callers treat that as "no pin, no drift
check".

### Maintenance helpers

- `find_orphan_pages(relative_dir, *, index_path)` — pages `index.md` never
  links; the cheap lint.
- `promote_evidence(target_relative, customer_id, threshold)` — bookkeeping only:
  append to `confirmed_by`, bump `evidence_count`, return whether the threshold
  is crossed. *Deciding* to promote stays the model's/operator's job.

Tests: `tests/unit/storage/test_memory.py`.

## 4. Semantic recall

`drivers/embedding.py` + `storage/vector_index.py`. Fuzzy recall over memory content — given a novel error or
question, which known patterns are semantically closest (catching paraphrases
that token-overlap misses).

- `EmbeddingDriver` protocol ([`drivers.md`](drivers.md) §3); shipped backends: OpenAI
  (`text-embedding-3-small`) and Huble. Pluggable — vendor neutrality survives.
- `CosineIndex` — pure-Python in-memory cosine similarity (`add(key, payload, vector)`,
  `top_k(query_vec, k=3)`); fine to ~10K entries, swap to FAISS behind the same
  interface past that.
- `EmbeddingCache` — SQLite-backed (`embedding_cache` table on the existing
  store), keyed `sha256(model || text)` so provider swaps never return stale
  vectors; vectors stored as packed float32 blobs.
- Activation is opt-in: with no provider configured, apps fall back to their
  deterministic matching.

Tests: `tests/unit/drivers/test_embedding.py`.

## 5. Session, config and storage plumbing

- `Session.working_memory` is part of session state: **checkpoints capture it**
  and `resume_from` rebuilds it — a resumed session keeps its lessons
  ([`session.md`](session.md)).
- `KernelConfig.memory_path` locates the memory root; apps subclass the config.
- `RunContext` carries the account prefix so middlewares that persist memory
  artifacts write account-scoped object-store keys.
- The three-store boundary is doctrine (`storage/README.md`): bulk blobs → object
  store, operational state → SQLite, conclusions → memory markdown. **Never
  cross them.** The physical medium under a store is becoming pluggable via
  storage-type drivers ([`drivers.md`](drivers.md) §5) — the doctrine and the
  tiers are untouched by that split.

## 6. The maintain seam — where the app plugs in

The kernel defines the middleware slot; the app supplies the loop.

- `MemoryMaintain` is position 9 in the fixed middleware order
  (`core/middleware/base.py`) — after SafetyGate, immediately around the engine
  inner. It is **app-specific by design**: the kernel cannot know what a finding
  means in a domain.
- The reference implementation
  (`ludo-agent/src/ludo/core/middleware/memory_maintain.py`) runs at session
  close: ingest findings → lint (orphans, §3) → reconcile (finding → memory
  rule) → promote (cross-case evidence) — using the kernel's locks and page
  primitives for every write.
- Working memory feeds this loop: the Transient log is the raw material the
  maintain pass distils into Episodic/Learnings pages.

---

*Everything below is DIRECTION — doctrine and converged design, not kernel code.*

## 7. Memory doctrine across an app

How the reference app (and any app on the kernel) organises the tiers:

- **Episodic splits two ways** — *per-tenant* (customer pages; the system of
  record may live outside the agent, e.g. in a control-plane DB, with the agent
  keeping a per-run working copy) and *per-context* (e.g. per version-pair:
  recipes, reconciled diagnoses).
- **Learnings are shared, so they carry no PII** — cross-tenant evidence uses
  anonymized fingerprints, never tenant identifiers. This is what makes the
  Learnings tier safe to share.
- **The memory verbs**: **reconcile** turns a finding into a memory rule (on
  both a context axis and a general type axis, so tenant N benefits from tenant
  M's evidence); **promote** flips a rule to trusted once `evidence_count`
  crosses the threshold (§3's `promote_evidence` is the bookkeeping half).
- **Memory is a substrate, not a cache** — rules are versioned (git), linted,
  evidence-carrying pages, not lookaside entries. The maturation pipeline
  (finding → memory → …) is the app's competence model; only the first arrow is
  automatic, everything beyond is operator-reviewed.
- Reference layout + operations: `ludo-agent/arch.md` §7.3–7.5; path resolution
  through the app's memory-paths module, never hand-built strings.

## 8. Convergence with context management

Retrieved memory is one **priority tier of the model window** (guardrails > goal
> working set > retrieved memory > history) — retrieval gating (*when* and *how
much* to pull) is window policy and lives in [`context.md`](context.md). The
working-memory system-block injection is already owned by the ContextManager
(one assembly path); treating retrieved memory as an untrusted injection vector
is likewise a context-policy concern. This doc owns what memory *is* and how it
persists; `context.md` owns what of it *enters the window*.
