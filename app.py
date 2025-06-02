import os
import json
from datetime import datetime, date
from flask import Flask, request, jsonify, redirect, session, url_for, send_from_directory
from flask_cors import CORS
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

# ─────────────────────────────────────────────────────────────────────────────
# 1. Load environment variables
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
REDIRECT_URI     = os.getenv("REDIRECT_URI", "").strip()
GOOGLE_CRED_JSON = os.getenv("GOOGLE_CRED_JSON")

# 2. Parse Google credentials (env var or local file)
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

# 3. Ensure required environment variables
if not OPENAI_API_KEY or not FLASK_SECRET_KEY:
    raise RuntimeError("Set OPENAI_API_KEY and FLASK_SECRET_KEY in environment")
if not REDIRECT_URI:
    raise RuntimeError("Set REDIRECT_URI in environment to your OAuth callback URL")

# 4. OAuth settings
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# 5. Flask setup
app = Flask(__name__, static_folder="static")
app.secret_key = FLASK_SECRET_KEY
CORS(app, supports_credentials=True)

# 6. Import project logic
from task_breakdown import breakdown_goal
from calendar_integration import schedule_tasks, create_calendar_events


# ─────────────────────────────────────────────────────────────────────────────
# Helper: Reconstruct Google Calendar service from session credentials
# ─────────────────────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
# 1) Serve index.html
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ─────────────────────────────────────────────────────────────────────────────
# 2) OAuth login start
# ─────────────────────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
# 3) OAuth2 callback
# ─────────────────────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
# 4) Return future timed events (no all‐day)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/events")
def api_events():
    service = get_calendar_service()
    if service is None:
        return jsonify({"error": "not_authenticated"}), 401
    try:
        now = datetime.utcnow().isoformat() + "Z"
        items = service.events().list(
            calendarId="primary",
            timeMin=now,
            singleEvents=True,
            orderBy="startTime"
        ).execute().get("items", [])
    except RefreshError:
        session.clear()
        return jsonify({"error": "not_authenticated"}), 401

    events = []
    for e in items:
        start = e["start"].get("dateTime")
        end   = e["end"].get("dateTime")
        if start and end:
            events.append({
                "title": e.get("summary", "(No title)"),
                "start": start,
                "end": end
            })
    return jsonify({"events": events})


# ─────────────────────────────────────────────────────────────────────────────
# 5) Task breakdown (POST /api/tasks)
#    Now computes totalTasks = days until deadline, so AI generates “as many
#    or as few” steps as needed based on that day count.
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/tasks", methods=["POST"])
def api_tasks():
    try:
        data = request.get_json(force=True)
        goal     = data.get("goal", "")
        level    = data.get("level", "easy")
        deadline = data.get("deadline", "")

        # Compute number of days from today → deadline (inclusive)
        try:
            deadline_date = datetime.fromisoformat(deadline).date()
        except Exception:
            return jsonify({
                "error": "invalid_deadline",
                "message": f"Could not parse deadline '{deadline}'"
            }), 400

        today_date = date.today()
        day_diff = (deadline_date - today_date).days
        if day_diff < 1:
            day_diff = 1  # if deadline is today or earlier, at least 1 day

        total_tasks = day_diff

        # Ask the AI to generate roughly 'total_tasks' steps
        tasks = breakdown_goal(goal, level, deadline, total_tasks)

        # Ensure each returned task has a duration
        for t in tasks:
            t.setdefault("duration_hours", 1.0)

        return jsonify({"tasks": tasks})
    except Exception as e:
        app.logger.exception("Error inside /api/tasks")
        return jsonify({
            "error": "task_generation_failed",
            "message": str(e),
            "type": e.__class__.__name__
        }), 500


# ─────────────────────────────────────────────────────────────────────────────
# 6) Scheduling + push (POST /api/schedule)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/schedule", methods=["POST"])
def api_schedule():
    try:
        data      = request.get_json(force=True)
        settings  = data.get("settings", {})
        max_hours_per_day  = settings.get("maxHoursPerDay", None)
        allowed_days       = settings.get("allowedDaysOfWeek", None)

        svc = get_calendar_service()
        if not svc:
            return jsonify({"error":"not_authenticated"}), 401

        tasks     = data.get("tasks", [])
        start_iso = data.get("start_date")
        deadline  = data.get("deadline")

        scheduled, unscheduled = schedule_tasks(
            svc,
            tasks,
            start_iso,
            deadline,
            max_hours_per_day=max_hours_per_day,
            allowed_days=allowed_days
        )

        ids = create_calendar_events(svc, scheduled)
        return jsonify({
            "eventIds":    ids,
            "unscheduled": unscheduled
        })
    except Exception as e:
        app.logger.exception("Error inside /api/schedule")
        return jsonify({
            "error": "schedule_failed",
            "message": str(e),
            "type": e.__class__.__name__
        }), 500


# ─────────────────────────────────────────────────────────────────────────────
# 7) Run the app via Gunicorn (no need for a Flask‐run block here)
# Note: On Render, the Start Command should be:  gunicorn app:app
# ─────────────────────────────────────────────────────────────────────────────
