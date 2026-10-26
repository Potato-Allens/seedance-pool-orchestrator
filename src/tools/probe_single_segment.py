# -*- coding: utf-8 -*-
"""Does a different egress IP reset the capflow budget?

Leases one pool account, runs a single segment through the given local proxy
port (0 = direct), prints submit/wait timings and credit delta.
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

import argparse, base64, json, os, sys, time
import paramiko

from pippit import PippitAccount, PippitError

HOST = os.environ.get("POOL_HOST", "127.0.0.1")
PORT = int(os.environ.get("POOL_SSH_PORT", "22"))
USER = os.environ.get("POOL_SSH_USER", "root")
PWD  = os.environ.get("POOL_SSH_PASSWORD", "")
DB = os.environ.get("POOL_DB", "/opt/pippit-pool/data/pool.db")
OUT = _os.path.join(OUT_DIR, "single_segment")


def ssh():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, PORT, USER, PWD, timeout=20)
    return c


def rpy(c, src, timeout=60):
    enc = base64.b64encode(src.encode()).decode()
    cmd = "echo %s | base64 -d > /tmp/_ipt_%d_%d.py && python3 /tmp/_ipt_%d_%d.py" % (
        enc, os.getpid(), int(time.time() * 1000) % 1000000, os.getpid(), int(time.time() * 1000) % 1000000)
    i, o, e = c.exec_command(cmd, timeout=timeout)
    return o.read().decode("utf-8", "replace"), e.read().decode("utf-8", "replace")


def lease(c, min_credit=300):
    src = (
        "import sqlite3,json\n"
        "c=sqlite3.connect(%r); c.row_factory=sqlite3.Row\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "r=c.execute(\"select * from accounts where status='ready' and credit>=? and cooldown_until<=? \"\n"
        "            \"order by credit desc, last_used asc limit 1\",(%d,__import__('time').time())).fetchone()\n"
        "if r:\n"
        "    c.execute(\"update accounts set status='busy' where uid=?\",(r['uid'],)); c.commit()\n"
        "    d={k:r[k] for k in r.keys()}; d['cookies']=json.loads(d['cookies'])\n"
        "    print('JSON:'+json.dumps(d))\n"
        "else:\n"
        "    c.commit(); print('NONE')\n"
    ) % (DB, min_credit)
    out, _ = rpy(c, src)
    for line in out.splitlines():
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    return None


def release(c, uid, credit, ok):
    src = (
        "import sqlite3,time\n"
        "d=sqlite3.connect(%r)\n"
        "d.execute(\"update accounts set status='ready', credit=?, last_used=?, jobs_done=jobs_done+?, jobs_failed=jobs_failed+? where uid=?\",\n"
        "          (%d,time.time(),%d,%d,%r))\n"
        "d.commit(); print('OK')\n"
    ) % (DB, int(credit), 1 if ok else 0, 0 if ok else 1, uid)
    rpy(c, src)


def ip_of(port):
    import requests
    px = {"http": "http://127.0.0.1:%d" % port, "https": "http://127.0.0.1:%d" % port} if port else None
    try:
        r = requests.get("https://ipinfo.io/json", proxies=px, timeout=15)
        d = r.json()
        return "%s %s %s" % (d.get("ip"), d.get("city"), d.get("org"))
    except Exception as e:
        return "ipcheck failed: %r" % (e,)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--secs", type=int, default=15)
    ap.add_argument("--min_credit", type=int, default=300)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    c = ssh()
    print("egress:", ip_of(a.port), flush=True)
    acct = lease(c, a.min_credit)
    if not acct:
        print("NO ACCOUNT"); return
    uid = acct["uid"]
    print("leased", uid, "credit", acct["credit"], flush=True)
    cl = PippitAccount(acct["cookies"], uid=uid, ports=[a.port],
                       tag=acct.get("tag"), workspace_id=acct.get("workspace_id"),
                       space_id=acct.get("space_id"))
    t0 = time.time()
    try:
        cl.bootstrap()
        c0 = cl.credit()["total"]
        print("bootstrap %.1fs credit=%d" % (time.time() - t0, c0), flush=True)
        dest = os.path.join(OUT, "ip%d_%s.mp4" % (a.port, time.strftime("%H%M%S")))
        t1 = time.time()
        events = []

        def prog(*e):
            events.append((round(time.time() - t1, 1),) + tuple(str(x)[:80] for x in e))
            if len(events) % 5 == 0:
                print("  ev", events[-1], flush=True)

        r = cl.run_video("A slow dolly shot down a rainy neon alley, 15 seconds, single continuous shot.",
                         dest, seconds=a.secs, resolution="1080p", ratio="9:16",
                         timeout=1500, on_progress=prog, model_key="Seedance_2.5")
        dt = time.time() - t1
        c1 = cl.credit()["total"]
        print("OK %.1fs bytes=%d used=%d -> %s" % (dt, r["bytes"], c0 - c1, dest), flush=True)
        release(c, uid, c1, True)
    except Exception as e:
        try:
            c1 = cl.credit()["total"]
        except Exception:
            c1 = acct["credit"]
        print("FAIL %.1fs %r" % (time.time() - t0, str(e)[:400]), flush=True)
        release(c, uid, c1, False)
    finally:
        c.close()


if __name__ == "__main__":
    main()
