"""Tests for scripts/rebuild_realized_from_activity.py — the pure ledger, join
and apply-planning logic. No DB, no HTTP.

Loads the script by file path (scripts/ is not a package), the same way
test_resolution.py loads app/services/resolution.py.

Runnable two ways:
    pytest tests/test_rebuild_realized.py
    python tests/test_rebuild_realized.py
"""
import importlib.util
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "rebuild_realized_from_activity.py",
)
_spec = importlib.util.spec_from_file_location("rebuild_realized", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["rebuild_realized"] = _mod
_spec.loader.exec_module(_mod)

build_ledgers = _mod.build_ledgers
join_ledgers = _mod.join_ledgers
plan_corrections = _mod.plan_corrections
cash_identity = _mod.cash_identity
carry_positions = _mod.carry_positions
equity_identity = _mod.equity_identity
D = Decimal

COND_A = "0xaaa"
COND_B = "0xbbb"
TOK_A_YES = "111"
TOK_A_NO = "112"
TOK_B_YES = "221"

T2025 = int(datetime(2025, 11, 1, tzinfo=timezone.utc).timestamp())
T_JAN = int(datetime(2026, 1, 10, tzinfo=timezone.utc).timestamp())
T_FEB = int(datetime(2026, 2, 10, tzinfo=timezone.utc).timestamp())
T_MAR = int(datetime(2026, 3, 10, tzinfo=timezone.utc).timestamp())
T_APR = int(datetime(2026, 4, 10, tzinfo=timezone.utc).timestamp())
NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def trade(side, size, price, ts, *, asset=TOK_A_YES, cond=COND_A, idx=0, usdc=None, title="Market A"):
    return {
        "type": "TRADE", "side": side, "asset": asset, "conditionId": cond,
        "outcomeIndex": idx, "outcome": "Yes" if idx == 0 else "No",
        "size": size, "price": price,
        "usdcSize": usdc if usdc is not None else round(size * price, 6),
        "timestamp": ts, "title": title, "slug": "market-a", "eventSlug": "event-a",
    }


def redeem(size, usdc, ts, *, cond=COND_A, idx=0, asset=""):
    return {
        "type": "REDEEM", "side": "", "asset": asset, "conditionId": cond,
        "outcomeIndex": idx, "outcome": "Yes" if idx == 0 else "No",
        "size": size, "usdcSize": usdc, "price": 0, "timestamp": ts,
        "title": "Market A", "slug": "market-a", "eventSlug": "event-a",
    }


def income(kind, usdc, ts):
    return {"type": kind, "asset": "", "conditionId": "", "outcomeIndex": 999,
            "size": usdc, "usdcSize": usdc, "price": 0, "timestamp": ts, "side": ""}


# ---------------------------------------------------------------- ledger ---

def test_buy_sell_average_cost():
    rows = [
        trade("BUY", 10, 0.5, T_JAN),          # cost 5
        trade("BUY", 10, 0.7, T_JAN + 1),      # cost 7 -> avg 0.6
        trade("SELL", 5, 0.8, T_FEB),          # 4 - 5*0.6 = +1.0
    ]
    result = build_ledgers(rows, now=NOW)
    led = result.assets[TOK_A_YES]
    assert led.shares == D("15")
    assert led.cost == D("9.0")
    assert led.avg_cost == D("0.6")
    assert led.realized(2026) == D("1.0")
    assert led.status == "open"
    assert led.direction == "yes"
    assert len(led.sells) == 1 and led.sells[0].size == D("5") and led.sells[0].price == D("0.8")


def test_multiple_partial_sells_use_running_average():
    rows = [
        trade("BUY", 10, 0.5, T_JAN),
        trade("BUY", 10, 0.7, T_JAN + 1),
        trade("SELL", 5, 0.8, T_FEB),          # +1.0, cost -> 6, shares 15
        trade("SELL", 10, 0.5, T_MAR),         # 5 - 10*0.6 = -1.0, cost -> 3, shares 5
    ]
    led = build_ledgers(rows, now=NOW).assets[TOK_A_YES]
    assert led.shares == D("5")
    assert led.cost == D("3.0")
    assert led.realized(2026) == D("0.0")
    assert len(led.sells) == 2
    # exit VWAP over sells: (5*0.8 + 10*0.5) / 15 = 0.6
    assert led.exit_vwap() == D("0.6")


def test_winner_redeem_matched_by_condition_and_outcome_with_empty_asset():
    rows = [
        trade("BUY", 20, 0.4, T_JAN),                      # cost 8
        redeem(20, 20, T_MAR, cond=COND_A, idx=0, asset=""),
    ]
    result = build_ledgers(rows, now=NOW)
    led = result.assets[TOK_A_YES]
    assert led.shares == D("0")
    assert led.realized(2026) == D("12.0")
    assert led.status == "closed"
    assert led.redeemed is True
    assert not led.flags
    assert result.unmatched_redeems == []
    assert led.exit_vwap() == D("1")


def test_winner_redeem_payout_mismatch_is_flagged_not_hidden():
    rows = [
        trade("BUY", 20, 0.4, T_JAN),
        redeem(19, 19, T_MAR),   # payout 19 for 20 shares held
    ]
    led = build_ledgers(rows, now=NOW).assets[TOK_A_YES]
    # realized uses the actual payout, shares wiped, mismatch flagged
    assert led.realized(2026) == D("11.0")
    assert led.shares == D("0")
    assert any("mismatch" in f for f in led.flags)


def test_loser_redeem_with_zero_usdc_wipes_shares_at_cost():
    rows = [
        trade("BUY", 20, 0.4, T_JAN),
        redeem(0, 0, T_MAR),     # feed reports size 0 for losers
    ]
    led = build_ledgers(rows, now=NOW).assets[TOK_A_YES]
    assert led.shares == D("0")
    assert led.cost == D("0")
    assert led.realized(2026) == D("-8.0")
    assert led.status == "closed"
    assert led.exit_vwap() == D("0")


def test_loser_redeem_with_nonzero_size_still_uses_our_net_shares():
    # Seen in the real feed: loser REDEEM rows sometimes carry size=50, usdc=0.
    rows = [
        trade("BUY", 20, 0.4, T_JAN),
        redeem(50, 0, T_MAR),
    ]
    led = build_ledgers(rows, now=NOW).assets[TOK_A_YES]
    assert led.realized(2026) == D("-8.0")
    assert led.shares == D("0")


def test_redeem_on_no_side_only_matches_outcome_index_1():
    rows = [
        trade("BUY", 10, 0.3, T_JAN, asset=TOK_A_YES, idx=0),   # cost 3
        trade("BUY", 10, 0.6, T_JAN, asset=TOK_A_NO, idx=1),    # cost 6
        redeem(0, 0, T_MAR, idx=0),       # YES lost
        redeem(10, 10, T_MAR, idx=1),     # NO won
    ]
    result = build_ledgers(rows, now=NOW)
    assert result.assets[TOK_A_YES].realized(2026) == D("-3.0")
    assert result.assets[TOK_A_NO].realized(2026) == D("4.0")
    assert result.assets[TOK_A_NO].direction == "no"


def test_unmatched_redeem_is_reported():
    rows = [redeem(5, 5, T_MAR, cond="0xnobody", idx=0)]
    result = build_ledgers(rows, now=NOW)
    assert len(result.unmatched_redeems) == 1
    assert result.assets == {}


def test_zero_payout_redeem_labelled_with_winner_index_wipes_our_side():
    # Pre-V2 feed rows label a REDEEM with the *winning* outcomeIndex. We held
    # YES (idx 0), NO won: the row says idx=1, usdc=0. Our YES is a loser,
    # booked at the redeem timestamp, no Gamma needed.
    rows = [
        trade("BUY", 20, 0.4, T_JAN),
        redeem(0, 0, T_MAR, idx=1),
    ]
    resolver = _fake_resolver({})
    result = build_ledgers(rows, resolver=resolver, now=NOW)
    led = result.assets[TOK_A_YES]
    assert led.shares == D("0")
    assert led.realized(2026) == D("-8.0")
    assert led.status == "closed"
    assert led.redeemed is True
    assert led.last_exit_ts == T_MAR
    assert result.unmatched_redeems == []
    assert resolver.calls == []


def test_winner_redeem_also_closes_our_losing_side_on_same_condition():
    rows = [
        trade("BUY", 10, 0.3, T_JAN, asset=TOK_A_YES, idx=0),   # cost 3
        trade("BUY", 10, 0.6, T_JAN, asset=TOK_A_NO, idx=1),    # cost 6
        redeem(10, 10, T_MAR, idx=1),                            # single row: NO won, paid 10
    ]
    result = build_ledgers(rows, resolver=_fake_resolver({}), now=NOW)
    assert result.assets[TOK_A_NO].realized(2026) == D("4.0")
    assert result.assets[TOK_A_YES].realized(2026) == D("-3.0")
    assert result.assets[TOK_A_YES].status == "closed"
    assert result.assets[TOK_A_YES].last_exit_ts == T_MAR


def test_zero_payout_redeem_with_nothing_held_is_a_noop_not_unmatched():
    rows = [
        trade("BUY", 10, 0.3, T_JAN), trade("SELL", 10, 0.5, T_FEB),
        redeem(0, 0, T_MAR, idx=1),
    ]
    result = build_ledgers(rows, now=NOW)
    assert result.unmatched_redeems == []
    assert result.noop_redeems == 1
    assert result.assets[TOK_A_YES].realized(2026) == D("2.0")


def test_dust_remainder_is_written_off_not_left_open():
    rows = [trade("BUY", 10, 0.3, T_JAN), trade("SELL", 9.99, 0.5, T_FEB, usdc=4.995)]
    resolver = _fake_resolver({})
    led = build_ledgers(rows, resolver=resolver, now=NOW).assets[TOK_A_YES]
    assert led.status == "closed"
    assert led.shares == D("0")
    assert any("dust" in f for f in led.flags)
    assert resolver.calls == []
    # 4.995 - 9.99*0.3 = 1.998, then dust write-off of 0.01*0.3 = 0.003
    assert led.realized(2026) == D("1.995")


def _fake_resolver(table):
    calls = []

    def resolve(condition_id):
        calls.append(condition_id)
        return table.get(condition_id)

    resolve.calls = calls
    return resolve


def test_unredeemed_resolved_winner_via_gamma():
    rows = [trade("BUY", 10, 0.3, T_JAN)]
    resolver = _fake_resolver({
        COND_A: {"resolved": True, "outcome": "yes",
                 "resolved_at": datetime(2026, 3, 1, tzinfo=timezone.utc), "prices": None},
    })
    led = build_ledgers(rows, resolver=resolver, now=NOW).assets[TOK_A_YES]
    assert resolver.calls == [COND_A]
    assert led.status == "resolved_unredeemed"
    assert led.realized(2026) == D("7.0")      # 10 * (1 - 0.3)
    assert led.shares == D("0")
    assert led.exit_vwap() == D("1")


def test_unredeemed_resolved_loser_via_gamma_uses_direction():
    rows = [trade("BUY", 10, 0.6, T_JAN, asset=TOK_A_NO, idx=1)]   # NO side, cost 6
    resolver = _fake_resolver({
        COND_A: {"resolved": True, "outcome": "yes",
                 "resolved_at": datetime(2026, 3, 1, tzinfo=timezone.utc), "prices": None},
    })
    led = build_ledgers(rows, resolver=resolver, now=NOW).assets[TOK_A_NO]
    assert led.status == "resolved_unredeemed"
    assert led.realized(2026) == D("-6.0")
    assert led.exit_vwap() == D("0")


def test_resolved_unredeemed_without_resolved_at_attributes_to_now():
    rows = [trade("BUY", 10, 0.3, T2025)]
    resolver = _fake_resolver({COND_A: {"resolved": True, "outcome": "yes", "resolved_at": None, "prices": None}})
    led = build_ledgers(rows, resolver=resolver, now=NOW).assets[TOK_A_YES]
    assert led.realized(2026) == D("7.0")
    assert led.realized(2025) == D("0")


def test_open_remainder_reported_as_unrealized():
    rows = [trade("BUY", 10, 0.3, T_JAN), trade("SELL", 4, 0.5, T_FEB)]
    resolver = _fake_resolver({COND_A: {"resolved": False, "outcome": None, "resolved_at": None,
                                        "prices": [D("0.45"), D("0.55")]}})
    led = build_ledgers(rows, resolver=resolver, now=NOW).assets[TOK_A_YES]
    assert led.status == "open"
    assert led.shares == D("6")
    assert led.avg_cost == D("0.3")
    assert led.realized(2026) == D("0.8")
    assert led.mark_price == D("0.45")
    assert led.unrealized() == D("0.9")        # 6 * (0.45 - 0.3)


def test_open_remainder_without_resolver_stays_open_and_no_gamma_call():
    rows = [trade("BUY", 10, 0.3, T_JAN)]
    led = build_ledgers(rows, resolver=None, now=NOW).assets[TOK_A_YES]
    assert led.status == "open"
    assert led.mark_price is None


def test_closed_assets_do_not_hit_resolver():
    rows = [trade("BUY", 10, 0.3, T_JAN), trade("SELL", 10, 0.5, T_FEB)]
    resolver = _fake_resolver({})
    build_ledgers(rows, resolver=resolver, now=NOW)
    assert resolver.calls == []


def test_year_attribution_follows_exit_timestamp():
    rows = [
        trade("BUY", 10, 0.5, T2025),          # bought in 2025
        trade("SELL", 4, 0.6, T2025 + 100),    # +0.4 in 2025
        trade("SELL", 6, 0.7, T_FEB),          # +1.2 in 2026
    ]
    led = build_ledgers(rows, now=NOW).assets[TOK_A_YES]
    assert led.realized(2025) == D("0.4")
    assert led.realized(2026) == D("1.2")
    assert led.realized() == D("1.6")
    assert led.first_buy_ts == T2025
    assert led.last_exit_ts == T_FEB


def test_sell_exceeding_holdings_is_flagged_and_clamped():
    rows = [trade("BUY", 5, 0.5, T_JAN), trade("SELL", 8, 0.6, T_FEB, usdc=4.8)]
    led = build_ledgers(rows, now=NOW).assets[TOK_A_YES]
    assert led.shares == D("0")
    assert any("oversell" in f for f in led.flags)


def test_yield_and_reward_summed_separately_by_year():
    rows = [
        income("YIELD", 0.0013, T_FEB), income("YIELD", 0.0027, T_MAR),
        income("REWARD", 0.2228, T2025), income("YIELD", 0.5, T2025),
        trade("BUY", 1, 0.5, T_JAN),
    ]
    result = build_ledgers(rows, now=NOW)
    assert result.yield_by_year[2026] == D("0.0040")
    assert result.yield_by_year[2025] == D("0.7228")
    assert result.yield_count == 4
    assert result.assets[TOK_A_YES].realized() == D("0")


def test_rows_are_processed_in_timestamp_order_regardless_of_input_order():
    rows = [
        trade("SELL", 5, 0.8, T_FEB),
        trade("BUY", 10, 0.5, T_JAN),
    ]
    led = build_ledgers(rows, now=NOW).assets[TOK_A_YES]
    assert led.realized(2026) == D("1.5")
    assert not any("oversell" in f for f in led.flags)


# ------------------------------------------------------------------ join ---

def db_row(pid, token, *, status="closed", realized="0", direction="yes", cond=COND_A,
           exit_year=2026, shares="10", cost_basis="5", current_value=None, market_id=1,
           slug="market-a"):
    return {
        "id": pid, "market_id": market_id, "direction": direction,
        "shares": D(shares), "entry_price": D("0.5"), "cost_basis": D(cost_basis),
        "exit_price": None, "realized_pnl": D(realized), "status": status,
        "entry_date": datetime(exit_year, 1, 5, tzinfo=timezone.utc),
        "exit_date": datetime(exit_year, 3, 5, tzinfo=timezone.utc) if status == "closed" else None,
        "current_price": D("0.7"), "current_value": D(current_value) if current_value else None,
        "exit_reasoning": None, "slug": slug, "title": "Market A", "condition_id": cond,
        "token_yes": token if direction == "yes" else None,
        "token_no": token if direction == "no" else None,
    }


def test_join_sums_multiple_db_rows_per_token_and_categorises():
    rows = [
        trade("BUY", 10, 0.5, T_JAN), trade("SELL", 10, 1.2, T_FEB, usdc=12),   # +7 on TOK_A_YES
        trade("BUY", 10, 0.5, T_JAN, asset=TOK_B_YES, cond=COND_B),
        trade("SELL", 4, 0.75, T_FEB, asset=TOK_B_YES, cond=COND_B),            # +1 partial, still open
        trade("BUY", 5, 0.2, T_JAN, asset="999", cond="0xccc"),
        trade("SELL", 5, 0.4, T_MAR, asset="999", cond="0xccc"),                 # +1, no DB row
    ]
    ledgers = build_ledgers(rows, now=NOW)
    db_rows = [
        db_row(1, TOK_A_YES, realized="3"),
        db_row(2, TOK_A_YES, realized="2"),                    # re-entry, same token
        db_row(3, TOK_B_YES, status="open", cond=COND_B, market_id=2, current_value="4.2"),
        db_row(4, "555", cond="0xddd", market_id=3, realized="-1.5"),   # closed, no feed activity
        db_row(5, TOK_A_YES, realized="9", exit_year=2025),    # outside year: ignored in sums
    ]
    joined = join_ledgers(ledgers, db_rows, year=2026)

    by_asset = {r.asset: r for r in joined.per_asset}
    a = by_asset[TOK_A_YES]
    assert a.fills_realized == D("7.0")
    assert a.db_realized == D("5")
    assert a.diff == D("2.0")
    assert sorted(a.db_ids) == [1, 2]
    assert 5 not in a.db_ids

    b = by_asset[TOK_B_YES]
    assert b.db_realized == D("0")
    assert b.fills_realized == D("1.0")
    assert [r.asset for r in joined.open_partial] == [TOK_B_YES]

    assert [r.asset for r in joined.backfill_candidates] == ["999"]
    assert [r["id"] for r in joined.suspect_db_rows] == [4]

    assert joined.total_fills_realized == D("9.0")
    assert joined.total_db_realized == D("3.5")          # 3 + 2 + 0 + (-1.5)
    assert joined.max_abs_diff == D("2.0")


def test_join_falls_back_to_condition_and_direction_when_token_missing():
    rows = [trade("BUY", 10, 0.5, T_JAN, asset=TOK_A_NO, idx=1), trade("SELL", 10, 0.6, T_FEB, asset=TOK_A_NO, idx=1)]
    ledgers = build_ledgers(rows, now=NOW)
    row = db_row(7, None, direction="no", realized="1")
    row["token_no"] = None
    joined = join_ledgers(ledgers, [row], year=2026)
    a = joined.per_asset[0]
    assert a.db_ids == [7]
    assert a.diff == D("0")
    assert "condition" in a.join_kind


def test_join_heuristic_link_for_rows_with_no_ids_requires_unique_match():
    rows = [
        trade("BUY", 10, 0.5, T_JAN, asset="777", cond="0xeee", idx=1, title="Old manual"),
        trade("SELL", 10, 0.3, T_FEB, asset="777", cond="0xeee", idx=1),   # -2.0
    ]
    ledgers = build_ledgers(rows, now=NOW)
    row = db_row(16, None, direction="no", realized="-2.00", cond=None, shares="10", slug="manual-slug")
    row["token_no"] = None
    joined = join_ledgers(ledgers, [row], year=2026)
    a = joined.per_asset[0]
    assert a.db_ids == [16]
    assert a.join_kind == "heuristic"
    assert joined.backfill_candidates == []
    assert joined.suspect_db_rows == []


def test_join_manual_link_overrides_and_accepts_asset_prefix():
    rows = [trade("BUY", 25, 0.4, T_JAN, asset="12332295999", cond="0xlla"), redeem(25, 25, T_MAR, cond="0xlla")]
    ledgers = build_ledgers(rows, now=NOW)
    row = db_row(2, None, realized="7.53", cond=None, shares="15", slug="la-metro")
    joined = join_ledgers(ledgers, [row], year=2026, manual_links={"12332295": 2})
    a = joined.per_asset[0]
    assert a.db_ids == [2] and a.join_kind == "manual"
    assert a.diff == D("7.47")            # 15.00 - 7.53
    assert joined.suspect_db_rows == [] and joined.backfill_candidates == []
    assert joined.heuristic_links == [("12332295999", 2)]


def test_join_only_reports_assets_with_activity_in_year():
    rows = [trade("BUY", 10, 0.5, T2025), trade("SELL", 10, 0.6, T2025 + 5)]
    ledgers = build_ledgers(rows, now=NOW)
    joined = join_ledgers(ledgers, [], year=2026)
    assert joined.per_asset == []
    assert joined.backfill_candidates == []


# ------------------------------------------------------------ identities ---

def test_cash_identity_windows_flows_to_snapshot_span():
    rows = [
        trade("BUY", 10, 0.5, T_JAN),                 # before start snapshot: excluded
        trade("BUY", 10, 0.4, T_FEB),                 # -4
        trade("SELL", 5, 0.6, T_MAR),                 # +3
        redeem(5, 5, T_APR),                          # +5
        income("YIELD", 0.5, T_MAR),                  # +0.5
    ]
    ledgers = build_ledgers(rows, now=NOW)
    start_ts = datetime(2026, 1, 31, tzinfo=timezone.utc)
    end_ts = datetime(2026, 9, 1, tzinfo=timezone.utc)
    ident = cash_identity(ledgers, start_cash=D("100"), start_ts=start_ts, end_ts=end_ts, end_cash=D("104.30"))
    assert ident["buys"] == D("4.0")
    assert ident["sells"] == D("3.0")
    assert ident["redeems"] == D("5")
    assert ident["yield"] == D("0.5")
    assert ident["expected_end_cash"] == D("104.5")
    assert ident["residual"] == D("-0.20")


def test_equity_identity():
    ident = equity_identity(start_capital=D("229.13"), fills_realized=D("50"), yield_income=D("0.1"),
                            unrealized=D("10.5"), latest_total_value=D("290.00"))
    assert ident["expected_total_value"] == D("289.73")
    assert ident["residual"] == D("0.27")


def test_carry_positions_reports_holdings_at_year_start():
    rows = [
        trade("BUY", 10, 0.5, T2025),                     # 2025: cost 5
        trade("SELL", 4, 0.6, T2025 + 100),               # 2025: cost out 2 -> 6 sh, cost 3
        trade("SELL", 6, 0.7, T_FEB),                     # 2026
        trade("BUY", 5, 0.2, T_JAN, asset=TOK_B_YES, cond=COND_B),   # bought in-year: not carried
    ]
    ledgers = build_ledgers(rows, now=NOW)
    carried = carry_positions(ledgers, 2026)
    assert [(c.asset, c.shares, c.cost) for c in carried] == [(TOK_A_YES, D("6"), D("3.0"))]


def test_carry_mark_from_resolution_uses_payout_when_resolved_before_boundary():
    boundary = datetime(2026, 1, 1, tzinfo=timezone.utc)
    won = {"resolved": True, "outcome": "yes", "resolved_at": datetime(2025, 9, 17, tzinfo=timezone.utc)}
    assert _mod.carry_mark_from_resolution(won, "yes", boundary) == D("1")
    assert _mod.carry_mark_from_resolution(won, "no", boundary) == D("0")
    later = {"resolved": True, "outcome": "yes", "resolved_at": datetime(2026, 1, 2, tzinfo=timezone.utc)}
    assert _mod.carry_mark_from_resolution(later, "yes", boundary) is None
    assert _mod.carry_mark_from_resolution({"resolved": False}, "yes", boundary) is None
    assert _mod.carry_mark_from_resolution(None, "yes", boundary) is None


def test_equity_identity_subtracts_carry_gain():
    ident = equity_identity(start_capital=D("229.13"), fills_realized=D("50"), yield_income=D("0.1"),
                            unrealized=D("10.5"), latest_total_value=D("290.00"), carry_gain=D("-4.90"))
    assert ident["expected_total_value"] == D("294.63")
    assert ident["residual"] == D("-4.63")


# ----------------------------------------------------------- apply plan ---

def test_plan_corrections_updates_inserts_and_leaves_matching_rows_alone():
    rows = [
        trade("BUY", 10, 0.5, T_JAN), trade("SELL", 10, 1.2, T_FEB, usdc=12),                 # +7 (db says 3)
        trade("BUY", 10, 0.5, T_JAN, asset=TOK_B_YES, cond=COND_B),
        trade("SELL", 4, 0.75, T_FEB, asset=TOK_B_YES, cond=COND_B),                          # +1 partial
        trade("BUY", 5, 0.2, T_JAN, asset="999", cond="0xccc", title="No row"),
        redeem(5, 5, T_MAR, cond="0xccc", idx=0),                                             # +4, no DB row
        trade("BUY", 5, 0.2, T_JAN, asset="888", cond="0xfff"), trade("SELL", 5, 0.5, T_MAR, asset="888", cond="0xfff"),  # +1.5 matches db
    ]
    ledgers = build_ledgers(rows, now=NOW)
    db_rows = [
        db_row(1, TOK_A_YES, realized="3"),
        db_row(3, TOK_B_YES, status="open", cond=COND_B, market_id=2),
        db_row(8, "888", cond="0xfff", market_id=4, realized="1.50"),
    ]
    joined = join_ledgers(ledgers, db_rows, year=2026)
    today = "2026-09-25"
    plan = plan_corrections(joined, ledgers, year=2026, today=today, existing_market_ids_by_condition={})

    kinds = sorted((a.kind, a.position_id) for a in plan)
    assert ("update_closed", 1) in kinds
    assert ("update_open", 3) in kinds
    assert all(a.position_id != 8 for a in plan)
    inserts = [a for a in plan if a.kind == "insert"]
    assert len(inserts) == 1

    upd = next(a for a in plan if a.kind == "update_closed")
    assert upd.new_realized == D("7.0")
    assert upd.old_realized == D("3")
    assert upd.exit_price == D("1.2")
    assert upd.exit_reasoning_suffix == f" | fills-reconciled {today} (was 3.00)"

    opn = next(a for a in plan if a.kind == "update_open")
    assert opn.new_realized == D("1.0")
    assert opn.exit_price is None

    ins = inserts[0]
    assert ins.market is not None and ins.market["condition_id"] == "0xccc"
    assert ins.market["clob_token_id_yes"] == "999" and ins.market["clob_token_id_no"] is None
    assert ins.position["direction"] == "yes"
    assert ins.position["shares"] == D("5")
    assert ins.position["entry_price"] == D("0.2")
    assert ins.position["cost_basis"] == D("1.0")
    assert ins.position["exit_price"] == D("1")
    assert ins.position["realized_pnl"] == D("4.0")
    assert ins.position["status"] == "closed"
    assert ins.position["api_miss_count"] == 0
    assert ins.position["exit_reasoning"] == f"backfill from Data API activity feed {today}"
    assert ins.position["entry_date"] == datetime.fromtimestamp(T_JAN, tz=timezone.utc)
    assert ins.position["exit_date"] == datetime.fromtimestamp(T_MAR, tz=timezone.utc)


def test_plan_reuses_existing_market_row_and_skips_open_unlinked_assets():
    rows = [
        trade("BUY", 5, 0.2, T_JAN, asset="999", cond="0xccc"), trade("SELL", 5, 0.4, T_MAR, asset="999", cond="0xccc"),
        trade("BUY", 5, 0.2, T_JAN, asset="998", cond="0xccd"), trade("SELL", 2, 0.4, T_MAR, asset="998", cond="0xccd"),  # still open
    ]
    ledgers = build_ledgers(rows, now=NOW)
    joined = join_ledgers(ledgers, [], year=2026)
    plan = plan_corrections(joined, ledgers, year=2026, today="2026-09-25",
                            existing_market_ids_by_condition={"0xccc": 42})
    inserts = [a for a in plan if a.kind == "insert"]
    assert len(inserts) == 1
    assert inserts[0].market is None
    assert inserts[0].market_id == 42
    assert inserts[0].asset == "999"


def test_plan_never_links_one_market_row_to_two_conditions():
    rows = [
        trade("BUY", 10, 0.5, T_JAN, asset="771", cond="0xsp1", idx=1), trade("SELL", 10, 0.3, T_FEB, asset="771", cond="0xsp1", idx=1),  # -2
        trade("BUY", 10, 0.5, T_JAN, asset="772", cond="0xsp2", idx=0), trade("SELL", 10, 0.32, T_FEB, asset="772", cond="0xsp2", idx=0),  # -1.8
    ]
    ledgers = build_ledgers(rows, now=NOW)
    r16 = db_row(16, None, direction="no", realized="-2.00", cond=None, market_id=16, slug="spacex")
    r17 = db_row(17, None, direction="yes", realized="-1.80", cond=None, market_id=16, slug="spacex")
    r16["token_no"] = None
    r17["token_yes"] = None
    joined = join_ledgers(ledgers, [r16, r17], year=2026)
    assert sorted(joined.heuristic_links) == [("771", 16), ("772", 17)]
    plan = plan_corrections(joined, ledgers, year=2026, today="2026-09-25", existing_market_ids_by_condition={})
    assert [a for a in plan if a.kind == "link_market"] == []
    assert any("shared by 2 conditions" in f for a in ("771", "772") for f in ledgers.assets[a].flags)


def test_plan_multiple_db_rows_adjusts_only_latest_row_by_residual():
    rows = [trade("BUY", 10, 0.5, T_JAN), trade("SELL", 10, 1.2, T_FEB, usdc=12)]   # +7
    ledgers = build_ledgers(rows, now=NOW)
    r1 = db_row(1, TOK_A_YES, realized="3")
    r2 = db_row(2, TOK_A_YES, realized="2")
    r2["exit_date"] = datetime(2026, 4, 1, tzinfo=timezone.utc)
    joined = join_ledgers(ledgers, [r1, r2], year=2026)
    plan = plan_corrections(joined, ledgers, year=2026, today="2026-09-25", existing_market_ids_by_condition={})
    assert len(plan) == 1
    assert plan[0].position_id == 2
    assert plan[0].new_realized == D("4.0")      # 2 + (7 - 5)
    assert "token total" in plan[0].exit_reasoning_suffix


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
