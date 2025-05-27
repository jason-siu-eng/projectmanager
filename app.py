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
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Load .env
load_dotenv()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
REDIRECT_URI     = os.getenv("REDIRECT_URI")

# Ensure secrets are set
if not OPENAI_API_KEY or not FLASK_SECRET_KEY:
    raise RuntimeError("Set OPENAI_API_KEY and FLASK_SECRET_KEY in .env")

# Flask setup
app = Flask(__name__, static_folder="static")
app.secret_key = FLASK_SECRET_KEY
CORS(app)

# OAuth settings
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CLIENT_SECRETS_FILE = "credentials.json"

# Project logic imports
from task_breakdown import breakdown_goal
from calendar_integration import schedule_tasks, create_calendar_events


def get_calendar_service():
    creds_info = session.get("credentials")
    if not creds_info:
        return None

    # Reconstruct Credentials object
    creds = Credentials(
        token=creds_info["token"],
        refresh_token=creds_info.get("refresh_token"),
        token_uri=creds_info["token_uri"],
        client_id=creds_info["client_id"],
        client_secret=creds_info["client_secret"],
        scopes=creds_info["scopes"],
    )

    try:
        # Always try to refresh if needed
        if not creds.valid:
            creds.refresh(Request())
        # Save any updated token back into the session
        session["credentials"] = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }
    except RefreshError:
        # Token expired/revoked → clear session so front-end will re-login
        session.clear()
        return None

    return build("calendar", "v3", credentials=creds)

# 1. Serve index.html
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

# 2. OAuth login start
@app.route("/login")
def login():
    session.clear()  # drop old creds & scopes
    flow = InstalledAppFlow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES
    )
    creds = flow.run_local_server(port=0)
    print("🔑 Granted scopes:", creds.scopes)      # << debug print
    session["credentials"] = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    return redirect(url_for("index"))


# 4. Return timed events (no all-day)
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

    # Filter out all-day events and return timed ones
    events = []
    for e in items:
        start = e["start"].get("dateTime")
        end   = e["end"].get("dateTime")
        if start and end:
            events.append({"title": e.get("summary","(No title)"), "start": start, "end": end})
    return jsonify({"events": events})

# 5. Task breakdown
@app.route("/api/tasks", methods=["POST"])
def api_tasks():
    data = request.get_json(force=True)
    goal     = data.get("goal","")
    level    = data.get("level","easy")
    deadline = data.get("deadline","")
    tasks = breakdown_goal(goal, level, deadline)
    # ensure duration_hours exists
    for t in tasks:
        t.setdefault("duration_hours", 1.0)
    return jsonify({"tasks": tasks})

# 6. Scheduling + push
@app.route("/api/schedule", methods=["POST"])
def api_schedule():
    data = request.get_json(force=True)
    svc = get_calendar_service()
    if not svc:
        return jsonify({"error":"not_authenticated"}), 401

    tasks      = data.get("tasks", [])
    start_iso  = data.get("start_date")
    deadline   = data.get("deadline")

    # schedule_tasks now returns a tuple
    scheduled, unscheduled = schedule_tasks(svc, tasks, start_iso, deadline)

    # insert the ones we did schedule
    ids = create_calendar_events(svc, scheduled)

    return jsonify({
       "eventIds":    ids,
       "unscheduled": unscheduled
    })