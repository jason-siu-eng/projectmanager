from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from googleapiclient.errors import HttpError

# Constants
LOCAL_TZ    = ZoneInfo("America/Los_Angeles")
WORK_START  = 9   #  9:00 AM
WORK_END    = 22  # 10:00 PM

def schedule_tasks(
    service,
    tasks,
    start_iso,
    deadline_iso,
    max_hours_per_day=None,
    allowed_days_of_week=None
):
    """
    service: authorized Google Calendar service
    tasks:   [ {"id":…, "task":…, "duration_hours":…}, … ]
    start_iso:    ISO timestamp string when scheduling may begin (ignored—always tomorrow)
    deadline_iso: ISO date string ("YYYY-MM-DD") by which all tasks must be scheduled
    max_hours_per_day: (float) how many total hours of tasks may be placed on any given day
    allowed_days_of_week: list of strings, e.g. ["MO","TU","WE","TH","FR"], or None

    Returns:
      scheduled:   [ {"summary":…, "start":iso, "end":iso}, … ]
      unscheduled: [ {"id":…, "task":…}, … ]
    """

    # 1) Always start at WORK_START tomorrow in LOCAL_TZ
    now      = datetime.fromisoformat(start_iso).astimezone(LOCAL_TZ)
    tomorrow = now.date() + timedelta(days=1)
    dt       = datetime.combine(
                 tomorrow,
                 time(WORK_START, 0),
                 tzinfo=LOCAL_TZ
               )

    # 2) Parse deadline
    try:
        deadline_date = datetime.fromisoformat(deadline_iso).date()
    except Exception:
        deadline_date = datetime.utcnow().date() + timedelta(days=7)

    # We want up through midnight after the deadline date
    time_max = datetime.combine(
        deadline_date + timedelta(days=1),
        time(0, 0),
        tzinfo=LOCAL_TZ
    )

    # 3) Fetch existing busy slots
    fb_req = {
        "timeMin": dt.isoformat(),
        "timeMax": time_max.isoformat(),
        "timeZone": str(LOCAL_TZ),
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
        print("⚠️ free/busy lookup failed:", e)

    scheduled, unscheduled = [], []

    # 4) Track used hours per day
    day_hours: dict[date, float] = {}

    # 5) Build allowed weekdays set
    weekday_map = {"MO":0, "TU":1, "WE":2, "TH":3, "FR":4, "SA":5, "SU":6}
    if allowed_days_of_week:
        allowed_weekdays = { weekday_map[d] for d in allowed_days_of_week if d in weekday_map }
    else:
        allowed_weekdays = set(range(7))

    # 6) Schedule each task
    for t in tasks:
        duration = timedelta(hours=float(t.get("duration_hours", 1.0)))
        slot     = None
        probe    = dt

        while probe.date() <= deadline_date:
            day   = probe.date()
            wkday = day.weekday()

            if wkday in allowed_weekdays:
                used = day_hours.get(day, 0.0)
                if max_hours_per_day is None or (used + duration.total_seconds()/3600) <= max_hours_per_day:
                    # Build this day's busy slices clipped to WORK_START–WORK_END
                    window_start = probe.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
                    window_end   = probe.replace(hour=WORK_END,   minute=0, second=0, microsecond=0)

                    # Clip busy intervals into this window
                    day_busy = sorted(
                        ( (max(window_start, bs), min(window_end, be))
                          for bs, be in busy
                          if bs.date() <= day <= be.date() ),
                        key=lambda x: x[0]
                    )

                    # Carve free windows
                    free_windows, cursor = [], window_start
                    for bs, be in day_busy:
                        if bs > cursor:
                            free_windows.append((cursor, bs))
                        cursor = max(cursor, be)
                    if cursor < window_end:
                        free_windows.append((cursor, window_end))

                    # Pick the first that fits
                    for ws, we in free_windows:
                        if (we - ws) >= duration:
                            if max_hours_per_day is None or (used + duration.total_seconds()/3600) <= max_hours_per_day:
                                slot = (ws, ws + duration)
                                break

            if slot:
                break

            # Move to next day at WORK_START
            next_midnight = datetime.combine(
                day + timedelta(days=1),
                time(0, 0),
                tzinfo=LOCAL_TZ
            )
            probe = next_midnight.replace(hour=WORK_START, minute=0, second=0, microsecond=0)

        if not slot:
            unscheduled.append({"id": t["id"], "task": t["task"]})
            continue

        # Record and mark busy
        scheduled.append({
            "summary": t["task"],
            "start":   slot[0].isoformat(),
            "end":     slot[1].isoformat()
        })
        busy.append(slot)

        # Update hours used
        day_key     = slot[0].date()
        day_hours[day_key] = day_hours.get(day_key, 0.0) + duration.total_seconds()/3600

        # Next probe starts after this task
        dt = slot[1]

    return scheduled, unscheduled


def create_calendar_events(service, scheduled):
    """
    Inserts scheduled slots into Google Calendar and returns their IDs.
    """
    ids = []
    for ev in scheduled:
        body = {
            "summary": ev["summary"],
            "start":   {"dateTime": ev["start"], "timeZone": str(LOCAL_TZ)},
            "end":     {"dateTime": ev["end"],   "timeZone": str(LOCAL_TZ)},
        }
        created = service.events().insert(calendarId="primary", body=body).execute()
        ids.append(created.get("id"))
    return ids
