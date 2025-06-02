# app.py

import os
import json
from datetime import datetime
from flask import Flask, request, jsonify, redirect, session, url_for, send_from_directory
from flask_cors import CORS
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

# ── 1) LOAD ENVIRONMENT ────────────────────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
REDIRECT_URI     = os.getenv("REDIRECT_URI", "").strip()
GOOGLE_CRED_JSON = os.getenv("GOOGLE_CRED_JSON")

# ── 2) PARSE GOOGLE CREDENTIALS ────────────────────────────────────────────────
if GOOGLE_CRED_JSON:
    try:
        parsed_creds = json.loads(GOOGLE_CRED_JSON)
    except json.JSONDecodeError:
        raise RuntimeError("Invalid JSON in GOOGLE_CRED_JSON env var")
elif os.path.exists("credentials.json"):
    with open("credentials.json", "r") as f:
        parsed_creds = json.load(f)
else:
    raise RuntimeError(
        "Missing Google credentials: set GOOGLE_CRED_JSON or provide credentials.json locally"
    )

# ── 3) VERIFY REQUIRED SECRETS ─────────────────────────────────────────────────
if not OPENAI_API_KEY or not FLASK_SECRET_KEY:
    raise RuntimeError("You must set OPENAI_API_KEY and FLASK_SECRET_KEY in your env")
if not REDIRECT_URI:
    raise RuntimeError("You must set REDIRECT_URI in your env to your OAuth callback")

# ── 4) OAUTH SETTINGS ──────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# ── 5) FLASK APP SETUP ─────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")
app.secret_key = FLASK_SECRET_KEY
CORS(app, supports_credentials=True)


# ── 6) IMPORT PROJECT LOGIC ────────────────────────────────────────────────────
# Make sure your task_breakdown.py defines: breakdown_goal(goal, level, deadline)
from task_breakdown import breakdown_goal
from calendar_integration import schedule_tasks, create_calendar_events


# ── 7) HELPER: BUILD & REFRESH GOOGLE CALENDAR SERVICE ──────────────────────────
def get_calendar_service():
    creds_info = session.get("credentials")
    if not creds_info:
        return None

    creds = Credentials(
        token=creds_info["token"],
        refresh_token=creds_info.get("refresh_token"),
        token_uri=creds_info["token_uri"],
        client_id=creds_info["client_id"],
        client_secret=creds_info["client_secret"],
        scopes=creds_info["scopes"],
    )

    try:
        if not creds.valid:
            creds.refresh(Request())
        # Save any refreshed token back into session
        session["credentials"] = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }
    except RefreshError:
        session.clear()
        return None

    return build("calendar", "v3", credentials=creds)


# ── 8) ROUTE: SERVE FRONT‐END STATIC INDEX.HTML ───────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ── 9) ROUTE: LOGIN → REDIRECT TO GOOGLE OAUTH CONSENT ────────────────────────────
@app.route("/login")
def login():
    try:
        session.clear()
        flow = InstalledAppFlow.from_client_config(
            parsed_creds,
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI
        )
        auth_url, state = flow.authorization_url(
            prompt="consent",
            access_type="offline"
        )
        session["state"] = state
        return redirect(auth_url)
    except Exception as e:
        app.logger.exception("Error in /login route")
        return jsonify({"error": "login_failed", "message": str(e)}), 500


# ── 10) ROUTE: GOOGLE CALLBACK ───────────────────────────────────────────────────
@app.route("/oauth2callback")
def oauth2callback():
    try:
        state = session.pop("state", None)
        if not state:
            return jsonify({"error": "invalid_state"}), 400

        flow = InstalledAppFlow.from_client_config(
            parsed_creds,
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI,
            state=state
        )
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        session["credentials"] = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }
        return redirect(url_for("index"))
    except Exception as e:
        app.logger.exception("Error in /oauth2callback route")
        return jsonify({"error": "oauth_callback_failed", "message": str(e)}), 500


# ── 11) ROUTE: RETURN UPCOMING CALENDAR EVENTS ────────────────────────────────────
@app.route("/api/events")
def api_events():
    service = get_calendar_service()
    if service is None:
        return jsonify({"error": "not_authenticated"}), 401

    try:
        now = datetime.utcnow().isoformat() + "Z"
        events = []
        items = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now,
                singleEvents=True,
                orderBy="startTime"
            )
            .execute()
            .get("items", [])
        )
        for e in items:
            start = e["start"].get("dateTime")
            end   = e["end"].get("dateTime")
            if start and end:
                events.append({
                    "title":   e.get("summary", "(No title)"),
                    "start":   start,
                    "end":     end
                })
        return jsonify({"events": events})

    except RefreshError:
        session.clear()
        return jsonify({"error": "not_authenticated"}), 401


# ── 12) ROUTE: GENERATE TASKS VIA OPENAI ─────────────────────────────────────────
@app.route("/api/tasks", methods=["POST"])
def api_tasks():
    data = request.get_json(force=True)
    goal     = data.get("goal", "").strip()
    level    = data.get("level", "easy").strip()
    deadline = data.get("deadline", "").strip()

    # 12.1) If no OPENAI_API_KEY present, return placeholder steps
    if not OPENAI_API_KEY:
        # Fallback: return 10 placeholder items (you can adjust count if you like)
        placeholder = [
            {
                "id": i + 1,
                "task": f"(Step {i+1} placeholder)",
                "duration_hours": 1.0
            }
            for i in range(10)
        ]
        return jsonify({"tasks": placeholder})

    # 12.2) Otherwise, call duty‐bound breakdown_goal(...) which must
    #       internally build an OpenAI prompt that looks at how many
    #       days remain between now and 'deadline' and returns whatever
    #       number of tasks is appropriate. Make sure your function
    #       signature is: breakdown_goal(goal, level, deadline)
    try:
        tasks = breakdown_goal(goal, level, deadline)
        # Ensure each returned dict has a duration_hours key
        for t in tasks:
            t.setdefault("duration_hours", 1.0)
        return jsonify({"tasks": tasks})

    except Exception as e:
        app.logger.exception("Error in /api/tasks route")
        return jsonify({"error": "task_generation_failed", "message": str(e)}), 500


# ── 13) ROUTE: SCHEDULE INTO GOOGLE CALENDAR ─────────────────────────────────────
@app.route("/api/schedule", methods=["POST"])
def api_schedule():
    data       = request.get_json(force=True)
    settings   = data.get("settings", {})
    max_per_day  = settings.get("maxEventsPerDay", None)
    allowed_days = settings.get("allowedDaysOfWeek", None)

    service = get_calendar_service()
    if not service:
        return jsonify({"error": "not_authenticated"}), 401

    tasks     = data.get("tasks", [])
    start_iso = data.get("start_date")
    deadline  = data.get("deadline")

    try:
        scheduled, unscheduled = schedule_tasks(
            service,
            tasks,
            start_iso,
            deadline,
            max_per_day=max_per_day,
            allowed_days=allowed_days
        )
        ids = create_calendar_events(service, scheduled)
        return jsonify({
            "eventIds":    ids,
            "unscheduled": unscheduled
        })
    except Exception as e:
        app.logger.exception("Error in /api/schedule route")
        return jsonify({"error": "schedule_failed", "message": str(e)}), 500


# ── 14) RUN APP ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
