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

# Load environment variables
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
REDIRECT_URI = os.getenv("REDIRECT_URI")
GOOGLE_CRED_JSON = os.getenv("GOOGLE_CRED_JSON")

# Ensure required environment variables are set
if not GOOGLE_CRED_JSON:
    raise RuntimeError("GOOGLE_CRED_JSON env var is missing")
if not OPENAI_API_KEY or not FLASK_SECRET_KEY:
    raise RuntimeError("Set OPENAI_API_KEY and FLASK_SECRET_KEY in environment")

# Parse Google credentials\parsed_creds = json.loads(GOOGLE_CRED_JSON)

# OAuth settings
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Flask setup
app = Flask(__name__, static_folder="static")
app.secret_key = FLASK_SECRET_KEY
CORS(app)

# Import project logic
from task_breakdown import breakdown_goal
from calendar_integration import schedule_tasks, create_calendar_events

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

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/login")
def login():
    try:
        print("🟡 /login hit")
        session.clear()

        if not REDIRECT_URI:
            print("❌ Missing REDIRECT_URI")
            return "Missing REDIRECT_URI", 500

        flow = InstalledAppFlow.from_client_config(
            parsed_creds,
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI
        )
        # Generate authorization URL and capture state
        auth_url, flow_state = flow.authorization_url(prompt='consent', access_type='offline')
        print("🔎 OAuth State:", flow_state)

        session["state"] = flow_state
        print("✅ Redirecting to auth URL:", auth_url)
        return redirect(auth_url)
    except Exception as e:
        print("🔥 Error in /login:", repr(e))
        return f"Login failed: {e}", 500

@app.route("/oauth2callback")
def oauth2callback():
    try:
        print("🟡 /oauth2callback hit")
        session_state = session.get("state")
        print("🔁 session_state:", session_state)

        flow = InstalledAppFlow.from_client_config(
            parsed_creds,
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI,
            state=session_state
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
        print("✅ OAuth2 callback complete")
        return redirect(url_for("index"))
    except Exception as e:
        print("🔥 Error in /oauth2callback:", repr(e))
        return f"OAuth callback failed: {e}", 500

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
        end = e["end"].get("dateTime")
        if start and end:
            events.append({"title": e.get("summary","(No title)"), "start": start, "end": end})
    return jsonify({"events": events})

@app.route("/api/tasks", methods=["POST"])
def api_tasks():
    data = request.get_json(force=True)
    tasks = breakdown_goal(data.get("goal",""), data.get("level","easy"), data.get("deadline",""))
    for t in tasks:
        t.setdefault("duration_hours", 1.0)
    return jsonify({"tasks": tasks})

@app.route("/api/schedule", methods=["POST"])
def api_schedule():
    data = request.get_json(force=True)
    svc = get_calendar_service()
    if not svc:
        return jsonify({"error":"not_authenticated"}), 401
    scheduled, unscheduled = schedule_tasks(svc, data.get("tasks", []), data.get("start_date"), data.get("deadline"))
    ids = create_calendar_events(svc, scheduled)
    return jsonify({"eventIds": ids, "unscheduled": unscheduled})
