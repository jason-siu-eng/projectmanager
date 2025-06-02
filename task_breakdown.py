# task_breakdown.py
import os, json
from typing import List, Dict
from datetime import datetime
import openai

# 1) Configure API key (this should come from Render’s env)
openai.api_key = os.getenv("OPENAI_API_KEY", "").strip()
if not openai.api_key:
    raise RuntimeError("OPENAI_API_KEY is missing or empty!")

def breakdown_goal(goal: str, level: str, deadline: str) -> List[Dict]:
    # Compute days_left exactly as you had it
    try:
        today = datetime.utcnow().date()
        dl = datetime.fromisoformat(deadline).date()
        days_left = max((dl - today).days, 1)
    except Exception:
        days_left = 7

    # Build prompt
    prompt = (
        f"You are a helpful assistant that breaks down high-level goals into steps. "
        f"User has {days_left} day(s) to achieve the goal: \"{goal}\" at proficiency \"{level}\". "
        f"Return an array of objects in JSON, each with keys: id (int), task (string), and duration_hours (decimal)."
    )

    # ─── Add these debug prints ──────────────────────────────────────────────────
    print("=== breakdown_goal called on Render ===")
    print("days_left =", days_left)
    print("PROMPT =")
    print(prompt)
    # ──────────────────────────────────────────────────────────────────────────────

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a JSON‐output specialist."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=days_left * 80
        )
        raw = response.choices[0].message.content.strip()

        # ─── Log raw AI output for inspection ─────────────────────────────────────
        print("RAW OPENAI RESPONSE:")
        print(raw)
        # ──────────────────────────────────────────────────────────────────────────
    except Exception as e:
        print("OpenAI API call failed with exception:", repr(e))
        raw = None

    # Try to parse JSON
    if raw:
        try:
            data = json.loads(raw)
            tasks = []
            for idx, obj in enumerate(data, start=1):
                desc = obj.get("task", "").strip()
                dur  = float(obj.get("duration_hours", 1.0))
                tasks.append({"id": idx, "task": desc, "duration_hours": dur})
            if not tasks:
                raise ValueError("Empty array returned")
            return tasks
        except Exception as parse_err:
            print("JSON parse failed. raw was:", raw)
            print("Parsing exception:", repr(parse_err))

    # Fallback to placeholders
    print("FALLING BACK to placeholders on Render.")
    fallback = [
        {"id": i + 1,
         "task": f"(Step {i + 1} placeholder)",
         "duration_hours": 1.0}
        for i in range(days_left)
    ]
    return fallback
