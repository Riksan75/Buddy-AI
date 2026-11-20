"""
YouTube Shorts Script Generator — INTERACTIVE MODE (Ollama + Mistral)
----------------------------------------------------------------------
An interactive session where you keep steering the tool with commands:
add your own topics, generate more ideas, regenerate scripts, or refine
a script with feedback like "make it funnier" or "shorten the hook".

REQUIREMENTS:
  1. Ollama installed and running (https://ollama.com)
  2. Mistral model pulled:   ollama pull mistral
  3. Python package:         pip install requests

USAGE:
  python shorts_generator_interactive.py

COMMANDS (type at the prompt):
  new <niche> [count]     -> start fresh: generate topics + scripts for a niche
  add <topic>             -> manually add your own topic and write its script
  more [count]            -> generate more topic ideas for the current niche
  list                    -> show all scripts (index + topic)
  view <index>            -> show the full script for that index
  regen <index>           -> regenerate a script from scratch
  edit <index> <feedback> -> refine a script with your feedback
  save                    -> save all scripts to a CSV
  help                    -> show this command list
  exit                    -> quit (prompts to save if unsaved changes)
"""

import requests
import csv
import re
import sys
from datetime import datetime

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "mistral"


def call_ollama(prompt: str, temperature: float = 0.8) -> str:
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": temperature}},
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["response"].strip()
    except requests.exceptions.ConnectionError:
        print("\n❌ Couldn't reach Ollama at http://localhost:11434")
        print("   Make sure Ollama is running: open the Ollama app, or run `ollama serve`\n")
        return ""
    except Exception as e:
        print(f"\n❌ Error calling Ollama: {e}\n")
        return ""


def generate_topics(niche: str, count: int, existing: list[str] = None) -> list[str]:
    existing = existing or []
    avoid = f"\nDo NOT repeat any of these already-used topics:\n" + "\n".join(existing) if existing else ""
    prompt = f"""You are a viral YouTube Shorts content strategist.

Generate exactly {count} highly engaging, scroll-stopping video topic ideas for the niche: "{niche}".
{avoid}

Rules:
- Each topic must be a single punchy line, phrased like a video title/hook.
- Favor curiosity gaps, surprising facts, controversy, or strong emotion.
- No numbering, no explanations, no extra text.
- Output ONLY the list, one topic per line.
"""
    raw = call_ollama(prompt, temperature=0.9)
    lines = [re.sub(r"^\s*[\d\.\-\)]+\s*", "", l).strip() for l in raw.split("\n")]
    return [l for l in lines if l][:count]


def parse_script(raw: str, topic: str) -> dict:
    def extract(tag):
        match = re.search(rf"{tag}:\s*(.*?)(?=\n[A-Z]+:|\Z)", raw, re.DOTALL)
        return match.group(1).strip() if match else ""
    return {"topic": topic, "hook": extract("HOOK"), "body": extract("BODY"), "cta": extract("CTA")}


def generate_script(topic: str) -> dict:
    prompt = f"""You are an expert YouTube Shorts scriptwriter. Write a script for a
30-45 second vertical short video on this topic:

"{topic}"

Requirements:
- HOOK: 1-2 sentences, first 3 seconds, must stop the scroll immediately (surprise, question, or bold claim). No greetings like "hey guys."
- BODY: The main content, punchy and fast-paced, plain spoken language, no fluff, 4-8 short sentences max.
- CTA: 1 short sentence encouraging follow/like/comment, tied naturally to the topic (not generic).

Respond ONLY in this exact format, nothing else:

HOOK: <text>
BODY: <text>
CTA: <text>
"""
    raw = call_ollama(prompt, temperature=0.75)
    return parse_script(raw, topic)


def refine_script(script: dict, feedback: str) -> dict:
    prompt = f"""Here is a YouTube Shorts script:

HOOK: {script['hook']}
BODY: {script['body']}
CTA: {script['cta']}

Revise this script based on the following feedback: "{feedback}"

Keep the same topic and overall structure. Respond ONLY in this exact format, nothing else:

HOOK: <text>
BODY: <text>
CTA: <text>
"""
    raw = call_ollama(prompt, temperature=0.75)
    return parse_script(raw, script["topic"])


def print_script(i: int, script: dict):
    print(f"\n--- Script #{i} ---")
    print(f"TOPIC: {script['topic']}")
    print(f"HOOK:  {script['hook']}")
    print(f"BODY:  {script['body']}")
    print(f"CTA:   {script['cta']}\n")


def print_help():
    print("""
Commands:
  new <niche> [count]     start fresh: generate topics + scripts for a niche
  add <topic>             manually add your own topic and write its script
  more [count]            generate more topic ideas for the current niche
  list                    show all scripts (index + topic)
  view <index>            show the full script for that index
  regen <index>           regenerate a script from scratch
  edit <index> <feedback> refine a script with your feedback
  save                    save all scripts to a CSV
  help                    show this command list
  exit                    quit (prompts to save if unsaved changes)
""")


def save_csv(scripts: list[dict]) -> str:
    filename = f"shorts_scripts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["topic", "hook", "body", "cta"])
        writer.writeheader()
        writer.writerows(scripts)
    return filename


def main():
    print("=== YouTube Shorts Script Generator — Interactive Mode ===")
    print("Type 'help' to see commands.\n")

    scripts: list[dict] = []
    current_niche = None
    unsaved = False

    while True:
        try:
            raw_cmd = input("shorts> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw_cmd:
            continue

        parts = raw_cmd.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "help":
            print_help()

        elif cmd == "new":
            sub = arg.rsplit(maxsplit=1)
            if len(sub) == 2 and sub[1].isdigit():
                niche, count = sub[0], int(sub[1])
            else:
                niche, count = arg, 10
            if not niche:
                print("Usage: new <niche> [count]")
                continue
            current_niche = niche
            scripts = []
            print(f"🔎 Generating {count} topics for '{niche}'...")
            topics = generate_topics(niche, count)
            for i, topic in enumerate(topics, 1):
                print(f"✍️  Writing script {i}/{len(topics)}...")
                scripts.append(generate_script(topic))
            unsaved = True
            print(f"✅ Done. {len(scripts)} scripts ready. Type 'list' to view them.")

        elif cmd == "add":
            topic = arg.strip()
            if not topic:
                print("Usage: add <topic>")
                continue
            print(f"✍️  Writing script for: {topic}...")
            scripts.append(generate_script(topic))
            unsaved = True
            print(f"✅ Added as script #{len(scripts)}.")

        elif cmd == "more":
            if not current_niche:
                print("No active niche yet. Use 'new <niche>' first.")
                continue
            count = int(arg) if arg.isdigit() else 5
            existing_topics = [s["topic"] for s in scripts]
            print(f"🔎 Generating {count} more topics for '{current_niche}'...")
            topics = generate_topics(current_niche, count, existing_topics)
            for i, topic in enumerate(topics, 1):
                print(f"✍️  Writing script {i}/{len(topics)}...")
                scripts.append(generate_script(topic))
            unsaved = True
            print(f"✅ Added {len(topics)} more scripts.")

        elif cmd == "list":
            if not scripts:
                print("No scripts yet. Use 'new <niche>' to get started.")
            for i, s in enumerate(scripts, 1):
                print(f"  {i}. {s['topic']}")

        elif cmd == "view":
            if not arg.isdigit() or not (1 <= int(arg) <= len(scripts)):
                print("Usage: view <index>  (see 'list' for valid indexes)")
                continue
            print_script(int(arg), scripts[int(arg) - 1])

        elif cmd == "regen":
            if not arg.isdigit() or not (1 <= int(arg) <= len(scripts)):
                print("Usage: regen <index>  (see 'list' for valid indexes)")
                continue
            idx = int(arg) - 1
            print(f"🔄 Regenerating script #{arg}...")
            scripts[idx] = generate_script(scripts[idx]["topic"])
            unsaved = True
            print_script(int(arg), scripts[idx])

        elif cmd == "edit":
            edit_parts = arg.split(maxsplit=1)
            if len(edit_parts) != 2 or not edit_parts[0].isdigit():
                print("Usage: edit <index> <feedback>")
                continue
            idx_str, feedback = edit_parts
            if not (1 <= int(idx_str) <= len(scripts)):
                print("Invalid index. See 'list' for valid indexes.")
                continue
            idx = int(idx_str) - 1
            print(f"✏️  Refining script #{idx_str} with feedback: \"{feedback}\"...")
            scripts[idx] = refine_script(scripts[idx], feedback)
            unsaved = True
            print_script(int(idx_str), scripts[idx])

        elif cmd == "save":
            if not scripts:
                print("Nothing to save yet.")
                continue
            filename = save_csv(scripts)
            unsaved = False
            print(f"💾 Saved {len(scripts)} scripts to: {filename}")

        elif cmd in ("exit", "quit"):
            if unsaved and scripts:
                confirm = input("You have unsaved scripts. Save before exiting? (y/n): ").strip().lower()
                if confirm == "y":
                    filename = save_csv(scripts)
                    print(f"💾 Saved to: {filename}")
            print("👋 Bye!")
            break

        else:
            print(f"Unknown command: '{cmd}'. Type 'help' for the list of commands.")


if __name__ == "__main__":
    main()
