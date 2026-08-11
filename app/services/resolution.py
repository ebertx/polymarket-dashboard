"""Market-resolution logic for the tracker.

Deliberately free of DB / async / HTTP imports so it can be unit-tested by
file-path load, the same way cost_basis.py and position_filter.py are.

Background (2026-08-11 diagnosis): the tracker had no resolution detection —
a position only closed when it *vanished* from the Polymarket Data API, which
happens on redemption, not on resolution. A losing negRisk bracket that nobody
bothers to redeem therefore stayed open forever, and when the resolved branch
did fire it booked ``shares x current_price`` (a stale CLOB midpoint) instead of
the true $0/$1 payout. This module supplies the two missing pieces: reading
resolution state out of a Gamma market payload, and computing the payout from
the resolved outcome rather than the last traded price.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, List, Optional

# Resolved markets report exact "0"/"1" outcome prices, but tolerate a little
# slop rather than silently failing to book a resolution. These thresholds are
# only ever consulted for a market Gamma has already flagged closed/resolved,
# so they cannot fire on a live market trading at 99c.
WINNER_PRICE_FLOOR = Decimal("0.99")
LOSER_PRICE_CEILING = Decimal("0.01")


@dataclass(frozen=True)
class ResolutionState:
    """What Gamma says about a market's resolution.

    ``outcome`` is 'yes' / 'no' / None. None with ``resolved=True`` means the
    market is settled but the winning side could not be determined, which
    forces the caller to fall back to the last known price.
    """

    resolved: bool
    outcome: Optional[str] = None
    resolved_at: Optional[datetime] = None


NOT_RESOLVED = ResolutionState(False, None, None)


def parse_gamma_timestamp(value: Any) -> Optional[datetime]:
    """Parse a Gamma timestamp into an aware UTC datetime, or None.

    Gamma is inconsistent: ``endDate`` is ``2026-08-04T16:00:00Z`` while
    ``closedTime`` is ``2026-08-04 13:09:41+00`` — a bare-hour offset that
    datetime.fromisoformat rejects before Python 3.11.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip().replace("Z", "+00:00")
    if re.search(r"[+-]\d{2}$", text):
        text += ":00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _coerce_prices(raw: Any) -> Optional[List[Decimal]]:
    """Gamma returns outcomePrices as a JSON *string* like '["0", "1"]'."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(raw, (list, tuple)):
        return None
    try:
        return [Decimal(str(p)) for p in raw]
    except (InvalidOperation, TypeError, ValueError):
        return None


def outcome_from_prices(raw: Any) -> Optional[str]:
    """Map outcomePrices to the winning direction.

    ``["1", "0"]`` -> 'yes', ``["0", "1"]`` -> 'no', anything ambiguous -> None.
    Index order follows Gamma's ``outcomes`` field, which is ``["Yes", "No"]``
    for every binary market we track.
    """
    prices = _coerce_prices(raw)
    if prices is None or len(prices) != 2:
        return None

    yes_price, no_price = prices
    if yes_price >= WINNER_PRICE_FLOOR and no_price <= LOSER_PRICE_CEILING:
        return "yes"
    if no_price >= WINNER_PRICE_FLOOR and yes_price <= LOSER_PRICE_CEILING:
        return "no"
    return None


def parse_resolution(raw: Optional[dict]) -> ResolutionState:
    """Read resolution state out of a Gamma market payload.

    Accepts the normalized dict produced by
    ``PolymarketClient.get_market_resolution`` (keys: closed, uma_status,
    outcome_prices, closed_time). A falsy payload means "not resolved".
    """
    if not raw:
        return NOT_RESOLVED

    uma_status = str(raw.get("uma_status") or "").strip().lower()
    closed = bool(raw.get("closed"))
    outcome = outcome_from_prices(raw.get("outcome_prices"))

    # Two acceptable proofs of settlement: UMA reports 'resolved', or Gamma has
    # closed the market AND its outcome prices have collapsed to an
    # unambiguous 0/1 pair. Both are post-resolution states. Notably NOT
    # accepted: end_date having passed — Polymarket end dates are placeholders
    # that routinely precede (and sometimes follow) actual UMA resolution.
    resolved = uma_status == "resolved" or (closed and outcome is not None)
    if not resolved:
        return NOT_RESOLVED

    return ResolutionState(
        resolved=True,
        outcome=outcome,
        resolved_at=parse_gamma_timestamp(raw.get("closed_time")),
    )


def should_check_resolution(
    *,
    resolved_at: Optional[datetime],
    end_date: Optional[datetime],
    now: datetime,
    redeemable: bool = False,
    missing_from_api: bool = False,
) -> bool:
    """Whether this market is worth a Gamma resolution lookup this cycle.

    Gates the call so the 60-second poll doesn't re-check every market forever:
      - already resolved in the DB -> nothing to learn
      - Data API says the position is redeemable -> settled, check now
      - position vanished from the Data API -> check before booking it
      - otherwise only once the market's end_date has passed
    """
    if resolved_at is not None:
        return False
    if redeemable or missing_from_api:
        return True
    return end_date is not None and end_date <= now


def compute_close_booking(
    *,
    direction: Optional[str],
    shares: Decimal,
    cost_basis: Decimal,
    resolution_outcome: Optional[str],
    last_price: Optional[Decimal],
) -> tuple[Decimal, Decimal]:
    """Return ``(exit_price, realized_pnl)`` for closing a position.

    With a known resolution outcome the payout is exactly $1 or $0 per share.
    Only when the outcome is genuinely unknown does this fall back to the last
    price, which for a resolved market can be an arbitrarily stale CLOB
    midpoint (``get_market_price`` returns None on an empty book, so
    ``current_price`` freezes at its last pre-resolution value).
    """
    shares = Decimal(str(shares or 0))
    cost_basis = Decimal(str(cost_basis or 0))

    outcome = (resolution_outcome or "").strip().lower() or None
    if outcome in ("yes", "no"):
        won = (direction or "").strip().lower() == outcome
        exit_price = Decimal("1.0") if won else Decimal("0")
    else:
        exit_price = Decimal(str(last_price)) if last_price is not None else Decimal("0")

    return exit_price, (shares * exit_price) - cost_basis
