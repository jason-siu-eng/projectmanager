# task_breakdown.py

import os
import openai
from typing import List, Dict

# Ensure OPENAI_API_KEY is set in environment
openai.api_key = os.getenv("OPENAI_API_KEY", "")
if not openai.api_key:
    raise RuntimeError("Please set OPENAI_API_KEY in environment")


def breakdown_goal(goal: str, level: str, deadline: str, totalTasks: int) -> List[Dict]:
    """
    Use OpenAI to break down `goal` into exactly `totalTasks` actionable steps.
    For each step, the model will also estimate "duration_hours" (a float).
    We instruct the model to output a pure JSON array, e.g.:
      [
        { "task": "Do X", "duration_hours": 2.5 },
        ...
      ]

    Returns a list of dictionaries:
      [ { "id": 1, "task": "...", "duration_hours": 2.5 }, … ]
    """
    prompt = (
        f"You are a helpful assistant that breaks down high-level goals into actionable steps. "
        f"Please create exactly {totalTasks} steps to achieve the following goal. "
        f"For each step, also estimate how many hours it will take (using decimals, e.g. 1.5). "
        f"Format your entire response as a JSON array (no extra text), where each element is an "
        f"object with these keys:\n"
        f"    task: (string) the description of the step,\n"
        f"    duration_hours: (number) the estimated hours needed.\n\n"
        f"Here are the constraints:\n"
        f"- The user’s proficiency level is \"{level}\".\n"
        f"- The deadline is {deadline}.\n"
        f"- Output exactly {totalTasks} objects in the array.\n\n"
        f"Goal: {goal}\n\n"
        f"Respond ONLY with valid JSON. Do NOT include any additional commentary or markdown."
    )

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a JSON‐output specialist."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=totalTasks * 50  # enough tokens for description + duration
        )
    except Exception:
        # On OpenAI error, return a fallback: totalTasks items with generic placeholders
        return [
            {"id": i + 1, "task": f"(Step {i + 1} placeholder)", "duration_hours": 1.0}
            for i in range(totalTasks)
        ]

    raw = response.choices[0].message.content.strip()

    # Attempt to parse the raw JSON array
    try:
        data = json.loads(raw)
        tasks: List[Dict] = []
        for i, obj in enumerate(data, start=1):
            task_desc = obj.get("task", "").strip()
            dur = float(obj.get("duration_hours", 1.0))
            tasks.append({"id": i, "task": task_desc, "duration_hours": dur})
        # If the model returned fewer/more than totalTasks, truncate or pad:
        if len(tasks) < totalTasks:
            # pad with placeholders
            for j in range(len(tasks), totalTasks):
                tasks.append({
                    "id": j + 1,
                    "task": f"(Step {j + 1} placeholder)",
                    "duration_hours": 1.0
                })
        elif len(tasks) > totalTasks:
            tasks = tasks[:totalTasks]
        return tasks
    except Exception:
        # If parsing fails, fall back to generic placeholders
        return [
            {"id": i + 1, "task": f"(Step {i + 1} placeholder)", "duration_hours": 1.0}
            for i in range(totalTasks)
        ]
