"""Unit tests for ``DriverRegistry.leased()`` (spec 001).

A driver's tool surface needs its own handle without the app threading it
through ToolContext. ``leased()`` is that read counterpart to ``lease()``:
keyed by the name leased *by*, scoped to the current session.
"""

from __future__ import annotations

import pytest

from agentix.drivers.base import DriverDescriptor
from agentix.drivers.registry import DriverRegistry
from agentix.drivers.session import session_scope


class _FakeErp:
    """Lease-built driver whose descriptor name is instance-specific.

    This is the real shape (``OdooDriver`` names itself ``odoo:<database>``),
    which is why ``leased()`` cannot match on ``descriptor.name``.
    """

    def __init__(self, database: str) -> None:
        self._descriptor = DriverDescriptor(name=f"odoo:{database}", type="erp", source="api")
        self.closed = False

    @property
    def descriptor(self) -> DriverDescriptor:
        return self._descriptor

    async def aclose(self) -> None:
        self.closed = True


def _registry() -> DriverRegistry:
    reg = DriverRegistry()
    reg.register_leasable("odoo-erp", lambda creds: _FakeErp(str(creds.get("database", "db"))))
    return reg


@pytest.mark.asyncio
async def test_leased_returns_handle_inside_the_lease() -> None:
    reg = _registry()
    async with session_scope("s1"):
        assert reg.leased("odoo-erp") is None
        async with reg.lease("odoo-erp", {"database": "ecotech"}) as driver:
            assert reg.leased("odoo-erp") is driver
        assert reg.leased("odoo-erp") is None


@pytest.mark.asyncio
async def test_leased_matches_the_leasable_name_not_the_descriptor() -> None:
    reg = _registry()
    async with session_scope("s1"), reg.lease("odoo-erp", {"database": "ecotech"}) as driver:
        assert driver.descriptor.name == "odoo:ecotech"
        assert reg.leased("odoo-erp") is driver
        assert reg.leased("odoo:ecotech") is None


@pytest.mark.asyncio
async def test_leases_are_isolated_across_sessions() -> None:
    reg = _registry()
    async with session_scope("s1"), reg.lease("odoo-erp", {"database": "a"}) as first:
        assert reg.leased("odoo-erp") is first
        async with session_scope("s2"):
            # s2 holds no lease of its own.
            assert reg.leased("odoo-erp") is None
            async with reg.lease("odoo-erp", {"database": "b"}) as second:
                assert reg.leased("odoo-erp") is second
                assert second is not first
        assert reg.leased("odoo-erp") is first


@pytest.mark.asyncio
async def test_unknown_name_returns_none() -> None:
    reg = _registry()
    async with session_scope("s1"), reg.lease("odoo-erp", {"database": "a"}):
        assert reg.leased("sap-erp") is None


@pytest.mark.asyncio
async def test_most_recent_lease_wins_for_a_duplicated_name() -> None:
    reg = _registry()
    async with session_scope("s1"), reg.lease("odoo-erp", {"database": "a"}) as first:
        async with reg.lease("odoo-erp", {"database": "b"}) as second:
            assert reg.leased("odoo-erp") is second
        # Inner lease closed; the outer one is visible again.
        assert reg.leased("odoo-erp") is first


@pytest.mark.asyncio
async def test_lease_teardown_still_closes_instances() -> None:
    """Regression guard for the (name, driver) entry-shape change."""
    reg = _registry()
    async with session_scope("s1"):
        async with reg.lease("odoo-erp", {"database": "a"}) as driver:
            assert not driver.closed
        assert driver.closed
        assert reg.leased("odoo-erp") is None
