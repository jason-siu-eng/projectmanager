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


def breakdown_goal(goal: str, level: str, deadline: str) -> List[Dict]:
    """
    Break down `goal` into actionable steps based on days until `deadline`.
    Each step includes an estimated duration_hours. Returns a list of:
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
        f"The user has {days_left} day(s) until the deadline. "
        f"Create however many steps are needed to achieve this goal in {days_left} days—roughly one step per day, "
        f"but you may combine or split logically as needed. "
        f"For each step, estimate how many hours it will take (decimal OK). "
        f"Respond with a pure JSON array of objects, each containing:\n"
        f"    id: (integer) step number,\n"
        f"    task: (string) step description,\n"
        f"    duration_hours: (number) hours (decimal OK).\n\n"
        f"User’s proficiency level: \"{level}\"\n"
        f"Deadline: {deadline}\n"
        f"Goal: {goal}\n\n"
        f"Respond **ONLY** with valid JSON (no extra text or markdown)."
    )

    # ── 4) DEBUG PRINTS FOR RENDER ─────────────────────────────────────────────────
    print("=== breakdown_goal called on Render ===")
    print("days_left =", days_left)
    print("PROMPT:")
    print(prompt)

    # ── 5) CALL OPENAI (v1 SDK) ───────────────────────────────────────────────────
    raw = None
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a JSON-output specialist."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=days_left * 80,
        )
        raw = response.choices[0].message.content.strip()
        print("RAW OPENAI RESPONSE:")
        print(raw)

    except Exception as e:
        # Catch all exceptions (invalid key, network, rate-limit, etc.)
        print("OpenAI API call failed with exception:", repr(e))
        raw = None

    # ── 6) STRIP TRIPLE-BACKTICK FENCES (IF ANY) ─────────────────────────────────
    if raw and raw.startswith("```"):
        # Remove the opening fence line (e.g. "```json\n")
        idx = raw.find("\n")
        if idx != -1:
            raw = raw[idx + 1 :]

        # If there's a trailing ``` at the end, strip it off
        if raw.strip().endswith("```"):
            raw = raw.rsplit("```", 1)[0].strip()

    # ── 7) PARSE JSON OR FALL BACK ────────────────────────────────────────────────
    if raw:
        try:
            data = json.loads(raw)
            tasks: List[Dict] = []
            for idx, obj in enumerate(data, start=1):
                desc = obj.get("task", "").strip()
                dur = float(obj.get("duration_hours", 1.0))
                tasks.append({"id": idx, "task": desc, "duration_hours": dur})

            if len(tasks) == 0:
                raise ValueError("AI returned an empty list")

            return tasks

        except Exception as parse_err:
            print("JSON parse failed. raw was:", raw)
            print("Parsing exception:", repr(parse_err))

    # ── 8) FALLBACK: GENERIC PLACEHOLDERS ────────────────────────────────────────
    print("FALLING BACK to placeholders")
    fallback_count = max(days_left, 1)
    return [
        {
            "id": i + 1,
            "task": f"(Step {i + 1} placeholder)",
            "duration_hours": 1.0,
        }
        for i in range(fallback_count)
    ]
