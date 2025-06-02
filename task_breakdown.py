# task_breakdown.py

import os
import json
from datetime import datetime
from typing import List, Dict

import openai

# ── 1) CONFIGURE OPENAI ───────────────────────────────────────────────────────────
openai.api_key = os.getenv("OPENAI_API_KEY", "").strip()
if not openai.api_key:
    raise RuntimeError("Please set OPENAI_API_KEY in environment")


def breakdown_goal(goal: str, level: str, deadline: str) -> List[Dict]:
    """
    Breaks down `goal` (string) into a variable number of actionable steps, 
    based on how many days remain until `deadline` (ISO date).
    For each step, also estimate "duration_hours" (a float).
    
    Returns:
      [
        { "id": 1, "task": "…", "duration_hours": 2.0 },
        { "id": 2, "task": "…", "duration_hours": 1.5 },
        …
      ]
    """
    # ── 2) CALCULATE DAYS UNTIL DEADLINE ────────────────────────────────────────
    try:
        today = datetime.utcnow().date()
        deadline_date = datetime.fromisoformat(deadline).date()
        days_left = max((deadline_date - today).days, 1)
    except Exception:
        # If parsing fails, default to 7 days
        days_left = 7

    # ── 3) BUILD PROMPT ─────────────────────────────────────────────────────────
    prompt = (
        f"You are a helpful assistant that breaks down high-level goals into actionable tasks. "
        f"The user has {days_left} day(s) until the deadline.  "
        f"Create however many steps are needed to achieve this goal in {days_left} days, "
        f"aiming for roughly one step per day but you may combine or split tasks logically.  "
        f"For each step, also estimate how many hours it will take (e.g. 1.5).  "
        f"Return your entire answer as a pure JSON array of objects, where each object has keys:\n\n"
        f"    id: (integer, step number),\n"
        f"    task: (string) the description of that step,\n"
        f"    duration_hours: (number) hours (can be decimal) for that step.\n\n"
        f"User’s proficiency level: \"{level}\"\n"
        f"Deadline: {deadline}\n"
        f"Goal: {goal}\n\n"
        f"Respond **ONLY** with valid JSON, with no extra text or markdown."
    )

    # ── 4) CALL OPENAI ────────────────────────────────────────────────────────────
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a JSON‐output specialist."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=days_left * 80  # adjust to allow enough tokens for X steps
        )
        raw = response.choices[0].message.content.strip()
    except Exception:
        # If the API call fails for any reason:
        raw = None

    # ── 5) PARSE JSON OR FALL BACK ────────────────────────────────────────────────
    if raw:
        try:
            data = json.loads(raw)
            tasks: List[Dict] = []
            for i, obj in enumerate(data, start=1):
                desc = obj.get("task", "").strip()
                dur = float(obj.get("duration_hours", 1.0))
                tasks.append({"id": i, "task": desc, "duration_hours": dur})
            # If the model for some reason returned fewer than 1 element, pad:
            if len(tasks) == 0:
                raise ValueError("No tasks returned")
            return tasks
        except Exception:
            # Fall thru to placeholder
            pass

    # ── 6) FALLBACK: GENERIC PLACEHOLDERS ────────────────────────────────────────
    # If we get here, parsing failed or raw==None.
    # Return one placeholder per day_left (or at least 1)
    fallback_count = max(days_left, 1)
    return [
        {"id": i + 1, "task": f"(Step {i + 1} placeholder)", "duration_hours": 1.0}
        for i in range(fallback_count)
    ]
