"""LedgerStore: append, read, hash chain (spec §9.1, §9.2).

@spec("9.1")
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from collections.abc import Iterable
from types import TracebackType
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as DBSession

from rekha.canonical import canonical_bytes, sha256_hex
from rekha.clock import Clock, SystemClock
from rekha.db.lifecycle import EngineLease
from rekha.db.models import Base, EventRow
from rekha.ledger.model import GENESIS_HASH, Event


def _row_to_event(row: EventRow) -> Event:
    return Event(
        event_id=row.event_id,
        session_id=row.session_id,
        step_seq=row.step_seq,
        type=row.type,
        at=row.at,
        payload=row.payload,
        set_hash=row.set_hash,
        prev_hash=row.prev_hash,
        hash=row.hash,
        initiated_by=row.initiated_by,
        on_behalf_of=row.on_behalf_of,
    )


def compute_hash(event: Event) -> str:
    """`hash = SHA-256(canonical(event without hash) || prev_hash)` (spec §9.2)."""
    data = canonical_bytes(event.unsigned_dict()) + event.prev_hash.encode("utf-8")
    return sha256_hex(data)


class LedgerStore:
    """Append-only, hash-chained event ledger backed by SQLite (spec §9.1).

    There is deliberately no update/delete method: `append` is the only
    write path exposed. Past events are immutable (spec §9.2/§9.3).
    """

    def __init__(
        self,
        db_url: str = "sqlite:///:memory:",
        *,
        engine: Engine | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._engine_lease = (
            EngineLease.create(db_url) if engine is None else EngineLease.borrow(engine)
        )
        self._engine = self._engine_lease.engine
        Base.metadata.create_all(self._engine)
        #: Injected for the same reason every other component takes one: a
        #: rolling-window limit (`rekha/policy/quota.py`,
        #: `rekha/policy/cumulative.py`) compares an event's `at` against
        #: `Clock.now()`. If those two come from different sources the window is
        #: unsound -- and untestable without rewriting `at` after the fact, which
        #: would break the hash chain.
        self._clock: Clock = clock or SystemClock()
        # ponytail: process-local lock per session, not a DB-level lock —
        # fine for single-process `rekha run`; multi-process writers to the
        # same SQLite file would need a real advisory/row lock instead.
        self._locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    def close(self) -> None:
        self._engine_lease.close()

    def __enter__(self) -> LedgerStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._session_locks[session_id]

    @property
    def engine(self) -> Engine:
        """The underlying SQLAlchemy engine, so other stores (e.g. `ApprovalQueue`)
        can share the same SQLite file without re-parsing a `db_url`."""
        return self._engine

    def append(
        self,
        session_id: str,
        type: str,
        payload: dict[str, Any],
        step_seq: int | None = None,
        set_hash: str | None = None,
        initiated_by: str | None = None,
        on_behalf_of: str | None = None,
    ) -> Event:
        """Append one event, computing `prev_hash`/`hash` (spec §9.1, §9.2).

        `initiated_by`/`on_behalf_of` (E14, plan-v2) are normally only passed
        by `Lifecycle.start_session()` for the `session_started` event -- see
        `rekha/ledger/model.py`'s `Event` docstring for why this isn't
        repeated on every event.
        """
        with self._lock_for(session_id), DBSession(self._engine) as db:
            last = db.scalars(
                select(EventRow)
                .where(EventRow.session_id == session_id)
                .order_by(EventRow.id.desc())
                .limit(1)
            ).first()
            prev_hash = last.hash if last is not None else GENESIS_HASH

            event = Event(
                event_id=str(uuid.uuid4()),
                session_id=session_id,
                step_seq=step_seq,
                type=type,
                at=self._clock.now().isoformat(),
                payload=payload,
                set_hash=set_hash,
                prev_hash=prev_hash,
                hash="",
                initiated_by=initiated_by,
                on_behalf_of=on_behalf_of,
            )
            event.hash = compute_hash(event)

            row = EventRow(
                event_id=event.event_id,
                session_id=event.session_id,
                step_seq=event.step_seq,
                type=event.type,
                at=event.at,
                payload=event.payload,
                set_hash=event.set_hash,
                prev_hash=event.prev_hash,
                hash=event.hash,
                initiated_by=event.initiated_by,
                on_behalf_of=event.on_behalf_of,
            )
            db.add(row)
            db.commit()
            return event

    def read(self, session_id: str) -> list[Event]:
        """Read every event of one session, in append order."""
        with DBSession(self._engine) as db:
            rows = db.scalars(
                select(EventRow).where(EventRow.session_id == session_id).order_by(EventRow.id)
            ).all()
            return [_row_to_event(row) for row in rows]

    def read_all(self) -> list[Event]:
        """Read every event in the store, across all sessions, in append order."""
        with DBSession(self._engine) as db:
            rows = db.scalars(select(EventRow).order_by(EventRow.id)).all()
            return [_row_to_event(row) for row in rows]

    def read_by_types(self, types: Iterable[str]) -> list[Event]:
        """Every event of the given `types`, across all sessions, in append order.

        Added for the cumulative/velocity fold (`rekha/policy/cumulative.py`),
        which needs five of the twenty event types and previously would have had
        to pull the whole table via `read_all()`. That matters because
        `state_captured` and `result_recorded` carry entire upstream responses,
        so they dominate the table's size while being irrelevant to a limit
        decision -- and a limit is evaluated on the path of every money-moving
        action, not once at startup.

        Filtered in SQL against an index on `type`, not in Python.
        """
        wanted = list(types)
        if not wanted:
            return []
        with DBSession(self._engine) as db:
            rows = db.scalars(
                select(EventRow).where(EventRow.type.in_(wanted)).order_by(EventRow.id)
            ).all()
            return [_row_to_event(row) for row in rows]
