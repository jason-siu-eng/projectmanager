import os
import json
from datetime import datetime
from flask import Flask, request, jsonify, redirect, session, url_for, send_from_directory
from flask_cors import CORS
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from dotenv import load_dotenv

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
if not REDIRECT_URI:
    raise RuntimeError("You must set REDIRECT_URI in your env to your OAuth callback")
# Note: OPENAI_API_KEY is optional—if missing, /api/tasks will fall back.

# ── 4) OAUTH SETTINGS ──────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# ── 5) FLASK APP SETUP ─────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")
app.secret_key = FLASK_SECRET_KEY
CORS(app, supports_credentials=True)

# ── 6) IMPORT PROJECT LOGIC ────────────────────────────────────────────────────
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

# ── 8) ROUTE: FRONT-END ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

# ── 9) LOGIN → GOOGLE OAUTH ────────────────────────────────────────────────────
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
        app.logger.exception("Error in /login")
        return jsonify({"error": "login_failed", "message": str(e)}), 500

# ──10) OAUTH2 CALLBACK ──────────────────────────────────────────────────────────
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
        app.logger.exception("Error in /oauth2callback")
        return jsonify({"error": "oauth_callback_failed", "message": str(e)}), 500

# ──11) API: FETCH EVENTS ────────────────────────────────────────────────────────
@app.route("/api/events")
def api_events():
    service = get_calendar_service()
    if not service:
        return jsonify({"error": "not_authenticated"}), 401

    # fetch Google color map
    colors_def = service.colors().get().execute().get("event", {})
    now = datetime.utcnow().isoformat() + "Z"
    items = service.events().list(
        calendarId="primary",
        timeMin=now,
        singleEvents=True,
        orderBy="startTime"
    ).execute().get("items", [])

    events = []
    for e in items:
        sd = e["start"].get("dateTime")
        ed = e["end"].get("dateTime")
        if not (sd and ed):
            continue
        cid = e.get("colorId")
        bg = colors_def.get(cid, {}).get("background") if cid else None
        fg = colors_def.get(cid, {}).get("foreground") if cid else None

        events.append({
            "title":     e.get("summary", "(No title)"),
            "start":     sd,
            "end":       ed,
            "color":     bg,
            "textColor": fg,
            "googleColor": cid
        })

    return jsonify({"events": events})

# ──12) HELPER: DECIDE TOTAL TASKS ───────────────────────────────────────────────
def decide_total_tasks(goal: str, level: str, deadline: str, override: int = None) -> int:
    # compute days_left
    try:
        today   = datetime.utcnow().date()
        dl_date = datetime.fromisoformat(deadline).date()
        days_left = max((dl_date - today).days, 1)
    except Exception:
        days_left = 7
    # baseline by proficiency
    prof_map = {"easy": 0.8, "medium": 1.0, "hard": 1.2}
    prof_mult = prof_map.get(level.lower(), 1.0)
    base_count = int(round(days_left * prof_mult))
    # honor override
    if override is not None and override >= 1:
        return override
    # (optional) complexity adjustment omitted / falls through
    # fallback
    if level.lower() == "easy":
        return max(int(round(days_left * 0.8)), 1)
    return max(days_left, 1)

# ──13) API: GENERATE TASKS ──────────────────────────────────────────────────────
@app.route("/api/tasks", methods=["POST"])
def api_tasks():
    goal          = data.get("goal", "").trim()
    current_level = data.get("currentLevel", "easy").trim()
    target_level  = data.get("targetLevel", "expert").trim()
    deadline      = data.get("deadline", "").trim()

    # Now pass both current & target into your breaker
    tasks = breakdown_goal(goal, current_level, target_level, deadline)

    # decide how many steps
    total = decide_total_tasks(goal, current, deadline, override)

    # pass all four into breakdown_goal
    try:
        tasks = breakdown_goal(goal, current, target, deadline)
    except Exception as e:
        app.logger.exception("Error in breakdown_goal")
        # fallback to placeholders if needed
        tasks = [
            {"id": i+1, "task": f"(Step {i+1} placeholder)", "duration_hours": 1.0}
            for i in range(total)
        ]

    # ensure exactly `total` items
    if len(tasks) < total:
        for i in range(len(tasks), total):
            tasks.append({
                "id": i+1,
                "task": f"(Step {i+1} placeholder)",
                "duration_hours": 1.0
            })
    elif len(tasks) > total:
        tasks = tasks[:total]

    return jsonify({"tasks": tasks})

# ──14) API: SCHEDULE INTO GOOGLE CALENDAR ───────────────────────────────────────
@app.route("/api/schedule", methods=["POST"])
def api_schedule():
    data     = request.get_json(force=True)
    settings = data.get("settings", {})
    max_hours = settings.get("maxHoursPerDay", None)
    allowed   = settings.get("allowedDaysOfWeek", None)

    service   = get_calendar_service()
    if not service:
        return jsonify({"error": "not_authenticated"}), 401

    tasks      = data.get("tasks", [])
    start_iso  = data.get("start_date")
    deadline   = data.get("deadline")

    try:
        scheduled, unscheduled = schedule_tasks(
            service,
            tasks,
            start_iso,
            deadline,
            max_hours_per_day   = max_hours,
            allowed_days_of_week= allowed
        )
        ids = create_calendar_events(service, scheduled)
        return jsonify({
            "eventIds":    ids,
            "scheduled":   scheduled,
            "unscheduled": unscheduled
        })
    except Exception as e:
        app.logger.exception("Error in /api/schedule")
        return jsonify({"error": "schedule_failed", "message": str(e)}), 500

# ──15) RUN APP FOR LOCAL DEBUG ─────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
