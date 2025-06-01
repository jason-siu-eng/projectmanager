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


def schedule_tasks(
    service,
    tasks,
    start_iso,
    deadline_iso,
    max_per_day=None,
    allowed_days=None
):
    """
    Always shift scheduling to “tomorrow at 9 AM.” Then carve out free windows
    between 9 AM and 10 PM on allowed weekdays, respecting max_per_day and leaving
    a 1-hour BUFFER between events. If a task cannot fit before the deadline, it goes
    into unscheduled.
    """
    # 1) Compute “start tomorrow at 9 AM”
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
        allowed_indices = {0, 1, 2, 3, 4}  # default Mon–Fri

    # 5) Fetch busy windows via FreeBusy
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
        # If FreeBusy fails, proceed as if the calendar were empty
        busy = []

    return _schedule_by_freebusy(
        tasks,
        next_day_dt,
        deadline_date,
        daily_limit,
        allowed_indices,
        busy
    )


def _schedule_by_freebusy(
    tasks,
    start_dt,
    deadline_date,
    daily_limit,
    allowed_indices,
    busy
):
    """
    Carve out free windows between WORK_START (9 AM) and WORK_END (10 PM).
    For each task:
      - Find the first available window that fits its duration
      - Insert a 1-hour BUFFER after each scheduled block
      - Respect daily_limit for each day
      - Skip days not in allowed_indices
    """
    scheduled = []
    unscheduled = []
    day_counts = {}

    # 1) Initialize probe at max(start_dt, 9 AM of start_dt’s date)
    probe = start_dt
    if probe.hour < WORK_START:
        probe = probe.replace(hour=WORK_START, minute=0, second=0, microsecond=0)

    def next_allowed_probe(dt_obj):
        """
        Move to 9 AM of the next allowed weekday after dt_obj.date()
        """
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

                    # 4) Pick the first free window that fits
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
            # 5) Mark this event + BUFFER as busy
            busy.append((slot_start, slot_end + BUFFER))
            day_counts[slot_start.date()] = day_counts.get(slot_start.date(), 0) + 1
            probe = slot_end + BUFFER

    return scheduled, unscheduled


def create_calendar_events(service, scheduled):
    """
    Insert each block in `scheduled` into Google Calendar.
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
