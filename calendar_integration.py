# calendar_integration.py

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from googleapiclient.errors import HttpError

# Constants
LOCAL_TZ          = ZoneInfo("America/Los_Angeles")
WORK_START        = 9    # 9 AM
WORK_END          = 22   # 10 PM
MAX_TASKS_PER_DAY = 2

def schedule_tasks(service, tasks, start_iso, deadline_iso):
    """
    service: authorized Google Calendar service
    tasks:   [ {"id":…, "task":…, "duration_hours":…}, … ]
    start_iso:    ISO timestamp string when scheduling may begin (e.g. now)
    deadline_iso: ISO date string ("YYYY-MM-DD") by which all tasks must be scheduled

    Returns:
      scheduled:   [ {"summary":…, "start":iso, "end":iso}, … ]
      unscheduled: [ {"id":…, "task":…}, … ]
    """
    # 1) Parse our scheduling window
    dt = datetime.fromisoformat(start_iso).astimezone(LOCAL_TZ)
    dt = dt.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
    deadline_date = datetime.fromisoformat(deadline_iso).date()
    time_max = datetime.combine(
        deadline_date + timedelta(days=1),
        time(0, 0),
        tzinfo=LOCAL_TZ
    )

    # 2) Fetch existing busy slots
    fb_req = {
        "timeMin": dt.isoformat(),
        "timeMax": time_max.isoformat(),
        "timeZone": "America/Los_Angeles",
        "items": [{"id": "primary"}]
    }
    busy = []
    try:
        resp = service.freebusy().query(body=fb_req).execute()
        for period in resp["calendars"]["primary"]["busy"]:
            start = datetime.fromisoformat(period["start"]).astimezone(LOCAL_TZ)
            end   = datetime.fromisoformat(period["end"]).astimezone(LOCAL_TZ)
            busy.append((start, end))
    except HttpError as e:
        # If free/busy fails, proceed as if calendar were empty
        print("⚠️ free/busy lookup failed:", e)

    scheduled   = []
    unscheduled = []
    day_counts  = {}

    # 3) For each task, find an open slot
    for t in tasks:
        duration = timedelta(hours=float(t.get("duration_hours", 1.0)))
        slot     = None
        probe    = dt

        # Walk day by day until deadline
        while probe.date() <= deadline_date:
            # Only Mon–Fri
            if probe.weekday() < 5:
                day = probe.date()
                if day_counts.get(day, 0) < MAX_TASKS_PER_DAY:
                    # Build this day’s busy slices
                    start_window = probe.replace(hour=WORK_START, minute=0)
                    end_window   = probe.replace(hour=WORK_END,   minute=0)

                    day_busy = sorted(
                        (
                            (max(start_window, bs), min(end_window, be))
                            for bs, be in busy
                            if bs.date() <= day <= be.date()
                        ),
                        key=lambda x: x[0]
                    )

                    # Carve out free windows
                    free_windows = []
                    cursor = start_window
                    for bs, be in day_busy:
                        if bs > cursor:
                            free_windows.append((cursor, bs))
                        cursor = max(cursor, be)
                    if cursor < end_window:
                        free_windows.append((cursor, end_window))

                    # Pick the first that fits
                    for ws, we in free_windows:
                        if (we - ws) >= duration:
                            slot = (ws, ws + duration)
                            break

            if slot:
                break

            # Move to next day at WORK_START
            next_day = datetime.combine(
                probe.date() + timedelta(days=1),
                time(0, 0),
                tzinfo=LOCAL_TZ
            )
            probe = next_day.replace(hour=WORK_START, minute=0)

        # If no slot found, record as unscheduled
        if not slot:
            unscheduled.append({"id": t["id"], "task": t["task"]})
            continue

        # Otherwise, record scheduled event
        scheduled.append({
            "summary": t["task"],
            "start":   slot[0].isoformat(),
            "end":     slot[1].isoformat()
        })

        # Mark that window as busy for subsequent tasks
        busy.append(slot)
        day_counts[slot[0].date()] = day_counts.get(slot[0].date(), 0) + 1
        # Next probe begins when this task ends
        dt = slot[1]

    return scheduled, unscheduled

def create_calendar_events(service, scheduled):
    """
    Inserts each block in `scheduled` into Google Calendar.
    Expects scheduled = [ {"summary":…, "start":iso, "end":iso}, … ]
    Returns a list of created event IDs.
    """
    ids = []
    for ev in scheduled:
        body = {
            "summary": ev["summary"],
            "start":   {"dateTime": ev["start"], "timeZone": "America/Los_Angeles"},
            "end":     {"dateTime": ev["end"],   "timeZone": "America/Los_Angeles"},
        }
        created = service.events().insert(calendarId="primary", body=body).execute()
        ids.append(created.get("id"))
    return ids
