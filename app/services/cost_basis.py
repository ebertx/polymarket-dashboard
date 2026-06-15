"""Cost-basis derivation for tracked positions.

Pure, dependency-free (Decimal only) so it is unit-testable without standing
up the async database stack.
"""
from decimal import Decimal
from typing import Optional


def resolve_cost_basis(
    *,
    api_avg: Optional[Decimal],
    shares: Decimal,
    old_shares: Optional[Decimal],
    old_entry_price: Optional[Decimal],
    old_cost_basis: Optional[Decimal],
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """Resolve (entry_price, cost_basis) for an open position after a sync.

    The Polymarket Data API's ``avg_price`` is the authoritative average entry
    of the *currently-open* shares. It is correct across all mutations:
      - add / average-down: reports the new blended basis
      - partial sell: average of the remaining shares is unchanged
      - fresh fill: reports the fill price

    Sourcing the basis from ``avg_price`` every cycle self-heals the historical
    artifact where a frozen ``entry_price`` was scaled proportionally on an add
    (overstating ``cost_basis``) or simply left stale.

    Fallback: when the Data API has not yet computed ``avg_price`` (the brief
    lag right after a fill, reported as None or 0), preserve the prior
    ``entry_price`` and scale the existing basis proportionally to the new share
    count — so the basis is neither zeroed out nor left attributed to the old
    share quantity.
    """
    if api_avg is not None and api_avg > 0:
        return api_avg, shares * api_avg

    # avg_price unavailable — preserve entry_price, scale basis to new shares.
    if (
        old_shares is not None
        and old_shares > 0
        and old_cost_basis is not None
        and shares != old_shares
    ):
        return old_entry_price, old_cost_basis * (shares / old_shares)

    return old_entry_price, old_cost_basis
