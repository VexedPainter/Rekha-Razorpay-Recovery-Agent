"""Per-identity rolling quota of approved-and-executed irreversible actions (plan-v2 E15).

Same philosophy as E10's `rekha.policy.baseline.BaselineStore`: read prior
ledger events, keep no second parallel in-memory store of truth. Unlike
E10 (per-session), quota is scoped per `initiated_by` **identity** (E14) --
one identity's sessions can span many `session_id`s, so `QuotaTracker`
reads `LedgerStore.read_all()` and groups by session to find which ones
belong to the identity in question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from rekha.ledger.store import LedgerStore

_WINDOW_RE = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(text: str) -> timedelta:
    """Parse `"1d"` / `"7d"` / `"12h"` / `"30m"` / `"45s"` into a `timedelta`."""
    match = _WINDOW_RE.match(text.strip())
    if match is None:
        raise ValueError(f"invalid quota window {text!r}, expected e.g. '1d', '7d', '12h'")
    value, unit = match.groups()
    return timedelta(seconds=int(value) * _UNIT_SECONDS[unit])


@dataclass
class QuotaTracker:
    """Counts one identity's approved-and-executed irreversible actions within a rolling window.

    Delegates the fold to `rekha/policy/cumulative.py`. Before this, the two
    modules each walked the ledger with their own copy of the same logic --
    including the `plan_id`-not-`step_seq` subtlety that stops a human-approved
    action being counted twice. Two copies of that reasoning is one copy too
    many: if they ever disagreed, one of two limits would be silently wrong.
    """

    ledger: LedgerStore

    def count(self, identity: str, *, now: datetime, window: timedelta) -> int:
        """Count `identity`'s irreversible actions that were both approved (or
        auto-allowed) *and* actually executed, with the deciding
        `policy_evaluated` event timestamped within `window` of `now`.

        Boundary rule: an event exactly `window` old still counts (`now - at
        <= window`); anything older does not.
        """
        from rekha.policy.cumulative import (
            FOLD_EVENT_TYPES,
            fold_authorized_actions,
            in_window,
        )

        result = fold_authorized_actions(self.ledger.read_by_types(FOLD_EVENT_TYPES))
        return sum(
            1
            for action in in_window(result.actions, now=now, window=window)
            if action.identity == identity and action.reversibility == "irreversible"
        )


