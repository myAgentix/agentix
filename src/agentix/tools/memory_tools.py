"""LLM-facing memory tools — read and write episodic/learnings tiers by slug.

Four kernel built-ins registered via ``register_kernel_tools``:
- ``memory_store``         — write one H2 section to an ep/ or ln/ page
- ``memory_recall``        — read a page by slug (optional section filter)
- ``memory_search``        — plain-text search across all registered pages
- ``memory_registry_list`` — list known slugs from registry.jsonl

All tools operate through ``ToolContext.memory`` (MemoryStore).  The registry
must have been loaded before the tool is called — ``OdooMemoryMaintain.create()``
calls ``store.registry.load()`` at startup; for other callers, load explicitly.
"""

from __future__ import annotations

import re
import time
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from agentix.tools.base import ToolContext, elapsed_ms
from agentix.tools.factory import tool

log = structlog.get_logger(__name__)

_EXCERPT_LEN = 80


# ── memory_store ──────────────────────────────────────────────────────────────


class MemoryStoreInput(BaseModel):
    slug: str = Field(
        ...,
        description=(
            "Registry slug identifying the target page. "
            "Prefix ep/ for episodic (tenant-specific), ln/ for learnings (cross-tenant). "
            "The slug must already exist in the registry — pages are created by the driver "
            "or via the seed script, not by this tool."
        ),
    )
    section: str = Field(
        ...,
        description="H2 section heading to write or overwrite on the page.",
    )
    body: str = Field(
        ...,
        description="Markdown body for the section. Replaces any existing content under that heading.",
    )
    reason: str = Field(
        ...,
        description="Why this finding is being stored. Used for audit logging; not written to the page.",
    )


class MemoryStoreOutput(BaseModel):
    slug: str
    path: str
    tier: str
    section: str
    ok: bool
    latency_ms: int = 0


@tool(
    name="memory_store",
    description=(
        "Write a finding to the episodic (ep/) or learnings (ln/) memory tier. "
        "The tier is determined by the slug prefix — ep/ for tenant-specific facts, "
        "ln/ for cross-tenant patterns with no PII. "
        "Call memory_registry_list first to discover available slugs."
    ),
    mutates_target=False,
)
async def memory_store(params: MemoryStoreInput, ctx: ToolContext) -> MemoryStoreOutput:
    started = time.perf_counter_ns()
    store = ctx.memory
    await store.registry.load()

    path = store.registry.resolve(params.slug)
    if path is None:
        raise ValueError(
            f"memory_store: unknown slug {params.slug!r}. "
            "Call memory_registry_list to see available slugs, or seed the page first."
        )

    tier = params.slug.split("/")[0]
    await store.write_section(path, params.section, params.body)
    log.info("memory_store.written", slug=params.slug, section=params.section, reason=params.reason)

    return MemoryStoreOutput(
        slug=params.slug,
        path=path,
        tier=tier,
        section=params.section,
        ok=True,
        latency_ms=elapsed_ms(started),
    )


# ── memory_recall ─────────────────────────────────────────────────────────────


class MemoryRecallInput(BaseModel):
    slug: str = Field(..., description="Registry slug of the page to read.")
    sections: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of H2 section headings to return. "
            "When omitted, all sections are returned. "
            "Use to reduce token cost when only part of the page is needed."
        ),
    )


class MemoryRecallOutput(BaseModel):
    slug: str
    found: bool
    frontmatter: dict = Field(default_factory=dict)
    sections: dict[str, str] = Field(default_factory=dict)
    latency_ms: int = 0


@tool(
    name="memory_recall",
    description=(
        "Read a memory page by slug. Returns frontmatter and H2 sections. "
        "Use the sections parameter to request only the headings you need, reducing token cost. "
        "Returns found=False (not an error) when the slug is unknown."
    ),
    mutates_target=False,
)
async def memory_recall(params: MemoryRecallInput, ctx: ToolContext) -> MemoryRecallOutput:
    started = time.perf_counter_ns()
    store = ctx.memory
    await store.registry.load()

    path = store.registry.resolve(params.slug)
    if path is None:
        return MemoryRecallOutput(slug=params.slug, found=False, latency_ms=elapsed_ms(started))

    page = await store.read_page(path)
    secs = page.sections
    if params.sections:
        secs = {k: v for k, v in secs.items() if k in params.sections}

    return MemoryRecallOutput(
        slug=params.slug,
        found=True,
        frontmatter=page.frontmatter,
        sections=secs,
        latency_ms=elapsed_ms(started),
    )


# ── memory_search ─────────────────────────────────────────────────────────────


class MemorySearchHit(BaseModel):
    slug: str
    path: str
    excerpt: str


class MemorySearchInput(BaseModel):
    query: str = Field(..., description="Text to search for (case-insensitive).")
    tier: Literal["ep", "ln", "all"] = Field(
        default="all",
        description="Limit search to one tier: ep (episodic), ln (learnings), or all.",
    )
    driver: str | None = Field(
        default=None,
        description="Limit search to pages registered by a specific driver name.",
    )


class MemorySearchOutput(BaseModel):
    hits: list[MemorySearchHit]
    total: int
    latency_ms: int = 0


@tool(
    name="memory_search",
    description=(
        "Plain-text search across all memory pages registered in registry.jsonl. "
        "No embedding required — always available. "
        "Returns matching slugs with an 80-character excerpt around the first match. "
        "Use tier and driver filters to narrow the scope."
    ),
    mutates_target=False,
)
async def memory_search(params: MemorySearchInput, ctx: ToolContext) -> MemorySearchOutput:
    started = time.perf_counter_ns()
    store = ctx.memory
    await store.registry.load()

    tier_filter = None if params.tier == "all" else params.tier
    candidates = store.registry.entries(tier=tier_filter, driver=params.driver)
    pattern = re.compile(re.escape(params.query), re.IGNORECASE)
    hits: list[MemorySearchHit] = []

    for entry in candidates:
        slug = entry.get("slug", "")
        path = entry.get("path", "")
        try:
            page = await store.read_page(path)
            full_text = page.preamble + "\n".join(
                f"## {h}\n{b}" for h, b in page.sections.items()
            )
        except Exception:
            continue
        m = pattern.search(full_text)
        if m:
            start = max(0, m.start() - 30)
            end = min(len(full_text), m.start() + _EXCERPT_LEN - 30)
            excerpt = full_text[start:end].replace("\n", " ").strip()
            hits.append(MemorySearchHit(slug=slug, path=path, excerpt=excerpt))

    log.debug("memory_search.done", query=params.query, hits=len(hits))
    return MemorySearchOutput(hits=hits, total=len(hits), latency_ms=elapsed_ms(started))


# ── memory_registry_list ──────────────────────────────────────────────────────


class RegistryEntry(BaseModel):
    slug: str
    path: str
    driver: str = ""
    tier: str = ""
    ts: str = ""


class MemoryRegistryListInput(BaseModel):
    tier: Literal["ep", "ln", "all"] = Field(
        default="all",
        description="Filter by tier: ep (episodic), ln (learnings), or all.",
    )
    driver: str | None = Field(
        default=None,
        description="Filter by driver name to narrow results to a specific integration.",
    )


class MemoryRegistryListOutput(BaseModel):
    entries: list[RegistryEntry]
    total: int
    latency_ms: int = 0


@tool(
    name="memory_registry_list",
    description=(
        "List all slugs known to the memory registry. "
        "Call this before memory_store or memory_recall to discover what memory already exists. "
        "Use tier and driver filters to narrow the results."
    ),
    mutates_target=False,
)
async def memory_registry_list(
    params: MemoryRegistryListInput, ctx: ToolContext
) -> MemoryRegistryListOutput:
    started = time.perf_counter_ns()
    store = ctx.memory
    await store.registry.load()

    tier_filter = None if params.tier == "all" else params.tier
    raw = store.registry.entries(tier=tier_filter, driver=params.driver)
    result = [
        RegistryEntry(
            slug=e.get("slug", ""),
            path=e.get("path", ""),
            driver=e.get("driver", ""),
            tier=e.get("tier", ""),
            ts=(e.get("ts") or "")[:19],
        )
        for e in sorted(raw, key=lambda x: x.get("slug", ""))
    ]

    return MemoryRegistryListOutput(entries=result, total=len(result), latency_ms=elapsed_ms(started))


__all__ = [
    "MemoryRecallInput",
    "MemoryRecallOutput",
    "MemoryRegistryListInput",
    "MemoryRegistryListOutput",
    "MemorySearchInput",
    "MemorySearchOutput",
    "MemoryStoreInput",
    "MemoryStoreOutput",
    "RegistryEntry",
    "memory_recall",
    "memory_registry_list",
    "memory_search",
    "memory_store",
]
