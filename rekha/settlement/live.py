"""The SETTLED leg from Razorpay's real settlement reconciliation API.

Separated from `verify.py` because the verifier is a pure fold and this is the one
piece that does I/O. Keeping them apart is what lets a verdict be recomputed from
evidence without credentials or a network.

**Verified against a real account:** a brand-new Razorpay test-mode key returns
HTTP 200 with zero records from `/settlements` and `/settlements/recon/combined`
(see `scripts/check_razorpay.py`). The endpoints are reachable and authorized;
settlement is simply a real banking event on a real cycle, and a test account that
has never taken a payment has never been settled. So this source is correct and
currently empty, and `FixtureSettlementSource` is what makes the verifier
exercisable. Which source ran is always reported, never implied.

Read-only: this module issues GETs and nothing else. It could not move money if
asked to.
"""

from __future__ import annotations

import base64
from typing import Any


class LiveSettlementSource:
    """Settlement reconciliation entries from the Razorpay REST API.

    Uses the REST endpoint directly rather than going through the governed MCP
    proxy, and that asymmetry is deliberate: the proxy exists to gate actions that
    *change* things, and routing a read-only verification query through it would
    add a ledger entry and an approval surface to a step whose entire purpose is to
    audit from outside. Verification must not be able to affect what it verifies.
    """

    name = "live"

    def __init__(
        self, key_id: str, key_secret: str, *, year: int, month: int, timeout: float = 60.0
    ) -> None:
        if not key_id.startswith("rzp_test_"):
            raise ValueError(
                f"refusing to run against a non-test key ({key_id[:12]}...); "
                f"this project is test-mode only"
            )
        self._auth = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
        self._year = year
        self._month = month
        self._timeout = timeout
        #: Set after a call, so a report can say whether the API was reached at all
        #: rather than conflating "no settlements" with "could not ask".
        self.last_status: int | None = None
        self.last_error: str = ""

    def recon_entries(self) -> list[dict[str, Any]]:
        """Fetch itemised reconciliation for the configured month.

        Returns an empty list on any failure rather than raising, and records why
        in `last_error`. A verification run must not crash because settlement data
        is unavailable -- unavailable data is a *verdict* (`unverifiable`), and
        turning it into an exception would lose that distinction.
        """
        import httpx

        url = (
            f"https://api.razorpay.com/v1/settlements/recon/combined"
            f"?year={self._year}&month={self._month}&count=100"
        )
        try:
            response = httpx.get(
                url,
                headers={"Authorization": f"Basic {self._auth}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

        self.last_status = response.status_code
        if response.status_code != 200:
            self.last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            return []

        try:
            body = response.json()
        except ValueError as exc:
            self.last_error = f"response was not JSON: {exc}"
            return []

        items = body.get("items") if isinstance(body, dict) else None
        return [dict(item) for item in items] if isinstance(items, list) else []
