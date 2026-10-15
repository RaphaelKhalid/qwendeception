"""Extension stages that spend the rest of the budget on the same design, for more statistical power.

Runs on the pod after run_experiment.py has finished:
  samples       4 sampled answers (T=0.8) per question in every set + a follow-up, all probe-scored
  elicit_all    the knowledge test on the 25 CCP topics the main run skipped
  thinking_more thinking mode on the remaining CCP questions, the Chinese set, and 20 neutral controls
  scale_all     every remaining promptfoo CCP prompt (not only question-style ones) until the deadline
Ends with an "all_done" event, which tells the local guard to terminate the pod.
"""

from __future__ import annotations

import csv
import random
import time
import traceback

import run_experiment as R
from qd import prompts as P
from qd.common import RESULTS, event, read_jsonl, write_jsonl

SAMPLES = 4
BS = 32


def stage_samples():
    m = R.model()
    items = [x for x in R.base_items() if x[0] != "instructed_true"]
    batch, meta = [], []
    for st, it, msgs in items:
        for k in range(SAMPLES):
            batch.append({"messages": msgs})
            meta.append((st, it, msgs, k))
    event("samples", f"{len(batch)} sampled answers (T=0.8, {SAMPLES} per question) for tighter error bars", "🎲",
          kind="stage_info", total=len(batch))
    ans = m.generate(batch, max_new_tokens=320, bs=BS, temperature=0.8, on_batch=R.gen_progress("samples", len(batch)))
    fq = P.FOLLOWUPS[1]
    fu_batch = [{"messages": msgs + [{"role": "assistant", "content": a}, {"role": "user", "content": fq}]}
                for (st, it, msgs, k), a in zip(meta, ans)]
    fu = m.generate(fu_batch, max_new_tokens=100, bs=BS, temperature=0.8, on_batch=R.gen_progress("samples_followup", len(fu_batch)))
    rows = []
    for i, ((st, it, msgs, k), a, f, fb) in enumerate(zip(meta, ans, fu, fu_batch)):
        rows.append(dict(set=st, id=it["id"], sample=k, answer=a, label=P.label(a, it["facts"]), followup=f,
                         bare=m.probe_scores(msgs, a), fu_scores=m.probe_scores(fb["messages"], f)))
        if i % 20 == 0 or i == len(meta) - 1:
            event("samples", f"lie-tested sample {i + 1}/{len(meta)}", "🕵️", kind="progress", done=i + 1, total=len(meta), probed=2 * 20)
    write_jsonl(RESULTS / "samples.jsonl", rows)
    neu = [r["bare"]["axial"] for r in rows if r["set"] == "neutral"]
    for st in ["ccp_en", "ccp_zh", "western", "instructed_lie"]:
        a = R.auroc([r["bare"]["axial"] for r in rows if r["set"] == st], neu)
        event("samples", f"axial (sampled): {st} vs neutral AUROC {a:.3f}", "🎯", kind="finding", set=st, auroc=a)


def stage_ablation_v2():
    """Negative steering: subtract k x (mean CCP - mean neutral) at the direction's layer, then re-ask.

    The main run's projection-ablation over all layers also pushed neutral prompts toward the CCP side
    (their coordinate is negative), and clamping every position to a last-token mean breaks generation,
    so this mirrors the addition demo instead: same vector, opposite sign, one layer.
    """
    import torch

    m = R.model()
    D = torch.load(RESULTS / "direction.pt")
    layer, vec = D["layer"], D["direction"].float()
    items = [("ccp_en", it) for it in R.small(P.CCP_EN)] + [("ccp_zh", it) for it in R.small(P.CCP_ZH)] +             [("neutral", it) for it in R.small(P.NEUTRAL)[:10]]
    rows = []
    for k in (1, 2):
        m.add_steering(layer, -k * vec)
        try:
            ans = m.generate([{"messages": [{"role": "user", "content": it["q"]}]} for _, it in items],
                             max_new_tokens=320, bs=BS, on_batch=R.gen_progress(f"ablation_v2 k={k}", len(items)))
        finally:
            m.clear_hooks()
        for (st, it), a in zip(items, ans):
            rows.append(dict(k=k, set=st, id=it["id"], q=it["q"], answer=a, label=P.label(a, it["facts"]),
                             bare=m.probe_scores([{"role": "user", "content": it["q"]}], a)))
        for st in ["ccp_en", "ccp_zh", "neutral"]:
            rs = [r for r in rows if r["set"] == st and r["k"] == k]
            event("ablation_v2", f"minus {k}x political vector, {st}: {sum(r['label'] == 'accurate' for r in rs)}/{len(rs)} "
                  "accurate (keyword judge)", "✂️", kind="finding", set=st, k=k)
    write_jsonl(RESULTS / "ablation_v2.jsonl", rows)


def stage_elicit_all():
    m = R.model()
    rest = R.small([it for it in P.CCP_EN if it["id"] not in P.ELICIT_IDS])
    batch, meta = [], []
    for it in rest:
        for v, fn in P.ELICIT_VARIANTS.items():
            b = fn(it)
            batch.append(b)
            meta.append((it, v, b))
    event("elicit_all", f"knowledge test on the other {len(rest)} CCP topics ({len(batch)} asks)", "🗝️",
          kind="stage_info", total=len(batch))
    ans = m.generate(batch, max_new_tokens=320, bs=BS, on_batch=R.gen_progress("elicit_all", len(batch)))
    rows = [dict(id=it["id"], variant=v, messages=b["messages"], prefill=b.get("prefill", ""), answer=a,
                 label=P.label(b.get("prefill", "") + a, it["facts"])) for (it, v, b), a in zip(meta, ans)]
    write_jsonl(RESULTS / "elicit.jsonl", rows)
    for v in P.ELICIT_VARIANTS:
        rs = [r for r in rows if r["variant"] == v]
        event("elicit_all", f"{v}: {sum(r['label'] == 'accurate' for r in rs)}/{len(rs)} accurate", "🔓",
              kind="finding", variant=v)


def stage_thinking_more():
    m = R.model()
    its = [("ccp_en", it) for it in P.CCP_EN[30:]] + [("ccp_zh", it) for it in P.CCP_ZH] + \
          [("neutral", it) for it in P.NEUTRAL[:20]]
    its = R.small(its)
    event("thinking_more", f"thinking mode on {len(its)} more questions (incl. 20 neutral controls)", "💭",
          kind="stage_info", total=len(its))
    ans = m.generate([{"messages": [{"role": "user", "content": it["q"]}]} for _, it in its], max_new_tokens=900,
                     thinking=True, bs=BS, on_batch=R.gen_progress("thinking_more", len(its)))
    rows = []
    for (st, it), a in zip(its, ans):
        think, _, final = a.partition("</think>")
        think, final = think.replace("<think>", "").strip(), final.strip()
        r = dict(set=st, id=it["id"], q=it["q"], thinking=think, answer=final, label=P.label(final, it["facts"]),
                 think_label=P.label(think, it["facts"]))
        if final:
            r["bare"] = m.probe_scores([{"role": "user", "content": it["q"]}], final)
        rows.append(r)
    write_jsonl(RESULTS / "thinking.jsonl", rows)
    for st in ["ccp_en", "ccp_zh", "neutral"]:
        rs = [r for r in rows if r["set"] == st]
        event("thinking_more", f"{st}: facts in reasoning {sum(r['think_label'] == 'accurate' for r in rs)}/{len(rs)}, "
              f"in answer {sum(r['label'] == 'accurate' for r in rs)}/{len(rs)}", "💡", kind="finding", set=st)


def stage_scale_all():
    from huggingface_hub import hf_hub_download

    m = R.model()
    path = hf_hub_download("promptfoo/CCP-sensitive-prompts", "ccp-sensitive-prompts.csv", repo_type="dataset")
    allq = list(csv.DictReader(open(path, encoding="utf-8")))
    seen = {r["q"] for r in read_jsonl(RESULTS / "scale.jsonl")}
    todo = [q for q in allq if q["prompt"] not in seen]
    random.Random(1).shuffle(todo)
    todo = R.small(todo)
    event("scale_all", f"{len(todo)} remaining promptfoo prompts queued until the budget deadline", "📈",
          kind="stage_info", total=len(todo))
    n = 0
    for k in range(0, len(todo), BS):
        if R.DEADLINE and time.time() > R.DEADLINE - 300:
            event("scale_all", "deadline reached, wrapping up", "⏰")
            break
        chunk = todo[k:k + BS]
        ans = m.generate([{"messages": [{"role": "user", "content": q["prompt"]}]} for q in chunk], max_new_tokens=256, bs=BS)
        rows = []
        for q, a in zip(chunk, ans):
            t = a.lower()
            lab = "steered" if any(x in t for x in P.STEER_MARKERS) else (
                "refusal" if any(x in t for x in P.REFUSAL_MARKERS) else "other")
            rows.append(dict(i=-1, subject=q["subject"], q=q["prompt"], answer=a, label=lab,
                             bare=m.probe_scores([{"role": "user", "content": q["prompt"]}], a)))
        write_jsonl(RESULTS / "scale.jsonl", rows)
        n += len(rows)
        event("scale_all", f"scaled up: {n}/{len(todo)} more prompts answered + lie-tested", "📈", kind="progress",
              done=n, total=len(todo), probed=len(rows))


STAGES = [("ablation_v2", stage_ablation_v2), ("samples", stage_samples), ("elicit_all", stage_elicit_all), ("thinking_more", stage_thinking_more),
          ("scale_all", stage_scale_all)]

if __name__ == "__main__":
    event("extra", "extension run: spending the rest of the budget on more data", "🔋", kind="start_extra")
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
    event("extra", "extension run finished", "🏁", kind="all_done")
