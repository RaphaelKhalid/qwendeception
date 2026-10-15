"""Build rubric-judge batches from the pod results: results/judge/in_XX.json (lists of items to label).

Each item: key, kind, question, reference (documented answer, for CCP items), text (what to judge).
Labels written by the judges go to results/judge/out_XX.json as [{"key", "label", "reason"}].
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qd import prompts as P  # noqa: E402
from qd.truths import truth_for  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
POD = ROOT / "results" / "pod"
OUT = ROOT / "results" / "judge"
OUT.mkdir(parents=True, exist_ok=True)
Q = {it["id"]: it for it in P.CCP_EN + P.CCP_ZH + P.NEUTRAL + P.WESTERN}


def load(name):
    p = POD / name
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()] if p.exists() else []


def cut(s, n):
    return s if len(s) <= n else s[:n] + " …[truncated]"


items = []


def add(key, kind, qid, question, text):
    items.append(dict(key=key, kind=kind, question=question, reference=truth_for(qid) or "", text=cut(text, 1800)))


for r in load("answers.jsonl"):
    kind = {"instructed_lie": "lie_instruction", "instructed_true": "plain"}.get(r["set"], "ccp" if r["set"].startswith("ccp") else "plain")
    add(f"answers|{r['set']}|{r['id']}", kind, r["id"], r["q"], r["answer"])
for r in load("elicit.jsonl"):
    add(f"elicit|{r['variant']}|{r['id']}", "ccp", r["id"], Q[r["id"]]["q"] + f"  [asked as: {r['variant']}]",
        (r.get("prefill") or "") + r["answer"])
for r in load("ablation.jsonl"):
    add(f"ablation|{r['set']}|{r['id']}", "ccp" if r["set"].startswith("ccp") else "plain", r["id"], r["q"], r["answer"])
for r in load("ablation_v2.jsonl"):
    add(f"ablation_v2|k{r['k']}|{r['set']}|{r['id']}", "ccp" if r["set"].startswith("ccp") else "plain", r["id"], r["q"], r["answer"])
for r in load("thinking.jsonl"):
    st = r.get("set", "ccp_en")
    kind = "ccp" if st.startswith("ccp") else "plain"
    add(f"thinking_answer|{st}|{r['id']}", kind, r["id"], r["q"], r["answer"] or "(no final answer: ran out of tokens while thinking)")
    add(f"thinking_reasoning|{st}|{r['id']}", "reasoning", r["id"], r["q"], cut(r["thinking"], 2600))
for r in load("base.jsonl"):
    add(f"base|{r['id']}", "ccp" if r["id"].startswith("ccp") else "plain", r["id"], "(base-model continuation of an encyclopedia opener)",
        r["opener"] + r["continuation"])
for r in load("samples.jsonl"):
    if r["set"] in ("ccp_en", "ccp_zh", "instructed_lie"):
        add(f"samples|{r['set']}|{r['id']}|{r['sample']}", "lie_instruction" if r["set"] == "instructed_lie" else "ccp",
            r["id"], Q[r["id"]]["q"], r["answer"])

for r in load("samples_uncensored.jsonl"):
    add(f"samples_unc|{r['set']}|{r['id']}|{r['sample']}", "ccp", r["id"], Q[r["id"]]["q"], r["answer"])
for r in load("samples_more.jsonl"):
    if r["round"] >= 5:  # judge the first 5 rounds (400 answers); later rounds feed the unlabelled analyses only
        continue
    add(f"samples_more|{r['cond']}|{r['round']}|{r['id']}", "ccp", r["id"], Q[r["id"]]["q"], r["answer"])
for r in load("sizes.jsonl"):
    if r["set"].startswith("ccp"):
        add(f"sizes|{r['model']}|{r['set']}|{r['id']}", "ccp", r["id"], r["q"], r["answer"])
        if r.get("uncensored_answer") is not None and r["set"] == "ccp_en":
            add(f"sizes_unc|{r['model']}|ccp_en|{r['id']}", "ccp", r["id"], r["q"], r["uncensored_answer"])

# only items no judge has labelled yet; each call writes a new pass of batches
done = {x["key"] for p in OUT.glob("out_*.json") for x in json.loads(p.read_text(encoding="utf-8"))}
todo = [x for x in items if x["key"] not in done]
pass_no = len({p.name.split("_")[1] for p in OUT.glob("in_*.json")})
CHUNK = 110
for i in range(0, len(todo), CHUNK):
    (OUT / f"in_p{pass_no}_{i // CHUNK:02d}.json").write_text(json.dumps(todo[i:i + CHUNK], ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{len(items)} items total, {len(done)} already labelled, {len(todo)} new in pass p{pass_no}")
