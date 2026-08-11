"""Tests for app/services/resolution.py — Gamma resolution parsing and payout math.

Regression for the 2026-08-11 close-detection diagnosis: the tracker had no
resolution detection (positions closed only on redemption) and booked payouts at
the last CLOB midpoint instead of the true $0/$1. The payloads below are the real
Gamma responses for the Truth Social July 28 - August 4 brackets.

Runnable two ways:
    pytest tests/test_resolution.py
    python tests/test_resolution.py        # no pytest dependency required
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# Load the pure module by file path, bypassing app/services/__init__.py (which
# eagerly imports the async-DB / aiohttp stack). Same pattern as
# test_cost_basis.py and test_position_filter.py.
_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "services", "resolution.py",
)
_spec = importlib.util.spec_from_file_location("resolution", _MODULE_PATH)
_resolution = importlib.util.module_from_spec(_spec)
# Register before exec: @dataclass resolves its own module via sys.modules.
sys.modules["resolution"] = _resolution
_spec.loader.exec_module(_resolution)

parse_resolution = _resolution.parse_resolution
parse_gamma_timestamp = _resolution.parse_gamma_timestamp
outcome_from_prices = _resolution.outcome_from_prices
should_check_resolution = _resolution.should_check_resolution
compute_close_booking = _resolution.compute_close_booking

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

# Real Gamma payload (normalized) for the resolved losing bracket, pid 101.
LOSER_PAYLOAD = {
    "closed": True,
    "uma_status": "resolved",
    "outcome_prices": '["0", "1"]',
    "closed_time": "2026-08-04 13:09:41+00",
    "slug": "donald-trump-of-truth-social-posts-july-28-august-4-100-119",
}

# The winning bracket in the same event.
WINNER_PAYLOAD = {
    "closed": True,
    "uma_status": "resolved",
    "outcome_prices": '["1", "0"]',
    "closed_time": "2026-08-04 13:09:41+00",
    "slug": "donald-trump-of-truth-social-posts-july-28-august-4-160-179",
}

# What Gamma returns for a market that is NOT closed: an empty list, which the
# client normalizes to closed=False.
UNRESOLVED_PAYLOAD = {
    "closed": False,
    "uma_status": None,
    "outcome_prices": None,
    "closed_time": None,
    "slug": None,
}


def D(x):
    return Decimal(str(x))


# --- parse_resolution -------------------------------------------------------

def test_parse_resolution_yes_winner():
    state = parse_resolution(WINNER_PAYLOAD)
    assert state.resolved and state.outcome == "yes"
    assert state.resolved_at == datetime(2026, 8, 4, 13, 9, 41, tzinfo=timezone.utc)


def test_parse_resolution_no_winner():
    state = parse_resolution(LOSER_PAYLOAD)
    assert state.resolved and state.outcome == "no"


def test_parse_resolution_unresolved_and_empty():
    assert parse_resolution(UNRESOLVED_PAYLOAD).resolved is False
    assert parse_resolution(None).resolved is False
    assert parse_resolution({}).resolved is False


def test_parse_resolution_open_market_at_99c_is_not_resolved():
    """A live market trading at 99c must never read as resolved."""
    state = parse_resolution({
        "closed": False,
        "uma_status": None,
        "outcome_prices": '["0.995", "0.005"]',
    })
    assert state.resolved is False


def test_parse_resolution_closed_without_uma_status_uses_prices():
    """Some closed markets carry no umaResolutionStatus; 0/1 prices suffice."""
    state = parse_resolution({
        "closed": True, "uma_status": None, "outcome_prices": ["0", "1"],
    })
    assert state.resolved and state.outcome == "no"


def test_parse_resolution_uma_resolved_but_ambiguous_prices():
    """Settled but unknown winner: resolved with outcome None (caller falls back)."""
    state = parse_resolution({
        "closed": True, "uma_status": "resolved", "outcome_prices": '["0.5", "0.5"]',
    })
    assert state.resolved is True and state.outcome is None


def test_parse_resolution_proposed_is_not_resolved():
    state = parse_resolution({
        "closed": False, "uma_status": "proposed", "outcome_prices": '["0.4", "0.6"]',
    })
    assert state.resolved is False


# --- timestamp / price parsing ---------------------------------------------

def test_parse_gamma_timestamp_formats():
    # closedTime: space separator, bare-hour offset (rejected by fromisoformat <3.11)
    assert parse_gamma_timestamp("2026-08-04 13:09:41+00") == datetime(
        2026, 8, 4, 13, 9, 41, tzinfo=timezone.utc)
    # endDate: ISO with Z
    assert parse_gamma_timestamp("2026-08-04T16:00:00Z") == datetime(
        2026, 8, 4, 16, 0, tzinfo=timezone.utc)
    # naive -> assumed UTC
    assert parse_gamma_timestamp("2026-08-04T16:00:00").tzinfo == timezone.utc
    assert parse_gamma_timestamp(None) is None
    assert parse_gamma_timestamp("") is None
    assert parse_gamma_timestamp("not-a-date") is None


def test_outcome_from_prices_malformed_inputs():
    assert outcome_from_prices("not json") is None
    assert outcome_from_prices('["1"]') is None           # wrong arity
    assert outcome_from_prices('["1","0","0"]') is None
    assert outcome_from_prices('["a","b"]') is None
    assert outcome_from_prices(None) is None


# --- should_check_resolution ------------------------------------------------

def test_no_check_once_already_resolved():
    assert not should_check_resolution(
        resolved_at=NOW, end_date=NOW - timedelta(days=1), now=NOW, redeemable=True)


def test_redeemable_triggers_check_even_with_future_end_date():
    """The 100-119 signal: redeemable=True is settlement, whatever end_date says."""
    assert should_check_resolution(
        resolved_at=None, end_date=NOW + timedelta(days=30), now=NOW, redeemable=True)


def test_end_date_passed_triggers_check():
    assert should_check_resolution(
        resolved_at=None, end_date=NOW - timedelta(hours=1), now=NOW)


def test_live_market_is_not_checked():
    assert not should_check_resolution(
        resolved_at=None, end_date=NOW + timedelta(days=30), now=NOW)
    assert not should_check_resolution(resolved_at=None, end_date=None, now=NOW)


def test_missing_from_api_triggers_check():
    assert should_check_resolution(
        resolved_at=None, end_date=None, now=NOW, missing_from_api=True)


# --- compute_close_booking --------------------------------------------------

def test_winner_books_at_exactly_one():
    """pid 98 (Fed July winner) was booked at 0.9995 instead of 1.0000."""
    exit_price, pnl = compute_close_booking(
        direction="yes", shares=D(10), cost_basis=D("6.50"),
        resolution_outcome="yes", last_price=D("0.9995"),
    )
    assert exit_price == Decimal("1.0")
    assert pnl == Decimal("3.50")


def test_loser_books_at_exactly_zero():
    exit_price, pnl = compute_close_booking(
        direction="yes", shares=D(25), cost_basis=D("2.25"),
        resolution_outcome="no", last_price=D("0.13"),
    )
    assert exit_price == Decimal("0")
    assert pnl == Decimal("-2.25")


def test_no_direction_winner_books_at_one():
    exit_price, pnl = compute_close_booking(
        direction="no", shares=D(50), cost_basis=D("20"),
        resolution_outcome="no", last_price=None,
    )
    assert exit_price == Decimal("1.0") and pnl == Decimal("30")


def test_unknown_outcome_falls_back_to_last_price():
    exit_price, pnl = compute_close_booking(
        direction="yes", shares=D(10), cost_basis=D("5"),
        resolution_outcome=None, last_price=D("0.40"),
    )
    assert exit_price == Decimal("0.40") and pnl == Decimal("-1.0")


def test_unknown_outcome_and_no_price_books_at_zero():
    exit_price, pnl = compute_close_booking(
        direction="yes", shares=D(10), cost_basis=D("5"),
        resolution_outcome=None, last_price=None,
    )
    assert exit_price == Decimal("0") and pnl == Decimal("-5")


def test_outcome_and_direction_case_insensitive():
    exit_price, _ = compute_close_booking(
        direction="YES", shares=D(1), cost_basis=D(0),
        resolution_outcome="Yes", last_price=None,
    )
    assert exit_price == Decimal("1.0")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("All resolution tests passed.")
