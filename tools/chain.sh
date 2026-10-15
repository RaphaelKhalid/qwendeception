#!/usr/bin/env bash
# wait for the main run (and its setup wrapper) to end, then run the extension, then signal all_done
cd /workspace/qd
export HF_HOME=/workspace/hf PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 QD_RESULTS=/workspace/qd/results QD_DEADLINE=1792071213
while pgrep -f "run_experiment.py" > /dev/null || pgrep -f "pod_setup.sh" > /dev/null; do sleep 10; done
python3 extra.py > results/extra.log 2>&1
CODE=$?
python3 -c "
import json,time
open('results/events.jsonl','a').write(json.dumps({'ts':time.time(),'src':'pod','stage':'main','emoji':'🏁','msg':'all runs complete (extra.py exit code $CODE)','kind':'all_done'})+'\n')"
