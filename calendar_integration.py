# calendar_integration.py

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from googleapiclient.errors import HttpError

# Constants
LOCAL_TZ           = ZoneInfo("America/Los_Angeles")
WORK_START         = 9    #  9:00 AM
WORK_END           = 22   # 10:00 PM

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
    start_iso:    ISO timestamp string when scheduling may begin (e.g. now)
    deadline_iso: ISO date string ("YYYY-MM-DD") by which all tasks must be scheduled
    max_hours_per_day: (float) how many total hours of tasks may be placed on any given day
    allowed_days_of_week: list of strings, e.g. ["MO","TU","WE","TH","FR"], or None

    Returns:
      scheduled:   [ {"summary":…, "start":iso, "end":iso}, … ]
      unscheduled: [ {"id":…, "task":…}, … ]
    """

    # 1) Parse our scheduling window
    dt = datetime.fromisoformat(start_iso).astimezone(LOCAL_TZ)
    # Force starting at WORK_START of today (or next earliest)
    dt = dt.replace(hour=WORK_START, minute=0, second=0, microsecond=0)

    try:
        deadline_date = datetime.fromisoformat(deadline_iso).date()
    except Exception:
        # if parsing fails, assume 7 days from today
        deadline_date = (datetime.utcnow().date() + timedelta(days=7))

    # We want everything up to midnight after the deadline date
    time_max = datetime.combine(
        deadline_date + timedelta(days=1),
        time(0, 0),
        tzinfo=LOCAL_TZ
    )

    # 2) Fetch existing busy slots from Google
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

    # 3) Keep track of how many hours are already scheduled on each day
    #    (weekday → total hours used)
    day_hours: dict[date, float] = {}

    # 4) Prepare allowed weekdays set
    #    Convert ["MO","TU","WE",…] to {0,1,2,…} where Monday=0, Sunday=6
    weekday_map = {"MO":0, "TU":1, "WE":2, "TH":3, "FR":4, "SA":5, "SU":6}
    if allowed_days_of_week:
        allowed_weekdays = { weekday_map[d] for d in allowed_days_of_week if d in weekday_map }
    else:
        # If not provided, allow all seven days
        allowed_weekdays = set(range(7))

    # 5) For each task, find an open slot respecting max_hours_per_day and allowed_weekdays
    for t in tasks:
        duration = timedelta(hours=float(t.get("duration_hours", 1.0)))
        slot     = None
        probe    = dt

        # Walk day by day until deadline
        while probe.date() <= deadline_date:
            current_day = probe.date()
            weekday     = current_day.weekday()

            # 5a) Only schedule on allowed weekdays
            if weekday in allowed_weekdays:
                # 5b) Check how many hours are already used on this day
                already_used = day_hours.get(current_day, 0.0)
                # If max_hours_per_day is not None, ensure adding this task won't exceed it
                if max_hours_per_day is None or (already_used + duration.total_seconds()/3600) <= max_hours_per_day:
                    # Build this day’s busy slices, clipped to WORK_START–WORK_END
                    start_window = probe.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
                    end_window   = probe.replace(hour=WORK_END,   minute=0, second=0, microsecond=0)

                    # 5c) Collect all busy intervals that overlap this day and clip them
                    day_busy = sorted(
                        (
                            (max(start_window, bs), min(end_window, be))
                            for (bs, be) in busy
                            if bs.date() <= current_day <= be.date()
                        ),
                        key=lambda x: x[0]
                    )

                    # 5d) Carve out free windows for this day
                    free_windows = []
                    cursor = start_window
                    for (bs, be) in day_busy:
                        if bs > cursor:
                            free_windows.append((cursor, bs))
                        cursor = max(cursor, be)
                    if cursor < end_window:
                        free_windows.append((cursor, end_window))

                    # 5e) Pick the first free window that can fit the entire duration
                    for (ws, we) in free_windows:
                        if (we - ws) >= duration:
                            # We have a candidate: check again that by placing it here,
                            # we are still not exceeding max_hours_per_day on this day.
                            # (Because some free windows might be long enough, but day-hours already near limit.)
                            free_span_hours = (we - ws).total_seconds()/3600
                            # But we only need 'duration' hours, so if already_used + duration <= max_hours_per_day → OK
                            if max_hours_per_day is None or (already_used + duration.total_seconds()/3600) <= max_hours_per_day:
                                slot = (ws, ws + duration)
                                break
                    # end for free_windows

            # If we found a slot, break out of the loop
            if slot:
                break

            # Otherwise move to next day at WORK_START
            next_day_midnight = datetime.combine(
                current_day + timedelta(days=1),
                time(0,0),
                tzinfo=LOCAL_TZ
            )
            probe = next_day_midnight.replace(hour=WORK_START, minute=0, second=0, microsecond=0)

        # end while probe

        if not slot:
            # Could not place this task anywhere → unscheduled
            unscheduled.append({"id": t["id"], "task": t["task"]})
            continue

        # Otherwise, record the scheduled event
        scheduled.append({
            "summary": t["task"],
            "start":   slot[0].isoformat(),
            "end":     slot[1].isoformat()
        })

        # Mark that time as busy for subsequent tasks
        busy.append(slot)

        # Update how many hours we've used on that day
        day_key = slot[0].date()
        used_hours = day_hours.get(day_key, 0.0)
        used_hours += duration.total_seconds()/3600
        day_hours[day_key] = used_hours

        # Next probe begins right after this task’s end
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
