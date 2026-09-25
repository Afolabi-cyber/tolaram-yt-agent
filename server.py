"""
server.py
---------
Tolaram YouTube Agent — Production Flask API Server
Serves aggregated stats to the dashboard and exposes agent control endpoints.

Production: launched via gunicorn (see Procfile)
Development: python server.py
"""

import io
import os
import csv
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, send_from_directory, request, make_response
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

from data_store import compute_dashboard_stats, load_json
from competitor_analysis import (
    list_brand_competitors, list_brands, list_all_competitors,
    add_competitor, remove_competitor, reset_to_defaults,
    pin_competitor_channel,
    load_cached_analysis, run_brand_scan,
    list_notes, add_note, delete_note,
    list_moves, add_move, update_move_status, delete_move,
)
from hashtag_search import (
    run_hashtag_search, get_cached_hashtag, list_recent_searches,
)
from boosted_tracker import (
    get_all_flags, set_flag, clear_flag, transparency_center_url,
)
from crisis_notify import (
    get_all_reviews, get_reviews_for, mark_reviewed, unmark_reviewed, configured_reviewers,
    send_test_email,
)
from youtube_analytics import get_channel_report

# ── Config ─────────────────────────────────────────────────────────────────────
PORT          = int(os.getenv("DASHBOARD_PORT", "5050"))
AGENT_SECRET  = os.getenv("AGENT_API_SECRET", "")          # optional auth guard
VERSION       = "2.0.0"
DASHBOARD_DIR = Path(__file__).parent / "dashboard"
_SERVER_START = datetime.now(timezone.utc).isoformat()
_agent_lock   = threading.Lock()                            # prevent concurrent runs

# ── App factory ────────────────────────────────────────────────────────────────

def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(DASHBOARD_DIR))
    CORS(app)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logger = logging.getLogger(__name__)

    # ── Auth helper ────────────────────────────────────────────────────────────
    def _check_secret():
        """If AGENT_API_SECRET is set, require it in X-Agent-Secret header."""
        if AGENT_SECRET:
            header = request.headers.get("X-Agent-Secret", "")
            if header != AGENT_SECRET:
                return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return None

    # ── API Routes ─────────────────────────────────────────────────────────────

    @app.route("/api/stats")
    def api_stats():
        """Main dashboard stats endpoint."""
        try:
            stats = compute_dashboard_stats()
            return jsonify({"ok": True, "data": stats})
        except Exception as e:
            logger.exception(f"Stats error: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/comments")
    def api_comments():
        """All classified comments. Supports ?brand=Indomie and ?category=crisis filters."""
        comments = load_json("all_comments.json", default=[])
        brand    = request.args.get("brand")
        category = request.args.get("category")
        if brand:
            comments = [c for c in comments if c.get("brand", "").lower() == brand.lower()]
        if category:
            comments = [c for c in comments if c.get("category", "").lower() == category.lower()]
        return jsonify({"ok": True, "data": comments, "total": len(comments)})

    @app.route("/api/videos")
    def api_videos():
        """All video records. Supports ?brand= filter."""
        videos = load_json("all_videos.json", default=[])
        brand  = request.args.get("brand")
        if brand:
            videos = [v for v in videos if v.get("brand", "").lower() == brand.lower()]
        return jsonify({"ok": True, "data": videos, "total": len(videos)})

    @app.route("/api/channels")
    def api_channels():
        """Channel statistics."""
        channels = load_json("channel_stats.json", default=[])
        return jsonify({"ok": True, "data": channels})

    @app.route("/api/insights")
    def api_insights():
        """AI-generated performance insights per channel."""
        insights = load_json("performance_insights.json", default={})
        return jsonify({"ok": True, "data": insights})

    @app.route("/api/briefs")
    def api_briefs():
        """AI-generated content briefs per channel."""
        briefs = load_json("content_briefs.json", default={})
        return jsonify({"ok": True, "data": briefs})

    @app.route("/api/crisis")
    def api_crisis():
        """Crisis-flagged comments only."""
        comments = load_json("all_comments.json", default=[])
        crises   = [c for c in comments if c.get("crisis_flag")]
        crises_sorted = sorted(crises, key=lambda x: x.get("published_at", ""), reverse=True)
        return jsonify({"ok": True, "data": crises_sorted, "total": len(crises_sorted)})

    @app.route("/api/run-history")
    def api_run_history():
        """Agent run history (last 50 runs)."""
        history = load_json("run_history.json", default=[])
        return jsonify({"ok": True, "data": history})

    @app.route("/api/health")
    def api_health():
        """Health check with data freshness and uptime info."""
        latest   = load_json("latest_run.json", default={})
        comments = load_json("all_comments.json", default=[])
        videos   = load_json("all_videos.json", default=[])
        return jsonify({
            "ok":              True,
            "status":          "running",
            "version":         VERSION,
            "server_started":  _SERVER_START,
            "last_run":        latest.get("started_at", "Never"),
            "last_run_ok":     len(latest.get("errors", [])) == 0,
            "total_comments":  len(comments),
            "total_videos":    len(videos),
            "crisis_pending":  sum(1 for c in comments if c.get("crisis_flag")),
        })

    @app.route("/api/trigger-run", methods=["POST"])
    def api_trigger_run():
        """
        Trigger an agent run in a background thread.
        Protected by X-Agent-Secret header if AGENT_API_SECRET is set in .env.
        """
        auth_error = _check_secret()
        if auth_error:
            return auth_error

        if _agent_lock.locked():
            return jsonify({"ok": False, "error": "Agent is already running"}), 409

        def _run():
            with _agent_lock:
                try:
                    from agent import run_agent
                    run_agent()
                except Exception as e:
                    logger.error(f"Background agent run failed: {e}")

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return jsonify({"ok": True, "message": "Agent run started in background", "timestamp": datetime.now(timezone.utc).isoformat()})

    @app.route("/api/agent-status")
    def api_agent_status():
        """Returns whether the agent is currently running."""
        return jsonify({"ok": True, "running": _agent_lock.locked()})

    @app.route("/api/export/comments.csv")
    def api_export_comments_csv():
        """Export all classified comments as a downloadable CSV file."""
        comments = load_json("all_comments.json", default=[])
        brand    = request.args.get("brand")
        if brand:
            comments = [c for c in comments if c.get("brand", "").lower() == brand.lower()]

        if not comments:
            return jsonify({"ok": False, "error": "No data to export"}), 404

        # Build CSV in memory
        output = io.StringIO()
        fieldnames = [
            "comment_id", "video_id", "brand", "channel_label", "video_title",
            "author", "text", "published_at",
            "category", "sentiment", "priority",
            "crisis_flag", "crisis_reason",
            "classification_confidence", "ai_summary", "suggested_reply",
            "like_count", "reply_count",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(comments)

        response = make_response(output.getvalue())
        response.headers["Content-Type"]        = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="tolaram_comments_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}.csv"'
        )
        return response

    @app.route("/api/export/videos.csv")
    def api_export_videos_csv():
        """Export all video records as a downloadable CSV file."""
        videos = load_json("all_videos.json", default=[])
        if not videos:
            return jsonify({"ok": False, "error": "No data to export"}), 404

        output = io.StringIO()
        fieldnames = [
            "video_id", "channel_id", "brand", "channel_label",
            "title", "published_at", "duration",
            "view_count", "like_count", "comment_count", "engagement_rate",
            "thumbnail", "fetched_at",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(videos)

        response = make_response(output.getvalue())
        response.headers["Content-Type"]        = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="tolaram_videos_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}.csv"'
        )
        return response

    # ── Monthly filter helpers ─────────────────────────────────────────────────

    @app.route("/api/months")
    def api_months():
        """
        Return the list of YYYY-MM months present in the video corpus,
        plus a count of videos per month. Feeds the dashboard's month picker.
        """
        videos   = load_json("all_videos.json", default=[])
        comments = load_json("all_comments.json", default=[])

        from collections import Counter
        vid_counter = Counter()
        com_counter = Counter()
        for v in videos:
            ym = (v.get("published_at") or "")[:7]
            if len(ym) == 7:
                vid_counter[ym] += 1
        for c in comments:
            ym = (c.get("published_at") or "")[:7]
            if len(ym) == 7:
                com_counter[ym] += 1

        all_months = sorted(set(vid_counter.keys()) | set(com_counter.keys()), reverse=True)
        return jsonify({
            "ok": True,
            "data": [
                {
                    "month":    m,
                    "videos":   vid_counter.get(m, 0),
                    "comments": com_counter.get(m, 0),
                }
                for m in all_months
            ],
        })

    @app.route("/api/reach")
    def api_reach():
        """
        YouTube 'reach' estimated via view_count.
        Supports ?month=YYYY-MM and ?brand= filters.

        Returns:
          - per_video:   reach per video (sorted)
          - per_brand:   aggregated reach per brand
          - per_month:   aggregated reach per month (across the filter set)
          - totals:      grand totals
        """
        videos = load_json("all_videos.json", default=[])

        month = request.args.get("month")
        brand = request.args.get("brand")

        if month:
            videos = [v for v in videos if (v.get("published_at") or "").startswith(month)]
        if brand:
            videos = [v for v in videos if (v.get("brand") or "").lower() == brand.lower()]

        # per-brand reach
        brand_totals: dict = {}
        for v in videos:
            b = v.get("brand") or "Unknown"
            brand_totals.setdefault(b, {"reach": 0, "videos": 0, "likes": 0, "comments": 0})
            brand_totals[b]["reach"]    += int(v.get("view_count") or 0)
            brand_totals[b]["videos"]   += 1
            brand_totals[b]["likes"]    += int(v.get("like_count") or 0)
            brand_totals[b]["comments"] += int(v.get("comment_count") or 0)

        # per-month reach
        month_totals: dict = {}
        for v in videos:
            ym = (v.get("published_at") or "")[:7]
            if len(ym) != 7:
                continue
            month_totals.setdefault(ym, {"reach": 0, "videos": 0})
            month_totals[ym]["reach"]  += int(v.get("view_count") or 0)
            month_totals[ym]["videos"] += 1

        # per-video rows
        per_video = [
            {
                "video_id":       v.get("video_id"),
                "title":          v.get("title"),
                "brand":          v.get("brand"),
                "channel_label":  v.get("channel_label"),
                "published_at":   v.get("published_at"),
                "month":          (v.get("published_at") or "")[:7],
                "thumbnail":      v.get("thumbnail"),
                "reach":          int(v.get("view_count") or 0),
                "likes":          int(v.get("like_count") or 0),
                "comments":       int(v.get("comment_count") or 0),
                "video_url":      f"https://www.youtube.com/watch?v={v.get('video_id')}" if v.get("video_id") else "",
            }
            for v in videos
        ]
        per_video.sort(key=lambda r: r["reach"], reverse=True)

        totals = {
            "reach":    sum(r["reach"] for r in per_video),
            "videos":   len(per_video),
            "likes":    sum(r["likes"] for r in per_video),
            "comments": sum(r["comments"] for r in per_video),
        }

        return jsonify({
            "ok": True,
            "data": {
                "filters":    {"month": month, "brand": brand},
                "totals":     totals,
                "per_brand":  [{"brand": b, **agg} for b, agg in sorted(
                    brand_totals.items(), key=lambda x: x[1]["reach"], reverse=True
                )],
                "per_month":  [{"month": m, **agg} for m, agg in sorted(
                    month_totals.items(), reverse=True
                )],
                "per_video":  per_video[:200],
            },
        })

    # ── Competitor analysis ───────────────────────────────────────────────────

    @app.route("/api/competitors", methods=["GET"])
    def api_competitors_list():
        """Full brand → [competitors] map."""
        return jsonify({"ok": True, "data": list_brand_competitors()})

    @app.route("/api/competitors/flat", methods=["GET"])
    def api_competitors_flat():
        """Flat list of every tracked competitor with parent brand."""
        return jsonify({"ok": True, "data": list_all_competitors()})

    @app.route("/api/competitors/brands", methods=["GET"])
    def api_competitors_brands():
        """List of Tolaram brands that have at least one tracked competitor."""
        return jsonify({"ok": True, "data": list_brands()})

    @app.route("/api/competitors", methods=["POST"])
    def api_competitors_add():
        """
        Add a competitor under a Tolaram brand. Body JSON:
          {brand, reference, label?, priority?, rationale?}
        """
        body = request.get_json(silent=True) or {}
        brand = (body.get("brand") or "").strip()
        ref   = (body.get("reference") or "").strip()
        if not brand or not ref:
            return jsonify({"ok": False, "error": "brand and reference required"}), 400
        return jsonify(add_competitor(
            brand     = brand,
            reference = ref,
            label     = body.get("label"),
            priority  = body.get("priority", "secondary"),
            rationale = body.get("rationale"),
        ))

    @app.route("/api/competitors", methods=["DELETE"])
    def api_competitors_remove():
        brand = (request.args.get("brand") or "").strip()
        ref   = (request.args.get("reference") or "").strip()
        if not brand or not ref:
            return jsonify({"ok": False, "error": "brand and reference required"}), 400
        return jsonify(remove_competitor(brand, ref))

    @app.route("/api/competitors/pin-channel", methods=["POST"])
    def api_competitors_pin_channel():
        """
        Manually confirm the correct channel for a competitor whose
        auto-resolved match was wrong or unverified. Body JSON:
          {brand, reference, channel_id}
        """
        body = request.get_json(silent=True) or {}
        brand      = (body.get("brand") or "").strip()
        ref        = (body.get("reference") or "").strip()
        channel_id = (body.get("channel_id") or "").strip()
        if not brand or not ref or not channel_id:
            return jsonify({"ok": False, "error": "brand, reference and channel_id required"}), 400
        return jsonify(pin_competitor_channel(brand, ref, channel_id))

    @app.route("/api/competitors/reset", methods=["POST"])
    def api_competitors_reset():
        """Reset competitor pairings to the seeded defaults (the 20 researched)."""
        auth_error = _check_secret()
        if auth_error:
            return auth_error
        return jsonify(reset_to_defaults())

    @app.route("/api/competitors/analysis", methods=["GET"])
    def api_competitors_analysis():
        """Returns the last cached competitor analysis payload."""
        return jsonify({"ok": True, "data": load_cached_analysis()})

    @app.route("/api/competitors/scan", methods=["POST"])
    def api_competitors_scan():
        """
        Trigger a fresh scan. Body JSON:
          {brand?: 'Indomie' (optional - scan all if omitted),
           max_videos_per_channel?: int,
           include_ai?: bool}
        """
        auth_error = _check_secret()
        if auth_error:
            return auth_error
        body = request.get_json(silent=True) or {}
        max_v = int(body.get("max_videos_per_channel", 15))
        max_v = min(max(max_v, 3), 30)
        try:
            result = run_brand_scan(
                brand                  = body.get("brand"),
                max_videos_per_channel = max_v,
                include_ai             = bool(body.get("include_ai", True)),
            )
            return jsonify(result)
        except Exception as e:
            logger.exception(f"Competitor scan failed: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500

    # ── Competitor notes (user observations) ──────────────────────────────────

    @app.route("/api/competitors/notes", methods=["GET"])
    def api_notes_list():
        ref = request.args.get("reference")
        return jsonify({"ok": True, "data": list_notes(ref)})

    @app.route("/api/competitors/notes", methods=["POST"])
    def api_notes_add():
        body = request.get_json(silent=True) or {}
        return jsonify(add_note(
            competitor_ref = (body.get("reference") or "").strip(),
            brand          = (body.get("brand") or "").strip(),
            body           = (body.get("body") or "").strip(),
            author         = (body.get("author") or "User").strip(),
        ))

    @app.route("/api/competitors/notes/<note_id>", methods=["DELETE"])
    def api_notes_delete(note_id):
        return jsonify(delete_note(note_id))

    # ── Counter-moves (planned actions) ───────────────────────────────────────

    @app.route("/api/competitors/moves", methods=["GET"])
    def api_moves_list():
        ref    = request.args.get("reference")
        status = request.args.get("status")
        return jsonify({"ok": True, "data": list_moves(ref, status)})

    @app.route("/api/competitors/moves", methods=["POST"])
    def api_moves_add():
        body = request.get_json(silent=True) or {}
        return jsonify(add_move(
            competitor_ref = (body.get("reference") or "").strip(),
            brand          = (body.get("brand") or "").strip(),
            move           = (body.get("move") or "").strip(),
            rationale      = (body.get("rationale") or "").strip(),
            urgency        = (body.get("urgency") or "medium"),
            due_date       = body.get("due_date"),
        ))

    @app.route("/api/competitors/moves/<move_id>", methods=["PATCH"])
    def api_moves_update(move_id):
        body = request.get_json(silent=True) or {}
        return jsonify(update_move_status(move_id, (body.get("status") or "").strip()))

    @app.route("/api/competitors/moves/<move_id>", methods=["DELETE"])
    def api_moves_delete(move_id):
        return jsonify(delete_move(move_id))

    # ── Hashtag campaign search ───────────────────────────────────────────────

    @app.route("/api/hashtag/search", methods=["POST"])
    def api_hashtag_search():
        """
        Search a hashtag across social platforms. Body JSON:
          { "hashtag": "#ShowSomeLovetoMum", "per_platform": 15, "refresh": false }
        """
        body = request.get_json(silent=True) or {}
        tag = (body.get("hashtag") or "").strip()
        if not tag:
            return jsonify({"ok": False, "error": "hashtag required"}), 400
        try:
            result = run_hashtag_search(
                hashtag=tag,
                per_platform=int(body.get("per_platform", 15)),
                force_refresh=bool(body.get("refresh", False)),
            )
            return jsonify(result)
        except Exception as e:
            logger.exception(f"Hashtag search failed: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/hashtag/<slug_or_tag>")
    def api_hashtag_get(slug_or_tag):
        """Read a cached hashtag result by hashtag string or slug."""
        data = get_cached_hashtag(slug_or_tag)
        if not data:
            return jsonify({"ok": False, "error": "No cached result for that hashtag"}), 404
        return jsonify({"ok": True, "data": data})

    @app.route("/api/hashtag/recent")
    def api_hashtag_recent():
        """Recent hashtag searches (for the sidebar history list)."""
        return jsonify({"ok": True, "data": list_recent_searches()})

    # ── Crisis review tracking ──────────────────────────────────────────────

    @app.route("/api/crisis/reviews")
    def api_crisis_reviews():
        """
        All crisis-comment review acknowledgments, or just one comment's:
        ?comment_id=<id> to filter.
        """
        comment_id = request.args.get("comment_id")
        if comment_id:
            return jsonify({"ok": True, "data": get_reviews_for(comment_id)})
        return jsonify({"ok": True, "data": get_all_reviews()})

    @app.route("/api/crisis/reviewers")
    def api_crisis_reviewers():
        """The configured reviewer list (from CRISIS_EMAIL_RECIPIENTS), for the UI's dropdown."""
        return jsonify({"ok": True, "data": configured_reviewers()})

    @app.route("/api/crisis/test-email", methods=["POST"])
    def api_crisis_test_email():
        """
        Send a real test email through the exact same code path as a real
        crisis alert, so you can verify SMTP works right after adding
        credentials, without waiting for an actual crisis to be detected.
        """
        return jsonify(send_test_email())

    # ── YouTube Analytics (demographics, geography, watch stats) ─────────────

    @app.route("/api/analytics/<brand>")
    def api_analytics_for_brand(brand):
        """
        Full audience report for one brand's own channel: overview metrics,
        demographics, geography, device type, traffic sources.
        ?days=90 (default) controls the lookback window.
        ?force=1 bypasses the cache and refetches live.
        Only works for channels Tolaram owns — see youtube_analytics.py.
        """
        days = request.args.get("days", "90")
        try:
            days = int(days)
        except ValueError:
            days = 90
        force = request.args.get("force") == "1"
        return jsonify(get_channel_report(brand, days=days, force=force))

    @app.route("/api/crisis/reviews", methods=["POST"])
    def api_crisis_reviews_mark():
        """Mark a crisis comment as reviewed. Body: {comment_id, reviewer, note?}"""
        body = request.get_json(silent=True) or {}
        comment_id = (body.get("comment_id") or "").strip()
        reviewer   = (body.get("reviewer") or "").strip()
        note       = body.get("note", "")
        if not comment_id or not reviewer:
            return jsonify({"ok": False, "error": "comment_id and reviewer required"}), 400
        return jsonify(mark_reviewed(comment_id, reviewer, note))

    @app.route("/api/crisis/reviews", methods=["DELETE"])
    def api_crisis_reviews_unmark():
        """Remove a reviewer's acknowledgment. Body: {comment_id, reviewer}"""
        body = request.get_json(silent=True) or {}
        comment_id = (body.get("comment_id") or "").strip()
        reviewer   = (body.get("reviewer") or "").strip()
        if not comment_id or not reviewer:
            return jsonify({"ok": False, "error": "comment_id and reviewer required"}), 400
        return jsonify(unmark_reviewed(comment_id, reviewer))

    # ── Boosted / organic tracking ────────────────────────────────────────────

    @app.route("/api/boosted", methods=["GET"])
    def api_boosted_list():
        """All stored boosted/organic flags, keyed by video_id."""
        return jsonify({"ok": True, "data": get_all_flags()})

    @app.route("/api/boosted", methods=["POST"])
    def api_boosted_set():
        """
        Mark a video boosted or organic after checking it (e.g. against
        Google's Ads Transparency Center). Body JSON:
          {video_id, boosted: bool, note?, source?: 'manual'|'transparency_center'}
        """
        body = request.get_json(silent=True) or {}
        video_id = (body.get("video_id") or "").strip()
        if not video_id:
            return jsonify({"ok": False, "error": "video_id required"}), 400
        return jsonify(set_flag(
            video_id = video_id,
            boosted  = bool(body.get("boosted", False)),
            note     = body.get("note", ""),
            source   = body.get("source", "manual"),
        ))

    @app.route("/api/boosted/<video_id>", methods=["DELETE"])
    def api_boosted_clear(video_id):
        """Reset a video back to 'unchecked'."""
        return jsonify(clear_flag(video_id))

    @app.route("/api/boosted/transparency-url")
    def api_boosted_transparency_url():
        """
        Returns the direct Google Ads Transparency Center search URL for a
        brand/advertiser name, so the dashboard can offer a one-click check.
        ?advertiser=Indomie&region=NG
        """
        advertiser = request.args.get("advertiser", "")
        region     = request.args.get("region", "NG")
        return jsonify({"ok": True, "data": {"url": transparency_center_url(advertiser, region)}})

    # ── Breakdown view: platform × time × boosted-vs-organic ─────────────────

    @app.route("/api/breakdown")
    def api_breakdown():
        """
        Combined breakdown for the dashboard's Breakdown page.
        Supports ?brand=, ?month=YYYY-MM, ?boosted=all|organic|boosted|unchecked

        NOTE on scope, please read before wiring new UI to this:
          - "youtube" numbers (reach, likes, comments, per-video, per-month,
            organic/boosted split) are real, from YouTube Data API v3 +
            the boosted_flags.json overlay.
          - "cross_platform_campaigns" is best-effort: it comes from cached
            hashtag searches (data/hashtag_*.json), which only capture POST
            COUNTS per platform via public web search snippets — not view/
            reach numbers (Instagram, Facebook, X, TikTok don't expose that
            without their own authenticated APIs). Don't chart these two
            sections on the same axis; they're different units.
          - Demography and ad-placement data are not included here at all —
            no data source in this app can produce them (see README/chat).
        """
        videos = load_json("all_videos.json", default=[])
        flags  = get_all_flags()

        brand       = request.args.get("brand")
        month       = request.args.get("month")
        boosted_arg = (request.args.get("boosted") or "all").lower()

        if brand:
            videos = [v for v in videos if (v.get("brand") or "").lower() == brand.lower()]
        if month:
            videos = [v for v in videos if (v.get("published_at") or "").startswith(month)]

        def _status(vid: str) -> str:
            f = flags.get(vid)
            if not f:
                return "unchecked"
            return "boosted" if f.get("boosted") else "organic"

        for v in videos:
            v["_boosted_status"] = _status(v.get("video_id"))

        if boosted_arg in ("organic", "boosted", "unchecked"):
            videos = [v for v in videos if v["_boosted_status"] == boosted_arg]

        # organic vs boosted vs unchecked split
        split: dict = {"organic": {"videos": 0, "reach": 0, "likes": 0},
                        "boosted": {"videos": 0, "reach": 0, "likes": 0},
                        "unchecked": {"videos": 0, "reach": 0, "likes": 0}}
        for v in videos:
            s = split[v["_boosted_status"]]
            s["videos"] += 1
            s["reach"]  += int(v.get("view_count") or 0)
            s["likes"]  += int(v.get("like_count") or 0)

        # per-month (within current filters)
        month_totals: dict = {}
        for v in videos:
            ym = (v.get("published_at") or "")[:7]
            if len(ym) != 7:
                continue
            month_totals.setdefault(ym, {"reach": 0, "videos": 0, "boosted": 0, "organic": 0})
            month_totals[ym]["reach"]  += int(v.get("view_count") or 0)
            month_totals[ym]["videos"] += 1
            if v["_boosted_status"] == "boosted":
                month_totals[ym]["boosted"] += 1
            elif v["_boosted_status"] == "organic":
                month_totals[ym]["organic"] += 1

        per_video = sorted([
            {
                "video_id":      v.get("video_id"),
                "title":         v.get("title"),
                "brand":         v.get("brand"),
                "channel_label": v.get("channel_label"),
                "published_at":  v.get("published_at"),
                "month":         (v.get("published_at") or "")[:7],
                "reach":         int(v.get("view_count") or 0),
                "likes":         int(v.get("like_count") or 0),
                "status":        v["_boosted_status"],
                "video_url":     f"https://www.youtube.com/watch?v={v.get('video_id')}" if v.get("video_id") else "",
            }
            for v in videos
        ], key=lambda r: r["reach"], reverse=True)

        # cross-platform campaign footprint — best-effort, post counts only
        hashtag_index = load_json("hashtag_index.json", default=[])
        platform_posts: dict = {}
        for entry in (hashtag_index if isinstance(hashtag_index, list) else []):
            slug = entry.get("slug")
            if not slug:
                continue
            cached = load_json(f"hashtag_{slug}.json", default=None)
            if not cached:
                continue
            for plat, posts in (cached.get("by_platform") or {}).items():
                platform_posts.setdefault(plat, 0)
                platform_posts[plat] += len(posts) if isinstance(posts, list) else 0

        return jsonify({
            "ok": True,
            "data": {
                "filters": {"brand": brand, "month": month, "boosted": boosted_arg},
                "youtube": {
                    "per_boosted": split,
                    "per_month":   [{"month": m, **agg} for m, agg in sorted(month_totals.items(), reverse=True)],
                    "per_video":   per_video[:300],
                    "totals": {
                        "videos": len(videos),
                        "reach":  sum(v.get("view_count") or 0 for v in videos),
                    },
                },
                "cross_platform_campaigns": {
                    "note": ("Post counts from cached hashtag/campaign searches — "
                             "not view/reach numbers, and not comparable to the YouTube figures above."),
                    "per_platform": [{"platform": p, "post_count": c} for p, c in
                                      sorted(platform_posts.items(), key=lambda x: x[1], reverse=True)],
                },
            },
        })

    # ── Static File Serving ────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return send_from_directory(str(DASHBOARD_DIR), "index.html")

    @app.route("/<path:path>")
    def static_files(path):
        return send_from_directory(str(DASHBOARD_DIR), path)

    # ── 404 / 500 handlers ────────────────────────────────────────────────────

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"ok": False, "error": "Not found"}), 404

    @app.errorhandler(500)
    def internal_error(e):
        return jsonify({"ok": False, "error": "Internal server error"}), 500

    return app


# ── WSGI entry point (used by gunicorn: server:app) ───────────────────────────
app = create_app()


# ── Dev entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"  🎯  Tolaram YouTube Agent — Dashboard Server v{VERSION}")
    print(f"  📊  Dashboard : http://localhost:{PORT}")
    print(f"  🔌  API       : http://localhost:{PORT}/api/stats")
    print(f"  🚀  Trigger   : POST http://localhost:{PORT}/api/trigger-run")
    print(f"  📥  Export    : http://localhost:{PORT}/api/export/comments.csv")
    print(f"{'='*60}\n")

    data_dir = Path(os.getenv("APP_DATA_DIR") or (Path(__file__).parent / "data"))
    if not any(data_dir.glob("*.json")):
        print("⚠️  No data found yet. Use the dashboard 'Run Agent' button or POST /api/trigger-run")
        print("   The dashboard will show an empty state until the first run completes.\n")

    app.run(host="0.0.0.0", port=PORT, debug=False)