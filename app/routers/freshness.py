from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from pathlib import Path
import re

import yaml

from app.database import get_db

router = APIRouter(prefix="/freshness", tags=["freshness"])

# Market type decay rates (from CLAUDE.md)
MARKET_DECAY_RATES = {
    "corporate": timedelta(hours=48),
    "ipo": timedelta(hours=48),
    "tech": timedelta(hours=48),
    "geopolitical": timedelta(days=7),
    "housing": timedelta(days=14),
    "climate": timedelta(days=14),
    "default": timedelta(days=7),
}


def parse_duration(duration_str: str) -> timedelta:
    """Parse duration string like '7d', '48h' to timedelta."""
    if not duration_str:
        return timedelta(days=7)
    match = re.match(r"(\d+)([dhwm])", duration_str.lower())
    if not match:
        return timedelta(days=7)

    value = int(match.group(1))
    unit = match.group(2)

    if unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    elif unit == "w":
        return timedelta(weeks=value)
    elif unit == "m":
        return timedelta(days=value * 30)
    return timedelta(days=7)


def get_market_type(market_slug: str) -> str:
    """Infer market type from slug for decay rate lookup."""
    slug_lower = market_slug.lower()

    if any(word in slug_lower for word in ["ipo", "spacex", "openai", "anthropic"]):
        return "ipo"
    if any(word in slug_lower for word in ["tech", "ai-model", "deepseek", "chatgpt"]):
        return "tech"
    if any(word in slug_lower for word in ["iran", "russia", "china", "ukraine", "regime", "strike"]):
        return "geopolitical"
    if any(word in slug_lower for word in ["housing", "home-value", "median-home", "zillow", "parcl"]):
        return "housing"
    if any(word in slug_lower for word in ["climate", "temperature", "hottest", "warming"]):
        return "climate"

    return "default"


@router.get("/topics")
async def get_topic_freshness():
    """
    Get freshness status for all research topics.
    Reads from polymarket-team research folder if available.
    """
    # Try to find the research folder
    # In production, this would be configured via environment variable
    possible_paths = [
        Path("/app/research/topics"),  # Docker mount
        Path.home() / "ai" / "polymarket-team" / "research" / "topics",  # Local dev
        Path("../polymarket-team/research/topics"),  # Relative
    ]

    research_dir = None
    for path in possible_paths:
        if path.exists():
            research_dir = path
            break

    if not research_dir:
        return {
            "topics": [],
            "error": "Research directory not found",
            "checked_at": datetime.utcnow().isoformat(),
        }

    now = datetime.utcnow()
    results = []

    for topic_dir in research_dir.iterdir():
        if not topic_dir.is_dir():
            continue

        meta_file = topic_dir / "_meta.yaml"
        if not meta_file.exists():
            continue

        try:
            with open(meta_file) as f:
                meta = yaml.safe_load(f)
        except Exception:
            continue

        topic_result = {
            "topic": meta.get("topic", topic_dir.name),
            "description": meta.get("description", ""),
            "status": meta.get("status", "UNKNOWN"),
            "is_fresh": True,
            "issues": [],
            "last_updated": None,
            "freshness_decay": meta.get("freshness_decay", "7d"),
            "next_catalyst": None,
            "linked_market_count": len(meta.get("linked_markets", [])),
        }

        # Parse last_updated
        last_updated = meta.get("last_updated")
        if last_updated:
            if isinstance(last_updated, str):
                last_updated = datetime.strptime(last_updated, "%Y-%m-%d")
            elif not isinstance(last_updated, datetime):
                last_updated = datetime.combine(last_updated, datetime.min.time())

            topic_result["last_updated"] = last_updated.isoformat()

            # Check decay
            decay = parse_duration(meta.get("freshness_decay", "7d"))
            if now > last_updated + decay:
                days_stale = (now - (last_updated + decay)).days
                topic_result["is_fresh"] = False
                topic_result["issues"].append(f"{days_stale} days past decay threshold")

        # Check status
        if meta.get("status") == "STALE":
            topic_result["is_fresh"] = False
            topic_result["issues"].append("Marked as STALE")
        elif meta.get("status") == "NEEDS_SEED":
            topic_result["is_fresh"] = False
            topic_result["issues"].append("Needs initial seeding")

        # Find next catalyst
        for catalyst in meta.get("key_catalysts", []):
            cat_date = catalyst.get("date")
            if cat_date:
                if isinstance(cat_date, str):
                    cat_date = datetime.strptime(cat_date, "%Y-%m-%d")
                elif not isinstance(cat_date, datetime):
                    cat_date = datetime.combine(cat_date, datetime.min.time())

                if cat_date >= now:
                    days_until = (cat_date - now).days
                    if topic_result["next_catalyst"] is None:
                        topic_result["next_catalyst"] = {
                            "event": catalyst.get("event"),
                            "date": cat_date.strftime("%Y-%m-%d"),
                            "days_until": days_until,
                        }
                    elif days_until < topic_result["next_catalyst"]["days_until"]:
                        topic_result["next_catalyst"] = {
                            "event": catalyst.get("event"),
                            "date": cat_date.strftime("%Y-%m-%d"),
                            "days_until": days_until,
                        }

        results.append(topic_result)

    # Sort by freshness (stale first)
    results.sort(key=lambda x: (x["is_fresh"], x["topic"]))

    return {
        "topics": results,
        "fresh_count": sum(1 for r in results if r["is_fresh"]),
        "stale_count": sum(1 for r in results if not r["is_fresh"]),
        "checked_at": now.isoformat(),
    }


@router.get("/market/{market_slug}")
async def check_market_freshness(market_slug: str):
    """
    Check freshness requirements for a specific market.
    Returns decay rate, cluster info, and any staleness issues.
    """
    now = datetime.utcnow()
    market_type = get_market_type(market_slug)
    decay = MARKET_DECAY_RATES.get(market_type, MARKET_DECAY_RATES["default"])

    result = {
        "market": market_slug,
        "market_type": market_type,
        "max_stale_data_hours": decay.total_seconds() / 3600,
        "is_fast_decay": market_type in ("corporate", "ipo", "tech"),
        "recommendation": "proceed",
        "issues": [],
        "cluster": None,
        "checked_at": now.isoformat(),
    }

    # Fast-decay markets need extra verification
    if result["is_fast_decay"]:
        result["issues"].append(f"Fast-decay market ({market_type}): verify news within 48h before entry")
        result["recommendation"] = "verify_news"

    # Try to find cluster info
    possible_cluster_paths = [
        Path("/app/research/clusters.yaml"),
        Path.home() / "ai" / "polymarket-team" / "research" / "clusters.yaml",
    ]

    for cluster_path in possible_cluster_paths:
        if cluster_path.exists():
            try:
                with open(cluster_path) as f:
                    data = yaml.safe_load(f)
                clusters = data.get("clusters", data)
                for cluster_name, cluster_data in clusters.items():
                    if market_slug in cluster_data.get("markets", []):
                        result["cluster"] = {
                            "name": cluster_name,
                            "primary_topic": cluster_data.get("primary_topic"),
                        }
                        break
            except Exception:
                pass
            break

    return result


@router.get("/decay-rates")
async def get_decay_rates():
    """
    Get the information decay rate table.
    Returns max stale data periods by market type.
    """
    return {
        "rates": [
            {
                "market_type": "corporate",
                "max_stale_hours": 48,
                "description": "Corporate events, IPOs, M&A",
                "action_if_stale": "Must re-verify before entry",
            },
            {
                "market_type": "ipo",
                "max_stale_hours": 48,
                "description": "IPO-related markets",
                "action_if_stale": "Must re-verify before entry",
            },
            {
                "market_type": "tech",
                "max_stale_hours": 48,
                "description": "AI models, tech announcements",
                "action_if_stale": "Must re-verify before entry",
            },
            {
                "market_type": "geopolitical",
                "max_stale_hours": 168,  # 7 days
                "description": "Geopolitical events, conflicts",
                "action_if_stale": "Check for developments",
            },
            {
                "market_type": "housing",
                "max_stale_hours": 336,  # 14 days
                "description": "Housing price markets",
                "action_if_stale": "Usually OK, check for major shifts",
            },
            {
                "market_type": "climate",
                "max_stale_hours": 336,  # 14 days
                "description": "Climate and temperature markets",
                "action_if_stale": "Usually OK, check for data releases",
            },
        ],
        "note": "Based on SpaceX IPO loss - stale info can invalidate entire thesis",
    }
