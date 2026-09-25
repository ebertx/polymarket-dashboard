"""Tests for the position attribution tags (beads pm-rfz.5).

Every position carries two nullable tags so P&L can be split by trade shape
(``strategy_tag``) and by how much research produced it (``pipeline_tag``):

  * the ORM model declares both columns and the vocabularies;
  * app/main.py's lifespan ensures both columns exist at startup with the same
    idempotent ALTER pattern used for ``api_miss_count``;
  * the positions API schema exposes both fields;
  * scripts/backfill_attribution_tags.py validates the vocabularies and plans a
    before/after diff without touching the DB (dry-run default).

Runnable two ways:
    pytest tests/test_attribution_tags.py
    python tests/test_attribution_tags.py
"""
import importlib.util
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.models.position import PIPELINE_TAGS, STRATEGY_TAGS, Position  # noqa: E402
from app.schemas.position import PositionResponse, PositionUpdate  # noqa: E402

_SCRIPT_PATH = os.path.join(ROOT, "scripts", "backfill_attribution_tags.py")
_spec = importlib.util.spec_from_file_location("backfill_attribution_tags", _SCRIPT_PATH)
_bf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _bf          # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(_bf)


# --- vocabularies ------------------------------------------------------------

def test_vocabularies_match_spec():
    assert STRATEGY_TAGS == frozenset({
        "value-midband", "carry-definitional", "carry-forecast",
        "fast-track-data", "longshot", "override",
    })
    assert PIPELINE_TAGS == frozenset({"full", "fast", "gate", "none"})


def test_backfill_script_vocab_matches_model():
    """The script can't import the async app stack, so it carries a copy."""
    assert _bf.STRATEGY_TAGS == STRATEGY_TAGS
    assert _bf.PIPELINE_TAGS == PIPELINE_TAGS


# --- ORM model ---------------------------------------------------------------

def test_position_model_has_nullable_tag_columns():
    cols = Position.__table__.columns
    assert "strategy_tag" in cols and "pipeline_tag" in cols
    assert cols["strategy_tag"].nullable and cols["pipeline_tag"].nullable
    assert cols["strategy_tag"].type.python_type is str
    assert cols["pipeline_tag"].type.python_type is str


def test_position_tags_default_to_none():
    pos = Position(direction="yes", shares=1, entry_price=0.5, cost_basis=0.5)
    assert pos.strategy_tag is None and pos.pipeline_tag is None


# --- startup schema ensure ----------------------------------------------------

def _main_source():
    with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("column", ["strategy_tag", "pipeline_tag"])
def test_lifespan_ensures_tag_column_like_api_miss_count(column):
    src = _main_source()
    # Same idempotent DO-block pattern as api_miss_count: ALTER guarded by
    # duplicate_column, so a second start is a no-op.
    pattern = re.compile(
        r"ALTER TABLE positions\s+ADD COLUMN " + column + r" VARCHAR\(\d+\);"
        r"\s+EXCEPTION\s+WHEN duplicate_column THEN NULL;",
        re.S,
    )
    assert pattern.search(src), f"no guarded ALTER for positions.{column} in app/main.py"
    assert f'Schema ensured: positions.{column}' in src
    assert f"could not add positions.{column}" in src


# --- API schema --------------------------------------------------------------

def test_position_response_exposes_tags():
    fields = PositionResponse.model_fields
    assert "strategy_tag" in fields and "pipeline_tag" in fields
    assert fields["strategy_tag"].default is None
    assert fields["pipeline_tag"].default is None


def test_position_update_validates_tag_vocabulary():
    ok = PositionUpdate(strategy_tag="value-midband", pipeline_tag="fast")
    assert ok.strategy_tag == "value-midband" and ok.pipeline_tag == "fast"
    with pytest.raises(ValueError):
        PositionUpdate(strategy_tag="moonshot")
    with pytest.raises(ValueError):
        PositionUpdate(pipeline_tag="partial")


# --- backfill script (pure parts) -------------------------------------------

def _write_csv(tmp_path, text):
    p = tmp_path / "tags.csv"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_rows_parses_csv_and_keeps_quoted_reason(tmp_path):
    p = _write_csv(tmp_path,
        "position_id,strategy_tag,pipeline_tag,reason\n"
        '98,override,gate,"user override, calibration size"\n'
        "64,fast-track-data,fast,Parcl bracket via Fast Track\n")
    rows = _bf.load_rows(p)
    assert [r.position_id for r in rows] == [98, 64]
    assert rows[0].strategy_tag == "override" and rows[0].pipeline_tag == "gate"
    assert rows[0].reason == "user override, calibration size"


def test_load_rows_rejects_bad_vocabulary_and_names_every_offender(tmp_path):
    p = _write_csv(tmp_path,
        "position_id,strategy_tag,pipeline_tag,reason\n"
        "1,value-midband,full,ok\n"
        "2,moonshot,full,bad strategy\n"
        "3,longshot,partial,bad pipeline\n")
    with pytest.raises(ValueError) as exc:
        _bf.load_rows(p)
    msg = str(exc.value)
    assert "position_id=2" in msg and "moonshot" in msg
    assert "position_id=3" in msg and "partial" in msg


def test_load_rows_rejects_duplicate_and_non_integer_ids(tmp_path):
    p = _write_csv(tmp_path,
        "position_id,strategy_tag,pipeline_tag,reason\n"
        "1,value-midband,full,a\n"
        "1,longshot,none,b\n")
    with pytest.raises(ValueError, match="duplicate"):
        _bf.load_rows(p)
    p2 = _write_csv(tmp_path,
        "position_id,strategy_tag,pipeline_tag,reason\n"
        "abc,value-midband,full,a\n")
    with pytest.raises(ValueError, match="position_id"):
        _bf.load_rows(p2)


def test_load_rows_requires_header_columns(tmp_path):
    p = _write_csv(tmp_path, "id,strategy,pipeline\n1,a,b\n")
    with pytest.raises(ValueError, match="header"):
        _bf.load_rows(p)


def test_plan_changes_diffs_against_current_and_flags_missing():
    rows = [
        _bf.TagRow(98, "override", "gate", "user override"),
        _bf.TagRow(64, "fast-track-data", "fast", "unchanged"),
        _bf.TagRow(999, "longshot", "none", "not in db"),
    ]
    current = {
        98: {"strategy_tag": None, "pipeline_tag": None, "title": "July Fed", "status": "closed"},
        64: {"strategy_tag": "fast-track-data", "pipeline_tag": "fast", "title": "Austin", "status": "closed"},
    }
    changes, unchanged, missing = _bf.plan_changes(rows, current)
    assert [c.position_id for c in changes] == [98]
    assert changes[0].before == (None, None)
    assert changes[0].after == ("override", "gate")
    assert [u.position_id for u in unchanged] == [64]
    assert missing == [999]


def test_format_table_shows_before_and_after():
    change = _bf.Change(98, "July Fed hike", "closed", (None, None), ("override", "gate"), "user override")
    table = _bf.format_table([change])
    assert "98" in table and "July Fed hike" in table
    assert "override" in table and "gate" in table
    assert "-/-" in table          # the "before" cell for untagged rows


def test_apply_changes_issues_one_update_per_change_and_commits():
    class Cur:
        def __init__(self):
            self.executed = []
        def execute(self, sql, params=None):
            self.executed.append((" ".join(sql.split()), params))
        def close(self):
            pass
    class Conn:
        def __init__(self):
            self.cur = Cur(); self.committed = False
        def cursor(self, **kw):
            return self.cur
        def commit(self):
            self.committed = True
    conn = Conn()
    changes = [
        _bf.Change(98, "t", "closed", (None, None), ("override", "gate"), "r"),
        _bf.Change(64, "t", "open", ("longshot", "none"), ("fast-track-data", "fast"), "r"),
    ]
    n = _bf.apply_changes(conn, changes)
    assert n == 2 and conn.committed
    assert len(conn.cur.executed) == 2
    sql, params = conn.cur.executed[0]
    assert "UPDATE positions SET strategy_tag = %s, pipeline_tag = %s" in sql
    assert params == ("override", "gate", 98)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
