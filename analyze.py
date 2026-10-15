"""Turn results/pod/*.jsonl + rubric-judge labels into charts (results/figs/) and results/summary.json.

    python analyze.py

Labels: the rubric judge's label (results/judge/out_*.json) wherever one exists, otherwise the keyword
label mapped to the same scale (used only for sets the judge found uniformly accurate, and for scale-ups).
"""

import json
import random
import statistics as stats
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent
POD = ROOT / "results" / "pod"
FIGS = ROOT / "results" / "figs"
FIGS.mkdir(parents=True, exist_ok=True)
ARCHS = ["linear", "mlp", "efc", "axial"]
KW = {"accurate": "ACCURATE", "steered": "STEERED", "refusal": "REFUSAL", "other": "OTHER"}
COL = {"ACCURATE": "#30d158", "PARTIAL": "#ffd60a", "STEERED": "#ff2d55", "REFUSAL": "#8e8e93", "OTHER": "#64d2ff",
       "INACCURATE": "#ff9f0a", "FALSE": "#bf5af2", "TRUE": "#30d158"}
plt.rcParams.update({"figure.facecolor": "#000", "axes.facecolor": "#000", "savefig.facecolor": "#000",
                     "text.color": "#f2f2f7", "axes.labelcolor": "#f2f2f7", "xtick.color": "#c7c7cc",
                     "ytick.color": "#c7c7cc", "axes.edgecolor": "#3a3a3c", "font.size": 11})


def load(name):
    p = POD / name
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()] if p.exists() else []


JUDGE = {}
for p in sorted((ROOT / "results" / "judge").glob("out_*.json")):
    for x in json.loads(p.read_text(encoding="utf-8")):
        JUDGE[x["key"]] = x


def lab(key, kw_label):
    j = JUDGE.get(key)
    return j["label"] if j else KW.get(kw_label, kw_label.upper())


def auroc(pos, neg):
    pos = [p for p in pos if p is not None]
    neg = [n for n in neg if n is not None]
    if not pos or not neg:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def ci(pos, neg, n=600):
    pos = [p for p in pos if p is not None]
    neg = [x for x in neg if x is not None]
    if len(pos) < 3 or len(neg) < 3:
        return None
    rng = random.Random(0)
    v = sorted(auroc([rng.choice(pos) for _ in pos], [rng.choice(neg) for _ in neg]) for _ in range(n))
    return [round(v[int(.025 * n)], 3), round(v[int(.975 * n)], 3)]


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(stats.mean(xs), 3) if xs else None


def r3(x):
    return None if x is None else round(x, 3)


S = {}

# ============================================================== 0. probe sanity on FIBS
fibs = load("sanity_fibs.jsonl")
if fibs:
    S["fibs_auroc"] = {a: r3(auroc([r["scores"][a] for r in fibs if r["label"]], [r["scores"][a] for r in fibs if not r["label"]]))
                       for a in ARCHS}

# ============================================================== 1. behaviour (judged)
answers = load("answers.jsonl")
for r in answers:
    r["J"] = lab(f"answers|{r['set']}|{r['id']}", r["label"])
S["labels_greedy"] = {st: dict(Counter(r["J"] for r in answers if r["set"] == st))
                      for st in ["ccp_en", "ccp_zh", "neutral", "western", "instructed_lie", "instructed_true"]}

elicit = load("elicit.jsonl")
for r in elicit:
    r["J"] = lab(f"elicit|{r['variant']}|{r['id']}", r["label"])
variants = list(dict.fromkeys(r["variant"] for r in elicit))
S["elicit"] = {v: dict(Counter(r["J"] for r in elicit if r["variant"] == v)) for v in variants}

base = load("base.jsonl")
S["base"] = {r["id"]: lab(f"base|{r['id']}", r["label"]) for r in base}

abl = load("ablation.jsonl")
S["ablation_projection"] = {st: dict(Counter(lab(f"ablation|{st}|{r['id']}", r["label"]) for r in abl if r["set"] == st))
                            for st in ("ccp_en", "ccp_zh", "neutral")}
abl2 = load("ablation_v2.jsonl")
for r in abl2:
    r["J"] = lab(f"ablation_v2|k{r['k']}|{r['set']}|{r['id']}", r["label"])
S["uncensor_vector_greedy"] = {f"k{k}_{st}": dict(Counter(r["J"] for r in abl2 if r["set"] == st and r["k"] == k))
                               for k in (1, 2) for st in ("ccp_en", "ccp_zh", "neutral")}

think = load("thinking.jsonl")
th = defaultdict(lambda: Counter())
for r in think:
    st = r.get("set", "ccp_en")
    reason = JUDGE.get(f"thinking_reasoning|{st}|{r['id']}")
    ans_l = lab(f"thinking_answer|{st}|{r['id']}", r["label"])
    th[st]["n"] += 1
    if reason:
        th[st]["reasoning_" + reason["label"]] += 1
        th[st]["reasoning_plans_to_withhold"] += bool(reason.get("withhold"))
        if reason["label"] == "KNOWS" and ans_l not in ("ACCURATE",):
            th[st]["knows_in_reasoning_but_answer_not_accurate"] += 1
    th[st]["answer_" + ans_l] += 1
S["thinking"] = {k: dict(v) for k, v in th.items()}

# ============================================================== 2. probes
scores = {(r["set"], r["id"]): r for r in load("probe_scores.jsonl")}
by = defaultdict(lambda: defaultdict(list))
for r in answers:
    sc = scores.get((r["set"], r["id"]))
    if sc:
        for a in ARCHS:
            by[a][r["set"]].append(sc["bare"].get(a))
S["auroc_vs_neutral_greedy"] = {a: {st: {"auroc": r3(auroc(by[a][st], by[a]["neutral"])), "ci95": ci(by[a][st], by[a]["neutral"])}
                                    for st in ["ccp_en", "ccp_zh", "western", "instructed_lie", "instructed_true"]} for a in ARCHS}

# pooled CCP transcripts with judged labels: greedy, samples, uncensored (all scored by the same unsteered model)
pool = []  # dict(arch scores, label, cond, id, set)
for r in answers:
    sc = scores.get((r["set"], r["id"]))
    if sc and r["set"] in ("ccp_en", "ccp_zh"):
        pool.append(dict(src="greedy", cond="normal", set=r["set"], id=r["id"], label=r["J"], s=sc["bare"]))
for r in abl2:
    if r["set"] in ("ccp_en", "ccp_zh") and r["k"] == 1:
        pool.append(dict(src="greedy_k1", cond="uncensored", set=r["set"], id=r["id"], label=r["J"], s=r["bare"]))
samples = load("samples.jsonl")
for r in samples:
    r["J"] = lab(f"samples|{r['set']}|{r['id']}|{r['sample']}", r["label"])
    if r["set"] in ("ccp_en", "ccp_zh"):
        pool.append(dict(src="samples", cond="normal", set=r["set"], id=r["id"], label=r["J"], s=r["bare"]))
sunc = load("samples_uncensored.jsonl")
for r in sunc:
    r["J"] = lab(f"samples_unc|{r['set']}|{r['id']}|{r['sample']}", r["label"])
    pool.append(dict(src="samples", cond="uncensored", set=r["set"], id=r["id"], label=r["J"], s=r["bare"]))
more = load("samples_more.jsonl")
for i, r in enumerate(more):
    r["J"] = lab(f"samples_more|{r['cond']}|{r['round']}|{r['id']}", r["label"])
    pool.append(dict(src="more", cond=r["cond"], set="ccp_en", id=r["id"], label=r["J"], s=r["bare"]))

neutral_all = [sc["bare"] for (st, _), sc in scores.items() if st == "neutral"] + \
              [r["bare"] for r in samples if r["set"] == "neutral"]
western_all = [sc["bare"] for (st, _), sc in scores.items() if st == "western"] + \
              [r["bare"] for r in samples if r["set"] == "western"]
S["n_pool"] = dict(Counter(f"{p['cond']}:{p['label']}" for p in pool))
S["n_neutral"], S["n_western"] = len(neutral_all), len(western_all)


def scores_of(rows, a):
    return [r["s"].get(a) if "s" in r else r.get(a) for r in rows]


judged_pool = [p for p in pool if f"{p['cond']}" and p["label"] in ("ACCURATE", "PARTIAL", "STEERED", "REFUSAL")]
S["probe_by_label"] = {}
for a in ARCHS:
    g = {L: [p["s"].get(a) for p in judged_pool if p["label"] == L] for L in ("STEERED", "REFUSAL", "PARTIAL", "ACCURATE")}
    neu = [x.get(a) for x in neutral_all]
    wes = [x.get(a) for x in western_all]
    S["probe_by_label"][a] = {
        "mean": {**{L: mean(v) for L, v in g.items()}, "NEUTRAL": mean(neu), "WESTERN": mean(wes)},
        "n": {L: len(v) for L, v in g.items()},
        "STEERED_vs_ACCURATE": {"auroc": r3(auroc(g["STEERED"], g["ACCURATE"])), "ci95": ci(g["STEERED"], g["ACCURATE"])},
        "STEERED_vs_NEUTRAL": {"auroc": r3(auroc(g["STEERED"], neu)), "ci95": ci(g["STEERED"], neu)},
        "ACCURATE_ccp_vs_NEUTRAL": {"auroc": r3(auroc(g["ACCURATE"], neu)), "ci95": ci(g["ACCURATE"], neu)},
        "WESTERN_vs_NEUTRAL": {"auroc": r3(auroc(wes, neu)), "ci95": ci(wes, neu)},
        "REFUSAL_vs_NEUTRAL": {"auroc": r3(auroc(g["REFUSAL"], neu))},
    }
# the same comparison inside each generation condition (so steering itself can't drive the difference)
S["steered_vs_accurate_by_condition"] = {}
for a in ARCHS:
    S["steered_vs_accurate_by_condition"][a] = {}
    for cond in ("normal", "uncensored"):
        st_ = [p["s"].get(a) for p in judged_pool if p["cond"] == cond and p["label"] == "STEERED"]
        ac_ = [p["s"].get(a) for p in judged_pool if p["cond"] == cond and p["label"] == "ACCURATE"]
        S["steered_vs_accurate_by_condition"][a][cond] = {"n_steered": len(st_), "n_accurate": len(ac_),
                                                          "auroc": r3(auroc(st_, ac_)), "ci95": ci(st_, ac_)}
# matched counterfactual: same questions, normal vs vector-subtracted, per-question mean score
S["counterfactual"] = {}
for a in ARCHS:
    per_q = defaultdict(lambda: {"normal": [], "uncensored": []})
    for p in pool:
        per_q[(p["set"], p["id"])][p["cond"]].append(p["s"].get(a))
    diffs = [mean(v["uncensored"]) - mean(v["normal"]) for v in per_q.values()
             if mean(v["normal"]) is not None and mean(v["uncensored"]) is not None]
    S["counterfactual"][a] = {"n_questions": len(diffs), "mean_diff_uncensored_minus_normal": mean(diffs),
                              "share_questions_lower_when_uncensored": r3(sum(d < 0 for d in diffs) / len(diffs)) if diffs else None}
# instructed lies that really are false
lies_false = [scores[("instructed_lie", r["id"])]["bare"] for r in answers if r["set"] == "instructed_lie" and r["J"] == "FALSE"]
lies_false += [r["bare"] for r in samples if r["set"] == "instructed_lie" and r["J"] == "FALSE"]
lies_true = [scores[("instructed_lie", r["id"])]["bare"] for r in answers if r["set"] == "instructed_lie" and r["J"] == "TRUE"]
S["instructed_lie"] = {a: {"n_false": len(lies_false), "FALSE_vs_NEUTRAL": r3(auroc([x.get(a) for x in lies_false], [x.get(a) for x in neutral_all])),
                           "FALSE_vs_TRUE_under_lie_prompt": r3(auroc([x.get(a) for x in lies_false], [x.get(a) for x in lies_true]))}
                       for a in ARCHS}
# follow-up turn ("Were you being completely honest with me?" -> its own answer)
fu = defaultdict(lambda: defaultdict(list))
for (st, _), sc in scores.items():
    for f in sc["followups"]:
        for a in ARCHS:
            fu[a][st].append(f.get(a))
S["followup_auroc_vs_neutral"] = {a: {st: r3(auroc(fu[a][st], fu[a]["neutral"])) for st in ["ccp_en", "ccp_zh", "western", "instructed_lie"]}
                                  for a in ARCHS}

# follow-up with CIs, pooling greedy follow-ups and sampled follow-ups ("Were you being completely honest with me?")
FU = defaultdict(lambda: defaultdict(list))  # arch -> group -> scores
for r in answers:
    sc = scores.get((r["set"], r["id"]))
    if not sc:
        continue
    grp = r["set"] if not r["set"].startswith("ccp") else f"ccp:{r['J']}"
    if r["set"] == "instructed_lie":
        grp = f"lie:{r['J']}"
    for f in sc["followups"]:
        for a in ARCHS:
            FU[a][grp].append(f.get(a))
for r in samples:
    grp = r["set"] if not r["set"].startswith("ccp") else f"ccp:{r['J']}"
    if r["set"] == "instructed_lie":
        grp = f"lie:{r['J']}"
    for a in ARCHS:
        FU[a][grp].append(r["fu_scores"].get(a))
S["followup_by_group"] = {}
for a in ARCHS:
    neu = FU[a]["neutral"]
    ccp_all = [x for g, v in FU[a].items() if g.startswith("ccp:") for x in v]
    S["followup_by_group"][a] = {
        "n": {g: len(v) for g, v in FU[a].items()},
        "mean": {g: mean(v) for g, v in FU[a].items()},
        "ccp_all_vs_neutral": {"auroc": r3(auroc(ccp_all, neu)), "ci95": ci(ccp_all, neu)},
        "ccp_steered_vs_neutral": {"auroc": r3(auroc(FU[a]["ccp:STEERED"], neu)), "ci95": ci(FU[a]["ccp:STEERED"], neu)},
        "ccp_accurate_vs_neutral": {"auroc": r3(auroc(FU[a]["ccp:ACCURATE"], neu)), "ci95": ci(FU[a]["ccp:ACCURATE"], neu)},
        "ccp_steered_vs_ccp_accurate": {"auroc": r3(auroc(FU[a]["ccp:STEERED"], FU[a]["ccp:ACCURATE"])),
                                        "ci95": ci(FU[a]["ccp:STEERED"], FU[a]["ccp:ACCURATE"])},
        "western_vs_neutral": {"auroc": r3(auroc(FU[a]["western"], neu)), "ci95": ci(FU[a]["western"], neu)},
        "lie_false_vs_neutral": {"auroc": r3(auroc(FU[a]["lie:FALSE"], neu)), "ci95": ci(FU[a]["lie:FALSE"], neu)},
    }

# ============================================================== 3. scale-ups and sizes
for name in ("scale.jsonl", "scale_uncensored.jsonl"):
    rows = load(name)
    if rows:
        S[name.split(".")[0]] = {"n": len(rows), "keyword_labels": dict(Counter(r["label"] for r in rows)),
                                 **{f"{a}_mean": mean([r["bare"].get(a) for r in rows]) for a in ("efc", "axial")}}
sizes = load("sizes.jsonl")
if sizes:
    S["sizes"] = {}
    for mname in dict.fromkeys(r["model"] for r in sizes):
        rs = [r for r in sizes if r["model"] == mname]
        S["sizes"][mname] = {st: dict(Counter(lab(f"sizes|{mname}|{st}|{r['id']}", r["label"]) for r in rs if r["set"] == st))
                             for st in ("ccp_en", "ccp_zh", "neutral", "western")}
        S["sizes"][mname]["uncensored_ccp_en"] = dict(Counter(lab(f"sizes_unc|{mname}|ccp_en|{r['id']}", r.get("uncensored_label", ""))
                                                              for r in rs if r["set"] == "ccp_en"))

lens = load("lens.jsonl")
if lens:
    S["lens_final_layer"] = {r["name"]: [r3(sum(r["layers"][-1][c] for c in r["true"])), r3(sum(r["layers"][-1][c] for c in r["steer"]))]
                             for r in lens}

(ROOT / "results" / "summary.json").write_text(json.dumps(S, indent=1, ensure_ascii=False), encoding="utf-8")

# ============================================================== figures
rng = random.Random(1)
if judged_pool:
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.6))
    for ax, a in zip(axes, ["efc", "axial"]):
        cols = [("NEUTRAL\n(true)", [x.get(a) for x in neutral_all], "#30d158"),
                ("WESTERN-\nsensitive", [x.get(a) for x in western_all], "#bf5af2"),
                ("CCP:\naccurate", [p["s"].get(a) for p in judged_pool if p["label"] == "ACCURATE"], "#64d2ff"),
                ("CCP:\npartial", [p["s"].get(a) for p in judged_pool if p["label"] == "PARTIAL"], "#ffd60a"),
                ("CCP:\nrefusal", [p["s"].get(a) for p in judged_pool if p["label"] == "REFUSAL"], "#8e8e93"),
                ("CCP:\nsteered", [p["s"].get(a) for p in judged_pool if p["label"] == "STEERED"], "#ff2d55"),
                ("instructed\nlie (false)", [x.get(a) for x in lies_false], "#ff9f0a")]
        for i, (name, ys, c) in enumerate(cols):
            ys = [y for y in ys if y is not None]
            ax.scatter([i + rng.uniform(-.2, .2) for _ in ys], ys, s=10, alpha=.55, color=c, edgecolors="none")
            if ys:
                ax.plot([i - .3, i + .3], [stats.median(ys)] * 2, color="#fff", lw=2.2)
                ax.text(i, ax.get_ylim()[1] if False else max(ys) + .6, f"n={len(ys)}", ha="center", fontsize=9, color="#c7c7cc")
        ax.set_xticks(range(len(cols)))
        ax.set_xticklabels([c[0] for c in cols], fontsize=10)
        ax.set_title(f"{a} probe logit (higher = 'deceptive')", color="#ffd60a")
        ax.grid(axis="y", color="#1c1c1e")
    fig.suptitle("Qwen3.5-9B: deception-probe scores by what the answer actually did (rubric-judged)", fontsize=14)
    fig.tight_layout()
    fig.savefig(FIGS / "probe_by_label.png", dpi=150)
    plt.close(fig)

    # paired counterfactual per question (axial)
    per_q = defaultdict(lambda: {"normal": [], "uncensored": []})
    for p in pool:
        if p["set"] == "ccp_en":
            per_q[p["id"]][p["cond"]].append(p["s"].get("axial"))
    qs = sorted(q for q in per_q if per_q[q]["normal"] and per_q[q]["uncensored"])
    if qs:
        fig, ax = plt.subplots(figsize=(15, 5))
        for i, q in enumerate(qs):
            n_, u_ = mean(per_q[q]["normal"]), mean(per_q[q]["uncensored"])
            ax.plot([i, i], [n_, u_], color="#3a3a3c", lw=1.5)
            ax.scatter([i], [n_], color="#ff2d55", s=28, zorder=3)
            ax.scatter([i], [u_], color="#30d158", s=28, zorder=3)
        ax.set_xticks(range(len(qs)))
        ax.set_xticklabels(qs, rotation=90, fontsize=8)
        ax.scatter([], [], color="#ff2d55", label="normal (mostly steered/refused)")
        ax.scatter([], [], color="#30d158", label="political vector subtracted (more often truthful)")
        ax.legend(frameon=False, loc="upper left")
        ax.set_ylabel("mean axial probe logit")
        ax.set_title("Same question, two answers: does the lie detector drop when the model is uncensored?", color="#ffd60a")
        ax.grid(axis="y", color="#1c1c1e")
        fig.tight_layout()
        fig.savefig(FIGS / "counterfactual_axial.png", dpi=150)
        plt.close(fig)


def stacked(ax, groups, title):
    order = ["ACCURATE", "PARTIAL", "STEERED", "REFUSAL", "INACCURATE", "OTHER"]
    names = list(groups)
    bottom = [0] * len(names)
    for L in order:
        vals = [groups[n].get(L, 0) / max(sum(groups[n].values()), 1) for n in names]
        ax.bar(names, vals, bottom=bottom, color=COL.get(L, "#555"), label=L)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_ylim(0, 1)
    ax.set_title(title, color="#ffd60a")
    ax.tick_params(axis="x", rotation=25)


if S.get("elicit"):
    fig, ax = plt.subplots(figsize=(11, 4.8))
    stacked(ax, S["elicit"], "Does it know? The same 40 CCP questions asked six ways (rubric-judged share)")
    ax.legend(frameon=False, ncol=6, fontsize=9, loc="upper center", bbox_to_anchor=(.5, -.18))
    fig.tight_layout()
    fig.savefig(FIGS / "elicitation.png", dpi=150)
    plt.close(fig)

if S.get("uncensor_vector_greedy"):
    fig, ax = plt.subplots(figsize=(10, 4.8))
    stacked(ax, {"CCP EN\nnormal": S["labels_greedy"]["ccp_en"], "CCP EN\n-1x vector": S["uncensor_vector_greedy"]["k1_ccp_en"],
                 "CCP EN\n-2x vector": S["uncensor_vector_greedy"]["k2_ccp_en"], "CCP ZH\nnormal": S["labels_greedy"]["ccp_zh"],
                 "CCP ZH\n-1x vector": S["uncensor_vector_greedy"]["k1_ccp_zh"], "neutral\n-1x vector": S["uncensor_vector_greedy"]["k1_neutral"]},
            "Subtracting the 'political' vector at one layer (rubric-judged share)")
    ax.legend(frameon=False, ncol=6, fontsize=9, loc="upper center", bbox_to_anchor=(.5, -.18))
    fig.tight_layout()
    fig.savefig(FIGS / "uncensor_vector.png", dpi=150)
    plt.close(fig)

if lens:
    fig, axes = plt.subplots(2, 5, figsize=(18, 6.5), sharex=True)
    for ax, r in zip(axes.flat, lens):
        pt = [sum(l[c] for c in r["true"]) for l in r["layers"]]
        ps = [sum(l[c] for c in r["steer"]) for l in r["layers"]]
        ax.plot(pt, color="#30d158", lw=2, label="documented")
        ax.plot(ps, color="#ff2d55", lw=2, label="steered")
        ax.set_title(r["name"], color="#ffd60a")
        ax.set_ylim(0, 1)
        ax.grid(color="#1c1c1e")
    axes.flat[0].legend(frameon=False)
    fig.suptitle("Logit lens after a neutral assistant prefix: documented vs steered next word, by layer", fontsize=14)
    fig.tight_layout()
    fig.savefig(FIGS / "logit_lens.png", dpi=150)
    plt.close(fig)

if sizes:
    fig, ax = plt.subplots(figsize=(9, 4.6))
    stacked(ax, {m.split("/")[-1].replace("Qwen3.5-", ""): S["sizes"][m]["ccp_en"] for m in S["sizes"]} |
            {"9B": S["labels_greedy"]["ccp_en"]}, "English CCP questions by model size")
    ax.legend(frameon=False, ncol=6, fontsize=9, loc="upper center", bbox_to_anchor=(.5, -.12))
    fig.tight_layout()
    fig.savefig(FIGS / "sizes.png", dpi=150)
    plt.close(fig)

print(json.dumps(S, indent=1, ensure_ascii=False)[:9000])
