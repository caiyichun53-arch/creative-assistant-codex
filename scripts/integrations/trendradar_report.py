"""Read-only current snapshots using TrendRadar's collector and weight function.

Run in the bundled TrendRadar Python environment. No AI/report/notification
entrypoint is invoked, and no daily-discovery state is touched.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "TrendRadar"


def collect_snapshot() -> dict:
    sys.path.insert(0, str(VENDOR))
    from trendradar.crawler.fetcher import DataFetcher

    config = yaml.safe_load((VENDOR / "config/config.yaml").read_text(encoding="utf-8"))
    sources = config["platforms"]["sources"]
    fetcher = DataFetcher(api_url=config["platforms"].get("api_url") or None)
    result = {"collected_at": datetime.now().astimezone().isoformat(), "platforms": [], "items": []}
    for source in sources:
        # Retain the vendor's existing collection behavior; no extra retry or timeout.
        with redirect_stdout(sys.stderr):
            raw, _, _ = fetcher.fetch_data((source["id"], source["name"]))
        if raw is None:
            raise RuntimeError(f"TrendRadar collection failed: {source['id']}")
        data = json.loads(raw)
        error = fetcher._check_domain_safety(data.get("items", []), source.get("expected_domain", ""))
        if error:
            raise ValueError(f"TrendRadar source domain mismatch: {error}")
        result["platforms"].append({"id": source["id"], "name": source["name"],
                                    "updated_at": datetime.fromtimestamp(data["updatedTime"] / 1000).astimezone().isoformat(),
                                    "status": data["status"]})
        for rank, item in enumerate(data["items"], 1):
            result["items"].append({"id": f"{source['id']}:{rank}", "platform_id": source["id"],
                                    "title": item["title"], "url": item.get("url") or item.get("mobileUrl") or "",
                                    "ranks": [rank], "count": 1})
    return result


def score_items(items: list[dict]) -> dict:
    sys.path.insert(0, str(VENDOR))
    from trendradar.core.analyzer import calculate_news_weight

    config = yaml.safe_load((VENDOR / "config/config.yaml").read_text(encoding="utf-8"))
    weights = config["advanced"]["weight"]
    threshold = config["report"]["rank_threshold"]
    native = {"RANK_WEIGHT": weights["rank"], "FREQUENCY_WEIGHT": weights["frequency"], "HOTNESS_WEIGHT": weights["hotness"]}
    return {"weights": weights, "rank_threshold": threshold,
            "scores": {item["id"]: calculate_news_weight(item, threshold, native) for item in items}}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    operation = sys.argv[1]
    if operation == "collect":
        value = collect_snapshot()
    elif operation == "score":
        value = score_items(json.load(sys.stdin))
    else:
        raise ValueError("unsupported TrendRadar report operation")
    print(json.dumps(value, ensure_ascii=False))
