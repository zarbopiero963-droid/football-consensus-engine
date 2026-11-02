"""In-memory stand-in for the shared PostgreSQL coordinator (offline tests only).

It enforces the same contract the live harness relies on: one unit per attempt, a
concurrency cap, release of every lease, and provider-reported exhaustion.
"""
from __future__ import annotations

from bridge.api_football.quota import QuotaConcurrencyBusy, QuotaExceeded, QuotaLease


class FakeCoordinator:
    def __init__(self, budget: int = 10_000, concurrency: int = 1):
        self.budget = budget
        self.concurrency = concurrency
        self.used = 0
        self.active: set[str] = set()
        self.released: list[str] = []
        self.exhausted: list[str] = []
        self.prepared = 0
        self.keys: set[str] = set()

    async def prepare(self) -> None:
        self.prepared += 1

    async def acquire(self, secret: str) -> QuotaLease:
        self.keys.add(secret)
        if self.exhausted or self.used >= self.budget:
            raise QuotaExceeded()
        if len(self.active) >= self.concurrency:
            raise QuotaConcurrencyBusy()
        self.used += 1
        lease = QuotaLease(lease_id=f"lease-{self.used}", key_hash="h")
        self.active.add(lease.lease_id)
        return lease

    async def release(self, lease: QuotaLease) -> None:
        self.active.discard(lease.lease_id)
        self.released.append(lease.lease_id)

    async def mark_exhausted(self, secret: str, kind: str) -> None:
        self.exhausted.append(kind)
