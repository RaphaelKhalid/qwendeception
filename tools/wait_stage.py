"""Block until a stage of the current attempt finishes (or the pod is terminated); print its events."""
import json, sys, time
stage, since = sys.argv[1], float(sys.argv[2])
while True:
    ev = [json.loads(l) for l in open("results/events.jsonl", encoding="utf-8") if l.strip()]
    ev = [e for e in ev if e.get("ts", 0) >= since]
    if any(e.get("stage") == stage and e.get("kind") in ("stage_done", "stage_failed", "skipped") for e in ev) \
            or any(e.get("kind") in ("terminated", "finished") for e in ev):
        for e in ev:
            if e.get("stage") == stage and e.get("kind") != "progress" or e.get("kind") in ("terminated", "finished"):
                print(e.get("emoji"), e.get("msg"), (e.get("tb") or "")[-600:])
        break
    time.sleep(5)
