"""
crisis_notify.py
-----------------
Two jobs:
  1. Email an alert the moment a crisis comment is detected during an
     agent run (SMTP — works with Gmail, Google Workspace, or any provider
     that gives you a host/port/user/password).
  2. Track, per crisis comment, who among the reviewers has acknowledged
     it — persisted so it survives restarts, same pattern as boosted_tracker.py.

Recipients are two-tier:
  - CRISIS_EMAIL_RECIPIENTS in .env — the base list, notified for every
    crisis regardless of brand (e.g. you + the Marketing Head).
  - BRAND_MANAGER_<BRAND> in .env — one extra recipient added on top of
    the base list, only for crises on that specific brand. Brand names
    are uppercased with spaces turned into underscores, e.g. Colgate's
    manager goes in BRAND_MANAGER_COLGATE, Power Oil's in
    BRAND_MANAGER_POWER_OIL. Unset for a brand means no extra recipient
    for it, not an error — the base list still gets notified.

Nothing here is hardcoded to a specific person long-term: adding or
changing any recipient is a one-line .env edit, not a code change. The
base list defaults to a single test address if CRISIS_EMAIL_RECIPIENTS
isn't set.
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

from data_store import save_json, load_json

logger = logging.getLogger(__name__)

REVIEWS_FILE = "crisis_reviews.json"  # {comment_id: [{"reviewer": str, "reviewed_at": iso}, ...]}

_DEFAULT_TEST_RECIPIENT = "afolabifaruq122@gmail.com"


def _base_recipients() -> List[str]:
    raw = os.getenv("CRISIS_EMAIL_RECIPIENTS", "").strip()
    if not raw:
        return [_DEFAULT_TEST_RECIPIENT]
    return [r.strip() for r in raw.split(",") if r.strip()]


def _brand_manager_env_key(brand: str) -> str:
    return "BRAND_MANAGER_" + (brand or "").strip().upper().replace(" ", "_")


def brand_manager(brand: str) -> Optional[str]:
    """The configured manager email for one brand, or None if unset."""
    if not brand:
        return None
    val = os.getenv(_brand_manager_env_key(brand), "").strip()
    return val or None


def recipients_for(brand: str = "") -> List[str]:
    """Base list plus that brand's manager, if one is configured. Deduped, order preserved."""
    recipients = list(_base_recipients())
    mgr = brand_manager(brand)
    if mgr and mgr.lower() not in (r.lower() for r in recipients):
        recipients.append(mgr)
    return recipients


def _smtp_configured() -> bool:
    return bool(os.getenv("CRISIS_SMTP_HOST") and os.getenv("CRISIS_SMTP_USER")
                and os.getenv("CRISIS_SMTP_PASSWORD"))


def send_test_email() -> Dict:
    """
    Sends a real email through the exact same send_crisis_email() path, with
    an obviously-fake comment, so a successful test genuinely proves SMTP is
    configured correctly — not a separate, simpler code path that could pass
    while the real one still fails.
    """
    fake_comment = {
        "author":       "Test Trigger",
        "text":         "This is a test crisis alert from Tolaram Intelligence. "
                        "If you're reading this in your inbox, SMTP is configured correctly.",
        "comment_id":   "test-" + datetime.now(timezone.utc).isoformat(),
        "crisis_reason": "Manual test — not a real crisis",
    }
    return send_crisis_email([fake_comment], brand="Test", video_title="Test Video")


def send_crisis_email(crisis_comments: List[Dict], brand: str, video_title: str) -> Dict:
    """
    Send one email covering all crisis comments found in a single batch
    (one video's worth, from one agent-run pass). Never raises — a failed
    send should not take down the rest of the agent run.
    """
    if not crisis_comments:
        return {"ok": True, "sent": False, "reason": "No crisis comments to send"}

    if not _smtp_configured():
        logger.warning(
            "Crisis detected but email not sent — CRISIS_SMTP_HOST/USER/PASSWORD "
            "not set in .env. Set those to enable crisis email alerts."
        )
        return {"ok": True, "sent": False, "reason": "SMTP not configured"}

    host      = os.getenv("CRISIS_SMTP_HOST")
    port      = int(os.getenv("CRISIS_SMTP_PORT", "587"))
    user      = os.getenv("CRISIS_SMTP_USER")
    password  = os.getenv("CRISIS_SMTP_PASSWORD")
    sender    = os.getenv("CRISIS_EMAIL_FROM", user)
    recipients = recipients_for(brand)

    subject = f"🚨 Crisis alert — {brand} — {len(crisis_comments)} comment(s) flagged"

    lines = [
        f"Tolaram Intelligence detected {len(crisis_comments)} potential crisis comment(s) "
        f"on \"{video_title}\" ({brand}).",
        "",
    ]
    for c in crisis_comments:
        lines.append(f"• Author: {c.get('author', 'Unknown')}")
        lines.append(f"  Reason: {c.get('crisis_reason', 'Unknown')}")
        lines.append(f"  Comment: {(c.get('text') or '')[:300]}")
        lines.append(f"  Comment ID: {c.get('comment_id')}")
        lines.append("")
    lines.append("Open the dashboard's Crisis Monitor page to review and acknowledge.")

    body = "\n".join(lines)

    try:
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(sender, recipients, msg.as_string())

        logger.critical(f"🚨 Crisis email sent to {', '.join(recipients)} for {brand}")
        return {"ok": True, "sent": True, "recipients": recipients}
    except Exception as e:
        logger.error(f"Crisis email failed to send: {e}")
        return {"ok": False, "sent": False, "error": str(e)}


# ── Reviewer acknowledgment tracking ────────────────────────────────────────

def get_all_reviews() -> Dict:
    return load_json(REVIEWS_FILE, default={})


def get_reviews_for(comment_id: str) -> List[Dict]:
    return get_all_reviews().get(comment_id, [])


def mark_reviewed(comment_id: str, reviewer: str, note: str = "") -> Dict:
    if not comment_id or not reviewer:
        return {"ok": False, "error": "comment_id and reviewer are required"}

    reviews = get_all_reviews()
    entry = reviews.setdefault(comment_id, [])

    # One ack per reviewer per comment — update the timestamp/note if they
    # re-mark it rather than piling up duplicate entries.
    for r in entry:
        if r.get("reviewer", "").lower() == reviewer.lower():
            r["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            r["note"] = note
            save_json(REVIEWS_FILE, reviews)
            return {"ok": True, "data": entry}

    entry.append({
        "reviewer":     reviewer,
        "reviewed_at":  datetime.now(timezone.utc).isoformat(),
        "note":         note,
    })
    save_json(REVIEWS_FILE, reviews)
    return {"ok": True, "data": entry}


def unmark_reviewed(comment_id: str, reviewer: str) -> Dict:
    reviews = get_all_reviews()
    entry = reviews.get(comment_id, [])
    new_entry = [r for r in entry if r.get("reviewer", "").lower() != reviewer.lower()]
    if len(new_entry) == len(entry):
        return {"ok": True, "removed": False}
    reviews[comment_id] = new_entry
    save_json(REVIEWS_FILE, reviews)
    return {"ok": True, "removed": True}


def configured_reviewers() -> Dict:
    """
    Base recipient list plus a brand -> manager map, so the UI can show the
    right reviewer chips per crisis card: the base list on every card, plus
    that specific brand's manager only on that brand's cards.
    """
    tracked_brands = ["Munch It", "Indomie", "Colgate", "Hypo", "Power Oil", "Nutrify", "Lush", "Minimie"]
    return {
        "base": _base_recipients(),
        "by_brand": {b: brand_manager(b) for b in tracked_brands if brand_manager(b)},
    }