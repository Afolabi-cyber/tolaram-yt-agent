"""
data_store.py
-------------
Thread-safe JSON persistence for the Tolaram YouTube Agent.
All data lives in the /data folder as flat JSON files.

Concurrency safety:
- Per-file threading.Lock prevents simultaneous read-modify-write races
  when the background agent and API requests overlap.
- Atomic write pattern (tmp file → rename) prevents partial-write corruption.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# APP_DATA_DIR lets a deploy point this at a mounted persistent disk (e.g. on
# Render, whose default filesystem is wiped on every redeploy) without
# needing to know or guess the platform's internal project path. Falls back
# to the local ./data folder for local development, unchanged.
DATA_DIR = Path(os.getenv("APP_DATA_DIR") or (Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Per-file locks — created on demand, never deleted
_locks: dict[str, threading.Lock] = {}
_locks_meta = threading.Lock()


def _get_lock(filename: str) -> threading.Lock:
    with _locks_meta:
        if filename not in _locks:
            _locks[filename] = threading.Lock()
        return _locks[filename]


def _path(filename: str) -> Path:
    return DATA_DIR / filename


# ── Core I/O ──────────────────────────────────────────────────────────────────

def save_json(filename: str, data) -> bool:
    """Atomically write data to a JSON file (tmp → rename)."""
    target = _path(filename)
    tmp    = target.with_suffix(".tmp")
    try:
        with _get_lock(filename):
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            tmp.replace(target)          # atomic on same filesystem
        return True
    except Exception as e:
        logger.error(f"Failed to save {filename}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def load_json(filename: str, default=None):
    """Load a JSON file. Returns default if file doesn't exist or is corrupt."""
    path = _path(filename)
    if not path.exists():
        return default
    try:
        with _get_lock(filename):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load {filename}: {e}")
        return default


# ── Append / merge ─────────────────────────────────────────────────────────────

def append_to_store(filename: str, new_items: list) -> int:
    """
    Merge new items into an existing list, deduplicating by comment_id or video_id.
    New classifications overwrite old ones for the same ID (ensures freshness).
    Returns total record count after merge.
    """
    with _get_lock(filename):
        path = _path(filename)
        existing = []
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception as e:
                logger.error(f"Failed to read {filename} for merge: {e}")

        id_field = None
        if new_items:
            if "comment_id" in new_items[0]:
                id_field = "comment_id"
            elif "video_id" in new_items[0]:
                id_field = "video_id"

        if id_field:
            existing_map = {item[id_field]: item for item in existing}
            for item in new_items:
                existing_map[item[id_field]] = item          # overwrite = refresh
            merged = list(existing_map.values())
        else:
            merged = existing + new_items

        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2, default=str)
            tmp.replace(path)
        except Exception as e:
            logger.error(f"Failed to write merged {filename}: {e}")
            tmp.unlink(missing_ok=True)

    return len(merged)


# ── Run history ────────────────────────────────────────────────────────────────

def save_run_summary(summary: dict) -> bool:
    """Append run summary to run_history.json (capped at 50) and save latest_run.json."""
    summary["saved_at"] = datetime.now(timezone.utc).isoformat()
    history = load_json("run_history.json", default=[])
    history.append(summary)
    save_json("run_history.json", history[-50:])
    return save_json("latest_run.json", summary)


# ── Dashboard stats aggregation ────────────────────────────────────────────────

def compute_dashboard_stats() -> dict:
    """Aggregate all stored data into one stats object for the dashboard API."""
    comments = load_json("all_comments.json",         default=[])
    videos   = load_json("all_videos.json",           default=[])
    channels = load_json("channel_stats.json",        default=[])
    insights = load_json("performance_insights.json", default={})
    briefs   = load_json("content_briefs.json",       default={})
    latest   = load_json("latest_run.json",           default={})
    history  = load_json("run_history.json",          default=[])

    # Comment aggregations
    by_category  = {}
    by_sentiment = {}
    by_brand     = {}
    by_priority  = {"low": 0, "medium": 0, "high": 0, "urgent": 0}
    crisis_list  = []
    needs_reply  = []

    for c in comments:
        cat   = c.get("category")
        cat   = cat if cat is not None else "unknown"
        
        sent  = c.get("sentiment")
        sent  = sent if sent is not None else "unknown"
        
        brand = c.get("brand")
        brand = brand if brand is not None else "Unknown"

        by_category[cat]   = by_category.get(cat, 0) + 1
        by_sentiment[sent] = by_sentiment.get(sent, 0) + 1
        by_brand[brand]    = by_brand.get(brand, 0) + 1
        
        priority = c.get("priority")
        priority = priority if priority is not None else "medium"
        by_priority[priority] = by_priority.get(priority, 0) + 1

        if c.get("crisis_flag"):
            crisis_list.append(c)
        if c.get("category") in ("question", "complaint") and c.get("suggested_reply"):
            needs_reply.append(c)

    # Actionable comments (excluding noise)
    actionable = [c for c in comments if c.get("category") not in ("spam", "irrelevant", "error")]
    actionable_sorted = sorted(actionable, key=lambda x: str(x.get("published_at") or ""), reverse=True)

    # Video engagement rate
    for v in videos:
        views = max(v.get("view_count") or 0, 1)
        v["engagement_rate"] = round(
            ((v.get("like_count") or 0) + (v.get("comment_count") or 0)) / views * 100, 2
        )

    top_videos = sorted(videos, key=lambda x: int(x.get("view_count") or 0), reverse=True)[:8]

    # Run history sparkline (last 14 runs)
    run_counts = [
        {
            "date":     r.get("started_at", "")[:10],
            "comments": r.get("total_comments_classified", 0),
        }
        for r in history[-14:]
    ]

    # Classification accuracy proxy (% with confidence ≥ 0.8)
    high_conf    = [c for c in comments if c.get("classification_confidence", 0) >= 0.8]
    accuracy_pct = round(len(high_conf) / max(len(comments), 1) * 100, 1)

    return {
        "summary": {
            "total_comments":      len(comments),
            "total_videos":        len(videos),
            "total_channels":      len(channels),
            "total_subscribers":   sum(c.get("subscriber_count", 0) for c in channels),
            "total_views":         sum(c.get("view_count", 0) for c in channels),
            "crisis_count":        len(crisis_list),
            "needs_reply_count":   len(needs_reply),
            "model_accuracy_pct":  accuracy_pct,
            "last_run":            latest.get("started_at", "Never"),
            "last_run_duration":   latest.get("duration_seconds", 0),
        },
        "by_category":      by_category,
        "by_sentiment":     by_sentiment,
        "by_brand":         by_brand,
        # Union of every brand that has videos OR comments — used to build
        # brand dropdowns so a brand with videos but (temporarily, or not
        # yet) any classified comments still shows up as selectable.
        "brands":           sorted(set(by_brand.keys()) | {v.get("brand") for v in videos if v.get("brand")}),
        "by_priority":      by_priority,
        "crisis_alerts":    sorted(crisis_list, key=lambda x: str(x.get("published_at") or ""), reverse=True)[:50],
        "needs_reply":      needs_reply[:100],
        "recent_comments":  actionable_sorted[:300],
        "top_videos":       top_videos,
        "all_videos":       sorted(videos, key=lambda x: str(x.get("published_at") or ""), reverse=True)[:100],
        "channels":         channels,
        "insights":         insights,
        "briefs":           briefs,
        "run_history":      run_counts,
        "latest_run":       latest,
    }