#!/usr/bin/env bash
# Runs on the pod: install, download models in parallel, then run every stage.
# usage: bash tools/pod_setup.sh <deadline-unix-ts>
set -u
cd /workspace/qd
mkdir -p results
export HF_HOME=/workspace/hf PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 QD_RESULTS=/workspace/qd/results
export QD_DEADLINE="$1"

ev() {  # ev <emoji> <stage> <message> [kind]
  python3 - "$@" <<'EOF'
import json, sys, time
emoji, stage, msg = sys.argv[1:4]
kind = sys.argv[4] if len(sys.argv) > 4 else "info"
with open("results/events.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"ts": time.time(), "src": "pod", "stage": stage, "emoji": emoji, "msg": msg, "kind": kind}, ensure_ascii=False) + "\n")
EOF
}

ev "🛬" setup "pod is up: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader)" stage_start
ev "📦" setup "installing transformers 5.5.4 + probe-inference, downloading Qwen3.5-9B (chat + base) in parallel"

pip install -q "huggingface_hub>=1.0" hf_xet > results/pip.log 2>&1
python3 - > results/download.log 2>&1 <<'EOF' &
import json, time
from huggingface_hub import snapshot_download, hf_hub_download
def ev(e, m):
    open("results/events.jsonl", "a", encoding="utf-8").write(json.dumps({"ts": time.time(), "src": "pod", "stage": "setup", "emoji": e, "msg": m, "kind": "info"}, ensure_ascii=False) + "\n")
t = time.time()
p = snapshot_download("Qwen/Qwen3.5-9B")
ev("⬇️", f"Qwen3.5-9B downloaded in {time.time()-t:.0f}s ({p.split('/')[-1][:8]})")
for s in ["honest_honest", "honest_deceptive", "deceptive_honest", "deceptive_deceptive"]:
    hf_hub_download("AlignmentResearch/fibs-v1", f"data/{s}-00000-of-00001.parquet", repo_type="dataset",
                    revision="65dccf12934620bfe01db665ea3d7d0e3c9f1355")
ev("⬇️", "FIBS sanity data downloaded")
t = time.time()
p = snapshot_download("Qwen/Qwen3.5-9B-Base")
ev("⬇️", f"Qwen3.5-9B-Base downloaded in {time.time()-t:.0f}s ({p.split('/')[-1][:8]})")
EOF
DL=$!

pip install -q "transformers==5.5.4" accelerate pyarrow \
  "git+https://github.com/AlignmentResearch/caught-in-the-act-probes.git" >> results/pip.log 2>&1 \
  && ev "✅" setup "python packages installed" \
  || ev "💥" setup "pip install failed, see pip.log"
pip install -q flash-linear-attention >> results/pip.log 2>&1 \
  && ev "⚡" setup "flash-linear-attention installed (fast Gated DeltaNet kernels)" \
  || ev "🐢" setup "flash-linear-attention unavailable, using the torch fallback"

# the chat model must be on disk before stage 1; the base model keeps downloading meanwhile
python3 - <<'EOF'
import time
from huggingface_hub import snapshot_download
while True:
    try:
        snapshot_download("Qwen/Qwen3.5-9B", local_files_only=True); break
    except Exception:
        time.sleep(5)
EOF
ev "🔥" setup "setup complete, starting the experiment" stage_done
python3 run_experiment.py > results/run.log 2>&1
CODE=$?
# always signal the end, even if the runner crashed, so the local guard terminates the pod promptly
ev "🏁" main "pod script complete (run_experiment.py exit code $CODE)" finished
