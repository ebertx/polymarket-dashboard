"""Tests for TrackerService resolution detection and auto-close booking.

Regression suite for the 2026-08-11 close-detection diagnosis:

  * a resolved market must close its positions even while the Data API keeps
    returning them (redemption, not resolution, is what removes a position);
  * winners must book at exactly 1.0 and losers at exactly 0.0, never at the
    last CLOB midpoint (the July Fed winner was booked at 0.9995);
  * an unresolved market whose end_date has passed must NOT be closed;
  * the api-miss counter must survive a container restart.

Uses a fake AsyncSession and a fake PolymarketClient rather than a database:
_sync_positions issues exactly one SELECT, and the ORM objects are usable as
plain objects while transient. Async tests are driven through asyncio.run so no
pytest-asyncio dependency is needed.

Runnable two ways:
    pytest tests/test_tracker_resolution.py
    python tests/test_tracker_resolution.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import Market, Position
from app.services import tracker as tracker_module
from app.services.tracker import (
    AUTO_CLOSE_MISS_THRESHOLD,
    RESOLUTION_SWEEP_MAX_CLOSES,
    TrackerService,
)

NOW = datetime.now(timezone.utc)
PAST = NOW - timedelta(days=7)
FUTURE = NOW + timedelta(days=30)
CLOSED_TIME = "2026-08-04 13:09:41+00"
CLOSED_TIME_DT = datetime(2026, 8, 4, 13, 9, 41, tzinfo=timezone.utc)

GAMMA_NOT_CLOSED = {
    "closed": False, "uma_status": None, "outcome_prices": None,
    "closed_time": None, "slug": None,
}
GAMMA_YES_WINNER = {
    "closed": True, "uma_status": "resolved", "outcome_prices": '["1", "0"]',
    "closed_time": CLOSED_TIME, "slug": "some-market",
}
GAMMA_NO_WINNER = {
    "closed": True, "uma_status": "resolved", "outcome_prices": '["0", "1"]',
    "closed_time": CLOSED_TIME, "slug": "some-market",
}


def D(x):
    return Decimal(str(x))


# --- fakes ------------------------------------------------------------------

class FakeResult:
    def __init__(self, rows=(), scalar=None):
        self._rows = list(rows)
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar if self._scalar is not None else 0


class FakeSession:
    """Answers the handful of SELECTs the tracker issues.

    The join query returns the canned (Position, Market) rows; the standalone
    market/position lookups in _auto_discover_positions get ``markets`` /
    ``positions`` respectively.
    """

    def __init__(self, rows, markets=None, positions=None):
        self.rows = rows
        self.markets = markets or {}      # slug or condition_id -> Market
        self.positions = positions or {}  # (market_id, direction) -> Position
        self.added = []

    async def execute(self, stmt):
        sql = str(stmt)
        if "JOIN markets" in sql:
            return FakeResult(rows=self.rows)
        if sql.startswith("SELECT markets") or " FROM markets" in sql:
            return FakeResult(scalar=next(iter(self.markets.values()), None))
        if " FROM positions" in sql:
            return FakeResult(scalar=next(iter(self.positions.values()), None))
        return FakeResult(rows=self.rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


class FakeClient:
    def __init__(self, resolutions=None, prices=None, metadata=None):
        self.resolutions = resolutions or {}
        self.prices = prices or {}
        self.metadata = metadata or {}
        self.resolution_calls = []
        self.price_calls = []

    async def get_market_resolution(self, token_id):
        self.resolution_calls.append(token_id)
        return self.resolutions.get(token_id, GAMMA_NOT_CLOSED)

    async def get_market_price(self, token_id):
        self.price_calls.append(token_id)
        return self.prices.get(token_id)

    async def lookup_market_by_token_id(self, token_id):
        return self.metadata.get(token_id)


def gamma_metadata(market):
    return {
        "conditionId": f"0xcond-{market.id}",
        "question": market.title,
        "slug": market.slug,
        "clobTokenIds": json.dumps([market.clob_token_id_yes, market.clob_token_id_no]),
        "endDate": "2026-08-04T16:00:00Z",
    }


def make_tracker(pairs, client, markets=None):
    """pairs: list of (position, market) rows the join query returns."""
    # Process-wide lookup throttle must not leak between tests.
    tracker_module._last_resolution_check.clear()
    session = FakeSession([(p, m) for p, m in pairs], markets=markets)
    tracker = TrackerService(session, client)

    async def _noop_clear(market_slug, market_title):
        return 0

    tracker.alert_service.clear_alerts_for_closed_position = _noop_clear
    return tracker


def make_pair(
    pid,
    *,
    direction="yes",
    shares="25",
    entry="0.09",
    current_price="0",
    end_date=None,
    resolved_at=None,
    resolution_outcome=None,
    api_miss_count=None,
):
    token_yes = f"token-yes-{pid}"
    market = Market(
        id=pid,
        slug=f"market-{pid}",
        title=f"Market {pid}",
        clob_token_id_yes=token_yes,
        clob_token_id_no=f"token-no-{pid}",
        end_date=end_date,
        resolved_at=resolved_at,
        resolution_outcome=resolution_outcome,
    )
    shares_d, entry_d = D(shares), D(entry)
    position = Position(
        id=pid,
        market_id=market.id,
        direction=direction,
        shares=shares_d,
        entry_price=entry_d,
        entry_date=PAST,
        cost_basis=shares_d * entry_d,
        current_price=D(current_price),
        current_value=shares_d * D(current_price),
        unrealized_pnl=shares_d * D(current_price) - shares_d * entry_d,
        status="open",
        api_miss_count=api_miss_count,
    )
    return position, market


def token_of(position, market):
    return market.clob_token_id_yes if position.direction == "yes" else market.clob_token_id_no


def api_pos(position, market, *, redeemable=False, size=None, price=None):
    shares = D(size) if size is not None else position.shares
    px = D(price) if price is not None else (position.current_price or D(0))
    return {
        "token_id": token_of(position, market),
        "size": shares,
        "avg_price": position.entry_price,
        "current_price": px,
        "value": shares * px,
        "redeemable": redeemable,
    }


def filler_pairs(count=2, start=900):
    """Live, unresolved positions so the API-outage guards stay quiet."""
    return [
        make_pair(start + i, entry="0.50", current_price="0.50", end_date=FUTURE)
        for i in range(count)
    ]


# --- tests ------------------------------------------------------------------

def test_resolved_winner_books_at_exactly_one():
    """A winner must book at 1.0, not at the stale 0.9995 midpoint (pid 98)."""
    pos, mkt = make_pair(98, shares="10", entry="0.65", current_price="0.9995",
                         end_date=PAST)
    fillers = filler_pairs()
    client = FakeClient({token_of(pos, mkt): GAMMA_YES_WINNER})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    api = [api_pos(pos, mkt)] + [api_pos(p, m) for p, m in fillers]
    asyncio.run(tracker._sync_positions(api))

    assert mkt.resolved_at == CLOSED_TIME_DT
    assert mkt.resolution_outcome == "yes"
    assert pos.status == "closed"
    assert pos.exit_price == Decimal("1.0")
    assert pos.realized_pnl == Decimal("3.50")
    assert pos.exit_date == CLOSED_TIME_DT
    assert pos.current_value == Decimal("0") and pos.unrealized_pnl == Decimal("0")
    assert "resolved yes" in pos.exit_reasoning
    # Filler positions untouched
    assert all(p.status == "open" for p, _ in fillers)


def test_redeemable_loser_still_in_api_is_closed_at_zero():
    """The exact pid-101 case: present in /positions with curPrice 0 and
    redeemable=True, market resolved NO, our side was YES."""
    pos, mkt = make_pair(101, shares="25", entry="0.09", current_price="0",
                         end_date=PAST)
    fillers = filler_pairs()
    client = FakeClient({token_of(pos, mkt): GAMMA_NO_WINNER})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    api = [api_pos(pos, mkt, redeemable=True)] + [api_pos(p, m) for p, m in fillers]
    asyncio.run(tracker._sync_positions(api))

    assert mkt.resolution_outcome == "no"
    assert pos.status == "closed"
    assert pos.exit_price == Decimal("0")
    assert pos.realized_pnl == -pos.cost_basis == Decimal("-2.25")
    assert pos.exit_date == CLOSED_TIME_DT


def test_unresolved_past_end_date_is_not_closed():
    """end_date is a placeholder that can precede UMA resolution — never close
    on the calendar alone."""
    pos, mkt = make_pair(1, end_date=PAST, current_price="0.30")
    fillers = filler_pairs()
    client = FakeClient()  # every lookup -> not closed
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    api = [api_pos(pos, mkt)] + [api_pos(p, m) for p, m in fillers]
    asyncio.run(tracker._sync_positions(api))

    assert client.resolution_calls == [token_of(pos, mkt)]
    assert pos.status == "open"
    assert mkt.resolved_at is None and mkt.resolution_outcome is None


def test_redeemable_triggers_lookup_even_with_future_end_date():
    pos, mkt = make_pair(2, end_date=FUTURE)
    fillers = filler_pairs()
    client = FakeClient({token_of(pos, mkt): GAMMA_NO_WINNER})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    api = [api_pos(pos, mkt, redeemable=True)] + [api_pos(p, m) for p, m in fillers]
    asyncio.run(tracker._sync_positions(api))

    assert client.resolution_calls == [token_of(pos, mkt)]
    assert pos.status == "closed" and pos.exit_price == Decimal("0")


def test_live_markets_cost_no_gamma_calls():
    """A portfolio of live markets must not hammer Gamma every 60s."""
    fillers = filler_pairs(4)
    client = FakeClient()
    tracker = make_tracker(fillers, client)

    asyncio.run(tracker._sync_positions([api_pos(p, m) for p, m in fillers]))

    assert client.resolution_calls == []
    assert all(p.status == "open" for p, _ in fillers)


def test_already_resolved_market_is_not_rechecked():
    pos, mkt = make_pair(3, end_date=PAST, resolved_at=CLOSED_TIME_DT,
                         resolution_outcome="yes")
    fillers = filler_pairs()
    client = FakeClient()
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    api = [api_pos(pos, mkt, redeemable=True)] + [api_pos(p, m) for p, m in fillers]
    asyncio.run(tracker._sync_positions(api))

    assert client.resolution_calls == []          # DB already knows
    assert pos.status == "closed" and pos.exit_price == Decimal("1.0")


def test_missing_position_with_resolved_market_books_at_resolution():
    """Redeemed winner: gone from the API AND resolved -> 1.0, not last price."""
    pos, mkt = make_pair(4, direction="yes", shares="50", entry="0.10",
                         current_price="0.9995", end_date=PAST)
    fillers = filler_pairs()
    client = FakeClient({token_of(pos, mkt): GAMMA_YES_WINNER})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    # Position absent from the API payload (redeemed), fillers present.
    asyncio.run(tracker._sync_positions([api_pos(p, m) for p, m in fillers]))

    assert pos.status == "closed"
    assert pos.exit_price == Decimal("1.0")
    assert pos.realized_pnl == Decimal("45.0")   # 50 x 1.0 - 5.00


def test_missing_position_unresolved_increments_persisted_miss_count():
    pos, mkt = make_pair(5, end_date=None, current_price="0.20")
    fillers = filler_pairs()
    client = FakeClient()
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    asyncio.run(tracker._sync_positions([api_pos(p, m) for p, m in fillers]))

    assert pos.status == "open"
    assert pos.api_miss_count == 1


def test_miss_count_survives_restart():
    """A restart used to zero the in-memory counter. With the count loaded from
    the DB at 2, this cycle is the third miss and must auto-close."""
    pos, mkt = make_pair(6, end_date=None, current_price="0.20",
                         api_miss_count=AUTO_CLOSE_MISS_THRESHOLD - 1)
    fillers = filler_pairs()
    client = FakeClient()
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    asyncio.run(tracker._sync_positions([api_pos(p, m) for p, m in fillers]))

    assert pos.status == "closed"
    assert pos.exit_price == Decimal("0.20")          # last known price
    assert pos.realized_pnl == Decimal("2.75")        # 25 x 0.20 - 2.25
    assert "3 sync cycles" in pos.exit_reasoning
    assert pos.api_miss_count == 0


def test_present_position_resets_miss_count():
    pos, mkt = make_pair(7, end_date=FUTURE, current_price="0.20", api_miss_count=2)
    fillers = filler_pairs()
    client = FakeClient()
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    api = [api_pos(pos, mkt)] + [api_pos(p, m) for p, m in fillers]
    asyncio.run(tracker._sync_positions(api))

    assert pos.status == "open" and pos.api_miss_count == 0


def test_sweep_refuses_to_mass_close_above_cap():
    pairs = [
        make_pair(10 + i, resolved_at=CLOSED_TIME_DT, resolution_outcome="no",
                  end_date=PAST)
        for i in range(RESOLUTION_SWEEP_MAX_CLOSES + 1)
    ]
    client = FakeClient()
    tracker = make_tracker(pairs, client)

    asyncio.run(tracker._sync_positions([api_pos(p, m) for p, m in pairs]))

    assert all(p.status == "open" for p, _ in pairs)


def test_sweep_closes_a_batch_at_the_cap():
    pairs = [
        make_pair(20 + i, resolved_at=CLOSED_TIME_DT, resolution_outcome="no",
                  end_date=PAST)
        for i in range(RESOLUTION_SWEEP_MAX_CLOSES)
    ]
    client = FakeClient()
    tracker = make_tracker(pairs, client)

    asyncio.run(tracker._sync_positions([api_pos(p, m) for p, m in pairs]))

    assert all(p.status == "closed" for p, _ in pairs)


def test_clob_refresh_skips_positions_closed_this_cycle():
    """The CLOB refresh re-queries 'open' positions from an uncommitted
    transaction; it must not re-price (and un-zero) a position just closed."""
    pos, mkt = make_pair(30, resolved_at=CLOSED_TIME_DT, resolution_outcome="no",
                         end_date=PAST)
    fillers = filler_pairs()
    client = FakeClient(prices={token_of(p, m): D("0.5") for p, m in fillers})
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    api = [api_pos(pos, mkt)] + [api_pos(p, m) for p, m in fillers]
    asyncio.run(tracker._sync_positions(api))
    refreshed = asyncio.run(tracker._refresh_prices_from_clob())

    assert pos.status == "closed" and pos.current_value == Decimal("0")
    assert token_of(pos, mkt) not in client.price_calls
    assert refreshed == len(fillers)


def test_end_date_lookup_is_throttled_across_cycles():
    """A market past end_date that UMA hasn't settled (dispute) must not be
    re-queried on every 60s poll."""
    pos, mkt = make_pair(60, end_date=PAST, current_price="0.30")
    fillers = filler_pairs()
    client = FakeClient()
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    api = [api_pos(pos, mkt)] + [api_pos(p, m) for p, m in fillers]
    asyncio.run(tracker._sync_positions(api))
    asyncio.run(tracker._sync_positions(api))

    assert client.resolution_calls == [token_of(pos, mkt)]
    assert pos.status == "open"


def test_redeemable_bypasses_the_throttle():
    pos, mkt = make_pair(61, end_date=PAST, current_price="0.30")
    fillers = filler_pairs()
    client = FakeClient()  # not resolved yet, so the position stays open
    tracker = make_tracker([(pos, mkt)] + fillers, client)

    api = [api_pos(pos, mkt, redeemable=True)] + [api_pos(p, m) for p, m in fillers]
    asyncio.run(tracker._sync_positions(api))
    asyncio.run(tracker._sync_positions(api))

    assert client.resolution_calls == [token_of(pos, mkt)] * 2


def test_settled_position_is_not_rediscovered():
    """A settled position stays in /positions until redeemed. Once the sweep has
    closed it, auto-discovery must not open a fresh one every cycle."""
    pos, mkt = make_pair(50, resolved_at=CLOSED_TIME_DT, resolution_outcome="no",
                         end_date=PAST)
    pos.status = "closed"                      # booked on an earlier cycle
    fillers = filler_pairs()
    client = FakeClient(metadata={token_of(pos, mkt): gamma_metadata(mkt)})
    # Only OPEN positions are in db_positions, so this token looks "unknown".
    tracker = make_tracker(fillers, client, markets={mkt.slug: mkt})

    api = [api_pos(pos, mkt, redeemable=True)] + [api_pos(p, m) for p, m in fillers]
    asyncio.run(tracker._sync_positions(api))

    assert tracker.db.added == []
    assert pos.status == "closed"


def test_unresolved_market_still_auto_discovers():
    """The guard above must be specific to resolved markets."""
    pos, mkt = make_pair(51, end_date=FUTURE, current_price="0.40")
    fillers = filler_pairs()
    client = FakeClient(metadata={token_of(pos, mkt): gamma_metadata(mkt)})
    tracker = make_tracker(fillers, client, markets={mkt.slug: mkt})

    api = [api_pos(pos, mkt)] + [api_pos(p, m) for p, m in fillers]
    asyncio.run(tracker._sync_positions(api))

    created = [obj for obj in tracker.db.added if isinstance(obj, Position)]
    assert len(created) == 1 and created[0].direction == "yes"


def test_api_outage_does_not_close_anything():
    """All positions missing: no closes, no Gamma calls driven by absence."""
    pairs = [make_pair(40 + i, end_date=PAST, current_price="0.30") for i in range(3)]
    client = FakeClient()
    tracker = make_tracker(pairs, client)

    asyncio.run(tracker._sync_positions([]))

    assert all(p.status == "open" for p, _ in pairs)
    assert all(p.api_miss_count in (None, 0) for p, _ in pairs)


def test_gamma_miss_for_unknown_token_warns_once_per_process():
    # A worthless still-redeemable token (Truth-Social bracket, pid 101) made
    # every 60s poll log the same warning. Warn once, then stay at debug.
    import logging
    tracker_module._gamma_miss_warned.clear()
    tracker = make_tracker([], FakeClient(metadata={}))
    records = []

    class _Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Grab(level=logging.DEBUG)
    tracker_module.logger.addHandler(handler)
    old_level = tracker_module.logger.level
    tracker_module.logger.setLevel(logging.DEBUG)
    try:
        for _ in range(3):
            asyncio.run(tracker._auto_discover_positions([{"token_id": "tok-dead"}]))
    finally:
        tracker_module.logger.removeHandler(handler)
        tracker_module.logger.setLevel(old_level)
    misses = [r for r in records if "returned no data" in r.getMessage()]
    assert [r.levelno for r in misses] == [logging.WARNING, logging.DEBUG, logging.DEBUG]


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"  ok {_name}")
    print("All tracker resolution tests passed.")
