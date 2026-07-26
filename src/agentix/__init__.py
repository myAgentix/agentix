"""Agentix — the open, async agentic-app kernel.

Agentix is the reusable, app-agnostic kernel distilled from the cluster: the
deterministic engine + middleware spine, the driver framework for external-system I/O, the three-store
storage abstraction, the skill catalogue, the tool protocol, and the Contract-B event
envelope. Apps (e.g. LUDO = "Agentix + the Odoo app") depend on this package and register
their own tools, skills, job types, and policies against the kernel API.

This package must remain free of any app-specific (`ludo.*` / Odoo) imports — it is the
core dependency that terminal/web/mobile/desktop agent apps build on.
"""

from __future__ import annotations

__version__ = "0.7.1"
