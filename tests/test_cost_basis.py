"""Tests for resolve_cost_basis — the cost-basis derivation used by the tracker.

Runnable two ways:
    pytest tests/test_cost_basis.py
    python tests/test_cost_basis.py        # no pytest dependency required
"""
import importlib.util
import os
from decimal import Decimal

# Load the pure module directly by file path. This deliberately bypasses the
# `app` package machinery: importing `app.services.*` triggers
# app/services/__init__.py, which eagerly imports the full async-DB stack and
# fails in any environment without the pinned SQLAlchemy (e.g. local pyenv).
# The function under test depends only on Decimal, so file-path loading gives a
# faithful test of the real source without that coupling.
_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "services", "cost_basis.py",
)
_spec = importlib.util.spec_from_file_location("cost_basis", _MODULE_PATH)
_cost_basis = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cost_basis)
resolve_cost_basis = _cost_basis.resolve_cost_basis


def D(x):
    return Decimal(str(x))


def test_add_uses_data_api_blended_basis():
    """Average-down: basis must come from avg_price, not proportional scaling.

    Regression for the Iran quirk: 14 sh carrying a stale 0.83 entry, then 20
    added at 0.15. Old behavior scaled 14*0.83=11.62 by 34/14 -> 28.22. The
    Data API reports the true blended avg 0.364, so basis must be 34*0.364.
    """
    entry, basis = resolve_cost_basis(
        api_avg=D("0.364"),
        shares=D("34"),
        old_shares=D("14"),
        old_entry_price=D("0.83"),
        old_cost_basis=D("11.62"),
    )
    assert entry == D("0.364")
    assert basis == D("34") * D("0.364")  # 12.376, not 28.22


def test_partial_sell_keeps_avg_unchanged():
    """Selling part of a position: avg of remainder is unchanged."""
    entry, basis = resolve_cost_basis(
        api_avg=D("0.50"),
        shares=D("10"),
        old_shares=D("25"),
        old_entry_price=D("0.50"),
        old_cost_basis=D("12.50"),
    )
    assert entry == D("0.50")
    assert basis == D("5.0")


def test_fresh_fill_lag_falls_back_to_proportional_scaling():
    """avg_price not yet computed (0): preserve entry, scale basis to new shares."""
    entry, basis = resolve_cost_basis(
        api_avg=D("0"),
        shares=D("30"),
        old_shares=D("20"),
        old_entry_price=D("0.40"),
        old_cost_basis=D("8.00"),
    )
    assert entry == D("0.40")  # unchanged
    assert basis == D("8.00") * (D("30") / D("20"))  # 12.00


def test_avg_price_missing_and_no_share_change_is_noop():
    """No avg_price and no share change: leave basis exactly as-is."""
    entry, basis = resolve_cost_basis(
        api_avg=None,
        shares=D("9"),
        old_shares=D("9"),
        old_entry_price=D("0.83"),
        old_cost_basis=D("7.47"),
    )
    assert entry == D("0.83")
    assert basis == D("7.47")


def test_stable_position_recomputes_to_same_basis():
    """A correct, unchanged position re-derives to the identical basis."""
    entry, basis = resolve_cost_basis(
        api_avg=D("0.21"),
        shares=D("48"),
        old_shares=D("48"),
        old_entry_price=D("0.21"),
        old_cost_basis=D("10.08"),
    )
    assert entry == D("0.21")
    assert basis == D("48") * D("0.21")  # 10.08


if __name__ == "__main__":
    import sys

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
