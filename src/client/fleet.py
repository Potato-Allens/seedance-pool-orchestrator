# -*- coding: utf-8 -*-
"""Fleet supervisor: one worker process per egress shard.

Each shard = batch_worker.py --ports <proxy port> --workers N.
Direct shard uses port 0. Proxy shards add independent egress IPs, which is the
only lever that raises total throughput once one IP hits capflow.
"""

import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_ROOT = _os.path.abspath(_os.path.join(_HERE, "..", ".."))
_SRV = _os.path.join(_ROOT, "src", "server")
if _SRV not in _sys.path: _sys.path.insert(0, _SRV)
_os.environ.setdefault("PP_HOME", _ROOT)
APP_DIR = _os.environ.get("APP_DIR", _os.path.join(_ROOT, "var"))
LOG_DIR = _os.environ.get("LOG_DIR", _os.path.join(_ROOT, "logs"))
OUT_DIR = _os.environ.get("OUT_DIR", _os.path.join(_ROOT, "outputs"))
for _d in (APP_DIR, LOG_DIR, OUT_DIR): _os.makedirs(_d, exist_ok=True)


import json, os, re, subprocess, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DIR = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
LOG = os.path.join(DIR, "fleet.log")
STALE_S = 2400
PROBE_EVERY = 240

# Shard layout: one process per egress IP.
# Tune per deployment: workers <= 10 per IP, otherwise capflow rises sharply.
# "order": desc = burn high-credit accounts first, asc = sweep low-credit leftovers.
SHARDS = [
    {"port": 0,    "workers": 10, "limit": 80, "order": "desc"},  # direct egress (shard A)
    {"port": 7899, "workers": 10, "limit": 80, "order": "desc"},  # proxy egress (shard B)
    {"port": 7900, "workers": 8,  "limit": 80, "order": "asc"},   # proxy egress (shard C, low-credit sweeper)
]


def log(m):
    line = time.strftime("%H:%M:%S ") + str(m)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def procs():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*batch_worker*' } | "
             "ForEach-Object { $_.ProcessId.ToString() + '|' + $_.CommandLine }"],
            capture_output=True, text=True, timeout=90)
        rows = []
        for line in out.stdout.splitlines():
            if "|" not in line:
                continue
            pid, cmd = line.split("|", 1)
            m = re.search(r"--ports\s+(\d+)", cmd)
            rows.append((int(pid), int(m.group(1)) if m else 0))
        return rows
    except Exception as e:
        log("probe error %r" % (e,))
        return None


def heartbeat(port):
    path = os.path.join(DIR, "heartbeat%s.json" % ("" if port == 0 else "_p%d" % port))
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        th = d.get("threads") or {}
        newest = max([v[0] for v in th.values()] + [d.get("ts") or 0])
        return time.time() - newest, len(th)
    except Exception:
        return None, 0


def start(sh):
    port = sh["port"]
    tag = time.strftime("%H%M")
    out = open(os.path.join(DIR, "shard_p%d_%s.out" % (port, tag)), "w", encoding="utf-8")
    args = [sys.executable, "-u", os.path.join(DIR, "batch_worker.py"),
            "--workers", str(sh["workers"]), "--limit", str(sh["limit"]),
            "--ports", str(port)]
    if sh.get("order"):
        args += ["--order", sh["order"]]
    subprocess.Popen(args, cwd=DIR, stdout=out, stderr=out, creationflags=0x08000000)
    log("started shard port=%d workers=%d order=%s" % (port, sh["workers"], sh.get("order") or "desc"))


def kill(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30)
        log("killed wedged pid=%d" % pid)
    except Exception as e:
        log("kill %d failed %r" % (pid, e))


while True:
    try:
        rows = procs()
        if rows is not None:
            for sh in SHARDS:
                mine = [p for (p, port) in rows if port == sh["port"]]
                if not mine:
                    start(sh)
                    continue
                if len(mine) > 1:
                    log("WARN port=%d has %d processes %s" % (sh["port"], len(mine), mine))
                age, nthr = heartbeat(sh["port"])
                if age is None:
                    log("shard port=%d pid=%d heartbeat missing" % (sh["port"], mine[0]))
                elif age > STALE_S:
                    log("shard port=%d pid=%d stale %.0fs -> restart" % (sh["port"], mine[0], age))
                    kill(mine[0])
                    time.sleep(5)
                    if not [p for (p, port) in (procs() or []) if port == sh["port"]]:
                        start(sh)
                else:
                    log("shard port=%d pid=%d threads=%d last_act=%.0fs" % (sh["port"], mine[0], nthr, age))
    except Exception as e:
        log("loop error %r" % (e,))
    time.sleep(PROBE_EVERY)
