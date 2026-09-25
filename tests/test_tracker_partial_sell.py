"""Tests for partial-sell realized P&L booking in TrackerService (pm-rfz.2).

Regression suite for the 2026 "vanishing profit-take" bug: when the Data API
reported fewer shares than the DB row, ``_sync_positions`` only shrank
``position.shares`` and never booked realized P&L for the sold slice. The later
close then *overwrote* ``realized_pnl`` with the close-only booking, so every
trim/profit-take of the year disappeared from the realized figure.

Covers:
  * a partial sell books ``sold * (fill - entry)`` from the activity feed;
  * successive partial sells accumulate;
  * empty / insufficient fill feed falls back to the Data API midpoint (WARNING);
  * a later resolution close ADDS to the prior partial realized;
  * a later "absent from API" auto-close likewise adds;
  * an add books nothing;
  * ``PolymarketClient.get_recent_sell_fills`` filtering (asset, wallet, since).

Reuses the fakes from tests/test_tracker_resolution.py; async tests are driven
through asyncio.run so no pytest-asyncio dependency is needed.
"""
import asyncio
import logging
import os
import sys
import time
from datetime import timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.polymarket import PolymarketClient
from app.services.tracker import AUTO_CLOSE_MISS_THRESHOLD
from test_tracker_resolution import (  # noqa: E402  (pytest prepends tests/)
    D,
    FUTURE,
    GAMMA_YES_WINNER,
    NOW,
    PAST,
    FakeClient as _BaseFakeClient,
    api_pos,
    filler_pairs,
    make_pair,
    make_tracker,
    token_of,
)


class FakeClient(_BaseFakeClient):
    """Base fake plus a canned sell-fill feed keyed by token_id.

    ``fills`` maps token_id -> list of (size, price) tuples, most recent first,
    exactly as the real client returns them. ``fills_error=True`` makes the
    feed raise, simulating a Data API outage.
    """

    def __init__(self, *args, fills=None, fills_error=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fills = fills or {}
        self.fills_error = fills_error
        self.fill_calls = []

    async def get_recent_sell_fills(self, token_id, since_ts):
        self.fill_calls.append((token_id, since_ts))
        if self.fills_error:
            raise RuntimeError("activity feed unavailable")
        return [(D(s), D(p)) for s, p in self.fills.get(token_id, [])]


def run_sync(tracker, api):
    asyncio.run(tracker._sync_positions(api))


# --- partial sells ------------------------------------------------------------

def test_partial_sell_books_slice_realized_from_fill():
    """Buy 40 @ 0.36, sell 20 @ 0.738 -> +7.56 realized, 20 shares still open."""
    pos, mkt = make_pair(1, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    fillers = filler_pairs()
    tok = token_of(pos, mkt)
    client = FakeClient(fills={tok: [("20", "0.738")]})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    api = [api_pos(pos, mkt, size="20", price="0.70")] + [api_pos(p, m) for p, m in fillers]
    run_sync(tracker, api)

    assert pos.status == "open"
    assert pos.shares == Decimal("20")
    assert pos.realized_pnl == Decimal("7.56")
    # Remaining shares re-derived from avg_price as before
    assert pos.entry_price == Decimal("0.36")
    assert pos.cost_basis == Decimal("7.20")
    assert client.fill_calls and client.fill_calls[0][0] == tok


def test_partial_sell_uses_size_weighted_average_of_covering_fills():
    """Two fills (12 @ 0.80 and 8 @ 0.70) covering a 20-share sell: weighted
    average 0.76 -> 20 * (0.76 - 0.36) = 8.00."""
    pos, mkt = make_pair(2, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    fillers = filler_pairs()
    tok = token_of(pos, mkt)
    client = FakeClient(fills={tok: [("12", "0.80"), ("8", "0.70"), ("100", "0.10")]})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    api = [api_pos(pos, mkt, size="20", price="0.70")] + [api_pos(p, m) for p, m in fillers]
    run_sync(tracker, api)

    # The stale 100 @ 0.10 fill must not be touched once the sell is covered.
    assert pos.realized_pnl == Decimal("8.00")


def test_two_successive_partial_sells_accumulate():
    pos, mkt = make_pair(3, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    fillers = filler_pairs()
    tok = token_of(pos, mkt)
    client = FakeClient(fills={tok: [("20", "0.738")]})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    run_sync(tracker, [api_pos(pos, mkt, size="20", price="0.70")]
             + [api_pos(p, m) for p, m in fillers])
    assert pos.realized_pnl == Decimal("7.56")

    # Second trim: 10 more @ 0.86 -> 10 * (0.86 - 0.36) = +5.00
    client.fills[tok] = [("10", "0.86"), ("20", "0.738")]
    run_sync(tracker, [api_pos(pos, mkt, size="10", price="0.85")]
             + [api_pos(p, m) for p, m in fillers])

    assert pos.shares == Decimal("10")
    assert pos.realized_pnl == Decimal("12.56")
    assert pos.status == "open"


def test_empty_fill_feed_books_at_midpoint_and_warns(caplog):
    pos, mkt = make_pair(4, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    fillers = filler_pairs()
    client = FakeClient()  # no fills at all
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    with caplog.at_level(logging.WARNING, logger="app.services.tracker"):
        run_sync(tracker, [api_pos(pos, mkt, size="20", price="0.70")]
                 + [api_pos(p, m) for p, m in fillers])

    # 20 * (0.70 - 0.36) = 6.80 at the Data API midpoint
    assert pos.realized_pnl == Decimal("6.80")
    assert any(
        r.levelno == logging.WARNING and "midpoint" in r.getMessage()
        for r in caplog.records
    )


def test_fill_feed_error_books_at_midpoint_and_warns(caplog):
    pos, mkt = make_pair(5, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    fillers = filler_pairs()
    client = FakeClient(fills_error=True)
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    with caplog.at_level(logging.WARNING, logger="app.services.tracker"):
        run_sync(tracker, [api_pos(pos, mkt, size="20", price="0.70")]
                 + [api_pos(p, m) for p, m in fillers])

    assert pos.status == "open" and pos.shares == Decimal("20")
    assert pos.realized_pnl == Decimal("6.80")
    assert any("midpoint" in r.getMessage() for r in caplog.records)


def test_partial_fill_coverage_falls_back_to_midpoint(caplog):
    """Feed only shows 5 of the 20 sold shares -> not trusted, use midpoint."""
    pos, mkt = make_pair(6, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    fillers = filler_pairs()
    tok = token_of(pos, mkt)
    client = FakeClient(fills={tok: [("5", "0.90")]})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    with caplog.at_level(logging.WARNING, logger="app.services.tracker"):
        run_sync(tracker, [api_pos(pos, mkt, size="20", price="0.70")]
                 + [api_pos(p, m) for p, m in fillers])

    assert pos.realized_pnl == Decimal("6.80")
    assert any("midpoint" in r.getMessage() for r in caplog.records)


def test_fill_within_tolerance_is_accepted():
    """Data API sizes are rounded; a feed covering 19.95 of 20 (0.25% short)
    is within the 0.5% tolerance and must be used."""
    pos, mkt = make_pair(7, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    fillers = filler_pairs()
    tok = token_of(pos, mkt)
    client = FakeClient(fills={tok: [("19.95", "0.738")]})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    run_sync(tracker, [api_pos(pos, mkt, size="20", price="0.70")]
             + [api_pos(p, m) for p, m in fillers])

    assert pos.realized_pnl == Decimal("20") * (Decimal("0.738") - Decimal("0.36"))


def test_add_books_nothing():
    pos, mkt = make_pair(8, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    fillers = filler_pairs()
    client = FakeClient()
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    run_sync(tracker, [api_pos(pos, mkt, size="60", price="0.70")]
             + [api_pos(p, m) for p, m in fillers])

    assert pos.shares == Decimal("60")
    assert pos.realized_pnl is None
    assert client.fill_calls == []


def test_unchanged_shares_book_nothing():
    pos, mkt = make_pair(9, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    fillers = filler_pairs()
    client = FakeClient()
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    run_sync(tracker, [api_pos(pos, mkt, price="0.75")]
             + [api_pos(p, m) for p, m in fillers])

    assert pos.realized_pnl is None
    assert client.fill_calls == []


def test_since_ts_derived_from_updated_at():
    """since_ts = updated_at - 15 min when set."""
    pos, mkt = make_pair(10, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    pos.updated_at = NOW - timedelta(hours=2)
    fillers = filler_pairs()
    tok = token_of(pos, mkt)
    client = FakeClient(fills={tok: [("20", "0.738")]})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    run_sync(tracker, [api_pos(pos, mkt, size="20", price="0.70")]
             + [api_pos(p, m) for p, m in fillers])

    (_, since_ts), = client.fill_calls
    expected = int((pos.updated_at - timedelta(minutes=15)).timestamp())
    assert since_ts == expected


def test_since_ts_defaults_to_24h_when_updated_at_unset():
    pos, mkt = make_pair(11, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    assert pos.updated_at is None
    fillers = filler_pairs()
    tok = token_of(pos, mkt)
    client = FakeClient(fills={tok: [("20", "0.738")]})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    before = time.time()
    run_sync(tracker, [api_pos(pos, mkt, size="20", price="0.70")]
             + [api_pos(p, m) for p, m in fillers])

    (_, since_ts), = client.fill_calls
    assert abs(since_ts - (before - 24 * 3600)) < 5


# --- close after partial sells ------------------------------------------------

def test_resolution_close_adds_to_prior_partial_realized():
    """Buy 40 @ 0.36, sell 20 @ 0.738 (+7.56); market resolves YES on the
    remaining 20 (+12.80). Final realized must be 20.36, not 12.80."""
    pos, mkt = make_pair(12, shares="40", entry="0.36", current_price="0.70",
                         end_date=FUTURE)
    fillers = filler_pairs()
    tok = token_of(pos, mkt)
    client = FakeClient(fills={tok: [("20", "0.738")]})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    run_sync(tracker, [api_pos(pos, mkt, size="20", price="0.70")]
             + [api_pos(p, m) for p, m in fillers])
    assert pos.realized_pnl == Decimal("7.56")

    # Now the market resolves YES (redeemable flag triggers the Gamma lookup).
    client.resolutions[tok] = GAMMA_YES_WINNER
    run_sync(tracker, [api_pos(pos, mkt, size="20", price="0.9995", redeemable=True)]
             + [api_pos(p, m) for p, m in fillers])

    assert pos.status == "closed"
    assert pos.exit_price == Decimal("1.0")
    assert pos.realized_pnl == Decimal("20.36")


def test_absent_from_api_auto_close_adds_to_prior_partial_realized():
    """Same position; instead of resolving, it vanishes from the API for
    AUTO_CLOSE_MISS_THRESHOLD cycles and is booked at last price."""
    pos, mkt = make_pair(13, shares="40", entry="0.36", current_price="0.70",
                         end_date=None)
    fillers = filler_pairs()
    tok = token_of(pos, mkt)
    client = FakeClient(fills={tok: [("20", "0.738")]})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    run_sync(tracker, [api_pos(pos, mkt, size="20", price="0.70")]
             + [api_pos(p, m) for p, m in fillers])
    assert pos.realized_pnl == Decimal("7.56")

    for _ in range(AUTO_CLOSE_MISS_THRESHOLD):
        run_sync(tracker, [api_pos(p, m) for p, m in fillers])

    assert pos.status == "closed"
    assert pos.exit_price == Decimal("0.70")
    # close booking: 20 * 0.70 - 7.20 = 6.80; plus 7.56 prior
    assert pos.realized_pnl == Decimal("14.36")


def test_close_without_prior_partial_is_unchanged():
    """No partial sells -> close booking is exactly what it was before."""
    pos, mkt = make_pair(14, shares="10", entry="0.65", current_price="0.9995",
                         end_date=PAST)
    fillers = filler_pairs()
    client = FakeClient({token_of(pos, mkt): GAMMA_YES_WINNER})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    run_sync(tracker, [api_pos(pos, mkt)] + [api_pos(p, m) for p, m in fillers])

    assert pos.status == "closed"
    assert pos.realized_pnl == Decimal("3.50")


# --- PolymarketClient.get_recent_sell_fills ------------------------------------

WALLET = "0xAbCdEf0000000000000000000000000000000001"


def _activity_row(asset, size, price, ts, wallet=WALLET, side="SELL"):
    return {
        "asset": asset,
        "side": side,
        "size": size,
        "price": price,
        "usdcSize": float(size) * float(price),
        "timestamp": ts,
        "proxyWallet": wallet,
    }


def test_get_recent_sell_fills_filters_asset_wallet_and_since():
    client = PolymarketClient(WALLET)
    calls = []
    now_ts = int(time.time())
    rows = [
        _activity_row("tok-A", 20, 0.738, now_ts - 60),            # keep
        _activity_row("tok-A", 5, 0.70, now_ts - 120),             # keep
        _activity_row("tok-B", 7, 0.50, now_ts - 60),              # wrong asset
        _activity_row("tok-A", 9, 0.60, now_ts - 60, wallet="0xdead"),  # foreign
        _activity_row("tok-A", 11, 0.55, now_ts - 10_000),         # too old
        _activity_row("tok-A", 3, 0.65, now_ts - 30, wallet=WALLET.lower()),  # case-insensitive
        _activity_row("tok-A", 4, 0.66, now_ts - 30, side="BUY"),  # defensive: not a sell
    ]

    async def fake_request(url, params=None):
        calls.append((url, params))
        return rows

    client._request = fake_request
    since_ts = now_ts - 3600

    fills = asyncio.run(client.get_recent_sell_fills("tok-A", since_ts))

    assert calls, "activity feed must be requested"
    url, params = calls[0]
    assert url == "https://data-api.polymarket.com/activity"
    assert params["user"] == WALLET.lower()
    assert params["type"] == "TRADE"
    assert params["side"] == "SELL"
    assert params["limit"] == 100

    # Most recent first, Decimals, only our sells of tok-A since since_ts.
    assert fills == [
        (Decimal("3"), Decimal("0.65")),
        (Decimal("20"), Decimal("0.738")),
        (Decimal("5"), Decimal("0.70")),
    ]
    assert all(isinstance(s, Decimal) and isinstance(p, Decimal) for s, p in fills)


def test_get_recent_sell_fills_returns_empty_on_error_or_bad_payload():
    client = PolymarketClient(WALLET)

    async def boom(url, params=None):
        raise RuntimeError("down")

    client._request = boom
    assert asyncio.run(client.get_recent_sell_fills("tok-A", 0)) == []

    async def not_a_list(url, params=None):
        return {"error": "nope"}

    client._request = not_a_list
    assert asyncio.run(client.get_recent_sell_fills("tok-A", 0)) == []


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn) and "caplog" not in _fn.__code__.co_varnames:
            _fn()
            print(f"  ok {_name}")
    print("Tracker partial-sell tests passed (caplog tests need pytest).")
