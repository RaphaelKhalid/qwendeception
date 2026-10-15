#!/usr/bin/env bash
# after the first extension (extra.py) ends, run the second one, then signal all_done2
cd /workspace/qd
export HF_HOME=/workspace/hf PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 QD_RESULTS=/workspace/qd/results QD_DEADLINE=1792071213
while pgrep -f "chain.sh" > /dev/null || pgrep -f "extra.py" > /dev/null || pgrep -f "run_experiment.py" > /dev/null; do sleep 10; done
python3 extra2.py > results/extra2.log 2>&1
CODE=$?
python3 -c "
import json,time
open('results/events.jsonl','a').write(json.dumps({'ts':time.time(),'src':'pod','stage':'main','emoji':'🏁','msg':'everything complete (extra2.py exit code $CODE)','kind':'all_done2'})+'\n')"
