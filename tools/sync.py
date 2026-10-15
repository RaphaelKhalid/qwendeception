"""Mirror the pod's event log and results to this machine, track spend, and terminate the pod.

Reads results/pod.json: {"pod_id", "host", "port", "price", "cap", "started_at", "gpu"}.
- streams new pod events into results/events.jsonl every few seconds (for the live monitor)
- copies result files every minute
- writes a cost event every 10 s
- when the pod reports "finished", or spend reaches the cap: final copy, then terminates the pod
  through the RunPod API (RUNPOD_API_KEY) and exits.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
EVENTS = RES / "events.jsonl"
POD_DIR = "/workspace/qd/results"
KEY = str(Path.home() / ".ssh" / "autolabs_runpod")
# event kind that means "nothing left to run": "finished" (main run) or "all_done" (after extra.py)
DONE_KIND = os.environ.get("QD_DONE_KIND", "finished")


def local_event(stage, msg, emoji="🛰️", **kw):
    rec = {"ts": time.time(), "src": "local", "stage": stage, "emoji": emoji, "msg": msg, **kw}
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if kw.get("kind") != "cost":
        print(f"{emoji} {msg}", flush=True)


def ssh_args(pod):
    return ["-i", KEY, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=15"]


def ssh(pod, cmd, timeout=60):
    return subprocess.run(["ssh", *ssh_args(pod), "-p", str(pod["port"]), f"root@{pod['host']}", cmd],
                          capture_output=True, timeout=timeout)


def pull_results(pod):
    r = ssh(pod, f"ls {POD_DIR}")
    if r.returncode != 0:
        return False
    names = [n for n in r.stdout.decode().split() if n != "events.jsonl"]
    pod_res = RES / "pod"
    pod_res.mkdir(exist_ok=True)
    for n in names + ["events.jsonl"]:
        subprocess.run(["scp", *ssh_args(pod), "-P", str(pod["port"]), f"root@{pod['host']}:{POD_DIR}/{n}",
                        str(pod_res / n)], capture_output=True, timeout=300)
    return True


def terminate(pod):
    key = os.environ.get("RUNPOD_API_KEY", "")
    pid = pod["pod_id"]
    try:
        req = urllib.request.Request(f"https://rest.runpod.io/v1/pods/{pid}", method="DELETE",
                                     headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return f"REST {resp.status}"
    except Exception as e:  # fall back to GraphQL
        body = json.dumps({"query": f'mutation {{ podTerminate(input: {{podId: "{pid}"}}) }}'}).encode()
        req = urllib.request.Request("https://api.runpod.io/graphql", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return f"GraphQL {resp.status} {resp.read()[:120]!r} (REST failed: {e!r})"
        except Exception as e2:
            return f"FAILED: {e!r} / {e2!r}"


def main():
    pod = json.loads((RES / "pod.json").read_text())
    offset = int(pod.get("offset", 0))
    last_pull = last_cost = 0.0
    fails = 0
    local_event("sync", f"watching pod {pod['pod_id']} ({pod['gpu']} @ ${pod['price']}/hr, cap ${pod['cap']})", "🛰️")
    while True:
        fresh = json.loads((RES / "pod.json").read_text())  # host/port get filled in once SSH is up
        pod.update({k: fresh[k] for k in ("host", "port") if k in fresh})
        now = time.time()
        spent = (now - pod["started_at"]) / 3600 * pod["price"]
        if now - last_cost >= 10:
            local_event("budget", f"${spent:.3f} spent", "💸", kind="cost", spent=spent, cap=pod["cap"], rate=pod["price"],
                        elapsed=now - pod["started_at"], running=True, gpu=pod["gpu"])
            last_cost = now
        finished = False
        try:
            r = ssh(pod, f"tail -c +{offset + 1} {POD_DIR}/events.jsonl 2>/dev/null", timeout=40)
            if r.returncode == 0:
                fails = 0
                data = r.stdout
                cut = data.rfind(b"\n") + 1
                if cut:
                    with open(EVENTS, "ab") as f:
                        f.write(data[:cut])
                    offset += cut
                    pod["offset"] = offset
                    (RES / "pod.json").write_text(json.dumps(pod))
                    finished = f'"kind": "{DONE_KIND}"'.encode() in data[:cut]
            else:
                fails += 1
        except subprocess.TimeoutExpired:
            fails += 1
        if fails and fails % 12 == 0:
            local_event("sync", f"pod unreachable for {fails} polls (still booting?)", "📡")
        if now - last_pull >= 60:
            pull_results(pod)
            last_pull = now
        if finished or spent >= pod["cap"]:
            why = "experiment finished" if finished else f"budget cap ${pod['cap']} reached"
            local_event("sync", f"{why}: copying final results…", "📥")
            pull_results(pod)
            res = terminate(pod)
            spent = (time.time() - pod["started_at"]) / 3600 * pod["price"]
            local_event("sync", f"pod {pod['pod_id']} terminated ({res}) · final spend ≈ ${spent:.3f}", "🛑",
                        kind="terminated")
            local_event("budget", f"${spent:.3f} spent", "💸", kind="cost", spent=spent, cap=pod["cap"],
                        rate=pod["price"], elapsed=time.time() - pod["started_at"], running=False, gpu=pod["gpu"])
            return
        time.sleep(4)


if __name__ == "__main__":
    sys.exit(main())
