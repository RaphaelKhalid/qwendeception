#!/usr/bin/env bash
# Quick look at the pod: last N events, last run.log lines, GPU use.  usage: tools/peek.sh [N]
R=results/pod.json
H=$(python -c "import json;print(json.load(open('$R'))['host'])"); P=$(python -c "import json;print(json.load(open('$R'))['port'])")
O="-i $HOME/.ssh/autolabs_runpod -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20"
ssh $O -p $P root@$H "cd /workspace/qd/results && python3 -c \"
import json
for l in open('events.jsonl'):
    e=json.loads(l); print(e['emoji'], e['stage'], e['msg'][:170])
\" | tail -${1:-14}; echo; tail -3 run.log | cut -c1-300; nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader"
