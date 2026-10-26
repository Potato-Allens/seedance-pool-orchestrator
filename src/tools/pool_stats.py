# -*- coding: utf-8 -*-
"""Append a one-line production snapshot to stats.csv."""

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


import io, json, os, re, time
import paramiko

HOST = os.environ.get("POOL_HOST", "127.0.0.1")
PORT = int(os.environ.get("POOL_SSH_PORT", "22"))
USER = os.environ.get("POOL_SSH_USER", "root")
PWD  = os.environ.get("POOL_SSH_PASSWORD", "")
DB = os.environ.get("POOL_DB", "/opt/pippit-pool/data/pool.db")
OUT = _os.path.join(LOG_DIR, "pool_stats.csv")
BATCH = OUT_DIR

SQL = (
    "import sqlite3,time\n"
    "d=sqlite3.connect(%r)\n"
    "now=time.time()\n"
    "g=lambda q,a=(): d.execute(q,a).fetchone()[0]\n"
    "credit=g('select coalesce(sum(credit),0) from accounts')\n"
    "running=g(\"select count(*) from jobs where status='running'\")\n"
    "queued=g(\"select count(*) from jobs where status='queued'\")\n"
    "done60=g(\"select count(*) from jobs where status='completed' and completed_at>?\",(now-3600,))\n"
    "seg60=g(\"select count(*) from events where ts>? and msg like '%%segment %% ok%%'\",(now-3600,))\n"
    "cap60=g(\"select count(*) from events where ts>? and msg like '%%capflow%%'\",(now-3600,))\n"
    "print('POOL:%%d,%%d,%%d,%%d,%%d,%%d'%%(credit,running,queued,done60,seg60,cap60))\n"
) % DB


def locals_stats():
    seg = done = fail = 0
    cut = time.strftime("%H:%M:%S", time.localtime(time.time() - 3600))
    for p in ("0", "7897", "7899"):
        fn = os.path.join(LOG_DIR, "batch_worker%s.log" % ("" if p == "0" else "_p" + p))
        try:
            lines = io.open(fn, encoding="utf-8", errors="replace").read().splitlines()
        except Exception:
            continue
        for l in lines:
            if l[:8] < cut:
                continue
            if re.search(r"seg\d+ ok", l):
                seg += 1
            if re.search(r"\] DONE", l):
                done += 1
            if re.search(r"\] FAIL", l):
                fail += 1
    return seg, done, fail


def finals_last_hour():
    n = 0
    now = time.time()
    for f in os.listdir(BATCH):
        if not f.endswith(".mp4"):
            continue
        if re.search(r"\.s\d+\.mp4$", f):
            continue
        try:
            if now - os.path.getmtime(os.path.join(BATCH, f)) < 3600:
                n += 1
        except Exception:
            pass
    return n


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, PORT, USER, PWD, timeout=20)
    i, o, e = c.exec_command("python3 - <<'EOF'\n" + SQL + "EOF", timeout=40)
    out = o.read().decode("utf-8", "replace").strip()
    c.close()
    pool = [x for x in out.splitlines() if x.startswith("POOL:")]
    pool = pool[0][5:].split(",") if pool else ["", "", "", "", "", ""]
    seg, done, fail = locals_stats()
    fin = finals_last_hour()
    row = [str(x) for x in [time.strftime("%Y-%m-%d %H:%M:%S"), pool[0], pool[1], pool[2], pool[3],
           pool[4], pool[5], seg, done, fail, fin]]
    new = not os.path.exists(OUT)
    with io.open(OUT, "a", encoding="utf-8", newline="\n") as f:
        if new:
            f.write("ts,credit,running,queued,pool_done_1h,pool_seg_1h,pool_capflow_1h,"
                    "loc_seg_1h,loc_done_1h,loc_fail_1h,finals_1h\n")
        f.write(",".join(row) + "\n")
    print(",".join(row))


if __name__ == "__main__":
    main()
