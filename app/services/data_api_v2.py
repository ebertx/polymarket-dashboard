"""Polymarket Data API v2 helpers (pure, stdlib only, unit-testable by file path).

Data API v1 (bare /positions, /activity, ...) was retired on 2026-11-02: every v1
route now answers HTTP 410. v2 differences absorbed here:

  * routes under /v2; payloads wrapped in {"data": [...], "pagination": {...}}
  * cursor pagination via pagination.next_cursor (``offset`` is a 400)
  * snake_case fields (token_id, current_size, current_price, proxy_wallet, ...)
  * /v2/positions returns status=OPEN by default and accepts ONE status per call;
    v1 /positions also returned resolved-but-unredeemed rows (``redeemable``), which
    the tracker's resolution logic depends on -> query OPEN + REDEEMABLE +
    REDEEMABLE_LOST and dedupe (the two redeemable statuses overlap)
  * ``sizeThreshold`` is silently ignored; v1's default hid positions < 1 share, so
    apply that floor client-side to keep the tracker's input identical

The tracker and position_filter read v1 camelCase keys; ``to_v1_position`` /
``to_v1_activity`` add them next to the v2 keys so that code is unchanged.

Mirror of polymarket-team/data/scripts/data_api.py (sync client); keep the alias
tables in step.
"""
from typing import Dict, Iterable, List

DATA_API_V2 = "https://data-api.polymarket.com/v2"
POSITION_STATUSES = ("OPEN", "REDEEMABLE", "REDEEMABLE_LOST")
V1_SIZE_THRESHOLD = 1.0  # v1 /positions default sizeThreshold

_POS_V1 = {
    "proxy_wallet": "proxyWallet", "token_id": "asset", "condition_id": "conditionId",
    "current_size": "size", "avg_price": "avgPrice", "entry_cost_usdc": "initialValue",
    "current_value": "currentValue", "unrealized_pnl": "cashPnl", "percent_pnl": "percentPnl",
    "total_size": "totalBought", "realized_pnl": "realizedPnl",
    "percent_realized_pnl": "percentRealizedPnl", "current_price": "curPrice",
    "event_slug": "eventSlug", "outcome_index": "outcomeIndex",
    "opposite_outcome": "oppositeOutcome", "opposite_token_id": "oppositeAsset",
    "end_date": "endDate", "negative_risk": "negativeRisk",
}
_ACT_V1 = {
    "proxy_wallet": "proxyWallet", "condition_id": "conditionId", "usdc_size": "usdcSize",
    "transaction_hash": "transactionHash", "token_id": "asset", "outcome_index": "outcomeIndex",
    "event_slug": "eventSlug",
}


def _with_aliases(row: Dict, table: Dict[str, str]) -> Dict:
    out = dict(row)
    for v2, v1 in table.items():
        if v2 in row and v1 not in out:
            out[v1] = row[v2]
    return out


def to_v1_position(row: Dict) -> Dict:
    return _with_aliases(row, _POS_V1)


def to_v1_activity(row: Dict) -> Dict:
    return _with_aliases(row, _ACT_V1)


def unwrap(envelope) -> List[Dict]:
    """Return the row list from a v2 envelope; raise ValueError on any other shape."""
    if not isinstance(envelope, dict) or "data" not in envelope:
        raise ValueError(f"unexpected Data API v2 payload: {type(envelope).__name__}")
    data = envelope["data"]
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError("Data API v2 'data' is not a list")
    return data


def next_cursor(envelope) -> str:
    return ((envelope or {}).get("pagination") or {}).get("next_cursor") or ""


def merge_positions(pages: Iterable[List[Dict]], min_size: float = V1_SIZE_THRESHOLD) -> List[Dict]:
    """Merge per-status row lists into v1-shaped rows: dedupe by token, apply v1 size floor."""
    seen: Dict[str, Dict] = {}
    for rows in pages:
        for r in rows:
            key = str(r.get("token_id") or r.get("condition_id"))
            seen.setdefault(key, r)
    out = []
    for r in seen.values():
        try:
            if float(r.get("current_size") or 0) < min_size:
                continue
        except (TypeError, ValueError):
            pass
        out.append(to_v1_position(r))
    return out
