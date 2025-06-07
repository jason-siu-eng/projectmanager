# task_breakdown.py

import os
import json
from datetime import datetime
from typing import List, Dict

from openai import OpenAI  # v1 client

# ── 1) INITIALIZE v1 CLIENT ────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("Please set OPENAI_API_KEY in environment")

client = OpenAI(api_key=OPENAI_API_KEY)


def breakdown_goal(
    goal: str,
    current_level: str,
    target_level: str,
    deadline: str
) -> List[Dict]:
    """
    Break down `goal` into actionable steps based on:
      - current proficiency: current_level
      - desired proficiency: target_level
      - days until deadline

    Returns a list:
      [ { "id": 1, "task": "...", "duration_hours": 2.0 }, … ]
    """

    # 2) Calculate days_left
    try:
        today = datetime.utcnow().date()
        dl_date = datetime.fromisoformat(deadline).date()
        days_left = max((dl_date - today).days, 1)
    except Exception:
        days_left = 7

    # 3) Build prompt
    prompt = (
        f"You are a helpful assistant that breaks down high-level goals into actionable tasks. "
        f"The user’s current proficiency is \"{current_level}\" and they wish to reach \"{target_level}\". "
        f"They have {days_left} day(s) until the deadline. Generate roughly one task per day, "
        f"but adjust so the plan logically moves from {current_level} to {target_level}. "
        f"For each task, estimate how many hours it will take (decimal OK).  \n\n"
        f"Respond with a pure JSON array of objects, each containing:\n"
        f"  id: integer step number,\n"
        f"  task: string step description,\n"
        f"  duration_hours: number hours.\n\n"
        f"Goal: {goal}\n"
        f"Deadline: {deadline}\n\n"
        f"Respond ONLY with valid JSON—no extra text or markdown."
    )

    # 4) Debug print
    print("=== breakdown_goal called ===")
    print(f"current_level = {current_level}")
    print(f"target_level  = {target_level}")
    print(f"days_left     = {days_left}")
    print("PROMPT:")
    print(prompt)

    # 5) Call OpenAI
    raw = None
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a JSON-output specialist."},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.7,
            max_tokens=days_left * 80,
        )
        raw = res.choices[0].message.content.strip()
        print("RAW OPENAI RESPONSE:")
        print(raw)
    except Exception as e:
        print("OpenAI API call failed:", repr(e))

    # 6) Parse or fallback
    if raw:
        try:
            arr = json.loads(raw)
            tasks: List[Dict] = []
            for i, obj in enumerate(arr, start=1):
                desc = obj.get("task", "").strip()
                dur  = float(obj.get("duration_hours", 1.0))
                tasks.append({"id": i, "task": desc, "duration_hours": dur})
            if tasks:
                return tasks
            else:
                raise ValueError("empty array")
        except Exception as pe:
            print("JSON parse failed:", repr(pe))

    # 7) Fallback to placeholders
    print("FALLING BACK to placeholders")
    return [
        {"id": i+1, "task": f"(Step {i+1} placeholder)", "duration_hours": 1.0}
        for i in range(days_left)
    ]
