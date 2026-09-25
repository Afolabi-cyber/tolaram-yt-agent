# Changelog

All notable changes to the Tolaram YouTube Intelligence Agent.

## v2.2 — Brand-Paired Competitor Intelligence with AI Briefs (Apr 27, 2026)

Replaces the generic "add a channel, see what they post" model with a
strategic **brand-paired** system. Each Tolaram brand has a defined competitor
set; each competitor is benchmarked specifically against its parent brand;
each scan produces an AI strategic brief with concrete counter-moves.

### What's new
- **20 competitors auto-seeded** across 7 Tolaram brands, sourced from
  Marketing Edge / Nairametrics / Vanguard / Business Hallmark coverage:
  - Indomie (5): Maggi, Honeywell, Dangote, Chikki, Supreme
  - Hypo (3): JIK, Harpic, Klin
  - Power Oil (4): Mamador, Devon King's, Sunola, Grand Pure Soya
  - Colgate (4): Close-Up, Oral-B, Pepsodent, Macleans
  - Munch It (2): Pringles, Lay's
  - Minimie (1): Mimee
  - Nutrify (1): Golden Morn
- **Brand tabs** on the Competitor page — pick a Tolaram brand, see its
  competitor set and analysis.
- **Priority levels**: each competitor tagged primary / secondary / watch.
- **Rationale field**: capture why this competitor matters for this brand.
- **Campaign detection**: clusters competitor videos into named campaigns
  by recurring hashtags and 3-4-gram title phrases. Outputs reach + window.
- **AI strategic brief** (Claude 3.5 Sonnet) for each competitor, returning:
  1. What they're doing (creative strategy summary)
  2. What's working (specific tactics + evidence)
  3. Where Tolaram lags (gap analysis)
  4. Counter-moves (3 concrete actions w/ rationale + urgency)
  5. Leverage play (the boldest leapfrog move)
- **Persistent notes per competitor** — append-only timestamped log of your
  observations. Stored in `data/competitor_notes.json`.
- **Counter-moves log** — track planned actions with status cycle
  (planned -> in_progress -> shipped -> dropped) and urgency levels. Stored
  in `data/competitor_moves.json`. Click status pill to advance.
- **One-click "Save as Counter-Move"** button on every AI-suggested action
  to convert AI recommendations into your tracked log.

### New API endpoints
| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/competitors` | Brand -> [competitors] map |
| `GET` | `/api/competitors/flat` | Flat list with parent brand |
| `GET` | `/api/competitors/brands` | List of Tolaram brands tracked |
| `POST` | `/api/competitors` | Add `{brand, reference, label?, priority?, rationale?}` |
| `DELETE` | `/api/competitors?brand=&reference=` | Remove competitor |
| `POST` | `/api/competitors/reset` | Reset to seeded defaults |
| `POST` | `/api/competitors/scan` | Scan `{brand?, max_videos_per_channel?, include_ai?}` |
| `GET` | `/api/competitors/analysis` | Last cached analysis |
| `GET/POST` | `/api/competitors/notes` | List or add notes |
| `DELETE` | `/api/competitors/notes/<note_id>` | Delete note |
| `GET/POST` | `/api/competitors/moves` | List or add counter-moves |
| `PATCH` | `/api/competitors/moves/<move_id>` | Update status |
| `DELETE` | `/api/competitors/moves/<move_id>` | Delete move |

### Configuration
The AI brief uses `ANTHROPIC_API_KEY` from `.env`. If it's not set, scans
still run but `ai_brief` returns `{ai_skipped: true}` and the UI shows a
yellow notice. Approx 1,500 tokens per competitor x 20 competitors ~=
30K tokens for a full all-brand scan.

### Breaking changes
- Old flat `/api/competitors` shape (returning `[...]`) is replaced by the
  brand-grouped map (`{Indomie: [...], Hypo: [...]}`). Use
  `/api/competitors/flat` for the old shape.
- Old function names (`list_competitors`, `run_competitor_scan`) renamed to
  `list_brand_competitors`, `run_brand_scan`.

---


---

## [2.1.0] — 2026-04-24

### 🎯 New Intelligence Features

#### Monthly filter
- **NEW** global month picker in the topbar — slices Command Center, Videos, Comments and Crisis pages by `YYYY-MM`
- **NEW** `GET /api/months` — lists every month present in the corpus with per-month video/comment counts
- Hero stats on Command Center now recompute for the selected month (comments, videos, reach, crisis alerts)

#### YouTube reach analytics
- **NEW** `GET /api/reach?month=&brand=` — per-video and per-brand reach (view_count), with totals, averages and engagement rate
- **NEW** Reach summary section on the Videos page: 5 KPI cards, per-brand table, per-video table (top 50)
- Videos page now leads with Reach data rather than engagement rate

#### Competitor analysis
- **NEW** `competitor_analysis.py` module — resolves channels by @handle / UC-id / free text, pulls recent uploads via YouTube Data API, extracts title/tag/description keywords, computes content gap vs Tolaram's own corpus
- **NEW** Competitor Analysis dashboard page: add/remove tracked competitors, run scans, see per-channel topic clouds, hashtag use, reach metrics, and top-5 videos
- **NEW** endpoints: `GET/POST/DELETE /api/competitors`, `GET /api/competitors/analysis`, `POST /api/competitors/scan`

#### Campaign / hashtag search
- **NEW** `hashtag_search.py` module — searches a hashtag across YouTube (native YouTube Data API), Instagram, Facebook, X, TikTok, LinkedIn, Threads (via DuckDuckGo + Bing HTML search)
- Results grouped by platform with per-platform post count and (where available) YouTube reach
- Per-post previews: title, snippet, channel, view count, hashtag match indicator, direct URL to the source
- 30-minute cache per hashtag; recent searches list for quick re-open
- **NEW** endpoints: `POST /api/hashtag/search`, `GET /api/hashtag/<tag>`, `GET /api/hashtag/recent`

#### Dashboard
- Two new sidebar nav items under "Intelligence": Competitor Analysis, Campaign Search
- New CSS components: month picker, reach cards, competitor cards, platform tabs, post cards, keyword clouds
- Content gap keywords highlighted in hot-pink to draw the eye
- Reach column replaces "Views" label on the video grid

#### Dependencies
- Added `beautifulsoup4>=4.12.0` to requirements (used by hashtag search HTML parsing)

---

## [2.0.0] — 2026-03-25

### 🚀 Production Release

#### Infrastructure
- Added `Procfile` for Heroku / Railway / Render deployment
- Added `runtime.txt` pinning Python 3.11.9
- Added `.gitignore` (excludes `.env`, `data/`, `__pycache__`, logs, venvs)
- Added `.env.example` safe template with all variables documented
- Added `gunicorn>=21.2.0` to requirements (WSGI production server)
- Added `APScheduler>=3.10.4` to requirements (in-process scheduler)
- Added `tenacity>=8.2.3` to requirements (retry/backoff)

#### Backend — `server.py`
- Converted to app factory pattern (`create_app()`) — fully gunicorn-compatible
- **NEW** `POST /api/trigger-run` — fires agent run in background thread; protected by optional `X-Agent-Secret` header
- **NEW** `GET /api/agent-status` — returns whether the agent is currently running
- **NEW** `GET /api/briefs` — exposes AI content briefs
- **NEW** `GET /api/export/comments.csv` — downloadable CSV export of all comments
- **NEW** `GET /api/export/videos.csv` — downloadable CSV export of all videos
- **ENHANCED** `GET /api/comments` — now supports `?brand=` and `?category=` query filters
- **ENHANCED** `GET /api/videos` — now supports `?brand=` query filter
- **ENHANCED** `GET /api/health` — now returns uptime, data freshness, total counts, crisis_pending
- Added `404` and `500` JSON error handlers
- Thread lock prevents concurrent agent runs

#### Backend — `agent.py`
- Refactored as a clean importable module (`run_agent()` has zero side effects at import time)
- Added `_validate_config()` — raises clear `RuntimeError` on bad config instead of calling `sys.exit()`
- Added `_build_logger()` — prevents duplicate handlers when imported alongside Flask
- CLI mode (`python agent.py`) clearly documented as dev/debug only
- `run_scheduled()` documented as dev-only (production uses APScheduler or `/api/trigger-run`)

#### Backend — `gemini_classifier.py`
- Added `tenacity` `@retry` decorator with exponential backoff (2s → 30s, 4 attempts) on all Gemini calls
- Failed classifications now use `"error"` category (was `"irrelevant"`) for better observability
- Extracted `_call_gemini()` as a dedicated retried method
- Added Python type hints throughout

#### Backend — `data_store.py`
- **Thread-safe**: per-file `threading.Lock` registry prevents concurrent read-modify-write races
- **Atomic writes**: write to `.tmp` then `rename` — prevents partial-write file corruption
- `append_to_store()` now acquires lock for the full read-merge-write cycle

#### Dashboard — `index.html`
- **NEW** `▶ Execute Agent` button now calls `POST /api/trigger-run` directly — no terminal required
  - Button shows live state: `⏳ Starting...` → `⚙ Running...` → back to `▶ Execute Agent` when done
  - Auto-refreshes all data when agent completes
- **NEW** `⬇ Export CSV` button in topbar — downloads `comments.csv` instantly
- **NEW** Content Briefs page — AI strategy briefs from audience questions
- **NEW** Health polling every 30 seconds — sidebar dot turns pink on active crisis alerts
- **FIXED** Model badge corrected from "Gemini 1.5 Flash" → "Gemini 2.5 Flash" (×3 locations)
- Removed all `python agent.py` instructions from empty states — replaced with UI-native guidance

---

## [1.0.0] — Initial Release

- YouTube Data API v3 integration (channels, videos, comments)
- Gemini AI comment classification (6 categories + sentiment + priority)
- AI-drafted replies for questions and complaints
- Crisis keyword detection + AI crisis classification
- JSON-based local data persistence
- Flask dashboard server with 6 API endpoints
- Single-page dashboard with 6 pages and Chart.js visualizations
- Channel-level performance insights and content briefs
- Scheduled agent runner (every 2 hours)
