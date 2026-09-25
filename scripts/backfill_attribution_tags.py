#!/usr/bin/env python3
"""Backfill position attribution tags from a CSV (beads pm-rfz.5).

Every position carries two tags so realized P&L can be split by trade shape
(``strategy_tag``) and by how much research produced it (``pipeline_tag``).
New positions get them from the desk's post_order_alerts.py; this script sets
them on historical rows from a reviewed CSV.

CSV columns (header required, extra columns ignored):
    position_id,strategy_tag,pipeline_tag,reason

Vocabulary
    strategy_tag  value-midband | carry-definitional | carry-forecast |
                  fast-track-data | longshot | override
    pipeline_tag  full | fast | gate | none

Modes
    (default)   dry run: validate the CSV, print a before/after table, exit 0
    --apply     write the tags in one transaction (asks to confirm unless --yes)

Usage
    python scripts/backfill_attribution_tags.py --csv attribution_backfill.csv
    python scripts/backfill_attribution_tags.py --csv attribution_backfill.csv --apply
    python scripts/backfill_attribution_tags.py --csv attribution_backfill.csv --apply --yes

Exit codes: 0 ok / nothing to do, 1 CSV invalid, 2 DB problem (unreachable,
columns missing — deploy the tracker first, its startup adds them), 3 CSV
names position ids that are not in the DB (nothing is written).

DB access mirrors scripts/rebuild_realized_from_activity.py: HOME_DB_* /
POSTGRES_* from --env-file, ~/.claude/credentials/.env, ./.env or
~/ai/polymarket-team/data/credentials/.env.
"""
from __future__ import annotations

import argparse
import csv
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# Copy of app.models.position.{STRATEGY_TAGS, PIPELINE_TAGS} — the app package
# drags in the async DB stack, which this script must not need. A test asserts
# the two stay identical.
STRATEGY_TAGS = frozenset({
    "value-midband", "carry-definitional", "carry-forecast",
    "fast-track-data", "longshot", "override",
})
PIPELINE_TAGS = frozenset({"full", "fast", "gate", "none"})

REQUIRED_COLUMNS = ("position_id", "strategy_tag", "pipeline_tag", "reason")


# =========================================================================== #
# Pure parts (unit-tested)
# =========================================================================== #

@dataclass(frozen=True)
class TagRow:
    position_id: int
    strategy_tag: str
    pipeline_tag: str
    reason: str


@dataclass(frozen=True)
class Change:
    position_id: int
    title: str
    status: str
    before: Tuple[Optional[str], Optional[str]]
    after: Tuple[str, str]
    reason: str


def load_rows(csv_path: Path) -> List[TagRow]:
    """Parse and validate the CSV. Raises ValueError naming every bad row."""
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise ValueError(f"CSV header missing column(s) {missing}; need {list(REQUIRED_COLUMNS)}")
        rows: List[TagRow] = []
        problems: List[str] = []
        seen: set = set()
        for lineno, raw in enumerate(reader, start=2):
            pid_raw = (raw.get("position_id") or "").strip()
            strat = (raw.get("strategy_tag") or "").strip()
            pipe = (raw.get("pipeline_tag") or "").strip()
            reason = (raw.get("reason") or "").strip()
            try:
                pid = int(pid_raw)
            except ValueError:
                problems.append(f"line {lineno}: position_id {pid_raw!r} is not an integer")
                continue
            if pid in seen:
                problems.append(f"line {lineno}: duplicate position_id={pid}")
            seen.add(pid)
            if strat not in STRATEGY_TAGS:
                problems.append(f"line {lineno}: position_id={pid} strategy_tag {strat!r} not in {sorted(STRATEGY_TAGS)}")
            if pipe not in PIPELINE_TAGS:
                problems.append(f"line {lineno}: position_id={pid} pipeline_tag {pipe!r} not in {sorted(PIPELINE_TAGS)}")
            rows.append(TagRow(pid, strat, pipe, reason))
    if problems:
        raise ValueError("invalid CSV:\n  " + "\n  ".join(problems))
    return rows


def plan_changes(rows: List[TagRow], current: Dict[int, dict]):
    """Diff the CSV against the DB rows.

    ``current`` maps position_id -> {"strategy_tag", "pipeline_tag", "title", "status"}.
    Returns (changes, unchanged, missing_ids).
    """
    changes: List[Change] = []
    unchanged: List[Change] = []
    missing: List[int] = []
    for row in rows:
        cur = current.get(row.position_id)
        if cur is None:
            missing.append(row.position_id)
            continue
        before = (cur.get("strategy_tag"), cur.get("pipeline_tag"))
        after = (row.strategy_tag, row.pipeline_tag)
        change = Change(row.position_id, cur.get("title") or "?", cur.get("status") or "?",
                        before, after, row.reason)
        (unchanged if before == after else changes).append(change)
    return changes, unchanged, missing


def _cell(pair: Tuple[Optional[str], Optional[str]]) -> str:
    return f"{pair[0] or '-'}/{pair[1] or '-'}"


def format_table(changes: List[Change], title_width: int = 44) -> str:
    if not changes:
        return "(no changes)"
    lines = [f"{'id':>5}  {'status':<6}  {'title':<{title_width}}  {'before':<32}  {'after':<32}  reason",
             "-" * (5 + 2 + 6 + 2 + title_width + 2 + 32 + 2 + 32 + 2 + 30)]
    for c in changes:
        t = (c.title or "")[:title_width]
        lines.append(f"{c.position_id:>5}  {c.status:<6}  {t:<{title_width}}  {_cell(c.before):<32}  {_cell(c.after):<32}  {c.reason}")
    return "\n".join(lines)


def apply_changes(conn, changes: List[Change]) -> int:
    """Write every change in one transaction. Returns the number of rows updated."""
    cur = conn.cursor()
    try:
        for c in changes:
            cur.execute(
                """
                UPDATE positions
                   SET strategy_tag = %s,
                       pipeline_tag = %s,
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (c.after[0], c.after[1], c.position_id),
            )
        conn.commit()
    finally:
        cur.close()
    return len(changes)


def tag_distribution(rows: List[TagRow]) -> Tuple[Dict[str, int], Dict[str, int]]:
    strat: Dict[str, int] = {}
    pipe: Dict[str, int] = {}
    for r in rows:
        strat[r.strategy_tag] = strat.get(r.strategy_tag, 0) + 1
        pipe[r.pipeline_tag] = pipe.get(r.pipeline_tag, 0) + 1
    return strat, pipe


# =========================================================================== #
# DB
# =========================================================================== #

def load_env(explicit: Optional[str] = None) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:            # env may already be exported (cron)
        return
    candidates = [explicit, os.getenv("RECON_ENV_FILE"), Path.home() / ".claude" / "credentials" / ".env",
                  REPO_ROOT / ".env", Path.home() / "ai" / "polymarket-team" / "data" / "credentials" / ".env"]
    for c in candidates:
        if c and Path(c).exists():
            load_dotenv(Path(c))


def resolve_db_host() -> str:
    """Local LAN, then Tailscale, then DNS — first host that accepts a TCP connect."""
    explicit = os.getenv("HOME_DB_HOST") or os.getenv("POSTGRES_HOST")
    candidates = [c for c in [explicit, "192.168.0.166", os.getenv("HOME_DB_HOST_TAILSCALE", "100.92.27.16"),
                              os.getenv("HOME_DB_HOST_DNS", "ebertx.duckdns.org")] if c]
    port = int(os.getenv("HOME_DB_PORT") or os.getenv("POSTGRES_PORT") or 5432)
    for host in candidates:
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
            return host
        except OSError:
            continue
    return candidates[0]


def get_connection():
    import psycopg2
    return psycopg2.connect(
        host=resolve_db_host(),
        port=os.getenv("HOME_DB_PORT") or os.getenv("POSTGRES_PORT") or 5432,
        user=os.getenv("HOME_DB_USER") or os.getenv("POSTGRES_USER"),
        password=os.getenv("HOME_DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD"),
        dbname=os.getenv("POLYBOT_DB_NAME") or os.getenv("POSTGRES_DATABASE") or "polybot",
        sslmode=os.getenv("POSTGRES_SSLMODE", "prefer"),
        connect_timeout=10,
    )


def tag_columns_present(conn) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.columns
             WHERE table_name = 'positions' AND column_name IN ('strategy_tag', 'pipeline_tag')
        """)
        return cur.fetchone()[0] == 2
    finally:
        cur.close()


def fetch_current(conn, ids: List[int]) -> Dict[int, dict]:
    if not ids:
        return {}
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT p.id, p.strategy_tag, p.pipeline_tag, p.status, m.title
              FROM positions p LEFT JOIN markets m ON p.market_id = m.id
             WHERE p.id = ANY(%s)
        """, (ids,))
        return {r[0]: {"strategy_tag": r[1], "pipeline_tag": r[2], "status": r[3], "title": r[4]}
                for r in cur.fetchall()}
    finally:
        cur.close()


# =========================================================================== #
# CLI
# =========================================================================== #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", required=True, help="CSV with position_id,strategy_tag,pipeline_tag,reason")
    p.add_argument("--apply", action="store_true", help="write the tags to the DB (one transaction)")
    p.add_argument("--yes", action="store_true", help="skip the interactive confirmation for --apply")
    p.add_argument("--env-file", help="explicit .env with HOME_DB_* / POSTGRES_* credentials")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        rows = load_rows(Path(args.csv))
    except (OSError, ValueError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    strat_dist, pipe_dist = tag_distribution(rows)
    print(f"CSV: {len(rows)} row(s) — strategy {dict(sorted(strat_dist.items()))}; pipeline {dict(sorted(pipe_dist.items()))}")

    load_env(args.env_file)
    try:
        conn = get_connection()
    except Exception as e:
        print(f"✗ could not connect to the tracker DB: {e}", file=sys.stderr)
        return 2
    try:
        if not tag_columns_present(conn):
            print("✗ positions.strategy_tag / pipeline_tag do not exist yet — deploy the tracker "
                  "(app/main.py's lifespan adds them at startup) and rerun.", file=sys.stderr)
            return 2
        current = fetch_current(conn, [r.position_id for r in rows])
        changes, unchanged, missing = plan_changes(rows, current)

        print(f"\nBefore/after ({len(changes)} change(s), {len(unchanged)} already correct, {len(missing)} missing):")
        print(format_table(changes))
        if missing:
            print(f"\n✗ {len(missing)} position id(s) not in the DB: {missing} — fix the CSV; nothing written.",
                  file=sys.stderr)
            return 3
        if not changes:
            print("\nNothing to do.")
            return 0
        if not args.apply:
            print(f"\n(dry run) --apply would update {len(changes)} row(s).")
            return 0
        if not args.yes:
            answer = input(f"\nApply {len(changes)} update(s) to the live DB? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted; nothing written.")
                return 0
        n = apply_changes(conn, changes)
        print(f"\nApplied {n} update(s). Re-run without --apply to verify (should print 'Nothing to do').")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
