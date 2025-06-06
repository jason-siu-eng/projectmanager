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
      - how many days remain until `deadline`
      - current proficiency `current_level`
      - desired proficiency `target_level`

    Returns a list of dicts:
      [ { "id": 1, "task": "...", "duration_hours": 2.0 }, … ]
    """

    # ── 2) CALCULATE days_left ───────────────────────────────────────────────────
    try:
        today = datetime.utcnow().date()
        dl_date = datetime.fromisoformat(deadline).date()
        days_left = max((dl_date - today).days, 1)
    except Exception:
        days_left = 7

    # ── 3) BUILD PROMPT ─────────────────────────────────────────────────────────
    prompt = (
        f"You are a helpful assistant that breaks down high-level goals into actionable tasks. "
        f"The user’s current proficiency is \"{current_level}\" and they wish to reach \"{target_level}\". "
        f"They have {days_left} day(s) until the deadline. "
        f"Generate a sequence of tasks—approximately one task per day, "
        f"but adjusted so that the plan logically moves from {current_level} to {target_level}. "
        f"For each task, estimate how many hours it will take (decimal OK).  \n\n"
        f"Respond with a pure JSON array of objects, each containing:\n"
        f"    id: (integer) step number,\n"
        f"    task: (string) step description,\n"
        f"    duration_hours: (number) hours (decimal OK).\n\n"
        f"Goal: {goal}\n"
        f"Deadline: {deadline}\n\n"
        f"Respond **ONLY** with valid JSON—no extra text or markdown."
    )

    # ── 4) DEBUG PRINTS FOR RENDER ─────────────────────────────────────────────────
    print("=== breakdown_goal called ===")
    print("current_level =", current_level)
    print("target_level =", target_level)
    print("days_left =", days_left)
    print("PROMPT:")
    print(prompt)

    # ── 5) CALL OPENAI (v1 SDK) ───────────────────────────────────────────────────
    raw_response = None
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a JSON-output specialist."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=days_left * 80
        )
        raw_response = response.choices[0].message.content.strip()
        print("RAW OPENAI RESPONSE:")
        print(raw_response)

    except Exception as e:
        # Catch all exceptions (invalid key, network, etc.)
        print("OpenAI API call failed with exception:", repr(e))
        raw_response = None

    # ── 6) PARSE JSON OR FALL BACK ────────────────────────────────────────────────
    if raw_response:
        try:
            data = json.loads(raw_response)
            tasks: List[Dict] = []
            for idx, obj in enumerate(data, start=1):
                desc = obj.get("task", "").strip()
                dur = float(obj.get("duration_hours", 1.0))
                tasks.append({"id": idx, "task": desc, "duration_hours": dur})

            if len(tasks) == 0:
                raise ValueError("AI returned empty array")

            return tasks

        except Exception as parse_err:
            print("JSON parse failed. raw was:", raw_response)
            print("Parsing exception:", repr(parse_err))

    # ── 7) FALLBACK: GENERIC PLACEHOLDERS ────────────────────────────────────────
    print("FALLING BACK to placeholders")
    fallback_count = max(days_left, 1)
    return [
        {
            "id": i + 1,
            "task": f"(Step {i + 1} placeholder)",
            "duration_hours": 1.0
        }
        for i in range(fallback_count)
    ]
