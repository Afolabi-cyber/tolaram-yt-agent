"""
boosted_tracker.py
-------------------
Tracks whether a given video (ours or a competitor's) is running as a paid
ad ("boosted"/"performance ads") vs organic.

Why this isn't auto-detected:
  YouTube Data API v3 (what this whole app runs on, via a plain API key)
  exposes only public counts — views, likes, comments. It has no field
  saying "this upload had ad spend behind it". That signal only exists in:
    - Google Ads API reporting — but only for OUR OWN ad account, via
      OAuth, not for any competitor.
    - YouTube Analytics API — same restriction, own-channel-only.
  For competitor videos there is no API path at all. The one real public
  source is Google's own Ads Transparency Center
  (https://adstransparency.google.com) — anyone can look up an advertiser
  there and see the video ads currently/recently running, no login
  required. Google doesn't publish a stable API for it, so rather than
  build a scraper against an undocumented endpoint (which breaks without
  warning), this module makes the manual check fast: one click opens the
  right transparency-center search, and the result gets stored here so it
  persists across rescans and feeds the organic/boosted breakdown.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from data_store import save_json, load_json

logger = logging.getLogger(__name__)

FLAGS_FILE = "boosted_flags.json"  # {video_id: {boosted, note, source, updated_at}}


def get_all_flags() -> Dict[str, Dict]:
    return load_json(FLAGS_FILE, default={})


def get_flag(video_id: str) -> Dict:
    flags = get_all_flags()
    return flags.get(video_id, {"boosted": False, "source": "unchecked", "note": "", "updated_at": ""})


def set_flag(video_id: str, boosted: bool, note: str = "",
             source: str = "manual") -> Dict:
    if not video_id:
        return {"ok": False, "error": "video_id required"}
    flags = get_all_flags()
    entry = {
        "boosted":    bool(boosted),
        "note":       (note or "").strip(),
        "source":     source if source in ("manual", "transparency_center") else "manual",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    flags[video_id] = entry
    save_json(FLAGS_FILE, flags)
    return {"ok": True, "data": {"video_id": video_id, **entry}}


def clear_flag(video_id: str) -> Dict:
    flags = get_all_flags()
    if video_id in flags:
        del flags[video_id]
        save_json(FLAGS_FILE, flags)
        return {"ok": True, "removed": True}
    return {"ok": True, "removed": False}


def transparency_center_url(advertiser_name: str, region: str = "NG") -> str:
    """
    Build the direct search URL a user can click to check a brand's live/
    recent video ads on Google's public Ads Transparency Center.
    """
    from urllib.parse import quote_plus
    q = quote_plus((advertiser_name or "").strip())
    return f"https://adstransparency.google.com/?region={quote_plus(region)}&query={q}"