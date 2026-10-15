"""Third extension (runs after extra2.py): the size curve, then more 9B samples until the deadline.

  sizes         Qwen3.5-0.8B / 2B / 4B: the same CCP (EN+ZH), neutral and Western questions (greedy), the
                published 2B deception probes where they exist, and each model's own political vector
                subtracted (does uncensoring work at every size?)
  more_samples  alternating normal / vector-subtracted 9B samples of the CCP questions, probe-scored,
                until the budget deadline
Ends with an "all_done3" event, which tells the local guard to terminate the pod.
"""

from __future__ import annotations

import gc
import os
import time
import traceback

import torch

import run_experiment as R
from qd import prompts as P
from qd.common import RESULTS, QModel, event, gpu_stats, write_jsonl

BS = 32
SIZES = [s.split(":") for s in os.environ.get(
    "QD_SIZES", "Qwen/Qwen3.5-0.8B:-,Qwen/Qwen3.5-2B:qwen3.5-2b,Qwen/Qwen3.5-4B:-").split(",")]


def stage_sizes():
    for name, probe_key in SIZES:
        if R.DEADLINE and time.time() > R.DEADLINE - 600:
            event("sizes", f"skipping {name}: out of budget time", "⏰")
            continue
        event("sizes", f"loading {name}", "📦")
        m = QModel(name, None if probe_key == "-" else probe_key)
        event("sizes", f"{name} ready ({m.n_layers} layers)", "✅", **gpu_stats())
        items = [(st, it) for st, its in (("ccp_en", P.CCP_EN), ("ccp_zh", P.CCP_ZH), ("neutral", P.NEUTRAL),
                                          ("western", P.WESTERN)) for it in R.small(its)]
        ans = m.generate([{"messages": [{"role": "user", "content": it["q"]}]} for _, it in items], max_new_tokens=320,
                         bs=BS, on_batch=R.gen_progress(f"sizes {name.split('/')[-1]}", len(items)))
        # this model's own political direction: diff of means at the last prompt token, mid layers
        A = torch.stack([m.last_token_states([{"role": "user", "content": it["q"]}]) for it in R.small(P.CCP_EN)])
        B = torch.stack([m.last_token_states([{"role": "user", "content": it["q"]}]) for it in R.small(P.NEUTRAL)])
        nA, nB = len(A) // 2, len(B) // 2
        best, best_acc = None, -1.0
        for l in range(int(m.n_layers * 0.25), int(m.n_layers * 0.75) + 1):
            d = A[:nA, l].mean(0) - B[:nB, l].mean(0)
            pa, pb = A[nA:, l] @ d, B[nB:, l] @ d
            thr = (pa.mean() + pb.mean()) / 2
            acc = float(((pa > thr).float().sum() + (pb <= thr).float().sum()) / (len(pa) + len(pb)))
            if acc > best_acc:
                best, best_acc = l, acc
        vec = (A[:, best].mean(0) - B[:, best].mean(0)).float()
        ccp_items = [(st, it) for st, it in items if st.startswith("ccp")]
        m.add_steering(best, -vec)
        try:
            unc = m.generate([{"messages": [{"role": "user", "content": it["q"]}]} for _, it in ccp_items],
                             max_new_tokens=320, bs=BS)
        finally:
            m.clear_hooks()
        unc_by = {(st, it["id"]): a for (st, it), a in zip(ccp_items, unc)}
        rows = []
        for (st, it), a in zip(items, ans):
            r = dict(model=name, set=st, id=it["id"], q=it["q"], answer=a, label=P.label(a, it["facts"]),
                     direction_layer=best, direction_acc=best_acc)
            if m.probes:
                r["bare"] = m.probe_scores([{"role": "user", "content": it["q"]}], a)
            if (st, it["id"]) in unc_by:
                u = unc_by[(st, it["id"])]
                r["uncensored_answer"] = u
                r["uncensored_label"] = P.label(u, it["facts"])
                if m.probes:
                    r["uncensored_bare"] = m.probe_scores([{"role": "user", "content": it["q"]}], u)
            rows.append(r)
        write_jsonl(RESULTS / "sizes.jsonl", rows)
        steer = sum(r["label"] in ("steered", "refusal") for r in rows if r["set"] == "ccp_en")
        event("sizes", f"{name.split('/')[-1]}: {steer}/40 English CCP answers steered or refused (keyword); "
              f"direction at layer {best} (acc {best_acc:.2f})", "📏", kind="finding", model=name)
        del m
        gc.collect()
        torch.cuda.empty_cache()


def stage_more_samples():
    m = R.model()
    D = torch.load(RESULTS / "direction.pt")
    items = R.small(P.CCP_EN)
    rnd = 0
    while not (R.DEADLINE and time.time() > R.DEADLINE - 420):
        for cond in ("normal", "uncensored"):
            if cond == "uncensored":
                m.add_steering(D["layer"], -1.0 * D["direction"].float())
            try:
                ans = m.generate([{"messages": [{"role": "user", "content": it["q"]}]} for it in items],
                                 max_new_tokens=320, bs=BS, temperature=0.8)
            finally:
                m.clear_hooks()
            rows = [dict(round=rnd, cond=cond, set="ccp_en", id=it["id"], answer=a, label=P.label(a, it["facts"]),
                         bare=m.probe_scores([{"role": "user", "content": it["q"]}], a)) for it, a in zip(items, ans)]
            write_jsonl(RESULTS / "samples_more.jsonl", rows)
        rnd += 1
        event("more_samples", f"round {rnd}: {2 * len(items) * rnd} extra 9B samples (normal + uncensored) lie-tested",
              "🎲", kind="progress", done=rnd, total=rnd + 1, probed=2 * len(items))
        if R.SMALL:
            break


STAGES = [("sizes", stage_sizes), ("more_samples", stage_more_samples)]

if __name__ == "__main__":
    event("extra3", "third extension: size curve + more samples until the budget is used", "🔋", kind="start_extra")
    for name, fn in STAGES:
        if name in R.state()["done"]:
            continue
        if R.DEADLINE and time.time() > R.DEADLINE - 420:
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
            torch.cuda.empty_cache()
    event("extra3", "third extension finished", "🏁", kind="all_done3")
