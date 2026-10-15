"""Block until an event matching (stage, kinds) appears after a timestamp, or the pod is terminated after it."""
import json, sys, time
stage, kinds, since = sys.argv[1], set(sys.argv[2].split(",")), float(sys.argv[3])
while True:
    for l in open("results/events.jsonl", encoding="utf-8"):
        if not l.strip():
            continue
        e = json.loads(l)
        if e.get("ts", 0) < since:
            continue
        if (e.get("stage") == stage and e.get("kind") in kinds) or e.get("kind") == "terminated":
            print(e.get("emoji"), e.get("stage"), e.get("msg"))
            sys.exit(0)
    time.sleep(10)
