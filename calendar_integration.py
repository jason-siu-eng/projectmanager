# calendar_integration.py

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from googleapiclient.errors import HttpError

# Constants
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
WORK_START = 9    # 9 AM
WORK_END   = 22   # 10 PM

def schedule_tasks(
    service,
    tasks,
    start_iso,
    deadline_iso,
    max_hours_per_day=None,
    allowed_days=None
):
    """
    service: authorized Google Calendar service
    tasks:   [ {"id":…, "task":…, "duration_hours":…}, … ]
    start_iso:    ISO timestamp string when scheduling may begin (e.g. now)
    deadline_iso: ISO date string ("YYYY-MM-DD") by which all tasks must be scheduled
    max_hours_per_day: integer or float, maximum total hours of OUR tasks per day
    allowed_days: list of weekday codes, e.g. ["MO","TU","WE","TH","FR"]

    Returns:
      scheduled:   [ {"summary":…, "start":iso, "end":iso}, … ]
      unscheduled: [ {"id":…, "task":…}, … ]
    """
    # 1) Parse our scheduling window
    dt = datetime.fromisoformat(start_iso).astimezone(LOCAL_TZ)
    # always start scheduling tomorrow if today has ANY events:
    dt = dt.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
    # Move dt to next day, because user wants “start tomorrow”
    dt = (dt + timedelta(days=1)).replace(hour=WORK_START, minute=0)

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

    # track total hours we’ve scheduled for each date
    day_hours = {}  # e.g. { date(2025,6,2): 3.5, … }

    # 3) For each task, find an open slot that also respects max_hours_per_day
    for t in tasks:
        duration = timedelta(hours=float(t.get("duration_hours", 1.0)))
        slot     = None
        probe    = dt

        # Walk day by day until deadline
        while probe.date() <= deadline_date:
            day = probe.date()
            # Check “allowed_days” if provided (weekday codes: Monday=0, …)
            if allowed_days:
                weekday_code = ["MO","TU","WE","TH","FR","SA","SU"][day.weekday()]
                if weekday_code not in allowed_days:
                    # skip that day
                    probe = (datetime.combine(day + timedelta(days=1), time(0,0), tzinfo=LOCAL_TZ)
                             .replace(hour=WORK_START, minute=0))
                    continue

            # figure how many hours we’ve scheduled so far on 'day'
            used_hours = day_hours.get(day, 0.0)
            if max_hours_per_day is not None and used_hours >= max_hours_per_day:
                # no more hours left that day
                probe = (datetime.combine(day + timedelta(days=1), time(0,0), tzinfo=LOCAL_TZ)
                         .replace(hour=WORK_START, minute=0))
                continue

            # Build this day’s busy slices clipped to working hours
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

            # carve out free windows within [start_window..end_window]
            free_windows = []
            cursor = start_window
            for bs, be in day_busy:
                if bs > cursor:
                    free_windows.append((cursor, bs))
                cursor = max(cursor, be)
            if cursor < end_window:
                free_windows.append((cursor, end_window))

            # how many hours remain allowed in that day?
            hours_left = None
            if max_hours_per_day is not None:
                hours_left = max_hours_per_day - used_hours
                if hours_left <= 0:
                    # no hours left => skip day
                    probe = (datetime.combine(day + timedelta(days=1), time(0,0), tzinfo=LOCAL_TZ)
                             .replace(hour=WORK_START, minute=0))
                    continue

            # pick the first free window large enough to fit 'duration'
            for ws, we in free_windows:
                free_len = we - ws
                # if “hours_left” is present, do not exceed it:
                if hours_left is not None:
                    allowed_slot_len = timedelta(hours=hours_left)
                    if free_len >= duration and duration <= allowed_slot_len:
                        slot = (ws, ws + duration)
                        break
                else:
                    if free_len >= duration:
                        slot = (ws, ws + duration)
                        break

            if slot:
                break

            # Move to next day at WORK_START
            next_day = datetime.combine(
                day + timedelta(days=1),
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

        # Increment day_hours
        d = slot[0].date()
        hrs = float((slot[1] - slot[0]).total_seconds() / 3600.0)
        day_hours[d] = day_hours.get(d, 0.0) + hrs

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
