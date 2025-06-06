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
if not FLASK_SECRET_KEY:
    raise RuntimeError("You must set FLASK_SECRET_KEY in your environment")
# Note: OPENAI_API_KEY is optional—if missing, we will skip AI calls and use placeholders.
if not REDIRECT_URI:
    raise RuntimeError("You must set REDIRECT_URI in your env to your OAuth callback")

# ── 4) OAUTH SETTINGS ──────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# ── 5) FLASK APP SETUP ─────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")
app.secret_key = FLASK_SECRET_KEY
CORS(app, supports_credentials=True)

# ── 6) IMPORT PROJECT LOGIC ────────────────────────────────────────────────────
from task_breakdown import ask_complexity_score, breakdown_goal
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
        auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")
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
            end = e["end"].get("dateTime")
            if start and end:
                events.append({"title": e.get("summary", "(No title)"), "start": start, "end": end})
        return jsonify({"events": events})
    except RefreshError:
        session.clear()
        return jsonify({"error": "not_authenticated"}), 401


# ── 12) HELPER: DECIDE TOTAL TASKS (TASK COUNT STACK) ────────────────────────────
def decide_total_tasks(goal: str, level: str, deadline: str, override: int = None) -> int:
    """
    1) Compute days_left
    2) Baseline: 1 task/day × proficiency_multiplier
    3) If override is provided, return override
    4) Otherwise, optionally ask OpenAI for complexity score 1–10, adjust
    5) If anything fails, fallback to “pile of days_left” (or days_left*0.8 if easy)
    """
    # 1) Compute days_left
    try:
        today = datetime.utcnow().date()
        dl_date = datetime.fromisoformat(deadline).date()
        days_left = max((dl_date - today).days, 1)
    except Exception:
        days_left = 7

    # 2) Baseline by proficiency
    prof_map = {"easy": 0.8, "medium": 1.0, "hard": 1.2}
    prof_mult = prof_map.get(level.lower(), 1.0)
    base_count = int(round(days_left * prof_mult))

    # 3) Honor user override if supplied and ≥1
    if override is not None:
        try:
            override_int = int(override)
            if override_int >= 1:
                return override_int
        except ValueError:
            pass  # ignore invalid override

    # 4) If OPENAI_API_KEY is present, ask for complexity score
    if OPENAI_API_KEY:
        try:
            complexity = ask_complexity_score(goal, level, deadline)
            # Adjustment = round(complexity_score / 3).  e.g. if complexity=8, adjustment ~ 3
            adjustment = round(complexity / 3)
            total = base_count + adjustment
            return max(total, 1)
        except Exception:
            # If something goes wrong with complexity call, fall back below
            pass

    # 5) Fallback: if “easy,” generate days_left * 0.8 (rounded), else days_left
    if level.lower() == "easy":
        fallback_count = int(round(days_left * 0.8))
        return max(fallback_count, 1)
    else:
        return max(days_left, 1)


# ── 13) ROUTE: GENERATE TASKS VIA OPENAI ─────────────────────────────────────────
@app.route("/api/tasks", methods=["POST"])
def api_tasks():
    data = request.get_json(force=True)
    goal = data.get("goal", "").strip()
    level = data.get("level", "easy").strip()
    deadline = data.get("deadline", "").strip()
    override = data.get("overrideTaskCount", None)  # optional field from client

    # 13.1) If no OPENAI_API_KEY, or missing inputs, fall back to days_left placeholders
    if not goal or not deadline:
        return jsonify({"tasks": []})

    # 13.2) Decide how many tasks to create
    total_tasks = decide_total_tasks(goal, level, deadline, override)

    # 13.3) Call breakdown_goal(goal, level, deadline, total_tasks)
    try:
        tasks = breakdown_goal(goal, level, deadline, total_tasks)
        for t in tasks:
            t.setdefault("duration_hours", 1.0)
        return jsonify({"tasks": tasks})
    except Exception as e:
        app.logger.exception("Error in /api/tasks route")
        # If something went wrong, just return placeholders
        fallback = [
            {"id": i + 1, "task": f"(Step {i+1} placeholder)", "duration_hours": 1.0}
            for i in range(total_tasks)
        ]
        return jsonify({"tasks": fallback})


# ── 14) ROUTE: SCHEDULE INTO GOOGLE CALENDAR ─────────────────────────────────────
@app.route("/api/schedule", methods=["POST"])
def api_schedule():
    data = request.get_json(force=True)
    settings = data.get("settings", {})
    max_per_day = settings.get("maxHoursPerDay", None)
    allowed_days = settings.get("allowedDaysOfWeek", None)

    service = get_calendar_service()
    if not service:
        return jsonify({"error": "not_authenticated"}), 401

    tasks = data.get("tasks", [])
    start_iso = data.get("start_date")
    deadline = data.get("deadline")

    try:
        # Notice: `schedule_tasks` must accept max_per_day and allowed_days
        scheduled, unscheduled = schedule_tasks(
            service,
            tasks,
            start_iso,
            deadline,
            max_per_day=max_per_day,
            allowed_days=allowed_days
        )
        ids = create_calendar_events(service, scheduled)
        return jsonify({"eventIds": ids, "unscheduled": unscheduled})
    except Exception as e:
        app.logger.exception("Error in /api/schedule route")
        return jsonify({"error": "schedule_failed", "message": str(e)}), 500


# ── 15) RUN APP (for local debugging) ───────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
