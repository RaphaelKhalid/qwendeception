"""Second extension: the 'uncensored' counterfactual at scale (runs after extra.py).

  samples_uncensored  4 sampled answers (T=0.8) per CCP question with 1x the political vector subtracted,
                      probe-scored, to pair with the normal samples from extra.py
  scale_uncensored    promptfoo CCP prompts answered with the vector subtracted, probe-scored, until the deadline
Ends with an "all_done2" event, which tells the local guard to terminate the pod.
"""

from __future__ import annotations

import csv
import random
import time
import traceback

import torch

import run_experiment as R
from qd import prompts as P
from qd.common import RESULTS, event, read_jsonl, write_jsonl

BS = 32
SAMPLES = 4


def steer_off(m):
    D = torch.load(RESULTS / "direction.pt")
    m.add_steering(D["layer"], -1.0 * D["direction"].float())


def stage_samples_uncensored():
    m = R.model()
    items = [("ccp_en", it) for it in R.small(P.CCP_EN)] + [("ccp_zh", it) for it in R.small(P.CCP_ZH)]
    batch, meta = [], []
    for st, it in items:
        for k in range(SAMPLES):
            batch.append({"messages": [{"role": "user", "content": it["q"]}]})
            meta.append((st, it, k))
    event("samples_uncensored", f"{len(batch)} sampled answers with the political vector subtracted", "🔓",
          kind="stage_info", total=len(batch))
    steer_off(m)
    try:
        ans = m.generate(batch, max_new_tokens=320, bs=BS, temperature=0.8,
                         on_batch=R.gen_progress("samples_uncensored", len(batch)))
    finally:
        m.clear_hooks()
    rows = []
    for i, ((st, it, k), a) in enumerate(zip(meta, ans)):
        rows.append(dict(set=st, id=it["id"], sample=k, answer=a, label=P.label(a, it["facts"]),
                         bare=m.probe_scores([{"role": "user", "content": it["q"]}], a)))
        if i % 20 == 0 or i == len(meta) - 1:
            event("samples_uncensored", f"lie-tested {i + 1}/{len(meta)}", "🕵️", kind="progress", done=i + 1,
                  total=len(meta), probed=20)
    write_jsonl(RESULTS / "samples_uncensored.jsonl", rows)
    for st in ("ccp_en", "ccp_zh"):
        rs = [r for r in rows if r["set"] == st]
        event("samples_uncensored", f"{st}: {sum(r['label'] == 'accurate' for r in rs)}/{len(rs)} accurate (keyword judge)",
              "🔓", kind="finding", set=st)


def stage_scale_uncensored():
    from huggingface_hub import hf_hub_download

    m = R.model()
    path = hf_hub_download("promptfoo/CCP-sensitive-prompts", "ccp-sensitive-prompts.csv", repo_type="dataset")
    allq = list(csv.DictReader(open(path, encoding="utf-8")))
    seen = {r["q"] for r in read_jsonl(RESULTS / "scale_uncensored.jsonl")}
    todo = [q for q in allq if q["prompt"] not in seen]
    random.Random(2).shuffle(todo)
    todo = R.small(todo)
    event("scale_uncensored", f"{len(todo)} promptfoo prompts with the political vector subtracted, until the deadline",
          "📈", kind="stage_info", total=len(todo))
    n = 0
    for k in range(0, len(todo), BS):
        if R.DEADLINE and time.time() > R.DEADLINE - 300:
            event("scale_uncensored", "deadline reached, wrapping up", "⏰")
            break
        chunk = todo[k:k + BS]
        steer_off(m)
        try:
            ans = m.generate([{"messages": [{"role": "user", "content": q["prompt"]}]} for q in chunk],
                             max_new_tokens=256, bs=BS)
        finally:
            m.clear_hooks()
        rows = []
        for q, a in zip(chunk, ans):
            t = a.lower()
            lab = "steered" if any(x in t for x in P.STEER_MARKERS) else (
                "refusal" if any(x in t for x in P.REFUSAL_MARKERS) else "other")
            rows.append(dict(subject=q["subject"], q=q["prompt"], answer=a, label=lab,
                             bare=m.probe_scores([{"role": "user", "content": q["prompt"]}], a)))
        write_jsonl(RESULTS / "scale_uncensored.jsonl", rows)
        n += len(rows)
        event("scale_uncensored", f"uncensored scale-up: {n}/{len(todo)} answered + lie-tested", "📈",
              kind="progress", done=n, total=len(todo), probed=len(rows))


STAGES = [("samples_uncensored", stage_samples_uncensored), ("scale_uncensored", stage_scale_uncensored)]

if __name__ == "__main__":
    event("extra2", "second extension: the uncensored counterfactual at scale", "🔋", kind="start_extra")
    for name, fn in STAGES:
        if name in R.state()["done"]:
            continue
        if R.DEADLINE and time.time() > R.DEADLINE - 300:
            event(name, "skipped: out of budget time", "⏰", kind="skipped")
            continue
        t = time.time()
        event(name, f"stage {name} starting", "▶️", kind="stage_start")
        try:
            fn()
            R.mark_done(name)
            event(name, f"stage {name} done in {time.time() - t:.0f}s", "✅", kind="stage_done", seconds=time.time() - t)
        except Exception as e:
            event(name, f"stage {name} FAILED: {e!r}", "💥", kind="stage_failed", tb=traceback.format_exc()[-2000:])
    event("extra2", "second extension finished", "🏁", kind="all_done2")
