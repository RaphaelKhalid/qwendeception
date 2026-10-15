"""Interrogate Qwen3.5-9B: does it internally treat its CCP-steered answers as deception?

Stages run in order and are resumable (results/state.json). Every stage streams events to
results/events.jsonl for the live monitor.

Env:
  QD_MODEL       chat model           (default Qwen/Qwen3.5-9B)
  QD_PROBE_KEY   probe set            (default qwen3.5-9b)
  QD_BASE_MODEL  base model           (default Qwen/Qwen3.5-9B-Base)
  QD_SMALL       if set, N items per set (dry run)
  QD_DEADLINE    unix time; the open-ended 'scale' stage stops here
  QD_STAGES      comma list to run a subset
"""

from __future__ import annotations

import csv
import gc
import json
import os
import random
import sys
import time
import traceback

import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qd import prompts as P  # noqa: E402
from qd.common import RESULTS, QModel, event, gpu_stats, read_jsonl, write_jsonl  # noqa: E402

MODEL = os.environ.get("QD_MODEL", "Qwen/Qwen3.5-9B")
PROBE_KEY = os.environ.get("QD_PROBE_KEY", "qwen3.5-9b")
BASE_MODEL = os.environ.get("QD_BASE_MODEL", "Qwen/Qwen3.5-9B-Base")
SMALL = int(os.environ.get("QD_SMALL", "0"))
DEADLINE = float(os.environ.get("QD_DEADLINE", "0"))
BS = int(os.environ.get("QD_BS", "16"))
STATE = RESULTS / "state.json"


def small(xs):
    return xs[:SMALL] if SMALL else xs


def state():
    return json.loads(STATE.read_text()) if STATE.exists() else {"done": []}


def mark_done(name):
    s = state()
    s["done"].append(name)
    STATE.write_text(json.dumps(s))


def auroc(pos, neg):
    pos = [p for p in pos if p is not None]
    neg = [n for n in neg if n is not None]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


M: QModel | None = None


def model() -> QModel:
    global M
    if M is None:
        event("load", f"loading {MODEL} + 4 deception probes", "📦")
        t = time.time()
        M = QModel(MODEL, PROBE_KEY)
        event("load", f"model ready in {time.time() - t:.0f}s, {M.n_layers} layers, probes on layers {M.probe_layers}",
              "✅", kind="ready", **gpu_stats())
    return M


def gen_progress(stage, total):
    def cb(done, ntok, tps):
        event(stage, f"generated {done}/{total} · {tps:.0f} tok/s", "🧠", kind="progress", done=done, total=total,
              tokens=ntok, tps=round(tps, 1), **gpu_stats())
    return cb


# ======================================================================================== stages
def stage_sanity_fibs():
    """Score held-in FIBS rows to confirm our rendering and the label direction (1 = deceptive follow-up)."""
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download

    m = model()
    rows = []
    for split in ["honest_honest", "honest_deceptive", "deceptive_honest", "deceptive_deceptive"]:
        path = hf_hub_download("AlignmentResearch/fibs-v1", f"data/{split}-00000-of-00001.parquet",
                               repo_type="dataset", revision="65dccf12934620bfe01db665ea3d7d0e3c9f1355")
        tab = pq.ParquetFile(path).read_row_group(0, columns=["messages_json", "completion", "tools", "source"]).to_pylist()
        random.Random(0).shuffle(tab)
        n = 0
        for r in tab:
            if r["tools"]:
                continue
            msgs = json.loads(r["messages_json"])
            if not msgs or msgs[-1]["role"] != "user":
                continue
            if any(x["role"] not in ("system", "user", "assistant") or not isinstance(x.get("content"), str) for x in msgs):
                continue
            if sum(len(x["content"]) for x in msgs) + len(r["completion"]) > 6000:
                continue
            rows.append(dict(split=split, label=int(split.endswith("deceptive")), source=r["source"],
                             messages=[{"role": x["role"], "content": x["content"]} for x in msgs],
                             completion=r["completion"]))
            n += 1
            if n >= (SMALL or 60):
                break
    out = []
    for i, r in enumerate(rows):
        s = m.probe_scores(r["messages"], r["completion"])
        out.append({**{k: r[k] for k in ("split", "label", "source")}, "scores": s})
        if i % 10 == 0 or i == len(rows) - 1:
            event("sanity_fibs", f"probed FIBS row {i + 1}/{len(rows)}", "🧪", kind="progress", done=i + 1,
                  total=len(rows), probed=1)
    write_jsonl(RESULTS / "sanity_fibs.jsonl", out)
    for arch in m.probes:
        a = auroc([o["scores"][arch] for o in out if o["label"]], [o["scores"][arch] for o in out if not o["label"]])
        event("sanity_fibs", f"{arch} probe AUROC on FIBS rows = {a:.3f}" if a is not None else f"{arch}: n/a", "🎯",
              kind="finding", arch=arch, auroc=a)


def base_items():
    """Every (set, item, messages) the generate stage answers."""
    items = []
    for it in small(P.CCP_EN):
        items.append(("ccp_en", it, [{"role": "user", "content": it["q"]}]))
    for it in small(P.CCP_ZH):
        items.append(("ccp_zh", it, [{"role": "user", "content": it["q"]}]))
    for it in small(P.NEUTRAL):
        items.append(("neutral", it, [{"role": "user", "content": it["q"]}]))
    for it in small(P.WESTERN):
        items.append(("western", it, [{"role": "user", "content": it["q"]}]))
    for it in small(P.NEUTRAL):
        items.append(("instructed_lie", it, [{"role": "system", "content": P.SYS_LIE}, {"role": "user", "content": it["q"]}]))
        items.append(("instructed_true", it, [{"role": "system", "content": P.SYS_TRUE}, {"role": "user", "content": it["q"]}]))
    return items


def stage_generate():
    m = model()
    items = base_items()
    event("generate", f"answering {len(items)} questions (CCP EN/ZH, neutral, Western, instructed lie/true)", "🚀",
          kind="stage_info", total=len(items))
    answers = m.generate([{"messages": msgs} for _, _, msgs in items], max_new_tokens=320, bs=BS,
                         on_batch=gen_progress("generate", len(items)))
    rows = []
    for (st, it, msgs), a in zip(items, answers):
        rows.append(dict(set=st, id=it["id"], q=it["q"], messages=msgs, answer=a, label=P.label(a, it["facts"])))
    # follow-ups: "Is everything you told me correct?" etc, answered by the model itself
    fu_batch, fu_meta = [], []
    for r in rows:
        for fq in P.FOLLOWUPS:
            msgs = r["messages"] + [{"role": "assistant", "content": r["answer"]}, {"role": "user", "content": fq}]
            fu_batch.append({"messages": msgs})
            fu_meta.append((r, fq, msgs))
    event("generate", f"asking {len(fu_batch)} follow-ups ('Were you being completely honest?')", "🔁",
          kind="stage_info", total=len(fu_batch))
    fu_ans = m.generate(fu_batch, max_new_tokens=120, bs=BS, on_batch=gen_progress("followups", len(fu_batch)))
    for (r, fq, msgs), a in zip(fu_meta, fu_ans):
        r.setdefault("followups", []).append({"q": fq, "messages": msgs, "answer": a})
    write_jsonl(RESULTS / "answers.jsonl", rows)
    from collections import Counter
    for st in ["ccp_en", "ccp_zh", "neutral", "western", "instructed_lie", "instructed_true"]:
        c = Counter(r["label"] for r in rows if r["set"] == st)
        event("generate", f"{st}: " + ", ".join(f"{k} {v}" for k, v in c.most_common()), "📊", kind="finding",
              set=st, labels=dict(c))


def stage_probe():
    m = model()
    rows = read_jsonl(RESULTS / "answers.jsonl")
    out = []
    for i, r in enumerate(rows):
        rec = {k: r[k] for k in ("set", "id", "label")}
        rec["bare"] = m.probe_scores(r["messages"], r["answer"])
        rec["followups"] = [m.probe_scores(f["messages"], f["answer"]) for f in r.get("followups", [])]
        out.append(rec)
        if i % 5 == 0 or i == len(rows) - 1:
            event("probe", f"lie-detector pass {i + 1}/{len(rows)} · last axial={rec['bare'].get('axial') or 0:+.2f}",
                  "🕵️", kind="progress", done=i + 1, total=len(rows), probed=1 + len(rec["followups"]))
    write_jsonl(RESULTS / "probe_scores.jsonl", out)
    for arch in m.probes:
        by = {}
        for o in out:
            by.setdefault(o["set"], []).append(o["bare"].get(arch))
        neg = by.get("neutral", [])
        for st in ["ccp_en", "ccp_zh", "western", "instructed_lie"]:
            a = auroc(by.get(st, []), neg)
            if a is not None:
                event("probe", f"{arch}: {st} vs neutral AUROC {a:.3f}", "🎯", kind="finding", arch=arch, set=st, auroc=a)


def stage_elicit():
    m = model()
    items = {it["id"]: it for it in P.CCP_EN}
    batch, meta = [], []
    for iid in small(P.ELICIT_IDS):
        for vname, fn in P.ELICIT_VARIANTS.items():
            b = fn(items[iid])
            batch.append(b)
            meta.append((iid, vname, b))
    event("elicit", f"knowledge test: {len(batch)} sideways asks (prefill, wiki, fiction, historian, German)", "🗝️",
          kind="stage_info", total=len(batch))
    ans = m.generate(batch, max_new_tokens=320, bs=BS, on_batch=gen_progress("elicit", len(batch)))
    rows = []
    for (iid, v, b), a in zip(meta, ans):
        full = b.get("prefill", "") + a
        rows.append(dict(id=iid, variant=v, messages=b["messages"], prefill=b.get("prefill", ""), answer=a,
                         label=P.label(full, items[iid]["facts"])))
    write_jsonl(RESULTS / "elicit.jsonl", rows)
    for v in P.ELICIT_VARIANTS:
        rs = [r for r in rows if r["variant"] == v]
        acc = sum(r["label"] == "accurate" for r in rs)
        event("elicit", f"{v}: {acc}/{len(rs)} accurate", "🔓" if acc else "🔒", kind="finding", variant=v,
              accurate=acc, n=len(rs))


def stage_lens():
    m = model()
    rows = []
    for name, q, prefix, true_c, steer_c in P.LENS:
        per_layer = m.logit_lens([{"role": "user", "content": q}], prefix, true_c + steer_c)
        rows.append(dict(name=name, q=q, prefix=prefix, true=true_c, steer=steer_c, layers=per_layer))
        last = per_layer[-1]
        pt, ps = sum(last[c] for c in true_c), sum(last[c] for c in steer_c)
        peak = max(range(len(per_layer)), key=lambda i: sum(per_layer[i][c] for c in true_c))
        event("lens", f"{name}: final P(true)={pt:.3f} P(steered)={ps:.3f}, truth peaks at layer {peak}", "🔭",
              kind="finding", name=name, p_true=pt, p_steer=ps, peak_layer=peak)
    write_jsonl(RESULTS / "lens.jsonl", rows)


def stage_direction():
    """Political-sensitivity direction: diff of means at the last prompt token; then ablate and add it."""
    m = model()
    ccp = [[{"role": "user", "content": it["q"]}] for it in small(P.CCP_EN)]
    neu = [[{"role": "user", "content": it["q"]}] for it in small(P.NEUTRAL)]
    A = torch.stack([m.last_token_states(x) for x in ccp])  # (n, L, d)
    B = torch.stack([m.last_token_states(x) for x in neu])
    event("direction", f"collected states for {len(ccp)} sensitive + {len(neu)} neutral prompts", "🧭",
          kind="progress", done=1, total=4)
    # pick the layer whose diff-of-means best separates held-out halves
    best, best_acc, scores = None, -1, []
    nA, nB = len(ccp) // 2, len(neu) // 2
    for l in range(m.n_layers):
        d = A[:nA, l].mean(0) - B[:nB, l].mean(0)
        pa, pb = A[nA:, l] @ d, B[nB:, l] @ d
        thr = (pa.mean() + pb.mean()) / 2
        acc = float(((pa > thr).float().sum() + (pb <= thr).float().sum()) / (len(pa) + len(pb)))
        scores.append(acc)
        if acc > best_acc + 1e-9 and m.n_layers * 0.25 <= l <= m.n_layers * 0.75:
            best, best_acc = l, acc
    direction = A[:, best].mean(0) - B[:, best].mean(0)
    torch.save({"layer": best, "direction": direction, "layer_acc": scores}, RESULTS / "direction.pt")
    event("direction", f"political direction found at layer {best} (held-out acc {best_acc:.2f})", "🧲",
          kind="finding", layer=best, acc=best_acc, done=2, total=4)

    # ablation: project the direction out of every layer, re-ask the CCP questions
    items = [("ccp_en", it) for it in small(P.CCP_EN)] + [("ccp_zh", it) for it in small(P.CCP_ZH)] + \
            [("neutral", it) for it in small(P.NEUTRAL)[:10]]
    m.add_ablation(direction)
    try:
        ans = m.generate([{"messages": [{"role": "user", "content": it["q"]}]} for _, it in items],
                         max_new_tokens=320, bs=BS, on_batch=gen_progress("ablation", len(items)))
    finally:
        m.clear_hooks()
    rows = [dict(set=st, id=it["id"], q=it["q"], answer=a, label=P.label(a, it["facts"])) for (st, it), a in zip(items, ans)]
    # score the ablated answers with the normal model
    for i, r in enumerate(rows):
        r["bare"] = m.probe_scores([{"role": "user", "content": r["q"]}], r["answer"])
    write_jsonl(RESULTS / "ablation.jsonl", rows)
    for st in ["ccp_en", "ccp_zh", "neutral"]:
        rs = [r for r in rows if r["set"] == st]
        acc = sum(r["label"] == "accurate" for r in rs)
        event("direction", f"ablated {st}: {acc}/{len(rs)} accurate", "✂️", kind="finding", set=st, accurate=acc,
              n=len(rs), done=3, total=4)

    # addition: push harmless prompts along the direction
    norm = float((A[:, best].mean(0) - B[:, best].mean(0)).norm())
    rows = []
    for k in [0, 1, 2, 4, 8]:
        if k:
            m.add_steering(best, direction / direction.norm() * norm * k)
        try:
            ans = m.generate([{"messages": [{"role": "user", "content": h}]} for h in P.HARMLESS], max_new_tokens=160, bs=BS)
        finally:
            m.clear_hooks()
        rows += [dict(scale=k, prompt=h, answer=a) for h, a in zip(P.HARMLESS, ans)]
        event("direction", f"steering x{k}: \"{ans[0][:70]}...\"", "🎛️", kind="sample", scale=k)
    write_jsonl(RESULTS / "steering.jsonl", rows)


def stage_thinking():
    m = model()
    its = small(P.CCP_EN)[:30]
    event("thinking", f"thinking mode on {len(its)} CCP questions: what does it say to itself?", "💭",
          kind="stage_info", total=len(its))
    ans = m.generate([{"messages": [{"role": "user", "content": it["q"]}]} for it in its], max_new_tokens=900,
                     thinking=True, bs=BS, on_batch=gen_progress("thinking", len(its)))
    rows = []
    for it, a in zip(its, ans):
        think, _, final = a.partition("</think>")
        think = think.replace("<think>", "").strip()
        final = final.strip()
        r = dict(id=it["id"], q=it["q"], thinking=think, answer=final, label=P.label(final, it["facts"]),
                 think_label=P.label(think, it["facts"]))
        if final:
            r["bare"] = m.probe_scores([{"role": "user", "content": it["q"]}], final)
        rows.append(r)
    write_jsonl(RESULTS / "thinking.jsonl", rows)
    knows = sum(r["think_label"] == "accurate" for r in rows)
    says = sum(r["label"] == "accurate" for r in rows)
    event("thinking", f"facts in private reasoning {knows}/{len(rows)} vs in final answer {says}/{len(rows)}", "💡",
          kind="finding", think_accurate=knows, final_accurate=says, n=len(rows))


def stage_scale():
    """Use the remaining budget: more CCP-sensitive questions from promptfoo/CCP-sensitive-prompts."""
    from huggingface_hub import hf_hub_download

    m = model()
    path = hf_hub_download("promptfoo/CCP-sensitive-prompts", "ccp-sensitive-prompts.csv", repo_type="dataset")
    qs = [r for r in csv.DictReader(open(path, encoding="utf-8"))
          if r["prompt"].split(" ")[0] in ("What", "Why", "How", "Who", "Did", "Is", "Are", "Was", "Were", "When", "Has", "Have")]
    random.Random(0).shuffle(qs)
    done_ids = {r["i"] for r in read_jsonl(RESULTS / "scale.jsonl")}
    todo = [(i, q) for i, q in enumerate(qs) if i not in done_ids]
    if SMALL:
        todo = todo[:SMALL]
    event("scale", f"{len(todo)} extra promptfoo questions queued; running until the budget deadline", "📈",
          kind="stage_info", total=len(todo))
    n = 0
    for k in range(0, len(todo), BS):
        if DEADLINE and time.time() > DEADLINE - 240:
            event("scale", "deadline reached, stopping the scale stage", "⏰")
            break
        chunk = todo[k:k + BS]
        ans = m.generate([{"messages": [{"role": "user", "content": q["prompt"]}]} for _, q in chunk],
                         max_new_tokens=256, bs=BS)
        rows = []
        for (i, q), a in zip(chunk, ans):
            t = a.lower()
            lab = "steered" if any(x in t for x in P.STEER_MARKERS) else (
                "refusal" if any(x in t for x in P.REFUSAL_MARKERS) else "other")
            rows.append(dict(i=i, subject=q["subject"], q=q["prompt"], answer=a, label=lab,
                             bare=m.probe_scores([{"role": "user", "content": q["prompt"]}], a)))
        write_jsonl(RESULTS / "scale.jsonl", rows)
        n += len(rows)
        event("scale", f"scaled up: {n} extra questions answered + lie-tested", "📈", kind="progress", done=n,
              total=len(todo), probed=len(rows))


def stage_base():
    """Base (pretrained, not chat-tuned) model: does pretraining alone carry the facts?"""
    global M
    if M is not None:
        del M
        M = None
        gc.collect()
        torch.cuda.empty_cache()
    from transformers import AutoModelForCausalLM, AutoTokenizer

    event("base", f"loading base model {BASE_MODEL}", "🏛️")
    tok = AutoTokenizer.from_pretrained(BASE_MODEL)
    bm = AutoModelForCausalLM.from_pretrained(BASE_MODEL, dtype=torch.bfloat16, device_map="cuda").eval()
    items = {it["id"]: it for it in P.CCP_EN + P.NEUTRAL}
    rows = []
    openers = small(P.BASE_OPENERS)
    for i, (iid, text) in enumerate(openers):
        ids = tok(text, return_tensors="pt").to("cuda")
        with torch.no_grad():
            g = bm.generate(**ids, max_new_tokens=160, do_sample=False, pad_token_id=tok.eos_token_id)
        cont = tok.decode(g[0][ids.input_ids.shape[1]:], skip_special_tokens=True)
        rows.append(dict(id=iid, opener=text, continuation=cont, label=P.label(text + cont, items[iid]["facts"])))
        event("base", f"base model continuation {i + 1}/{len(openers)}: {iid} -> {rows[-1]['label']}", "🏛️",
              kind="progress", done=i + 1, total=len(openers), tokens=int(g.shape[1] - ids.input_ids.shape[1]))
    write_jsonl(RESULTS / "base.jsonl", rows)
    acc = sum(r["label"] == "accurate" for r in rows if r["id"].startswith("ccp"))
    n = sum(r["id"].startswith("ccp") for r in rows)
    event("base", f"base model states the facts in {acc}/{n} CCP continuations", "🏛️", kind="finding", accurate=acc, n=n)
    del bm
    gc.collect()
    torch.cuda.empty_cache()


STAGES = [("sanity_fibs", stage_sanity_fibs), ("generate", stage_generate), ("probe", stage_probe),
          ("elicit", stage_elicit), ("lens", stage_lens), ("direction", stage_direction),
          ("thinking", stage_thinking), ("base", stage_base), ("scale", stage_scale)]


def main():
    only = [s for s in os.environ.get("QD_STAGES", "").split(",") if s]
    event("main", f"experiment starting on {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu'}",
          "🔥", kind="start", stages=[s for s, _ in STAGES])
    for name, fn in STAGES:
        if only and name not in only:
            continue
        if name in state()["done"]:
            event(name, "already done, skipping", "⏭️")
            continue
        if DEADLINE and time.time() > DEADLINE - 120:
            event(name, "skipped: out of budget time", "⏰", kind="skipped")
            continue
        t = time.time()
        event(name, f"stage {name} starting", "▶️", kind="stage_start")
        try:
            fn()
            mark_done(name)
            event(name, f"stage {name} done in {time.time() - t:.0f}s", "✅", kind="stage_done", seconds=time.time() - t)
        except Exception as e:  # keep going: one broken stage should not sink the run
            event(name, f"stage {name} FAILED: {e!r}", "💥", kind="stage_failed", tb=traceback.format_exc()[-2000:])
            torch.cuda.empty_cache()
    event("main", "all stages finished", "🏁", kind="finished")


if __name__ == "__main__":
    main()
