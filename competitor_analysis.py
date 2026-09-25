"""
competitor_analysis.py
----------------------
Brand-paired competitor intelligence.

Model:
  Each Tolaram brand (Indomie, Hypo, Power Oil, Colgate, ...) has a
  defined competitor set (Maggi, JIK, Mamador, Close-Up, ...). Every
  competitor is "owned" by one Tolaram brand and is benchmarked against
  it specifically — not the whole portfolio.

Three layers:
  1. Pairing layer   — data/brand_competitors.json (which competitor → which brand)
  2. Intelligence layer
       - YouTube data fetched per competitor (recent uploads + metrics)
       - Campaign detection: clusters videos by recurring hashtags / title patterns
       - AI strategic analysis: Gemini reads the data and writes
         (a) what they're doing, (b) what's working, (c) what Tolaram should do
  3. Action layer
       - Persistent notes per competitor (timestamped, append-only)
       - Counter-moves log (planned response with status + due date)

All caches live in data/ as JSON. Scans are on-demand.
"""
from __future__ import annotations

import os
import re
import json
import logging
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import List, Dict, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from data_store import save_json, load_json

logger = logging.getLogger(__name__)

# ── Files ─────────────────────────────────────────────────────────────────────

BRAND_COMPETITORS_FILE = "brand_competitors.json"   # who tracks what
ANALYSIS_FILE          = "competitor_analysis.json" # last scan results
NOTES_FILE             = "competitor_notes.json"    # user observations
MOVES_FILE             = "competitor_moves.json"    # counter-move log

_scan_lock = threading.Lock()


# ── Default competitor set: 20 competitors across Tolaram brands ─────────────
# Sourced from Marketing Edge, Nairametrics, Vanguard and Business Hallmark
# coverage of each FMCG category in Nigeria. The user can edit, add or
# remove these via the dashboard at any time.

DEFAULT_BRAND_COMPETITORS: Dict[str, List[Dict]] = {
    "Indomie": [
        {"reference": "@MaggiNigeria", "label": "Maggi (Nestlé)", "priority": "primary", "rationale": "Closest premium competitor by brand equity; aggressive TV + digital."},
        {"reference": "Honeywell Noodles", "label": "Honeywell Noodles", "priority": "primary", "channel_id": "UCqUiywLq-5iUNa6nbFH5Jgg", "rationale": "Consistent #2 in noodles satisfaction surveys; FinIntell verified."},
        {"reference": "Supreme Noodles", "label": "Supreme Noodles", "priority": "watch", "rationale": "Late entrant (~2017) capitalising on FX-driven price gaps."},
        {"reference": "@goldenpennyfoodsm", "label": "Golden Penny Foods", "priority": "secondary", "channel_id": "UCc3HKZPLNtJHUIGocNbgiAQ", "rationale": "Flour Mills of Nigeria brand — instant noodles, pasta, semovita line."},
    ],
    "Hypo": [
        {"reference": "JIK Nigeria", "label": "JIK (Reckitt)", "priority": "primary", "rationale": "Original market leader pre-Hypo; still dominant in supermarkets."},
        {"reference": "Harpic Nigeria", "label": "Harpic (Reckitt)", "priority": "primary", "rationale": "Premium toilet-cleaner segment; takes share from Hypo middle-class users."},
        {"reference": "Klin Nigeria", "label": "Klin", "priority": "watch", "rationale": "Nearly identical product profile; could become next viral copy target."},
    ],
    "Power Oil": [
        {"reference": "Mamador Nigeria", "label": "Mamador (PZ Wilmar)", "priority": "primary", "rationale": "Health-positioned premium oil; PZ Wilmar's flagship. Leads upper class."},
        {"reference": "Devon Kings", "label": "Devon King's (PZ Wilmar)", "priority": "primary", "channel_id": "UCJdlhHxlc7850x1PZqj9j6Q", "rationale": "Generic-name brand for vegetable oil. Controls 61% of high-end with Mamador."},
        {"reference": "Sunola Oil", "label": "Sunola Oil", "priority": "secondary", "rationale": "Sunseed/Kewalram Chanrai. Vitamin-fortified positioning."},
        {"reference": "Grand Pure Soya Oil", "label": "Grand Pure Soya", "priority": "secondary", "rationale": "Heart-foundation endorsed; competing on health credentials."},
    ],
    "Colgate": [
        {"reference": "@CloseUpNigeria", "label": "Close-Up (Unilever)", "priority": "primary", "channel_id": "UCxVtQ8NhPzUAB-CkJZhmmqg", "rationale": "Genericised brand for toothpaste in Nigeria; Unilever's flagship oral care."},
        {"reference": "Oral-B Nigeria", "label": "Oral-B (P&G)", "priority": "secondary", "rationale": "Premium dental positioning; toothbrush + toothpaste."},
        {"reference": "Pepsodent Nigeria", "label": "Pepsodent (Unilever)", "priority": "secondary", "rationale": "Mass-market Unilever toothpaste; competes on price."},
        {"reference": "Macleans toothpaste", "label": "Macleans", "priority": "watch", "rationale": "Legacy genericised toothpaste brand."},
    ],
    "Munch It": [
        {"reference": "@PringlesNigeria", "label": "Pringles", "priority": "primary", "channel_id": "UCrue10ghHHplaYiQjpEYtYA", "rationale": "Premium global crisp brand active in Nigerian retail."},
        {"reference": "Lays Nigeria", "label": "Lay's", "priority": "secondary", "rationale": "PepsiCo crisp brand; younger consumer overlap.", "resolution_note": "No verifiably official Lay's Nigeria YouTube channel found — PepsiCo does not appear to run one for this market. A prior auto-match to the musician Omah Lay's channel was a false positive (a matching-algorithm bug, since fixed) and has been cleared. Confirm and pin manually if a real channel exists."},
        {"reference": "@funsnax", "label": "Fun Snax", "priority": "secondary", "channel_id": "UCBtfPawfP529MW_Btd7A-Uw", "rationale": "Nigerian snack brand — direct category competitor."},
    ],
    "Minimie": [
    ],
    "Nutrify": [
        {"reference": "Golden Morn Nigeria", "label": "Golden Morn (Nestlé)", "priority": "primary", "rationale": "Leading Nigerian breakfast cereal; Nestlé portfolio."},
    ],
}


# ── Brand-competitor pairing CRUD ─────────────────────────────────────────────

def _ensure_seeded() -> Dict[str, List[Dict]]:
    """If the brand-competitor file doesn't exist yet, seed it with defaults."""
    existing = load_json(BRAND_COMPETITORS_FILE, default=None)
    if existing is None:
        save_json(BRAND_COMPETITORS_FILE, DEFAULT_BRAND_COMPETITORS)
        logger.info("Seeded brand_competitors.json with 20 default competitors")
        return DEFAULT_BRAND_COMPETITORS
    return existing


def list_brand_competitors() -> Dict[str, List[Dict]]:
    """Return the full brand → [competitors] map."""
    return _ensure_seeded()


def list_brands() -> List[str]:
    """List Tolaram brands that have at least one tracked competitor."""
    return list(_ensure_seeded().keys())


def list_all_competitors() -> List[Dict]:
    """Flattened list of every competitor with its parent brand attached."""
    out = []
    for brand, comps in _ensure_seeded().items():
        for c in comps:
            out.append({**c, "tolaram_brand": brand})
    return out


def add_competitor(brand: str, reference: str,
                   label: Optional[str] = None,
                   priority: str = "secondary",
                   rationale: Optional[str] = None) -> Dict:
    """Pair a new competitor under a Tolaram brand."""
    brand = (brand or "").strip()
    reference = (reference or "").strip()
    if not brand or not reference:
        return {"ok": False, "error": "brand and reference required"}

    pairings = _ensure_seeded()
    pairings.setdefault(brand, [])
    for c in pairings[brand]:
        if c.get("reference", "").lower() == reference.lower():
            return {"ok": False, "error": "Already tracked under this brand"}

    entry = {
        "reference": reference,
        "label":     label or reference,
        "priority":  priority if priority in ("primary", "secondary", "watch") else "secondary",
        "rationale": rationale or "",
        "added_at":  datetime.now(timezone.utc).isoformat(),
    }
    pairings[brand].append(entry)
    save_json(BRAND_COMPETITORS_FILE, pairings)
    return {"ok": True, "data": entry}


def remove_competitor(brand: str, reference: str) -> Dict:
    pairings = _ensure_seeded()
    if brand not in pairings:
        return {"ok": False, "error": "Brand not tracked"}
    before = len(pairings[brand])
    pairings[brand] = [
        c for c in pairings[brand]
        if c.get("reference", "").lower() != reference.lower()
    ]
    save_json(BRAND_COMPETITORS_FILE, pairings)
    return {"ok": True, "removed": before - len(pairings[brand])}


def pin_competitor_channel(brand: str, reference: str, channel_id: str) -> Dict:
    """
    Manually confirm the correct UC-id for a competitor whose auto-resolution
    was wrong or unverified. Once pinned, scans always use this id directly —
    no more free-text guessing for this entry.
    """
    channel_id = (channel_id or "").strip()
    if not channel_id.startswith("UC") or len(channel_id) < 20:
        return {"ok": False, "error": "channel_id must be a UC-… channel id"}

    pairings = _ensure_seeded()
    if brand not in pairings:
        return {"ok": False, "error": "Brand not tracked"}
    for c in pairings[brand]:
        if c.get("reference", "").lower() == reference.lower():
            c["channel_id"] = channel_id
            save_json(BRAND_COMPETITORS_FILE, pairings)
            return {"ok": True, "data": c}
    return {"ok": False, "error": "Competitor not found under this brand"}


def reset_to_defaults() -> Dict:
    """Wipe and re-seed with the defaults — useful if the user wants a clean slate."""
    save_json(BRAND_COMPETITORS_FILE, DEFAULT_BRAND_COMPETITORS)
    return {"ok": True, "data": DEFAULT_BRAND_COMPETITORS}


# ── YouTube fetch helpers ─────────────────────────────────────────────────────

_MATCH_STOPWORDS = {
    "nigeria", "official", "tv", "the", "a", "an", "and", "ng", "channel",
    "brand", "com", "inc", "limited", "ltd", "plc",
}


def _normalize_for_match(s: str) -> set:
    """Lowercase, strip punctuation, drop stopwords/short tokens -> a token set.

    Apostrophes are DROPPED (not turned into spaces) so "Lay's" normalizes
    to "lays" as one token, not "lay" + "s" — the latter previously let
    "Lay's" false-match onto the musician Omah Lay's channel purely because
    "lay" happened to appear in both titles.
    """
    s = (s or "").lower().replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return {t for t in s.split() if t and t not in _MATCH_STOPWORDS and len(t) > 2}


def _match_score(query_tokens: set, candidate_title: str) -> float:
    """Fraction of query tokens found in the candidate channel title. 0..1."""
    if not query_tokens:
        return 0.0
    cand_tokens = _normalize_for_match(candidate_title)
    if not cand_tokens:
        return 0.0
    return len(query_tokens & cand_tokens) / len(query_tokens)


# Minimum token-overlap score to auto-accept a free-text search result.
# Below this we refuse to guess and surface candidates for manual confirmation
# instead — this is what was silently matching "Close-Up" to "Sunday Goodness".
_MATCH_ACCEPT_THRESHOLD = 0.5


def _resolve_channel_id(youtube, ref: str, label: str = "",
                        pinned_channel_id: str = "") -> Dict:
    """
    Resolve a competitor reference to a canonical UC-id.

    Returns:
      {"channel_id": str|None, "confidence": "pinned"|"id"|"handle"|"verified_search"|"unverified"|"none",
       "candidates": [{"channel_id","title","score"}, ...],   # present when unverified/verified_search
       "reason": str}                                          # present when unresolved
    """
    ref = (ref or "").strip()

    # A manually confirmed channel_id always wins — no re-guessing.
    if pinned_channel_id:
        return {"channel_id": pinned_channel_id, "confidence": "pinned"}

    if not ref:
        return {"channel_id": None, "confidence": "none", "reason": "No reference given"}

    if ref.startswith("UC") and len(ref) >= 20:
        return {"channel_id": ref, "confidence": "id"}

    query_tokens = _normalize_for_match(label or ref)

    if ref.startswith("@"):
        try:
            r = youtube.channels().list(part="id", forHandle=ref.lstrip("@")).execute()
            items = r.get("items", [])
            if items:
                return {"channel_id": items[0]["id"], "confidence": "handle"}
        except HttpError as e:
            logger.warning(f"Handle resolve failed for {ref}: {e}")

    # Free-text search: pull several candidates and SCORE them against the
    # brand label before accepting any — never take result #1 on faith.
    try:
        r = youtube.search().list(part="snippet", q=ref, type="channel", maxResults=5).execute()
        items = r.get("items", [])
        scored = []
        for it in items:
            title = it.get("snippet", {}).get("channelTitle", "")
            scored.append({
                "channel_id": it["snippet"]["channelId"],
                "title":      title,
                "score":      round(_match_score(query_tokens, title), 2),
            })
        scored.sort(key=lambda x: x["score"], reverse=True)

        if scored and scored[0]["score"] >= _MATCH_ACCEPT_THRESHOLD:
            return {
                "channel_id": scored[0]["channel_id"],
                "confidence": "verified_search",
                "candidates": scored[:5],
            }
        if scored:
            return {
                "channel_id": None,
                "confidence": "unverified",
                "candidates": scored[:5],
                "reason": (f"No confident match — best guess was \"{scored[0]['title']}\" "
                           f"which doesn't look like {label or ref}. Confirm the real channel manually."),
            }
    except HttpError as e:
        logger.warning(f"Channel search failed for {ref}: {e}")

    return {"channel_id": None, "confidence": "none",
            "reason": f"No YouTube channel found for \"{ref}\" — this brand may not run a dedicated channel."}


def _fetch_channel_overview(youtube, channel_id: str) -> Dict:
    try:
        r = youtube.channels().list(
            part="snippet,statistics,contentDetails", id=channel_id
        ).execute()
        if not r.get("items"):
            return {}
        item = r["items"][0]
        s, st = item.get("snippet", {}), item.get("statistics", {})
        return {
            "channel_id":       channel_id,
            "title":            s.get("title", "Unknown"),
            "description":      (s.get("description") or "")[:300],
            "country":          s.get("country", "N/A"),
            "subscriber_count": int(st.get("subscriberCount", 0) or 0),
            "video_count":      int(st.get("videoCount", 0) or 0),
            "view_count":       int(st.get("viewCount", 0) or 0),
            "thumbnail":        s.get("thumbnails", {}).get("high", {}).get("url", ""),
            "uploads_playlist": item.get("contentDetails", {})
                                    .get("relatedPlaylists", {}).get("uploads", ""),
        }
    except HttpError as e:
        logger.error(f"Overview fetch failed for {channel_id}: {e}")
        return {}


def _fetch_recent_videos(youtube, uploads_playlist: str, max_results: int = 20) -> List[Dict]:
    if not uploads_playlist:
        return []
    video_ids: List[str] = []
    next_page = None
    try:
        while len(video_ids) < max_results:
            r = youtube.playlistItems().list(
                part="contentDetails", playlistId=uploads_playlist,
                maxResults=min(max_results - len(video_ids), 50),
                pageToken=next_page,
            ).execute()
            video_ids.extend(i["contentDetails"]["videoId"] for i in r.get("items", []))
            next_page = r.get("nextPageToken")
            if not next_page:
                break
    except HttpError as e:
        logger.error(f"Playlist fetch failed: {e}")
        return []

    if not video_ids:
        return []

    videos: List[Dict] = []
    try:
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i:i + 50]
            r = youtube.videos().list(
                part="snippet,statistics,contentDetails", id=",".join(chunk)
            ).execute()
            for item in r.get("items", []):
                s  = item.get("snippet", {})
                st = item.get("statistics", {})
                cd = item.get("contentDetails", {})
                videos.append({
                    "video_id":      item["id"],
                    "title":         s.get("title", ""),
                    "description":   (s.get("description") or "")[:600],
                    "published_at":  s.get("publishedAt", ""),
                    "thumbnail":     s.get("thumbnails", {}).get("high", {}).get("url", ""),
                    "tags":          s.get("tags", [])[:15],
                    "duration":      cd.get("duration", ""),
                    "view_count":    int(st.get("viewCount", 0) or 0),
                    "like_count":    int(st.get("likeCount", 0) or 0),
                    "comment_count": int(st.get("commentCount", 0) or 0),
                    "video_url":     f"https://www.youtube.com/watch?v={item['id']}",
                })
    except HttpError as e:
        logger.error(f"Video detail fetch failed: {e}")
    return videos


# ── Campaign detection ───────────────────────────────────────────────────────

_HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")
_PHRASE_RE  = re.compile(r"[a-zA-Z][a-zA-Z0-9'\-]{2,}")

_BANAL_WORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "at", "by", "from", "is", "are", "video", "shorts", "official",
    "watch", "subscribe", "now", "new", "all", "your", "our", "this", "that",
}


def _detect_campaigns(videos: List[Dict]) -> List[Dict]:
    """
    Cluster videos into 'campaigns' by recurring hashtag/phrase signature.
    A campaign = 2+ videos sharing a distinctive hashtag or 3-word title phrase.
    """
    hashtag_to_videos: Dict[str, List[Dict]] = defaultdict(list)
    for v in videos:
        text = (v.get("title", "") + " " + v.get("description", ""))
        for h in _HASHTAG_RE.findall(text):
            hashtag_to_videos["#" + h.lower()].append(v)

    title_phrases: Counter = Counter()
    phrase_to_videos: Dict[str, List[Dict]] = defaultdict(list)
    for v in videos:
        words = [w.lower() for w in _PHRASE_RE.findall(v.get("title", ""))
                 if w.lower() not in _BANAL_WORDS and len(w) >= 3]
        for n in (4, 3):
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i:i + n])
                title_phrases[phrase] += 1
                phrase_to_videos[phrase].append(v)

    campaigns: List[Dict] = []
    seen = set()

    for tag, vids in sorted(hashtag_to_videos.items(),
                            key=lambda x: len(x[1]), reverse=True):
        if len(vids) < 2:
            continue
        new_vids = [v for v in vids if v["video_id"] not in seen]
        if len(new_vids) < 2:
            continue
        campaigns.append(_make_campaign("hashtag", tag, new_vids))
        seen.update(v["video_id"] for v in new_vids)

    for phrase, n in title_phrases.most_common(15):
        if n < 2:
            break
        vids = phrase_to_videos[phrase]
        new_vids = [v for v in vids if v["video_id"] not in seen]
        if len(new_vids) < 2:
            continue
        campaigns.append(_make_campaign("phrase", f'"{phrase}"', new_vids))
        seen.update(v["video_id"] for v in new_vids)

    return sorted(campaigns, key=lambda c: c["total_reach"], reverse=True)[:8]


def _make_campaign(kind: str, signature: str, vids: List[Dict]) -> Dict:
    return {
        "signature":      signature,
        "type":           kind,
        "video_count":    len(vids),
        "total_reach":    sum(v.get("view_count", 0) for v in vids),
        "total_likes":    sum(v.get("like_count", 0) for v in vids),
        "total_comments": sum(v.get("comment_count", 0) for v in vids),
        "first_seen":     min((v.get("published_at", "") for v in vids), default=""),
        "last_seen":      max((v.get("published_at", "") for v in vids), default=""),
        "videos":         sorted(vids, key=lambda v: v.get("view_count", 0), reverse=True)[:8],
    }


# ── AI strategic analysis ────────────────────────────────────────────────────

def _build_strategic_prompt(tolaram_brand: str, competitor: Dict,
                            videos: List[Dict], campaigns: List[Dict],
                            own_brand_videos: List[Dict]) -> str:
    """Build the prompt that goes to Gemini for strategic synthesis."""

    top = sorted(videos, key=lambda v: v.get("view_count", 0), reverse=True)[:10]
    comp_lines = [
        f"  - \"{v.get('title','')[:120]}\" — {v.get('view_count',0):,} views, "
        f"{v.get('like_count',0):,} likes, published {v.get('published_at','')[:10]}"
        for v in top
    ]

    camp_lines = [
        f"  - {c['signature']} ({c['type']}) — {c['video_count']} videos, "
        f"{c['total_reach']:,} reach, {c['first_seen'][:10]} → {c['last_seen'][:10]}"
        for c in campaigns[:5]
    ]

    own_top = sorted(own_brand_videos, key=lambda v: v.get("view_count", 0), reverse=True)[:8]
    own_lines = [
        f"  - \"{v.get('title','')[:120]}\" — {v.get('view_count',0):,} views"
        for v in own_top
    ]

    return f"""You are a senior brand strategist at Tolaram Group analysing a YouTube competitor.

TOLARAM BRAND: {tolaram_brand}
COMPETITOR: {competitor.get('label')} (rationale: {competitor.get('rationale','')})
PRIORITY: {competitor.get('priority','secondary')}

COMPETITOR'S TOP 10 RECENT VIDEOS:
{chr(10).join(comp_lines) if comp_lines else "  (none)"}

COMPETITOR'S DETECTED CAMPAIGNS:
{chr(10).join(camp_lines) if camp_lines else "  (none — no recurring hashtags or phrases detected)"}

{tolaram_brand.upper()}'S OWN RECENT TOP VIDEOS ON YOUTUBE:
{chr(10).join(own_lines) if own_lines else "  (none)"}

Write a tight, decision-oriented competitor brief in this exact JSON shape (no markdown, no preamble).
Keep every string under 25 words — this is a scannable brief, not an essay:

{{
  "what_theyre_doing": "<1-2 short sentences: their current creative strategy, themes, formats.>",
  "whats_working": [
    "<short bullet — a specific tactic that's earning reach, with the evidence>",
    "<another short bullet>"
  ],
  "where_we_lag": [
    "<a specific thing the competitor does that {tolaram_brand} isn't matching, short>"
  ],
  "counter_moves": [
    {{"move": "<concrete action, under 20 words>", "rationale": "<why this beats them, under 15 words>", "urgency": "high|medium|low"}},
    {{"move": "<another move, under 20 words>", "rationale": "<why, under 15 words>", "urgency": "high|medium|low"}}
  ],
  "leverage_play": "<1 short sentence: the single boldest move {tolaram_brand} could make to leapfrog this competitor>"
}}

Be direct. No hedging. No 'consider exploring' language. Real campaign ideas, not platitudes. Terse over complete.
Return ONLY the JSON object, nothing else."""


def _repair_truncated_json(text: str) -> Optional[Dict]:
    """
    Best-effort repair for JSON that got cut off mid-stream (e.g. the model
    hit its output limit while still writing). Walks the text tracking
    string/bracket state, finds the last point where it's safe to cut
    (right after a closed string or a closed object/array — never
    mid-string), truncates there, closes out whatever brackets are still
    open, and tries to parse. Worst case it drops the last unfinished
    bullet/counter-move; it never fabricates content.
    """
    if not text:
        return None

    stack: List[str] = []
    in_string = False
    escape = False
    last_safe_idx = None
    last_safe_stack: Optional[List[str]] = None

    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
                last_safe_idx = i + 1
                last_safe_stack = list(stack)
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            last_safe_idx = i + 1
            last_safe_stack = list(stack)

    if last_safe_idx is None or not last_safe_stack:
        return None

    truncated = re.sub(r",\s*$", "", text[:last_safe_idx])
    closers = "".join("}" if c == "{" else "]" for c in reversed(last_safe_stack))

    try:
        return json.loads(truncated + closers)
    except json.JSONDecodeError:
        return None


def _parse_ai_json(text: str) -> Dict:
    """Best-effort JSON parse shared by every LLM backend. Tries a clean
    parse first, then a truncation repair, before giving up — the UI should
    never have to show raw model output to a marketing user."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    repaired = _repair_truncated_json(text)
    if repaired is not None:
        repaired["_repaired_from_truncation"] = True
        logger.info("Strategic brief JSON was truncated — repaired by trimming to the last complete field.")
        return repaired

    return {"ai_raw": text, "parse_error": True}


def _call_gemini(prompt: str) -> Dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key.strip().upper() in ("", "YOUR_GEMINI_API_KEY_HERE"):
        logger.warning("Gemini strategic analysis skipped: GEMINI_API_KEY not set (or still the .env placeholder)")
        return {"ai_skipped": True, "reason": "GEMINI_API_KEY not set"}
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        # Hardcoded, no env override — a stale GEMINI_COMPETITOR_MODEL value in
        # .env was previously reintroducing the deprecated gemini-2.5-flash model.
        model = genai.GenerativeModel(model_name="gemini-3.6-flash")

        # Repeated truncation at the same point even after raising
        # max_output_tokens points at hidden "thinking" tokens eating the
        # budget before visible JSON gets written (default behaviour on
        # recent Gemini generations). Try disabling that first; if this
        # model/SDK combo doesn't recognise the param, fall back cleanly.
        base_config = {"response_mime_type": "application/json", "max_output_tokens": 16384}
        try:
            resp = model.generate_content(
                prompt,
                generation_config={**base_config, "thinking_config": {"thinking_budget": 0}},
            )
        except Exception as e:
            logger.info(f"Gemini call with thinking_budget=0 rejected ({e}); retrying without it")
            resp = model.generate_content(prompt, generation_config=base_config)

        text = (resp.text or "") if hasattr(resp, "text") else ""
        try:
            finish_reason = resp.candidates[0].finish_reason if resp.candidates else None
            if finish_reason and str(finish_reason).upper() not in ("STOP", "1", "FINISHREASON.STOP"):
                logger.warning(f"Gemini strategic brief may be truncated — finish_reason={finish_reason}")
        except Exception:
            pass
        return _parse_ai_json(text)
    except Exception as e:
        logger.warning(f"Gemini strategic analysis failed: {e}")
        return {"ai_error": str(e), "ai_engine": "gemini"}


def _ai_strategic_analysis(tolaram_brand: str, competitor: Dict,
                           videos: List[Dict], campaigns: List[Dict],
                           own_brand_videos: List[Dict]) -> Dict:
    """Ask Gemini to write the strategic brief. Claude is not used here."""
    prompt = _build_strategic_prompt(
        tolaram_brand, competitor, videos, campaigns, own_brand_videos
    )
    return _call_gemini(prompt)


# ── Main scan orchestrator ────────────────────────────────────────────────────

def run_brand_scan(brand: Optional[str] = None,
                   max_videos_per_channel: int = 15,
                   include_ai: bool = True) -> Dict:
    """
    Scan competitors. If `brand` is None, scan all brands.
    Otherwise scan only competitors paired to that one Tolaram brand.
    """
    api_key = os.getenv("YOUTUBE_API_KEY") or os.getenv("API_KEY")
    if not api_key:
        return {"ok": False, "error": "YOUTUBE_API_KEY not set"}

    pairings = _ensure_seeded()
    if brand and brand not in pairings:
        return {"ok": False, "error": f"Brand '{brand}' has no tracked competitors"}

    brands_to_scan = [brand] if brand else list(pairings.keys())

    def _key_present(name: str) -> bool:
        v = os.getenv(name, "")
        return bool(v) and v.strip().upper() != f"YOUR_{name}_HERE"

    if include_ai:
        logger.info(
            f"AI backend check (Gemini only) — GEMINI_API_KEY: "
            f"{'present' if _key_present('GEMINI_API_KEY') else 'MISSING/placeholder'}"
        )

    own_videos = load_json("all_videos.json", default=[])
    own_by_brand: Dict[str, List[Dict]] = defaultdict(list)
    for v in own_videos:
        own_by_brand[v.get("brand", "Unknown")].append(v)

    with _scan_lock:
        youtube = build("youtube", "v3", developerKey=api_key)
        results: Dict[str, List[Dict]] = {}

        for tol_brand in brands_to_scan:
            comps = pairings.get(tol_brand, [])
            brand_results: List[Dict] = []
            logger.info(f"Scanning {len(comps)} competitors for {tol_brand}")

            for comp in comps:
                ref = comp["reference"]
                resolution = _resolve_channel_id(
                    youtube, ref,
                    label=comp.get("label", ""),
                    pinned_channel_id=comp.get("channel_id", ""),
                )
                channel_id = resolution.get("channel_id")
                if not channel_id:
                    brand_results.append({
                        **comp, "tolaram_brand": tol_brand,
                        "error": resolution.get("reason", "Could not resolve channel"),
                        "resolution_confidence": resolution.get("confidence"),
                        "candidates": resolution.get("candidates", []),
                    })
                    continue

                overview = _fetch_channel_overview(youtube, channel_id)
                if not overview:
                    brand_results.append({
                        **comp, "tolaram_brand": tol_brand,
                        "channel_id": channel_id,
                        "error": "Could not fetch channel overview",
                    })
                    continue

                videos = _fetch_recent_videos(
                    youtube, overview.get("uploads_playlist", ""),
                    max_results=max_videos_per_channel,
                )
                campaigns = _detect_campaigns(videos)

                total_reach = sum(v.get("view_count", 0) for v in videos)
                avg_views   = round(total_reach / max(len(videos), 1))

                ai_brief = None
                if include_ai and videos:
                    ai_brief = _ai_strategic_analysis(
                        tol_brand, comp, videos, campaigns,
                        own_by_brand.get(tol_brand, []),
                    )

                brand_results.append({
                    **comp,
                    "tolaram_brand":        tol_brand,
                    "channel_id":           channel_id,
                    "resolution_confidence": resolution.get("confidence"),
                    "channel":        overview,
                    "video_count":    len(videos),
                    "videos":         videos[:8],
                    "campaigns":      campaigns,
                    "metrics": {
                        "window_reach":     total_reach,
                        "window_avg_views": avg_views,
                        "window_likes":     sum(v.get("like_count", 0) for v in videos),
                        "window_comments":  sum(v.get("comment_count", 0) for v in videos),
                    },
                    "ai_brief":       ai_brief,
                    "scanned_at":     datetime.now(timezone.utc).isoformat(),
                })

            results[tol_brand] = brand_results

    cache = load_json(ANALYSIS_FILE, default={"brands": {}, "generated_at": ""})
    cache.setdefault("brands", {})
    for k, v in results.items():
        cache["brands"][k] = v
    cache["generated_at"] = datetime.now(timezone.utc).isoformat()
    cache["last_scanned_brands"] = brands_to_scan
    save_json(ANALYSIS_FILE, cache)
    logger.info(f"Scan complete for: {brands_to_scan}")
    return {"ok": True, "data": cache}


def load_cached_analysis() -> Dict:
    return load_json(ANALYSIS_FILE, default={"brands": {}, "generated_at": ""})


# ── Notes (per competitor, append-only timestamped log) ──────────────────────

def list_notes(competitor_ref: Optional[str] = None) -> List[Dict]:
    notes = load_json(NOTES_FILE, default=[])
    if competitor_ref:
        notes = [n for n in notes if n.get("competitor_ref", "").lower() == competitor_ref.lower()]
    return sorted(notes, key=lambda n: n.get("created_at", ""), reverse=True)


def add_note(competitor_ref: str, brand: str, body: str,
             author: str = "User") -> Dict:
    if not competitor_ref or not body.strip():
        return {"ok": False, "error": "competitor_ref and body required"}
    notes = load_json(NOTES_FILE, default=[])
    note = {
        "id":             f"n_{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        "competitor_ref": competitor_ref,
        "brand":          brand,
        "body":           body.strip(),
        "author":         author,
        "created_at":     datetime.now(timezone.utc).isoformat(),
    }
    notes.append(note)
    save_json(NOTES_FILE, notes)
    return {"ok": True, "data": note}


def delete_note(note_id: str) -> Dict:
    notes = load_json(NOTES_FILE, default=[])
    new = [n for n in notes if n.get("id") != note_id]
    save_json(NOTES_FILE, new)
    return {"ok": True, "removed": len(notes) - len(new)}


# ── Counter-moves log ────────────────────────────────────────────────────────

def list_moves(competitor_ref: Optional[str] = None,
               status: Optional[str] = None) -> List[Dict]:
    moves = load_json(MOVES_FILE, default=[])
    if competitor_ref:
        moves = [m for m in moves if m.get("competitor_ref", "").lower() == competitor_ref.lower()]
    if status:
        moves = [m for m in moves if m.get("status") == status]
    return sorted(moves, key=lambda m: m.get("created_at", ""), reverse=True)


def add_move(competitor_ref: str, brand: str, move: str,
             rationale: str = "", urgency: str = "medium",
             due_date: Optional[str] = None) -> Dict:
    if not competitor_ref or not move.strip():
        return {"ok": False, "error": "competitor_ref and move required"}
    moves = load_json(MOVES_FILE, default=[])
    record = {
        "id":             f"m_{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        "competitor_ref": competitor_ref,
        "brand":          brand,
        "move":           move.strip(),
        "rationale":      rationale.strip(),
        "urgency":        urgency if urgency in ("high", "medium", "low") else "medium",
        "due_date":       due_date or "",
        "status":         "planned",
        "created_at":     datetime.now(timezone.utc).isoformat(),
    }
    moves.append(record)
    save_json(MOVES_FILE, moves)
    return {"ok": True, "data": record}


def update_move_status(move_id: str, status: str) -> Dict:
    if status not in ("planned", "in_progress", "shipped", "dropped"):
        return {"ok": False, "error": "invalid status"}
    moves = load_json(MOVES_FILE, default=[])
    found = None
    for m in moves:
        if m.get("id") == move_id:
            m["status"] = status
            m["updated_at"] = datetime.now(timezone.utc).isoformat()
            found = m
            break
    if not found:
        return {"ok": False, "error": "move not found"}
    save_json(MOVES_FILE, moves)
    return {"ok": True, "data": found}


def delete_move(move_id: str) -> Dict:
    moves = load_json(MOVES_FILE, default=[])
    new = [m for m in moves if m.get("id") != move_id]
    save_json(MOVES_FILE, new)
    return {"ok": True, "removed": len(moves) - len(new)}