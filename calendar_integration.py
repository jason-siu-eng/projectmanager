# calendar_integration.py

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from googleapiclient.errors import HttpError

# Constants
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
WORK_START = 9      # 9 AM (fallback freebusy window)
WORK_END   = 22     # 10 PM (fallback freebusy window)

# Default daily maximum if user doesn't override
DEFAULT_MAX_TASKS_PER_DAY = 2

# Map from two-letter code to Python weekday integer
CODE_TO_WEEKDAY = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6
}

# Define “preferred time” windows (start_hour, end_hour)
PREFERRED_WINDOWS = {
    "morning":   (8, 12),  # 8 AM → 12 PM
    "noon":      (12, 14), # 12 PM → 2 PM
    "afternoon": (14, 18), # 2 PM → 6 PM
    "night":     (18, 22)  # 6 PM → 10 PM
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
    Schedule tasks into Google Calendar.

    Args:
      service: authorized Google Calendar API service
      tasks: list of dicts, each with {"id":…, "task":…, "duration_hours":…}
      start_iso: ISO timestamp string to begin scheduling (e.g. "2025-05-31T08:00:00Z")
      deadline_iso: ISO date string (YYYY-MM-DD) by which tasks must be placed
      max_per_day: int override for maximum events per day
      allowed_days: list of two-letter weekday codes, e.g. ["MO","TU","WE","TH","FR"]
      preferred_time: one of "morning","noon","afternoon","night"
    Returns:
      scheduled:   list of dicts { "summary":…, "start":iso, "end":iso, … }
      unscheduled: list of dicts { "id":…, "task":… }
    """

    # 1) Parse the scheduling window
    dt_start = datetime.fromisoformat(start_iso).astimezone(LOCAL_TZ)

    # Always begin at the user’s WORK_START or the next allowed slot:
    # We'll override if we’re in preferred_time logic.
    # But store the “day” for initial probe:
    deadline_date = datetime.fromisoformat(deadline_iso).date()

    # Determine daily limit
    daily_limit = max_per_day if (max_per_day is not None) else DEFAULT_MAX_TASKS_PER_DAY

    # Build set of allowed weekday indices
    if allowed_days:
        allowed_indices = { CODE_TO_WEEKDAY.get(code) for code in allowed_days if code in CODE_TO_WEEKDAY }
    else:
        # default to Mon–Fri
        allowed_indices = {0, 1, 2, 3, 4}

    # -----------------------
    # 2) Free/Busy query
    # -----------------------
    # We ask Google for all busy slots between dt_start and (deadline+1d) at 00:00
    window_end = datetime.combine(
        deadline_date + timedelta(days=1),
        time(0,0),
        tzinfo=LOCAL_TZ
    )
    fb_req = {
        "timeMin": dt_start.isoformat(),
        "timeMax": window_end.isoformat(),
        "timeZone": str(LOCAL_TZ),
        "items": [{ "id": "primary" }]
    }

    busy = []
    try:
        resp = service.freebusy().query(body=fb_req).execute()
        busy_periods = resp["calendars"]["primary"]["busy"]
        for period in busy_periods:
            bs = datetime.fromisoformat(period["start"]).astimezone(LOCAL_TZ)
            be = datetime.fromisoformat(period["end"]).astimezone(LOCAL_TZ)
            busy.append((bs, be))
    except HttpError:
        # If the Free/Busy call fails, treat the calendar as “empty”
        busy = []

    # If busy is empty → no existing events → use “preferred_time” logic.
    # Otherwise, use original free/busy algorithm.
    if not busy and preferred_time in PREFERRED_WINDOWS:
        return _schedule_by_preferred_time(
            tasks,
            dt_start,
            deadline_date,
            daily_limit,
            allowed_indices,
            preferred_time
        )
    else:
        return _schedule_by_freebusy(
            tasks,
            dt_start,
            deadline_date,
            daily_limit,
            allowed_indices,
            busy
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
    If there are no busy slots, place each task strictly in the user's chosen
    preferred window (morning/noon/afternoon/night), respecting max_per_day and allowed weekdays.
    """
    scheduled = []
    unscheduled = []
    day_counts = {}

    # Extract hour‐range for that preference
    window_start_hour, window_end_hour = PREFERRED_WINDOWS[preferred_time]

    # Helper to find next allowed day from a given date
    def next_allowed_date(from_date):
        d = from_date
        while True:
            if d.weekday() in allowed_indices:
                return d
            d = d + timedelta(days=1)

    # Start probing from the first allowed day ≥ dt_start.date()
    start_date = dt_start.date()

    # If dt_start occurs after the chosen window start, we clamp it inside that window.
    # Otherwise, we begin exactly at window_start_hour on that date.
    current_day = start_date
    # Move to the next allowed weekday if needed
    current_day = next_allowed_date(current_day)

    def get_initial_cursor_for(date_obj):
        # Default to HH:00 on that date
        base = datetime.combine(date_obj, time(hour=window_start_hour, minute=0), tzinfo=LOCAL_TZ)
        # If dt_start is later than that, clamp into that day
        if date_obj == dt_start.date() and dt_start.hour >= window_start_hour:
            # if dt_start < window_end, we can start at max(dt_start, window_start)
            if dt_start.hour < window_end_hour:
                return dt_start
        return base

    cursor = get_initial_cursor_for(current_day)

    for t in tasks:
        duration = timedelta(hours=float(t.get("duration_hours", 1.0)))
        placed = False

        while current_day <= deadline_date:
            # Check if this weekday is allowed
            wd = current_day.weekday()
            if wd in allowed_indices:
                # Count how many tasks we've already placed on that day
                placed_count = day_counts.get(current_day, 0)
                # Compute window_end datetime for this day
                day_end = datetime.combine(current_day, time(hour=window_end_hour, minute=0), tzinfo=LOCAL_TZ)

                # If we still have slots left and this task fits inside the window
                if (placed_count < daily_limit) and (cursor + duration <= day_end):
                    # We can place this task at (cursor → cursor + duration)
                    slot_start = cursor
                    slot_end   = cursor + duration

                    scheduled.append({
                        "summary": t["task"],
                        "start":   slot_start.isoformat(),
                        "end":     slot_end.isoformat()
                    })

                    # Mark it as busy for the remainder of this “preferred window” logic
                    day_counts[current_day] = placed_count + 1
                    # Advance cursor by this task's duration
                    cursor = slot_end
                    placed = True
                    break

            # Otherwise, move to next allowed day at window_start
            next_day = next_allowed_date(current_day + timedelta(days=1))
            if next_day > deadline_date:
                # We’ve run out of days
                break
            current_day = next_day
            # Reset cursor at that next day’s window start
            cursor = datetime.combine(current_day, time(hour=window_start_hour, minute=0), tzinfo=LOCAL_TZ)

        if not placed:
            unscheduled.append({ "id": t["id"], "task": t["task"] })

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
    Original free/busy approach: for each task, scan day by day (Mon–Fri by default),
    carve out free windows within [WORK_START..WORK_END], and place the task in the first slot that fits.
    """
    scheduled = []
    unscheduled = []
    day_counts = {}

    # Begin at WORK_START on the date of dt_start, unless dt_start is later
    probe = dt_start.replace(hour=WORK_START, minute=0, second=0, microsecond=0)

    def next_allowed_datetime(dt_obj):
        # Move to next day's WORK_START, skipping disallowed weekdays
        next_day = datetime.combine(
            dt_obj.date() + timedelta(days=1),
            time(0, 0),
            tzinfo=LOCAL_TZ
        )
        # Advance to WORK_START hour
        next_day = next_day.replace(hour=WORK_START, minute=0, second=0, microsecond=0)

        # Skip until an allowed weekday
        while next_day.weekday() not in allowed_indices:
            next_day = next_day + timedelta(days=1)
            next_day = next_day.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
        return next_day

    for t in tasks:
        duration = timedelta(hours=float(t.get("duration_hours", 1.0)))
        slot = None

        while probe.date() <= deadline_date:
            # Only schedule on allowed weekdays
            if probe.weekday() in allowed_indices:
                day = probe.date()
                if day_counts.get(day, 0) < daily_limit:
                    # Build this day's WORK_START .. WORK_END window
                    day_start = probe.replace(hour=WORK_START, minute=0)
                    day_end   = probe.replace(hour=WORK_END,   minute=0)

                    # Collect busy slices that overlap this day
                    day_busy = []
                    for bs, be in busy:
                        if bs.date() <= day <= be.date():
                            seg_start = max(bs, day_start)
                            seg_end   = min(be, day_end)
                            if seg_start < seg_end:
                                day_busy.append((seg_start, seg_end))
                    day_busy.sort(key=lambda x: x[0])

                    # Carve out free windows that day
                    free_windows = []
                    cursor_fb = day_start
                    for bs, be in day_busy:
                        if bs > cursor_fb:
                            free_windows.append((cursor_fb, bs))
                        cursor_fb = max(cursor_fb, be)
                    if cursor_fb < day_end:
                        free_windows.append((cursor_fb, day_end))

                    # Find first free window that can fit this task
                    for ws, we in free_windows:
                        if (we - ws) >= duration:
                            slot = (ws, ws + duration)
                            break

            if slot:
                break

            # Otherwise, move to next allowed day at WORK_START
            probe = next_allowed_datetime(probe)

        # If we never found a slot, mark as unscheduled
        if not slot:
            unscheduled.append({ "id": t["id"], "task": t["task"] })
            continue

        # Record this event
        scheduled.append({
            "summary": t["task"],
            "start":   slot[0].isoformat(),
            "end":     slot[1].isoformat()
        })

        # Mark that time as busy (so subsequent tasks won’t double-book)
        busy.append(slot)
        day_counts[slot[0].date()] = day_counts.get(slot[0].date(), 0) + 1

        # Next probe starts immediately after this slot
        probe = slot[1]

    return scheduled, unscheduled
