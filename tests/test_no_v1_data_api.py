"""Guard: Data API v1 was retired 2026-11-02 (HTTP 410 on every route). All reads use /v2."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V1 = re.compile(r"data-api\.polymarket\.com(?!/v2)(/|[\"'])")


def test_no_v1_data_api_urls():
    hits = []
    for base in (ROOT / "app", ROOT / "scripts", ROOT / "dashboard"):
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.suffix not in (".py", ".ts", ".tsx", ".js", ".vue") or "node_modules" in f.parts:
                continue
            for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if V1.search(line):
                    hits.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    assert not hits, "Data API v1 is retired; use app/services/data_api_v2.py:\n" + "\n".join(hits)
