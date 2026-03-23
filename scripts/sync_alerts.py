#!/usr/bin/env python3
"""
Sync alert definitions from alerts.yaml into the PostgreSQL alert_definitions table.

Usage:
    python scripts/sync_alerts.py [path/to/alerts.yaml]
    python scripts/sync_alerts.py --dry-run
    python scripts/sync_alerts.py --clear  # Remove all definitions first

If no path is given, defaults to ../polymarket-team/portfolio/alerts.yaml

Uses psycopg2 (synchronous) so it works in any Python environment.
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------


def _resolve_db_host():
    """Try local IP, then Tailscale, then DNS — return first reachable host."""
    import socket
    candidates = [
        os.getenv("HOME_DB_HOST", "192.168.0.166"),
        os.getenv("HOME_DB_HOST_TAILSCALE", "100.92.27.16"),
        os.getenv("HOME_DB_HOST_DNS", "ebertx.duckdns.org"),
    ]
    port = int(os.getenv("HOME_DB_PORT") or os.getenv("POSTGRES_PORT", 5432))
    for host in candidates:
        try:
            sock = socket.create_connection((host, port), timeout=2)
            sock.close()
            return host
        except (socket.timeout, socket.error, OSError):
            continue
    return candidates[0]


def get_connection():
    """Get a psycopg2 connection. Auto-detects best host (local/Tailscale/DNS)."""
    import psycopg2

    # Try loading from dotenv
    try:
        from dotenv import load_dotenv
        env_paths = [
            Path.home() / ".claude" / "credentials" / ".env",
            Path(__file__).parent.parent / ".env",
            Path.home() / "ai" / "polymarket-team" / "data" / "credentials" / ".env",
        ]
        for p in env_paths:
            if p.exists():
                load_dotenv(p)
                break
    except ImportError:
        pass

    host = _resolve_db_host()
    port = os.getenv("HOME_DB_PORT") or os.getenv("POSTGRES_PORT") or "5432"
    user = os.getenv("HOME_DB_USER") or os.getenv("POSTGRES_USER") or "ebertx"
    password = os.getenv("HOME_DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD") or ""
    database = os.getenv("POLYBOT_DB_NAME") or os.getenv("POSTGRES_DATABASE") or "polybot"

    conn = psycopg2.connect(
        host=host, port=port, user=user, password=password,
        dbname=database, sslmode="prefer", connect_timeout=10,
    )
    conn.autocommit = False
    return conn


# Map alert types to default severity
SEVERITY_MAP = {
    "drawdown": "WARNING",
    "price_below": "CRITICAL",
    "price_above": "WARNING",
    "catalyst": "INFO",
    "resolution_approaching": "INFO",
    "thesis_invalidation": "INFO",
    "drawdown_any": "WARNING",
    "portfolio_drawdown": "CRITICAL",
    "deployment_high": "WARNING",
}

DRAWDOWN_CRITICAL_THRESHOLD = 40


def build_definitions_from_yaml(alert_defs: dict) -> list:
    """Parse alerts.yaml into a list of definition dicts."""
    definitions = []

    for pos_key, cfg in alert_defs.get("positions", {}).items():
        market_slug = cfg.get("slug", pos_key)
        market_name = cfg.get("market", pos_key)
        direction = cfg.get("direction")
        entry_price = cfg.get("entry_price")

        for rule in cfg.get("alerts", []):
            rtype = rule["type"]
            if rtype == "thesis_invalidation":
                continue

            threshold_part = ""
            if "threshold" in rule:
                threshold_part = f":{rule['threshold']}"
            elif "days" in rule:
                threshold_part = f":{rule['days']}d"
            elif "date" in rule:
                threshold_part = f":{rule['date']}"

            slug = f"{pos_key}:{rtype}{threshold_part}"

            severity = SEVERITY_MAP.get(rtype, "WARNING")
            if rtype == "drawdown" and rule.get("threshold", 0) >= DRAWDOWN_CRITICAL_THRESHOLD:
                severity = "CRITICAL"

            defn = {
                "slug": slug,
                "market_slug": market_slug,
                "market_name": market_name,
                "direction": direction,
                "alert_type": rtype,
                "threshold": rule.get("threshold"),
                "catalyst_date": rule.get("date"),
                "catalyst_description": rule.get("description"),
                "days_before": rule.get("days_before") or rule.get("days"),
                "action": rule["action"],
                "severity": severity,
                "entry_price": entry_price,
                "is_global": False,
                "enabled": True,
            }
            definitions.append(defn)

    for rule in alert_defs.get("global", []):
        rtype = rule["type"]
        threshold = rule.get("threshold", 0)
        slug = f"global:{rtype}:{threshold}"
        severity = SEVERITY_MAP.get(rtype, "WARNING")
        if rtype == "drawdown_any" and threshold >= 50:
            severity = "CRITICAL"

        defn = {
            "slug": slug,
            "market_slug": None,
            "market_name": None,
            "direction": None,
            "alert_type": rtype,
            "threshold": threshold,
            "catalyst_date": None,
            "catalyst_description": None,
            "days_before": None,
            "action": rule["action"],
            "severity": severity,
            "entry_price": None,
            "is_global": True,
            "enabled": True,
        }
        definitions.append(defn)

    return definitions


def ensure_tables(conn):
    """Create alert tables if they don't exist."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alert_definitions (
            id SERIAL PRIMARY KEY,
            slug VARCHAR(255) NOT NULL UNIQUE,
            market_slug VARCHAR(255),
            market_name VARCHAR(500),
            direction VARCHAR(10),
            alert_type VARCHAR(50) NOT NULL,
            threshold NUMERIC(10, 4),
            catalyst_date TIMESTAMPTZ,
            catalyst_description TEXT,
            days_before INTEGER,
            action TEXT NOT NULL,
            severity VARCHAR(20) NOT NULL DEFAULT 'WARNING',
            entry_price NUMERIC(10, 4),
            is_global BOOLEAN DEFAULT FALSE,
            enabled BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alert_events (
            id SERIAL PRIMARY KEY,
            definition_id INTEGER NOT NULL REFERENCES alert_definitions(id),
            market_slug VARCHAR(255),
            market_name VARCHAR(500),
            alert_type VARCHAR(50) NOT NULL,
            severity VARCHAR(20) NOT NULL,
            message TEXT NOT NULL,
            action TEXT,
            details JSONB,
            notified BOOLEAN DEFAULT FALSE,
            notification_error TEXT,
            triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            cleared_at TIMESTAMPTZ
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alert_events_definition ON alert_events(definition_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alert_events_cleared ON alert_events(cleared_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alert_events_triggered ON alert_events(triggered_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alert_definitions_enabled ON alert_definitions(enabled)")
    conn.commit()
    cur.close()


def sync_definitions(conn, definitions, clear=False):
    """Sync definitions into DB. Returns stats."""
    stats = {"created": 0, "updated": 0, "disabled": 0}
    cur = conn.cursor()

    if clear:
        cur.execute("DELETE FROM alert_events")
        cur.execute("DELETE FROM alert_definitions")
        conn.commit()
        print("Cleared all existing definitions and events.")

    # Get existing definitions by slug
    cur.execute("SELECT id, slug FROM alert_definitions")
    existing = {row[1]: row[0] for row in cur.fetchall()}

    yaml_slugs = set()

    for defn in definitions:
        yaml_slugs.add(defn["slug"])

        cat_date = defn.get("catalyst_date")
        if cat_date and isinstance(cat_date, str):
            cat_date = f"{cat_date}T00:00:00+00:00"
        else:
            cat_date = None

        params = (
            defn.get("market_slug"), defn.get("market_name"), defn.get("direction"),
            defn["alert_type"], defn.get("threshold"), cat_date,
            defn.get("catalyst_description"), defn.get("days_before"),
            defn["action"], defn["severity"], defn.get("entry_price"),
            defn.get("is_global", False), defn.get("enabled", True),
        )

        if defn["slug"] in existing:
            cur.execute("""
                UPDATE alert_definitions SET
                    market_slug = %s, market_name = %s, direction = %s,
                    alert_type = %s, threshold = %s, catalyst_date = %s,
                    catalyst_description = %s, days_before = %s,
                    action = %s, severity = %s, entry_price = %s,
                    is_global = %s, enabled = %s, updated_at = NOW()
                WHERE slug = %s
            """, params + (defn["slug"],))
            stats["updated"] += 1
        else:
            cur.execute("""
                INSERT INTO alert_definitions
                    (slug, market_slug, market_name, direction, alert_type, threshold,
                     catalyst_date, catalyst_description, days_before, action, severity,
                     entry_price, is_global, enabled)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (defn["slug"],) + params)
            stats["created"] += 1

    # Disable definitions not in YAML
    for slug in existing:
        if slug not in yaml_slugs:
            cur.execute(
                "UPDATE alert_definitions SET enabled = FALSE, updated_at = NOW() WHERE slug = %s",
                (slug,),
            )
            stats["disabled"] += 1

    conn.commit()
    cur.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Sync alerts.yaml into PostgreSQL")
    parser.add_argument("yaml_path", nargs="?", help="Path to alerts.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Parse YAML and show what would be synced")
    parser.add_argument("--clear", action="store_true", help="Clear all definitions before syncing")
    args = parser.parse_args()

    # Find alerts.yaml
    candidates = [
        Path(__file__).parent.parent.parent / "polymarket-team" / "portfolio" / "alerts.yaml",
        Path.home() / "ai" / "polymarket-team" / "portfolio" / "alerts.yaml",
        Path("/app/alerts.yaml"),
    ]

    if args.yaml_path:
        yaml_path = Path(args.yaml_path)
    else:
        yaml_path = None
        for p in candidates:
            if p.exists():
                yaml_path = p
                break

    if yaml_path is None or not yaml_path.exists():
        print(f"Error: alerts.yaml not found. Tried: {[str(p) for p in candidates]}")
        sys.exit(1)

    print(f"Reading: {yaml_path}")

    with open(yaml_path) as f:
        alert_defs = yaml.safe_load(f)

    definitions = build_definitions_from_yaml(alert_defs)
    print(f"Parsed {len(definitions)} alert definitions from YAML")

    if args.dry_run:
        print("\n--- DRY RUN ---")
        for d in definitions:
            scope = "GLOBAL" if d["is_global"] else d["market_slug"]
            print(f"  [{d['severity']:8s}] {scope}: {d['alert_type']} "
                  f"(threshold={d.get('threshold')}) -> {d['action'][:60]}")
        print(f"\nTotal: {len(definitions)} definitions")
        return

    conn = get_connection()
    print(f"Connected to database.")

    ensure_tables(conn)
    print("Tables ensured.")

    stats = sync_definitions(conn, definitions, clear=args.clear)
    conn.close()

    print(f"\nSync complete:")
    print(f"  Created:   {stats['created']}")
    print(f"  Updated:   {stats['updated']}")
    print(f"  Disabled:  {stats['disabled']}")


if __name__ == "__main__":
    main()
