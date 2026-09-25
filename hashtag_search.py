"""
hashtag_search.py
-----------------
Campaign hashtag search across social platforms.

Given a hashtag like "#ShowSomeLovetoMum", this module:

  1. Runs a Google web search restricted to each supported platform
     (Facebook, Instagram, X/Twitter, TikTok, YouTube) and harvests
     the matching post URLs + snippet text.
  2. Uses the YouTube Data API directly for YouTube matches
     (gives us real view_count, like_count, reach).
  3. Groups results by platform with per-post content preview.
  4. Caches the full result bundle to data/hashtag_<slug>.json.

Design notes:
- Social platforms aggressively block scraping, so we deliberately
  rely on Google's public index via DuckDuckGo's HTML front-end
  (no API key required). This gives URLs + snippet text — which is
  enough to show "which platforms carry this hashtag, what are they
  saying". Full post bodies on Facebook / Instagram require
  authenticated access and are out of scope.
- All network calls have strict timeouts and retries.
"""
from __future__ import annotations

import os
import re
import json
import time
import logging
import threading
from datetime import datetime, timezone
from typing import List, Dict, Optional
from urllib.parse import quote_plus, urlparse, unquote

import requests
from bs4 import BeautifulSoup

from data_store import save_json, load_json

logger = logging.getLogger(__name__)

# ── Platform config ───────────────────────────────────────────────────────────

PLATFORMS = [
    {
        "key":   "youtube",
        "label": "YouTube",
        "domains": ["youtube.com", "youtu.be"],
    },
    {
        "key":   "instagram",
        "label": "Instagram",
        "domains": ["instagram.com"],
    },
    {
        "key":   "facebook",
        "label": "Facebook",
        "domains": ["facebook.com", "m.facebook.com", "fb.com", "fb.watch"],
    },
    {
        "key":   "x",
        "label": "X / Twitter",
        "domains": ["twitter.com", "x.com"],
    },
    {
        "key":   "tiktok",
        "label": "TikTok",
        "domains": ["tiktok.com"],
    },
    {
        "key":   "linkedin",
        "label": "LinkedIn",
        "domains": ["linkedin.com"],
    },
    {
        "key":   "threads",
        "label": "Threads",
        "domains": ["threads.net"],
    },
]

_DOMAIN_TO_PLATFORM = {
    d: p["key"] for p in PLATFORMS for d in p["domains"]
}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent":      _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_search_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_hashtag(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not raw.startswith("#"):
        raw = "#" + raw
    # Strip non-hashtag-safe characters but keep unicode letters / digits
    raw = re.sub(r"\s+", "", raw)
    return raw


def _slugify(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower() or "untitled"


def _classify_url(url: str) -> Optional[str]:
    """Returns the platform key for a URL, or None if not a tracked platform."""
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return None
    for domain, plat in _DOMAIN_TO_PLATFORM.items():
        if host == domain or host.endswith("." + domain):
            return plat
    return None


def _decode_ddg_url(href: str) -> str:
    """
    DuckDuckGo HTML results wrap target URLs in a redirect:
      //duckduckgo.com/l/?uddg=<encoded>&rut=...
    Unwrap them.
    """
    if not href:
        return href
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            return unquote(m.group(1))
    return href


# ── Google (via DuckDuckGo HTML) search ───────────────────────────────────────

def _ddg_search(query: str, max_results: int = 20) -> List[Dict]:
    """
    Scrape DuckDuckGo's non-JS HTML endpoint for a single query.
    Returns list of {url, title, snippet}.
    We use DDG instead of Google because Google blocks scraping hard;
    DDG's HTML endpoint is explicitly designed for lightweight clients.
    """
    url = "https://html.duckduckgo.com/html/"
    # Try POST first, then GET (DDG alternates which one it rate-limits)
    resp = None
    for attempt in ("post", "get"):
        try:
            if attempt == "post":
                resp = requests.post(url, data={"q": query}, headers=_HEADERS, timeout=15)
            else:
                resp = requests.get(url, params={"q": query}, headers=_HEADERS, timeout=15)
            if resp.status_code == 200:
                break
            resp = None
        except requests.RequestException as e:
            logger.debug(f"DDG {attempt} failed for '{query[:60]}': {e}")
            resp = None

    if resp is None:
        logger.warning(f"DDG search unreachable for '{query[:60]}'")
        return _bing_search(query, max_results=max_results)

    soup = BeautifulSoup(resp.text, "html.parser")
    results: List[Dict] = []

    for block in soup.select("div.result")[:max_results * 2]:
        a = block.select_one("a.result__a")
        if not a:
            continue
        href = _decode_ddg_url(a.get("href", ""))
        if not href:
            continue
        title = a.get_text(strip=True)
        snippet_el = block.select_one(".result__snippet")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        results.append({"url": href, "title": title, "snippet": snippet})
        if len(results) >= max_results:
            break

    # If DDG returned an empty page (captcha / rate limit), fall back to Bing
    if not results:
        return _bing_search(query, max_results=max_results)

    return results


def _bing_search(query: str, max_results: int = 20) -> List[Dict]:
    """
    Bing HTML search fallback. Bing's results page is scrapable without
    an API key and tends to work when DDG is rate-limiting.
    """
    try:
        resp = requests.get(
            "https://www.bing.com/search",
            params={"q": query, "count": max_results},
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Bing search failed for '{query[:60]}': {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results: List[Dict] = []
    for li in soup.select("li.b_algo")[:max_results * 2]:
        a = li.select_one("h2 a")
        if not a:
            continue
        href  = a.get("href", "")
        if not href or not href.startswith("http"):
            continue
        title = a.get_text(strip=True)
        sn_el = li.select_one(".b_caption p, .b_descript")
        snippet = sn_el.get_text(" ", strip=True) if sn_el else ""
        results.append({"url": href, "title": title, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def _search_platform(hashtag: str, platform: Dict, per_platform: int) -> List[Dict]:
    """Search Google/DDG for one platform's domain."""
    domain_filter = " OR ".join(f"site:{d}" for d in platform["domains"])
    # We include the raw hashtag AND the name (no-#) to catch sites that
    # index the word rather than the octothorpe.
    bare = hashtag.lstrip("#")
    query = f'({domain_filter}) "{hashtag}" OR "#{bare}"'
    hits = _ddg_search(query, max_results=per_platform)

    # Only keep hits whose URL actually matches this platform
    clean: List[Dict] = []
    for h in hits:
        if _classify_url(h["url"]) == platform["key"]:
            clean.append({
                "platform":  platform["key"],
                "platform_label": platform["label"],
                "url":       h["url"],
                "title":     h["title"],
                "snippet":   h["snippet"],
                "has_hashtag_in_snippet": hashtag.lower() in (h["title"] + " " + h["snippet"]).lower()
                                           or ("#" + bare.lower()) in (h["title"] + " " + h["snippet"]).lower(),
            })
    return clean


# ── YouTube native search (richer than scraping) ──────────────────────────────

def _youtube_hashtag_search(hashtag: str, max_results: int = 25) -> List[Dict]:
    """Use YouTube Data API for YouTube posts — returns real metrics."""
    api_key = os.getenv("YOUTUBE_API_KEY") or os.getenv("API_KEY")
    if not api_key:
        logger.info("No YouTube API key — skipping native YouTube search.")
        return []

    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        youtube = build("youtube", "v3", developerKey=api_key)
    except Exception as e:
        logger.warning(f"YouTube client init failed: {e}")
        return []

    bare = hashtag.lstrip("#")
    try:
        search = youtube.search().list(
            part="snippet",
            q=hashtag,
            type="video",
            maxResults=min(max_results, 50),
            order="relevance",
        ).execute()
    except Exception as e:
        # Catch broadly — HttpError, SSL errors, network issues all fall here
        logger.warning(f"YouTube search failed: {e}")
        return []

    video_ids = [i["id"]["videoId"] for i in search.get("items", []) if i.get("id", {}).get("videoId")]
    if not video_ids:
        return []

    # Hydrate with metrics
    try:
        details = youtube.videos().list(
            part="snippet,statistics",
            id=",".join(video_ids),
        ).execute()
    except Exception as e:
        logger.warning(f"YouTube video details failed: {e}")
        return []

    hits: List[Dict] = []
    for item in details.get("items", []):
        s  = item.get("snippet", {})
        st = item.get("statistics", {})
        text_blob = (s.get("title", "") + " " + s.get("description", "")).lower()
        hits.append({
            "platform":        "youtube",
            "platform_label":  "YouTube",
            "url":             f"https://www.youtube.com/watch?v={item['id']}",
            "title":           s.get("title", ""),
            "snippet":         (s.get("description") or "")[:400],
            "thumbnail":       s.get("thumbnails", {}).get("high", {}).get("url", ""),
            "channel_title":   s.get("channelTitle", ""),
            "channel_id":      s.get("channelId", ""),
            "published_at":    s.get("publishedAt", ""),
            "view_count":      int(st.get("viewCount", 0) or 0),
            "like_count":      int(st.get("likeCount", 0) or 0),
            "comment_count":   int(st.get("commentCount", 0) or 0),
            "has_hashtag_in_snippet": hashtag.lower() in text_blob or ("#" + bare.lower()) in text_blob,
        })
    return hits


# ── Public API ────────────────────────────────────────────────────────────────

def run_hashtag_search(
    hashtag: str,
    per_platform: int = 15,
    force_refresh: bool = False,
) -> Dict:
    """
    Search a hashtag across all tracked platforms. Returns a grouped,
    per-platform breakdown. Caches per-hashtag to data/hashtag_<slug>.json
    unless force_refresh=True.
    """
    hashtag = _normalize_hashtag(hashtag)
    if not hashtag or hashtag == "#":
        return {"ok": False, "error": "Empty hashtag"}

    slug = _slugify(hashtag.lstrip("#"))
    cache_file = f"hashtag_{slug}.json"

    if not force_refresh:
        cached = load_json(cache_file, default=None)
        if cached and cached.get("generated_at"):
            age = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(cached["generated_at"].replace("Z", "+00:00"))
            ).total_seconds()
            if age < 60 * 30:   # 30-minute cache
                cached["from_cache"] = True
                return {"ok": True, "data": cached}

    with _search_lock:
        all_hits: List[Dict] = []

        # 1. YouTube native search first (richest metadata)
        yt_hits = _youtube_hashtag_search(hashtag, max_results=per_platform)
        all_hits.extend(yt_hits)
        seen_urls = {h["url"] for h in all_hits}

        # 2. DDG search per platform (skip YouTube — already covered above)
        for plat in PLATFORMS:
            if plat["key"] == "youtube" and yt_hits:
                # Still scrape YT via DDG too — catches shorts / community posts
                pass
            logger.info(f"Searching {plat['label']} for {hashtag}")
            hits = _search_platform(hashtag, plat, per_platform)
            for h in hits:
                if h["url"] not in seen_urls:
                    all_hits.append(h)
                    seen_urls.add(h["url"])
            time.sleep(0.6)   # be polite to DDG

    # Group by platform
    by_platform: Dict[str, List[Dict]] = {p["key"]: [] for p in PLATFORMS}
    by_platform["other"] = []
    for h in all_hits:
        key = h.get("platform", "other")
        by_platform.setdefault(key, []).append(h)

    # Platform-level totals
    platform_summary = []
    for p in PLATFORMS:
        posts = by_platform.get(p["key"], [])
        reach = sum(h.get("view_count", 0) for h in posts)
        platform_summary.append({
            "key":          p["key"],
            "label":        p["label"],
            "post_count":   len(posts),
            "reach":        reach,   # only populated for YouTube right now
            "has_metrics":  p["key"] == "youtube",
        })

    total_youtube_reach = sum(
        h.get("view_count", 0) for h in by_platform.get("youtube", [])
    )

    payload = {
        "hashtag":           hashtag,
        "slug":              slug,
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "total_posts":       len(all_hits),
        "platforms":         platform_summary,
        "by_platform":       by_platform,
        "total_youtube_reach": total_youtube_reach,
        "from_cache":        False,
    }

    save_json(cache_file, payload)

    # Also update an index so the dashboard can show recent searches
    index = load_json("hashtag_index.json", default=[])
    index = [i for i in index if i.get("hashtag", "").lower() != hashtag.lower()]
    index.insert(0, {
        "hashtag":      hashtag,
        "slug":         slug,
        "generated_at": payload["generated_at"],
        "total_posts":  payload["total_posts"],
    })
    save_json("hashtag_index.json", index[:30])

    return {"ok": True, "data": payload}


def get_cached_hashtag(hashtag: str) -> Optional[Dict]:
    hashtag = _normalize_hashtag(hashtag)
    if not hashtag:
        return None
    slug = _slugify(hashtag.lstrip("#"))
    return load_json(f"hashtag_{slug}.json", default=None)


def list_recent_searches() -> List[Dict]:
    return load_json("hashtag_index.json", default=[])
