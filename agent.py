"""
agent.py
--------
Tolaram YouTube Agent — Core Runner Module

Fetches channel/video/comment data from YouTube, classifies
comments via Gemini, saves results locally, and returns a run summary.

This module is imported by server.py (background runs via /api/trigger-run)
and can also be run directly for one-off or scheduled execution:

    python agent.py               # single run (dev/debug)
    python agent.py --schedule    # runs every N hours (dev/debug)

Production: use the dashboard "Run Agent" button or POST /api/trigger-run.
"""
from __future__ import annotations

import os
import sys
import json
import time
import logging
import argparse
import colorlog
import threading
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────────────
def _build_logger() -> logging.Logger:
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan", "INFO": "green", "WARNING": "yellow",
            "ERROR": "red", "CRITICAL": "bold_red",
        },
    ))
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, handlers=[handler])
    return logging.getLogger(__name__)

logger = _build_logger()

# ── Local imports ─────────────────────────────────────────────────────────────
from youtube_client import YouTubeClient
from claude_classifier import ClaudeClassifier
from gemini_classifier import GeminiClassifier
from rate_limiter import RateLimiter
from crisis_notify import send_crisis_email
from data_store import (
    save_json, load_json, append_to_store,
    save_run_summary, compute_dashboard_stats,
)

# TransformerClassifier is optional — imported lazily so missing 'transformers'
# package does not crash the app (graceful degradation to Gemini-only mode).
try:
    from transformer_classifier import TransformerClassifier as _TC
except ImportError:
    _TC = None  # type: ignore

DATA_DIR = Path(os.getenv("APP_DATA_DIR") or (Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Config ─────────────────────────────────────────────────────────────────────
YOUTUBE_API_KEY    = os.getenv("YOUTUBE_API_KEY") or os.getenv("API_KEY")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
VIDEOS_PER_CHANNEL = int(os.getenv("VIDEOS_PER_CHANNEL", "5"))
COMMENTS_PER_VIDEO = int(os.getenv("COMMENTS_PER_VIDEO", "50"))
ANTHROPIC_RPM      = 50   # request/min ceiling for whichever LLM backend is active
CRISIS_KEYWORDS    = os.getenv(
    "CRISIS_KEYWORDS",
    "reaction,sick,hospital,poison,hurt,injury,death,died,allergic,lawsuit,dangerous"
).split(",")

# Both classifiers swallow LLM errors internally and return one of these
# placeholder strings instead of raising, so a failed call looks identical
# to a real result at the call site — check against these rather than
# assuming any returned string means success.
_LLM_FAILURE_MARKERS = {
    "Performance insights temporarily unavailable.",  # ClaudeClassifier
    "Insight generation temporarily unavailable.",     # GeminiClassifier
    "Content brief generation temporarily unavailable.",  # both
}

# Channel map: label → (channel_id, brand_name)
CHANNELS: dict[str, tuple[str, str]] = {}
for _key, _label, _brand in [
    ("CHANNEL_MUNCHIT",   "MUNCH IT",  "Munch It"),
    ("CHANNEL_INDOMIE",   "Indomie",   "Indomie"),
    ("CHANNEL_COLGATE",   "Colgate",   "Colgate"),
    ("CHANNEL_HYPO",      "Hypo",      "Hypo"),
    ("CHANNEL_POWER_OIL", "Power Oil", "Power Oil"),
    ("CHANNEL_NUTRIFY",   "Nutrify",   "Nutrify"),
    ("CHANNEL_LUSH",      "Lush",      "Lush"),
    ("CHANNEL_MINIMIE",   "Minimie",   "Minimie"),
    ("CHANNEL_ID",        "Primary",   "General"),
]:
    _val = os.getenv(_key)
    if _val and _val.startswith("UC") and "xxxx" not in _val:
        CHANNELS[_label] = (_val, _brand)


def _validate_config() -> bool:
    """Validate required environment variables. Returns True if valid."""
    ok = True
    if not CHANNELS:
        logger.error("No valid channel IDs found in .env. Set CHANNEL_ID or CHANNEL_INDOMIE etc.")
        ok = False
    if not YOUTUBE_API_KEY:
        logger.error("YOUTUBE_API_KEY (or API_KEY) not set in .env")
        ok = False
    if not GEMINI_API_KEY and not ANTHROPIC_API_KEY:
        logger.error("Neither GEMINI_API_KEY nor ANTHROPIC_API_KEY is set in .env — need at least one LLM backend")
        ok = False
    return ok


# ── Main Agent Logic ───────────────────────────────────────────────────────────

def run_agent() -> dict:
    """
    Execute one full agent cycle: fetch → classify → save → report.

    Returns the run summary dict. Raises RuntimeError if config is invalid.
    Can be called from server.py background threads or directly.
    """
    if not _validate_config():
        raise RuntimeError("Agent configuration is invalid — check .env settings.")

    start_time = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("🚀  TOLARAM YOUTUBE AGENT — Starting Run")
    logger.info(f"    Channels  : {list(CHANNELS.keys())}")
    logger.info(f"    Videos    : {VIDEOS_PER_CHANNEL} per channel")
    logger.info(f"    Comments  : {COMMENTS_PER_VIDEO} per video")
    logger.info("=" * 60)

    yt = YouTubeClient(YOUTUBE_API_KEY)

    # Build rate limiter (always active)
    limiter = RateLimiter(calls_per_minute=ANTHROPIC_RPM)

    # Build transformer pre-filter (optional — skipped if package not installed)
    transformer = None
    if _TC is not None:
        transformer = _TC()
    else:
        logger.warning("TransformerClassifier unavailable — using Gemini-only mode.")

    # Select LLM — Gemini first (matches the rest of the app), Claude only
    # as a fallback if no Gemini key is configured.
    if GEMINI_API_KEY:
        classifier = GeminiClassifier(
            api_key=GEMINI_API_KEY,
            crisis_keywords=CRISIS_KEYWORDS,
            transformer=transformer,
            rate_limiter=limiter
        )
        logger.info("Using Gemini as the primary LLM.")
    else:
        classifier = ClaudeClassifier(
            api_key=ANTHROPIC_API_KEY,
            crisis_keywords=CRISIS_KEYWORDS,
            transformer=transformer,
            rate_limiter=limiter
        )
        logger.info("GEMINI_API_KEY not set — falling back to Claude (Anthropic) as the LLM.")

    run_summary = {
        "run_id":                  start_time.strftime("%Y%m%d_%H%M%S"),
        "started_at":              start_time.isoformat(),
        "channels_processed":      [],
        "total_videos":            0,
        "total_comments_fetched":  0,
        "total_comments_classified": 0,
        "crisis_alerts":           [],
        "errors":                  [],
    }

    all_channel_stats = []
    all_videos = load_json("all_videos.json", default=[])
    existing_video_ids = {v["video_id"] for v in all_videos}
    
    all_comments_data = load_json("all_comments.json", default=[])
    existing_comment_ids = {c["comment_id"] for c in all_comments_data if "comment_id" in c}
    
    # Shared state locks
    state_lock = threading.Lock()

    def process_channel(label: str, channel_id: str, brand: str):
        """Worker function for parallel channel processing."""
        nonlocal all_videos
        logger.info(f"\n📺  Starting parallel task for: {label} ({channel_id})")

        # Dynamically select API key for this brand
        brand_key_env = f"{brand.upper().replace(' ', '_')}_API_KEY"
        brand_key = os.getenv(brand_key_env) or YOUTUBE_API_KEY
        
        yt = YouTubeClient(brand_key)

        # 1. Channel info
        channel_info = yt.get_channel_info(channel_id)
        if not channel_info:
            logger.warning(f"    Skipping {label} — could not fetch channel info.")
            with state_lock:
                run_summary["errors"].append(f"Could not fetch channel info for {label}")
            return

        channel_info["brand"] = brand
        with state_lock:
            all_channel_stats.append(channel_info)
        
        logger.info(
            f"    ✅ {channel_info.get('title', label)} metadata fetched."
        )

        # 2. Recent videos
        videos = yt.get_recent_videos(channel_id, max_results=VIDEOS_PER_CHANNEL)
        if not videos:
            logger.warning(f"    No videos found for {label}.")
            return

        brand_videos = []
        for v in videos:
            v["brand"]         = brand
            v["channel_label"] = label
            brand_videos.append(v)

        with state_lock:
            new_vids = [v for v in brand_videos if v["video_id"] not in existing_video_ids]
            all_videos.extend(new_vids)
            existing_video_ids.update(v["video_id"] for v in new_vids)
            run_summary["total_videos"] += len(brand_videos)

        logger.info(f"    📹 {label}: {len(brand_videos)} videos fetched.")

        # 3. Comments + classification
        ch_comments_fetched = 0
        ch_comments_classified = 0

        for video in brand_videos:
            vid_id = video["video_id"]
            comments = yt.get_comments(vid_id, max_results=COMMENTS_PER_VIDEO)
            ch_comments_fetched += len(comments)

            if not comments:
                continue
                
            new_comments = [c for c in comments if c.get("comment_id") not in existing_comment_ids]
            
            if not new_comments:
                continue

            for c in new_comments:
                c["brand"]         = brand
                c["channel_label"] = label
                c["video_title"]   = video["title"]

            classified = classifier.classify_batch(new_comments, brand=brand, delay=2.0)
            ch_comments_classified += len(classified)

            with state_lock:
                for c in classified:
                    existing_comment_ids.add(c.get("comment_id"))

            append_to_store("all_comments.json", classified)

            # Collect crisis alerts
            current_crisis = [c for c in classified if c.get("crisis_flag")]
            if current_crisis:
                with state_lock:
                    for crisis in current_crisis:
                        run_summary["crisis_alerts"].append({
                            "comment_id": crisis["comment_id"],
                            "video_title": video["title"],
                            "text": crisis["text"][:200],
                            "author": crisis["author"],
                            "reason": crisis.get("crisis_reason", "Unknown"),
                            "brand": brand,
                        })
                logger.critical(f"    🚨 {label} CRISIS: '{current_crisis[0]['text'][:80]}...'")
                send_crisis_email(current_crisis, brand, video["title"])

        with state_lock:
            run_summary["total_comments_fetched"] += ch_comments_fetched
            run_summary["total_comments_classified"] += ch_comments_classified
            run_summary["channels_processed"].append({
                "label": label, "brand": brand, "channel_id": channel_id,
                "title": channel_info.get("title", label),
                "videos_processed": len(brand_videos),
                "comments_fetched": ch_comments_fetched,
                "comments_classified": ch_comments_classified,
            })
            
            # Intermediate save
            save_json("all_videos.json", all_videos)
            save_json("channel_stats.json", all_channel_stats)

        logger.info(f"    🏁 Done processing {label}.")

    # ── Parallel Execution ──────────────────────────────────────────────────
    max_threads = min(len(CHANNELS), 8)
    logger.info(f"🚀  Launching {max_threads} parallel brand scanners...")
    
    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        for label, (channel_id, brand) in CHANNELS.items():
            executor.submit(process_channel, label, channel_id, brand)

    # ── Save final videos & channels ──────────────────────────────────────────
    save_json("all_videos.json", all_videos)
    save_json("channel_stats.json", all_channel_stats)


    # ── Generate performance insights ─────────────────────────────────────────
    logger.info("\n📊  Generating AI performance insights...")
    insights = {}
    for label, (channel_id, brand) in CHANNELS.items():
        channel_videos = [v for v in all_videos if v.get("channel_label") == label]
        if channel_videos:
            result = classifier.generate_performance_insight(channel_videos[:10], brand)
            insights[label] = result
            # generate_performance_insight() swallows LLM errors and returns a
            # placeholder string instead of raising — check for that instead
            # of unconditionally logging success, or a failed call silently
            # looks identical to a real insight in both the log and the file.
            if result in _LLM_FAILURE_MARKERS:
                logger.warning(f"    ⚠ Insight generation FAILED for {label} — LLM call errored (see error above)")
            else:
                logger.info(f"    ✅ Insight generated for {label}")
    save_json("performance_insights.json", insights)

    # ── Generate content briefs from top questions/compliments ────────────────
    logger.info("\n💡  Generating content briefs...")
    all_comments_data = load_json("all_comments.json", default=[])
    briefs = {}
    for label, (channel_id, brand) in CHANNELS.items():
        channel_q = [
            c["text"] for c in all_comments_data
            if c.get("channel_label") == label
            and c.get("category") in ("question", "compliment")
        ]
        if channel_q:
            result = classifier.generate_content_brief(brand, channel_q)
            briefs[label] = result
            if result in _LLM_FAILURE_MARKERS:
                logger.warning(f"    ⚠ Brief generation FAILED for {label} — LLM call errored (see error above)")
            else:
                logger.info(f"    ✅ Brief generated for {label}")
        else:
            logger.info(f"    Skipping {label} — no questions found.")
    save_json("content_briefs.json", briefs)

    # ── Finalise run summary ──────────────────────────────────────────────────
    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()
    run_summary["completed_at"]     = end_time.isoformat()
    run_summary["duration_seconds"] = round(duration, 1)
    save_run_summary(run_summary)

    # ── Log summary ───────────────────────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("✅  AGENT RUN COMPLETE")
    logger.info(f"    Duration   : {duration:.1f}s")
    logger.info(f"    Videos     : {run_summary['total_videos']}")
    logger.info(
        f"    Comments   : {run_summary['total_comments_fetched']} fetched, "
        f"{run_summary['total_comments_classified']} classified"
    )
    if run_summary["crisis_alerts"]:
        logger.critical(
            f"    🚨 CRISIS ALERTS: {len(run_summary['crisis_alerts'])} — check dashboard immediately!"
        )
    else:
        logger.info("    Crisis     : None detected ✅")
    logger.info("=" * 60)

    return run_summary


# ── Scheduler (dev/debug only) ────────────────────────────────────────────────

def run_scheduled(interval_hours: float = 2.0) -> None:
    """
    Run the agent on a fixed schedule (dev/debug mode).
    In production the scheduler is managed by APScheduler inside server.py.
    """
    import schedule as _schedule

    logger.info(f"⏰  Dev scheduler: running every {interval_hours} hours")
    logger.info("    Press Ctrl+C to stop.\n")

    def _job():
        try:
            run_agent()
        except Exception as e:
            logger.error(f"Scheduled run failed: {e}")

    _job()
    _schedule.every(interval_hours).hours.do(_job)

    while True:
        _schedule.run_pending()
        time.sleep(60)


# ── CLI entry point (dev / one-off runs) ──────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tolaram YouTube Agent — dev runner. "
                    "Use the dashboard or POST /api/trigger-run in production."
    )
    parser.add_argument(
        "--schedule", action="store_true",
        help="Run on a repeating schedule (dev only)",
    )
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="Schedule interval in hours (default: 2.0)",
    )
    args = parser.parse_args()

    if args.schedule:
        run_scheduled(interval_hours=args.interval)
    else:
        run_agent()