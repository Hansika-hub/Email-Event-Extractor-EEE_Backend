from flask import Flask, redirect, request, jsonify, session
from gmail_utils import get_gmail_service
from extractor import extract_event_details, is_event_like, count_event_fields
from flask_cors import CORS
import os
from dotenv import load_dotenv
from db_utils import DB_NAME, save_to_db
import requests
import logging
import base64
from threading import BoundedSemaphore
import sqlite3
import json
import re
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from icalendar import Calendar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY is not configured")
# ✅ Secure session settings (if ever needed)
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='None'
)

"""CORS configuration
- Allow the deployed frontend origin
- Explicitly allow Authorization header and common methods for preflight success
"""
CORS(
    app,
    supports_credentials=True,
    origins=["https://email-mu-eight.vercel.app"],
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "OPTIONS"],
)

scan_slot = BoundedSemaphore(1)


def cached_event(mailbox, message_id):
    # Store results on disk so repeated mailboxes do not grow the worker heap.
    with sqlite3.connect(DB_NAME) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS extraction_cache (
            mailbox TEXT, message_id TEXT, updated INTEGER, result TEXT,
            PRIMARY KEY (mailbox, message_id))""")
        row = db.execute("""SELECT result, updated > strftime('%s','now') - 300
                            FROM extraction_cache WHERE mailbox=? AND message_id=?""",
                         (mailbox, message_id)).fetchone()
    return (json.loads(row[0]) if row and row[1] else None, bool(row))


def cache_event(mailbox, message_id, result):
    with sqlite3.connect(DB_NAME) as db:
        db.execute("""INSERT OR REPLACE INTO extraction_cache VALUES
                      (?, ?, strftime('%s','now'), ?)""", (mailbox, message_id, json.dumps(result)))


# ---- Helpers to extract readable body text from Gmail payload ----
def _decode_base64_to_text(data: str, max_encoded=None) -> str:
    try:
        return base64.urlsafe_b64decode(data[:max_encoded]).decode("utf-8", errors="ignore")
    except Exception:
        return ""

def _strip_html(html: str) -> str:
    if not html:
        return ""
    # Remove script/style
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    # Replace breaks with newlines
    html = re.sub(r"<(br|/p|/div)>", "\n", html, flags=re.IGNORECASE)
    # Strip tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    return re.sub(r"[^\S\n]+", " ", text).strip()

def _walk_parts_for_text(payload: dict) -> str:
    if not payload:
        return ""
    # Prefer text/plain
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode_base64_to_text(payload["body"]["data"], 131072) or ""
    # Fallback text/html
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        html = _decode_base64_to_text(payload["body"]["data"], 131072) or ""
        return _strip_html(html)
    # Recurse into parts
    for part in (payload.get("parts") or []):
        text = _walk_parts_for_text(part)
        if text:
            return text
    # Last resort: body at this level
    if payload.get("body", {}).get("data"):
        return _decode_base64_to_text(payload["body"]["data"], 131072) or ""
    return ""

def _walk_parts_for_calendar(payload: dict) -> str:
    """Return raw ICS text if a text/calendar part is found."""
    if not payload:
        return ""
    if payload.get("mimeType") == "text/calendar" and payload.get("body", {}).get("data"):
        return _decode_base64_to_text(payload["body"]["data"]) or ""
    for part in (payload.get("parts") or []):
        data = _walk_parts_for_calendar(part)
        if data:
            return data
    if payload.get("body", {}).get("data") and payload.get("mimeType", "").endswith("calendar"):
        return _decode_base64_to_text(payload["body"]["data"]) or ""
    return ""

def _extract_event_from_ics(ics_text: str) -> dict:
    """Parse ICS and return event fields if possible."""
    try:
        cal = Calendar.from_ical(ics_text)
    except Exception:
        return {}
    summary = None
    date_str = None
    time_str = None
    venue = None
    for component in cal.walk():
        if component.name == "VEVENT":
            summary = str(component.get("summary")) if component.get("summary") else None
            dtstart = component.get("dtstart")
            location = component.get("location")
            if dtstart:
                try:
                    val = dtstart.dt
                    if isinstance(val, datetime):
                        date_str = val.strftime("%Y-%m-%d")
                        time_str = val.strftime("%H:%M")
                    else:
                        # date only
                        date_str = val.strftime("%Y-%m-%d")
                except Exception:
                    pass
            if location:
                venue = str(location)
            break
    if any([summary, date_str, time_str, venue]):
        return {
            "event": summary,
            "event_name": summary,
            "date": date_str,
            "time": time_str,
            "venue": venue,
            "all_day": bool(date_str and not time_str),
            "timezone": (getattr(val.tzinfo, "key", None) or getattr(val.tzinfo, "zone", None) or ("UTC" if val.utcoffset() == timezone.utc.utcoffset(None) else None)) if time_str else None,
            "source": "ics",
            "confidence": 1.0 if date_str and (time_str or venue) else 0.9,
        }
    return {}

# Optional logging configuration
if os.getenv("DEBUG_NER", "0") not in (None, "", "0", "false", "False"):
    logging.basicConfig(level=logging.INFO, format='%(message)s')

# ✅ Optional: Block non-JSON POST requests
@app.before_request
def block_non_json_post():
    # Allow POSTs without JSON for token-in-header endpoints
    exempt_endpoints = {"fetch_emails", "process_all_emails", "cleanup"}
    if request.method == 'POST' and not request.is_json and (request.endpoint not in exempt_endpoints):
        return jsonify({"error": "Only JSON POST requests allowed"}), 415


@app.route("/", methods=["GET"]) 
def health_check():
    return jsonify({"status": "ok"}), 200


@app.route("/", methods=["POST"])
def authenticate():
    data = request.get_json()
    id_token_str = data.get("token")

    if not id_token_str:
        return jsonify({"error": "Missing ID token"}), 400

    try:
        idinfo = id_token.verify_oauth2_token(
            id_token_str,
            google_requests.Request(),
            "721040422695-9m0ge0d19gqaha28rse2le19ghran03u.apps.googleusercontent.com"
        )

        session["email"] = idinfo["email"]

        return jsonify({
            "status": "authenticated",
            "user": idinfo["email"],
            # Note: This is an ID token (OpenID Connect), NOT a Gmail OAuth access token
            # Kept as 'accessToken' for backward compatibility with existing frontend code
            "accessToken": id_token_str,
            "idToken": id_token_str,
            "tokenType": "id"
        }), 200

    except Exception as e:
        print("❌ Token verification error:", str(e))
        return jsonify({"error": "Token verification failed"}), 400


def _extract_bearer_or_body_token():
    auth_header = request.headers.get("Authorization", "") or ""
    if auth_header.startswith("Bearer "):
        return auth_header.split(" ")[1]
    if request.is_json:
        body = request.get_json(silent=True) or {}
        return body.get("accessToken") or body.get("access_token") or body.get("token") or None
    return None


@app.route("/debug_token", methods=["GET", "POST", "OPTIONS"]) 
def debug_token():
    token = _extract_bearer_or_body_token()
    if not token:
        return jsonify({"error": "No token provided"}), 400

    is_jwt = token.count(".") == 2
    info = {"looks_like": "jwt_id_token" if is_jwt else "access_token"}

    try:
        if is_jwt:
            resp = requests.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": token},
                timeout=6,
            )
            info["id_token_info"] = resp.json()
        else:
            resp = requests.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": token},
                timeout=6,
            )
            info["access_token_info"] = resp.json()
    except Exception as e:
        info["error"] = f"tokeninfo request failed: {e}"

    return jsonify(info), 200


@app.route("/fetch_emails", methods=["GET", "POST", "OPTIONS"])
def fetch_emails():
    if request.method == 'OPTIONS':
        return jsonify({"ok": True}), 200
    access_token = _extract_bearer_or_body_token()
    if not access_token:
        return jsonify({
            "error": "Missing access token",
            "hint": "Send a Gmail OAuth access token via Authorization: Bearer <token> or JSON {accessToken}. An ID token will not work for Gmail API."
        }), 401

    try:
        creds = Credentials(token=access_token)
        service = build("gmail", "v1", credentials=creds)

        results = service.users().messages().list(userId="me", maxResults=10, q="is:unread").execute()
        messages = results.get("messages", [])

        email_list = []
        for msg in messages:
            msg_detail = service.users().messages().get(userId="me", id=msg['id'], format='metadata', metadataHeaders=['Subject']).execute()
            headers = msg_detail.get("payload", {}).get("headers", [])
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "No Subject")
            email_list.append({
                "id": msg["id"],
                "subject": subject
            })

        return jsonify(email_list)
    except Exception as e:
        print("📡 Gmail API error:", str(e))
        return jsonify({
            "error": "Failed to fetch emails from Gmail",
            "hint": "Ensure the provided token is a Gmail OAuth access token with gmail.readonly scope."
        }), 401

@app.route("/process_emails", methods=["GET", "POST", "OPTIONS"])
def process_all_emails():
    if request.method == 'OPTIONS':
        return jsonify({"ok": True}), 200
    access_token = _extract_bearer_or_body_token()
    if not access_token:
        return jsonify({
            "error": "Missing access token",
            "hint": "Send a Gmail OAuth access token via Authorization: Bearer <token> or JSON {accessToken}. An ID token will not work for Gmail API."
        }), 401

    if not scan_slot.acquire(blocking=False):
        return jsonify({"error": "An email scan is in progress. Please retry shortly."}), 429, {"Retry-After": "5"}

    try:
        creds = Credentials(token=access_token)
        service = build("gmail", "v1", credentials=creds)
        results = service.users().messages().list(
            userId="me", maxResults=20, q="is:unread"
        ).execute()
        messages = results.get("messages", [])
        print(f"📥 Fetched unread messages: {len(messages)}")
        mailbox = service.users().getProfile(userId="me").execute()["emailAddress"]
        extracted = []

        for msg in messages:
            try:
                key = (mailbox, msg["id"])
                cached, previously_saved = cached_event(*key)
                if cached is not None:
                    extracted.append(cached)
                    continue
                msg_detail = service.users().messages().get(
                    userId="me", id=msg["id"], format="full"
                ).execute()

                # ✅ Extract Subject
                headers = msg_detail.get("payload", {}).get("headers", [])
                subject = next(
                    (h["value"] for h in headers if h["name"] == "Subject"),
                    "No Subject"
                )

                payload = msg_detail.get("payload", {})
                ics_data = _walk_parts_for_calendar(payload)
                structured = _extract_event_from_ics(ics_data) if ics_data else {}
                if structured.get("date") and structured.get("event_name"):
                    result = {**structured, "trace": ["ics"]}
                else:
                    stamp = next((h["value"] for h in headers if h["name"].lower() == "date"), "")
                    try:
                        reference = parsedate_to_datetime(stamp).replace(tzinfo=None)
                    except (TypeError, ValueError, OverflowError):
                        reference = datetime.fromtimestamp(int(msg_detail["internalDate"])/1000, timezone.utc).replace(tzinfo=None)
                    result = extract_event_details(subject, _walk_parts_for_text(payload), reference)
                    if structured:
                        result.update({k:v for k,v in structured.items() if v is not None and k not in ("source","confidence")})
                        result["trace"].insert(0, "ics (partial)")
                if is_event_like(result) or (structured and result.get("date")):
                    result["message_id"] = msg["id"]
                    # If all three present, mark attendees = 1 (legacy behavior)
                    if count_event_fields(result) >= 3:
                        result["attendees"] = 1
                    extracted.append(result)
                    if not previously_saved:
                        save_to_db(result)
                    cache_event(*key, result)
                else:
                    logging.info("Skipped email with insufficient event evidence")

            except Exception as e:
                print(f"⚠️ Skipping email due to error: {e}")
                continue

        print(f"✅ Extracted events: {len(extracted)}")
        return jsonify(extracted)

    except Exception as e:
        print("📡 Gmail API error:", str(e))
        return jsonify({
            "error": "Failed to process emails",
            "hint": "Ensure the provided token is a Gmail OAuth access token with gmail.readonly scope."
        }), 401
    finally:
        scan_slot.release()


@app.route("/cleanup_reminders", methods=["POST"])
def cleanup():
    from db_utils import delete_expired_events
    deleted = delete_expired_events()
    return jsonify({"deleted": deleted})


# ✅ Main runner
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
