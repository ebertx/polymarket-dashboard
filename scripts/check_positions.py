#!/usr/bin/env python3
"""Quick diagnostic: show all positions and their status."""
import asyncio
from app.database import AsyncSessionLocal
from sqlalchemy import text

async def check():
    async with AsyncSessionLocal() as db:
        r = await db.execute(text(
            "SELECT p.id, substr(m.title, 1, 55) as t, p.status, "
            "p.exit_date, substr(coalesce(p.exit_reasoning, ''), 1, 70) as reason, "
            "p.shares, p.entry_price, p.exit_price "
            "FROM positions p LEFT JOIN markets m ON p.market_id = m.id "
            "ORDER BY p.status, p.exit_date DESC NULLS LAST"
        ))
        print("ID | Title | Status | Exit Date | Reason | Shares | Entry | Exit")
        print("-" * 120)
        for row in r.fetchall():
            print(f"{row[0]:3} | {row[1] or '?':55} | {row[2]:6} | {str(row[3])[:19]:19} | {row[4] or '':30} | {row[5]:8} | {row[6]} | {row[7]}")

asyncio.run(check())
