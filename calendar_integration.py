# calendar_integration.py

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from googleapiclient.errors import HttpError

# ─── Constants ────────────────────────────────────────────────────────────────
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
WORK_START = 9      # 9 AM fallback window
WORK_END   = 22     # 10 PM fallback window

DEFAULT_MAX_TASKS_PER_DAY = 2

CODE_TO_WEEKDAY = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6
}

PREFERRED_WINDOWS = {
    "morning":   (8, 12),   #  8:00–12:00
    "noon":      (12, 14),  # 12:00–14:00
    "afternoon": (14, 18),  # 14:00–18:00
    "night":     (18, 22)   # 18:00–22:00
}


def schedule_tasks(
    service,
    tasks,
    start_iso,
    deadline_iso,
    max_per_day=None,
    allowed_days=None,
    preferred_time=None
):
    """
    - If the user’s calendar is completely empty (no busy slots), schedule each task
      in the user’s “preferred_time” window (e.g. 8–12 for morning) on allowed weekdays.
    - Otherwise, carve out free slots between WORK_START (9 AM) and WORK_END (10 PM),
      respecting max_per_day and allowed_days.
    """
    # 1) Parse scheduling window
    dt_start = datetime.fromisoformat(start_iso).astimezone(LOCAL_TZ)
    deadline_date = datetime.fromisoformat(deadline_iso).date()

    # Determine daily limit
    daily_limit = max_per_day or DEFAULT_MAX_TASKS_PER_DAY

    # Build allowed‐weekday set
    if allowed_days:
        allowed_indices = { CODE_TO_WEEKDAY.get(code) for code in allowed_days if code in CODE_TO_WEEKDAY }
    else:
        allowed_indices = {0,1,2,3,4}  # default Mon–Fri

    # 2) Free/Busy query
    window_end = datetime.combine(
        deadline_date + timedelta(days=1),
        time(0,0),
        tzinfo=LOCAL_TZ
    )
    fb_req = {
        "timeMin": dt_start.isoformat(),
        "timeMax": window_end.isoformat(),
        "timeZone": str(LOCAL_TZ),
        "items": [{"id": "primary"}]
    }
    busy = []
    try:
        resp = service.freebusy().query(body=fb_req).execute()
        periods = resp["calendars"]["primary"]["busy"]
        for p in periods:
            bs = datetime.fromisoformat(p["start"]).astimezone(LOCAL_TZ)
            be = datetime.fromisoformat(p["end"]).astimezone(LOCAL_TZ)
            busy.append((bs, be))
    except HttpError:
        # Treat as “empty calendar” if freebusy fails
        busy = []

    # If calendar is empty and user selected a valid preferred_time, use that logic
    if not busy and preferred_time in PREFERRED_WINDOWS:
        return _schedule_by_preferred_time(
            tasks, dt_start, deadline_date,
            daily_limit, allowed_indices, preferred_time
        )
    else:
        return _schedule_by_freebusy(
            tasks, dt_start, deadline_date,
            daily_limit, allowed_indices, busy
        )


def _schedule_by_preferred_time(
    tasks,
    dt_start,
    deadline_date,
    daily_limit,
    allowed_indices,
    preferred_time
):
    """
    Place each task strictly within the user’s chosen window (e.g. 8–12 for morning),
    up to daily_limit on allowed weekdays. If a task can’t fit before the deadline, it’s unscheduled.
    """
    scheduled = []
    unscheduled = []
    day_counts = {}

    window_start, window_end = PREFERRED_WINDOWS[preferred_time]

    # Helper to get next allowed date
    def next_allowed(d):
        while d.weekday() not in allowed_indices:
            d = d + timedelta(days=1)
        return d

    current_date = next_allowed(dt_start.date())

    # Starting cursor: clamp to window_start on that date (or if dt_start is later but still before window_end, use dt_start)
    if current_date == dt_start.date() and dt_start.hour >= window_start and dt_start.hour < window_end:
        cursor = dt_start
    else:
        cursor = datetime.combine(current_date, time(hour=window_start, minute=0), tzinfo=LOCAL_TZ)

    for t in tasks:
        duration = timedelta(hours=float(t.get("duration_hours", 1.0)))
        placed = False

        while current_date <= deadline_date:
            if current_date.weekday() in allowed_indices:
                if day_counts.get(current_date, 0) < daily_limit:
                    day_window_end = datetime.combine(current_date, time(hour=window_end, minute=0), tzinfo=LOCAL_TZ)
                    if cursor + duration <= day_window_end:
                        # Place it here
                        scheduled.append({
                            "summary": t["task"],
                            "start":   cursor.isoformat(),
                            "end":     (cursor + duration).isoformat()
                        })
                        day_counts[current_date] = day_counts.get(current_date, 0) + 1
                        cursor = cursor + duration
                        placed = True
                        break

            # Move to the next allowed weekday at window_start
            next_day = next_allowed(current_date + timedelta(days=1))
            if next_day > deadline_date:
                break
            current_date = next_day
            cursor = datetime.combine(current_date, time(hour=window_start, minute=0), tzinfo=LOCAL_TZ)

        if not placed:
            unscheduled.append({"id": t["id"], "task": t["task"]})

    return scheduled, unscheduled


def _schedule_by_freebusy(
    tasks,
    dt_start,
    deadline_date,
    daily_limit,
    allowed_indices,
    busy
):
    """
    Original approach: carve out free windows between WORK_START and WORK_END 
    on each allowed weekday and place tasks in the first spot that fits.
    """
    scheduled = []
    unscheduled = []
    day_counts = {}

    # Start probing at WORK_START on dt_start’s date (or later if dt_start is after 9 AM)
    probe = dt_start.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
    if dt_start.hour > WORK_START:
        probe = dt_start

    def next_allowed_probe(dt_obj):
        nxt = datetime.combine(
            dt_obj.date() + timedelta(days=1),
            time(hour=WORK_START, minute=0),
            tzinfo=LOCAL_TZ
        )
        while nxt.weekday() not in allowed_indices:
            nxt = nxt + timedelta(days=1)
            nxt = nxt.replace(hour=WORK_START, minute=0)
        return nxt

    for t in tasks:
        duration = timedelta(hours=float(t.get("duration_hours", 1.0)))
        slot = None

        while probe.date() <= deadline_date:
            if probe.weekday() in allowed_indices:
                day = probe.date()
                if day_counts.get(day, 0) < daily_limit:
                    day_start = probe.replace(hour=WORK_START, minute=0)
                    day_end   = probe.replace(hour=WORK_END,   minute=0)

                    # Build that day’s busy slices
                    day_busy = []
                    for bs, be in busy:
                        if bs.date() <= day <= be.date():
                            seg_start = max(bs, day_start)
                            seg_end   = min(be, day_end)
                            if seg_start < seg_end:
                                day_busy.append((seg_start, seg_end))
                    day_busy.sort(key=lambda x: x[0])

                    # Carve out free windows
                    free = []
                    cursor_fb = day_start
                    for bs, be in day_busy:
                        if bs > cursor_fb:
                            free.append((cursor_fb, bs))
                        cursor_fb = max(cursor_fb, be)
                    if cursor_fb < day_end:
                        free.append((cursor_fb, day_end))

                    # Find first free window that fits
                    for ws, we in free:
                        if (we - ws) >= duration:
                            slot = (ws, ws + duration)
                            break

            if slot:
                break

            probe = next_allowed_probe(probe)

        if not slot:
            unscheduled.append({"id": t["id"], "task": t["task"]})
        else:
            scheduled.append({
                "summary": t["task"],
                "start":   slot[0].isoformat(),
                "end":     slot[1].isoformat()
            })
            busy.append(slot)
            day_counts[slot[0].date()] = day_counts.get(slot[0].date(), 0) + 1
            probe = slot[1]

    return scheduled, unscheduled


def create_calendar_events(service, scheduled):
    """
    For each dict in `scheduled` (having "summary", "start", "end"), insert it into Google Calendar.
    Return a list of created event IDs.
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
