"""Pure guards for validating Data API position payloads before ingestion.

Defensive guard against a Polymarket Data API failure mode observed 2026-06-29:
a query for our wallet (`GET /positions?user=<ours>`) intermittently returned a
*different* account's positions — a live FIFA World Cup bettor's book worth
~$13K. The tracker's auto-discover ingested them as ours, then auto-closed them
when they vanished on later cycles, booking ~$4,994 of phantom realized P&L and
briefly inflating the portfolio to a fictional $3,048 (real value ~$300).

Every Data API position echoes its true owner in `proxyWallet`. We drop any
position whose owner is not our configured wallet, so a misrouted/cross-account
API response can never again pollute the portfolio.

Kept dependency-free (stdlib only) so it can be unit-tested by file-path import
without pulling the async-DB / aiohttp stack.
"""
from typing import Dict, List, Tuple


def own_positions(
    positions: List[Dict], wallet_address: str
) -> Tuple[List[Dict], List[Dict]]:
    """Split raw Data API positions into (ours, foreign) by ``proxyWallet``.

    - Comparison is case-insensitive (API returns mixed checksum/lower casing).
    - A position whose ``proxyWallet`` is present and differs from ours is
      treated as foreign and excluded.
    - A position missing ``proxyWallet`` is kept (fail-open): a well-formed Data
      API response always includes it, and failing open avoids a future API
      shape change silently emptying the result — which would let the tracker
      auto-close the entire real book.
    - An empty/unset ``wallet_address`` disables filtering (returns all as ours)
      rather than dropping everything.
    """
    want = (wallet_address or "").strip().lower()
    if not want:
        return list(positions), []

    ours: List[Dict] = []
    foreign: List[Dict] = []
    for p in positions:
        owner = str(p.get("proxyWallet", "") or "").strip().lower()
        if owner and owner != want:
            foreign.append(p)
        else:
            ours.append(p)
    return ours, foreign
