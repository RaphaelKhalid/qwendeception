#!/usr/bin/env bash
# after the second extension (extra2.py) ends, run the third one, then signal all_done3
cd /workspace/qd
export HF_HOME=/workspace/hf PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 QD_RESULTS=/workspace/qd/results QD_DEADLINE=1792071213
while pgrep -f "chain2.sh" > /dev/null || pgrep -f "extra2.py" > /dev/null || pgrep -f "run_experiment.py" > /dev/null; do sleep 10; done
python3 extra3.py > results/extra3.log 2>&1
CODE=$?
python3 -c "
import json,time
open('results/events.jsonl','a').write(json.dumps({'ts':time.time(),'src':'pod','stage':'main','emoji':'🏁','msg':'everything complete (extra3.py exit code $CODE)','kind':'all_done3'})+'\n')"
