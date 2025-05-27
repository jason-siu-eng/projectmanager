import os
import re
import json
import sys
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIError

# 1. Load API key from .env
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError("Please set OPENAI_API_KEY in your .env")

# 2. Initialize OpenAI client
openai = OpenAI(api_key=api_key)

def breakdown_goal(goal: str, level: str, deadline: str) -> list[dict]:
    prompt = (
        f"You are an expert project planner.\n"
        f"Goal: {goal}\n"
        f"Level: {level}\n"
        f"Deadline: {deadline}\n\n"
        "Return ONLY valid JSON with a single key \"tasks\" whose value is a list of objects "
        "each with an integer \"id\" and a short string \"task\".\n\n"
        "Example:\n"
        '{\n'
        '  "tasks": [\n'
        '    {"id":1,"task":"First step"},\n'
        '    {"id":2,"task":"Second step"}\n'
        '  ]\n'
        '}'
    )
    try:
        resp = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user",   "content": prompt}
            ],
            temperature=0.3,
            max_tokens=300
        )
        content = resp.choices[0].message.content.strip()
        # strip markdown fences
        if content.startswith("```"):
            content = content.split("```", 2)[1].strip()
        # extract JSON blob
        m = re.search(r"\{[\s\S]*\}", content)
        blob = m.group(0) if m else content

        data = json.loads(blob)
        tasks = data.get("tasks")
        if isinstance(tasks, list):
            return tasks

    except Exception as e:
        print("❗️ Failed to parse LLM output:", repr(content), e, file=sys.stderr)

    # FALLBACK: extract each {"id":…, "task":"…"} object manually
    fallback = []
    for obj_str in re.findall(r"\{[^}]*\"id\"\s*:\s*\d+[^}]*\}", content):
        try:
            obj = json.loads(obj_str)
            fallback.append(obj)
        except:
            pass
    if fallback:
        return fallback

    # ULTIMATE FALLBACK: one task = the goal
    return [{"id": 1, "task": goal}]


if __name__ == "__main__":
    # CLI test
    goal = input("Enter your goal: ")
    level = input("Enter your level (easy/medium/hard): ")
    deadline = input("Enter your deadline (YYYY-MM-DD): ")
    tasks = breakdown_goal(goal, level, deadline)
    print("\nGenerated tasks:")
    for t in tasks:
        print(f"{t['id']}. {t['task']}")
