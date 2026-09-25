# Tolaram YouTube Intelligence Agent

> AI-powered YouTube comment monitoring, sentiment classification, crisis detection, competitor intelligence and cross-platform campaign tracking — built for Tolaram Group brands.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)](https://python.org)
[![Gemini 2.5 Flash](https://img.shields.io/badge/AI-Gemini%202.5%20Flash-purple)](https://aistudio.google.com)
[![Flask](https://img.shields.io/badge/server-Flask%20%2B%20Gunicorn-green)](https://flask.palletsprojects.com)

---

## What It Does

| Feature | Description |
|---|---|
| **Comment Classification** | Classifies every comment as question / complaint / compliment / crisis / spam via Gemini / Claude |
| **Crisis Detection** | Keyword + AI detection of health, safety, and legal issues — instant alerts |
| **AI Reply Drafting** | Drafts on-brand responses for questions and complaints |
| **Performance Insights** | Weekly narrative insights generated per channel |
| **Content Briefs** | AI strategy briefs built from top audience questions |
| **Monthly Filter** | Global month picker slices every page (overview, videos, comments, crisis, reach) by `YYYY-MM` |
| **YouTube Reach** | Per-video and per-brand reach (view_count) with totals, averages, and engagement rate — month-aware |
| **Competitor Analysis** | Brand-paired model — 20 default competitors across 7 Tolaram brands. Each scan produces an AI strategic brief (Claude 3.5 Sonnet) covering what they're doing, what's working, where Tolaram lags, and concrete counter-moves with urgency. Persistent notes + counter-moves log per competitor. |
| **Campaign Search** | Hashtag-based cross-platform tracker — YouTube, Instagram, Facebook, X, TikTok, LinkedIn, Threads. Uses YouTube Data API + DuckDuckGo/Bing web search |
| **Live Dashboard** | Single-page dashboard, 9 pages, charts, CSV export |
| **Agent Trigger** | Run the agent from the dashboard button — no terminal required |

---

## Architecture

```
YouTube Data API v3 ─┐
                     ├─→ YouTubeClient         (channels, videos, comments)
                     ├─→ CompetitorAnalysis    (rival channels → topic/gap analysis)
                     └─→ HashtagSearch         (native YouTube search path)
                              ↓
DDG/Bing web search ─→ HashtagSearch           (Instagram, FB, X, TikTok, etc.)
                              ↓
  ClaudeClassifier       ← classifies + drafts replies
                              ↓
  DataStore (JSON)       ← thread-safe atomic file persistence
                              ↓
  Flask API (server.py)  ← 26 REST endpoints
                              ↓
  Dashboard (index.html) ← single-page dashboard with 9 pages
```

---

## Quick Start (Development)

### 1. Clone & install
```powershell
pip install -r requirements.txt
```

### 2. Configure environment
```powershell
copy .env.example .env
# Edit .env — fill in your API keys
```

### 3. Start the server
```powershell
python server.py
```

### 4. Open the dashboard
```
http://localhost:5050
```

### 5. Run the agent
Click **▶ Execute Agent** in the dashboard sidebar — the agent runs in the background and the dashboard refreshes automatically when done.

> **Dev only:** You can also run `python agent.py` directly from the terminal.

---

## Production Deployment

### Railway / Render / Heroku

1. Push to a Git repository
2. Set environment variables from `.env.example` in your platform's dashboard
3. The `Procfile` handles startup automatically:
   ```
   web: gunicorn server:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
   ```

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `API_KEY` | ✅ | YouTube Data API v3 key |
| `GEMINI_API_KEY` | ✅ | Google Gemini API key |
| `CHANNEL_ID` | ✅ | Primary channel ID (starts with `UC`) |
| `CHANNEL_INDOMIE` | — | Indomie channel ID |
| `CHANNEL_DANO` | — | Dano channel ID |
| `CHANNEL_HYPO` | — | Hypo channel ID |
| `CHANNEL_POWER_OIL` | — | Power Oil channel ID |
| `VIDEOS_PER_CHANNEL` | — | Videos to fetch per channel (default: 5) |
| `COMMENTS_PER_VIDEO` | — | Comments to fetch per video (default: 50) |
| `CRISIS_KEYWORDS` | — | Comma-separated crisis keywords |
| `AGENT_API_SECRET` | — | Optional header secret for /api/trigger-run |
| `DASHBOARD_PORT` | — | Server port (default: 5050) |
| `LOG_LEVEL` | — | Logging level (default: INFO) |

---

## API Reference

### Core
| Endpoint | Method | Description |
|---|---|---|
| `GET /api/stats` | GET | Full dashboard stats payload |
| `GET /api/comments` | GET | All comments (supports `?brand=` `?category=` filters) |
| `GET /api/videos` | GET | All videos (supports `?brand=` filter) |
| `GET /api/channels` | GET | Channel stats |
| `GET /api/insights` | GET | AI performance insights |
| `GET /api/briefs` | GET | AI content briefs |
| `GET /api/crisis` | GET | Crisis-flagged comments only |
| `GET /api/run-history` | GET | Last 50 agent run summaries |
| `GET /api/health` | GET | Health check + data freshness |
| `POST /api/trigger-run` | POST | Trigger an agent run (background thread) |
| `GET /api/agent-status` | GET | Is the agent currently running? |
| `GET /api/export/comments.csv` | GET | Download all comments as CSV |
| `GET /api/export/videos.csv` | GET | Download all videos as CSV |

### Monthly filter / reach
| Endpoint | Method | Description |
|---|---|---|
| `GET /api/months` | GET | List of `YYYY-MM` months present in the corpus with per-month video/comment counts |
| `GET /api/reach` | GET | Per-brand / per-video / per-month YouTube reach. Supports `?month=YYYY-MM` and `?brand=` filters |

### Competitor analysis (brand-paired, with AI strategic briefs)
| Endpoint | Method | Description |
|---|---|---|
| `GET /api/competitors` | GET | Brand → [competitors] map (auto-seeded with 20 defaults across 7 Tolaram brands on first call) |
| `GET /api/competitors/flat` | GET | Flat list of every competitor with its parent Tolaram brand |
| `GET /api/competitors/brands` | GET | List of Tolaram brands tracked |
| `POST /api/competitors` | POST | Pair a new competitor under a Tolaram brand. Body: `{brand, reference, label?, priority?, rationale?}` |
| `DELETE /api/competitors?brand=&reference=` | DELETE | Remove a competitor from a brand |
| `POST /api/competitors/reset` | POST | Reset competitor pairings to seeded defaults |
| `GET /api/competitors/analysis` | GET | Last cached competitor scan (per-brand) |
| `POST /api/competitors/scan` | POST | Run a fresh scan. Body: `{brand?, max_videos_per_channel?, include_ai?}`. Returns AI strategic brief per competitor when `ANTHROPIC_API_KEY` is set |
| `GET /api/competitors/notes?reference=` | GET | List notes (optionally filtered by competitor) |
| `POST /api/competitors/notes` | POST | Add a note. Body: `{reference, brand, body, author?}` |
| `DELETE /api/competitors/notes/<note_id>` | DELETE | Delete a note |
| `GET /api/competitors/moves?reference=&status=` | GET | List counter-moves |
| `POST /api/competitors/moves` | POST | Add a counter-move. Body: `{reference, brand, move, rationale?, urgency?}` |
| `PATCH /api/competitors/moves/<move_id>` | PATCH | Update status: planned → in_progress → shipped → dropped |
| `DELETE /api/competitors/moves/<move_id>` | DELETE | Delete a counter-move |

### Campaign / hashtag search
| Endpoint | Method | Description |
|---|---|---|
| `POST /api/hashtag/search` | POST | Run a cross-platform hashtag search. Body: `{hashtag, per_platform?, refresh?}` |
| `GET /api/hashtag/<tag-or-slug>` | GET | Load a cached hashtag result |
| `GET /api/hashtag/recent` | GET | Recent hashtag searches for sidebar history |

**Optional auth:** Set `AGENT_API_SECRET` in `.env` and pass `X-Agent-Secret: <value>` header on `POST /api/trigger-run`, `POST /api/competitors/scan`, and `POST /api/competitors/reset`.

---

## Dashboard Pages

| Page | What it shows |
|---|---|
| Command Center | KPI cards + all key charts + model analytics. Month-aware when filter is active |
| Comment Stream | Every classified comment with AI-drafted reply, filterable by category and month |
| Video Intelligence | **Reach summary cards**, per-brand reach table, per-video reach table, engagement chart, full video grid — all month-aware |
| AI Insights | Weekly narrative per channel |
| Content Briefs | AI-generated content strategy from audience questions |
| Crisis Monitor | Urgent comments requiring immediate escalation (month-aware) |
| Model Performance | Classification accuracy, confidence distribution, processing stats |
| **Competitor Analysis** | Brand-paired competitor intelligence: pick a Tolaram brand, see its tracked competitors (auto-seeded with 20 defaults), run scans that produce per-competitor AI strategic briefs (what they're doing, what's working, where you lag, counter-moves), persistent notes per competitor, and a counter-moves log with status tracking |
| **Campaign Search** | Search any hashtag across YouTube, Instagram, Facebook, X, TikTok, LinkedIn, Threads — see which platforms carry the campaign and drill into individual posts |

---

## Data Files

All data lives in `data/` as JSON (auto-created on first run):

| File | Contents |
|---|---|
| `all_comments.json` | Every classified comment |
| `all_videos.json` | All video records |
| `channel_stats.json` | Channel-level stats |
| `performance_insights.json` | AI narrative per channel |
| `content_briefs.json` | AI content briefs per channel |
| `latest_run.json` | Most recent agent run summary |
| `run_history.json` | Last 50 run summaries |

---

## How to Find a YouTube Channel ID
1. Go to the channel on YouTube
2. View page source (`Ctrl+U`)
3. Search for `externalId` — the value next to it is the channel ID (starts with `UC`)

---

## Troubleshooting

| Error | Fix |
|---|---|
| `GEMINI_API_KEY not set` | Check `.env` file exists and key is filled in |
| `No valid channel IDs found` | Make sure channel ID starts with `UC` and has no `xxxx` placeholder |
| Dashboard shows empty state | Click **Execute Agent** to run first data collection |
| Gemini quota errors | Add `time.sleep()` or reduce `COMMENTS_PER_VIDEO` |
| YouTube quota exceeded | Daily limit is 10,000 units — reduce `COMMENTS_PER_VIDEO` or `VIDEOS_PER_CHANNEL` |
