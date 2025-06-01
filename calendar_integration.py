# calendar_integration.py

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from googleapiclient.errors import HttpError

# ─── Constants ────────────────────────────────────────────────────────────────
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
WORK_START = 9      # 9 AM fallback window
WORK_END   = 22     # 10 PM fallback window
BUFFER     = timedelta(hours=1)  # 1-hour break between events

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

# Preferred-time windows (start_hour, end_hour)
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
    Schedule tasks into Google Calendar under these rules:
      1) Always begin ON THE NEXT DAY after `start_iso` (never use current day).
      2) If the calendar is empty (no busy slots), and `preferred_time` is valid,
         place each task in that chosen window (e.g. 8–12 for morning) on allowed weekdays,
         with at least a one-hour BUFFER between events.
      3) If there are any busy slots, fall back to “free/busy carve-out” between
         WORK_START (9 AM) and WORK_END (10 PM), ensuring BUFFER between events.
    """
    # 1) Parse start date, then shift to next calendar day
    original_dt = datetime.fromisoformat(start_iso).astimezone(LOCAL_TZ)
    next_day_dt = (original_dt + timedelta(days=1)).replace(
        hour=WORK_START, minute=0, second=0, microsecond=0
    )

    # 2) Parse deadline date
    deadline_date = datetime.fromisoformat(deadline_iso).date()

    # 3) Determine daily limit
    daily_limit = max_per_day if (max_per_day is not None) else DEFAULT_MAX_TASKS_PER_DAY

    # 4) Build set of allowed weekday indices
    if allowed_days:
        allowed_indices = { CODE_TO_WEEKDAY.get(code) for code in allowed_days if code in CODE_TO_WEEKDAY }
    else:
        # Default to Monday–Friday
        allowed_indices = {0, 1, 2, 3, 4}

    # 5) Free/Busy query from next_day_dt to day after deadline
    window_end = datetime.combine(
        deadline_date + timedelta(days=1),
        time(0, 0),
        tzinfo=LOCAL_TZ
    )
    fb_req = {
        "timeMin": next_day_dt.isoformat(),
        "timeMax": window_end.isoformat(),
        "timeZone": str(LOCAL_TZ),
        "items": [{"id": "primary"}]
    }

    busy = []
    try:
        resp = service.freebusy().query(body=fb_req).execute()
        for period in resp["calendars"]["primary"]["busy"]:
            bs = datetime.fromisoformat(period["start"]).astimezone(LOCAL_TZ)
            be = datetime.fromisoformat(period["end"]).astimezone(LOCAL_TZ)
            busy.append((bs, be))
    except HttpError:
        # If freebusy fails, treat as completely empty calendar
        busy = []

    # 6) If no busy slots AND preferred_time is valid, use preferred-time logic
    if not busy and preferred_time in PREFERRED_WINDOWS:
        return _schedule_by_preferred_time(
            tasks,
            next_day_dt,
            deadline_date,
            daily_limit,
            allowed_indices,
            preferred_time
        )

    # 7) Otherwise, use “freebusy carve-out” logic
    return _schedule_by_freebusy(
        tasks,
        next_day_dt,
        deadline_date,
        daily_limit,
        allowed_indices,
        busy
    )


def _schedule_by_preferred_time(
    tasks,
    start_dt,
    deadline_date,
    daily_limit,
    allowed_indices,
    preferred_time
):
    """
    Place each task strictly within the user’s chosen window (e.g. 8–12 for morning)
    on allowed weekdays. Always begins at start_dt (which is already next-day at WORK_START).
    Ensures a 1-hour BUFFER between scheduled tasks.
    """
    scheduled = []
    unscheduled = []
    day_counts = {}

    window_start_hour, window_end_hour = PREFERRED_WINDOWS[preferred_time]

    # Helper: find the next allowed date (including the given date) >= dt_obj.date()
    def next_allowed_date_from(dt_obj):
        d = dt_obj.date()
        while d.weekday() not in allowed_indices:
            d = d + timedelta(days=1)
        return d

    # 1) Find the first allowed date >= start_dt
    current_date = next_allowed_date_from(start_dt)

    # 2) Determine initial cursor:
    #    a) If start_dt is already within the window, use start_dt, else clamp to window start
    if (current_date == start_dt.date() and
        window_start_hour <= start_dt.hour < window_end_hour):
        cursor = start_dt
    else:
        cursor = datetime.combine(
            current_date,
            time(hour=window_start_hour, minute=0),
            tzinfo=LOCAL_TZ
        )

    for t in tasks:
        duration = timedelta(hours=float(t.get("duration_hours", 1.0)))
        placed = False

        while current_date <= deadline_date:
            # 3) Only attempt if weekday is allowed
            if current_date.weekday() in allowed_indices:
                count_for_day = day_counts.get(current_date, 0)
                if count_for_day < daily_limit:
                    # Build this day’s window end
                    day_window_end = datetime.combine(
                        current_date,
                        time(hour=window_end_hour, minute=0),
                        tzinfo=LOCAL_TZ
                    )
                    # If cursor is before the window start, clamp it
                    earliest_allowed = datetime.combine(
                        current_date,
                        time(hour=window_start_hour, minute=0),
                        tzinfo=LOCAL_TZ
                    )
                    if cursor < earliest_allowed:
                        cursor = earliest_allowed

                    # 4) Check if task fits before window_end (we do not need buffer for the last task)
                    if cursor + duration <= day_window_end:
                        slot_start = cursor
                        slot_end   = cursor + duration

                        scheduled.append({
                            "summary": t["task"],
                            "start":   slot_start.isoformat(),
                            "end":     slot_end.isoformat()
                        })
                        day_counts[current_date] = count_for_day + 1

                        # 5) Move cursor past end + BUFFER (1-hour gap)
                        cursor = slot_end + BUFFER
                        placed = True
                        break

            # 6) Move to the next allowed date at window_start
            next_date = current_date + timedelta(days=1)
            next_date = next_allowed_date_from(datetime.combine(next_date, time(0, 0), tzinfo=LOCAL_TZ))
            if next_date > deadline_date:
                break
            current_date = next_date
            cursor = datetime.combine(
                current_date,
                time(hour=window_start_hour, minute=0),
                tzinfo=LOCAL_TZ
            )

        if not placed:
            unscheduled.append({"id": t["id"], "task": t["task"]})

    return scheduled, unscheduled


def _schedule_by_freebusy(
    tasks,
    start_dt,
    deadline_date,
    daily_limit,
    allowed_indices,
    busy
):
    """
    Original freebusy carve-out:
      - Begin at start_dt (already next day at 9 AM).
      - For each task, scan allowed weekdays day by day, carve out free windows
        between 9 AM and 10 PM, and place the task in the first fitting slot.
      - Ensure a 1-hour BUFFER after each placed task by marking that time as busy.
    """
    scheduled = []
    unscheduled = []
    day_counts = {}

    # 1) Initialize probe at 9 AM of start_dt’s date (or keep start_dt if later than 9 AM)
    probe = start_dt
    if probe.hour < WORK_START:
        probe = probe.replace(hour=WORK_START, minute=0, second=0, microsecond=0)

    def next_allowed_probe(dt_obj):
        nxt = datetime.combine(
            dt_obj.date() + timedelta(days=1),
            time(hour=WORK_START, minute=0),
            tzinfo=LOCAL_TZ
        )
        while nxt.weekday() not in allowed_indices:
            nxt = nxt + timedelta(days=1)
            nxt = nxt.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
        return nxt

    for t in tasks:
        duration = timedelta(hours=float(t.get("duration_hours", 1.0)))
        slot = None

        while probe.date() <= deadline_date:
            if probe.weekday() in allowed_indices:
                day = probe.date()
                if day_counts.get(day, 0) < daily_limit:
                    day_start = probe.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
                    day_end   = probe.replace(hour=WORK_END,   minute=0, second=0, microsecond=0)

                    # 2) Build that day’s busy slices within [day_start, day_end]
                    day_busy = []
                    for bs, be in busy:
                        if bs.date() <= day <= be.date():
                            seg_start = max(bs, day_start)
                            seg_end   = min(be, day_end)
                            if seg_start < seg_end:
                                day_busy.append((seg_start, seg_end))
                    day_busy.sort(key=lambda x: x[0])

                    # 3) Carve out free windows
                    free_windows = []
                    cursor_fb = day_start
                    for bs, be in day_busy:
                        if bs > cursor_fb:
                            free_windows.append((cursor_fb, bs))
                        cursor_fb = max(cursor_fb, be)
                    if cursor_fb < day_end:
                        free_windows.append((cursor_fb, day_end))

                    # 4) Pick the first free window that fits the task
                    for ws, we in free_windows:
                        if (we - ws) >= duration:
                            slot = (ws, ws + duration)
                            break

            if slot:
                break

            probe = next_allowed_probe(probe)

        if not slot:
            unscheduled.append({"id": t["id"], "task": t["task"]})
        else:
            slot_start, slot_end = slot
            scheduled.append({
                "summary": t["task"],
                "start":   slot_start.isoformat(),
                "end":     slot_end.isoformat()
            })
            # 5) Mark event + BUFFER as busy for subsequent iterations
            busy.append((slot_start, slot_end + BUFFER))
            day_counts[slot_start.date()] = day_counts.get(slot_start.date(), 0) + 1
            probe = slot_end + BUFFER  # next search begins after the buffer

    return scheduled, unscheduled


def create_calendar_events(service, scheduled):
    """
    Insert each event in `scheduled` into the user’s primary calendar.
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
