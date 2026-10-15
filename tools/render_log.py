"""Freeze the live monitor into results/log.html (self-contained) and results/LOG.md.

    python tools/render_log.py
"""

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
events = [json.loads(l) for l in open(ROOT / "results" / "events.jsonl", encoding="utf-8") if l.strip()]

html = (ROOT / "monitor" / "index.html").read_text(encoding="utf-8")
payload = json.dumps(events, ensure_ascii=False).replace("</", "<\\/")
html = html.replace("<script>\nconst STAGES", f"<script>window.STATIC_EVENTS = {payload};</script>\n<script>\nconst STAGES", 1)
html = html.replace("<title>Qwen Interrogation Live</title>", "<title>Qwen Interrogation Log</title>")
(ROOT / "results" / "log.html").write_text(html, encoding="utf-8")

lines = ["# 📜 Run log: Qwen3.5-9B interrogation", "",
         "Every event from the pod and the local guard, in order. Open `log.html` for the dashboard version.", "",
         "| time (UTC) | | stage | event |", "|---|---|---|---|"]
for e in events:
    if e.get("kind") == "cost":
        continue
    t = time.strftime("%H:%M:%S", time.gmtime(e.get("ts", 0)))
    msg = str(e.get("msg", "")).replace("|", "\\|").replace("\n", " ")
    lines.append(f"| {t} | {e.get('emoji', '')} | {e.get('stage', '')} | {msg} |")
(ROOT / "results" / "LOG.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote log.html and LOG.md with {len(events)} events")
