# -*- coding: utf-8 -*-
"""Probe reward_task list on aged pool accounts (daily/repeatable free credits?)."""

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

import base64, json, os, sys, time
import paramiko
from pippit import PippitAccount

HOST = os.environ.get("POOL_HOST", "127.0.0.1")
PORT = int(os.environ.get("POOL_SSH_PORT", "22"))
USER = os.environ.get("POOL_SSH_USER", "root")
PWD  = os.environ.get("POOL_SSH_PASSWORD", "")
DB = os.environ.get("POOL_DB", "/opt/pippit-pool/data/pool.db")
ADMIN = os.environ.get("POOL_ADMIN_TOKEN", "")

def ssh():
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, PORT, USER, PWD, timeout=20); return c

def rpy(c, src, timeout=90):
    enc = base64.b64encode(src.encode()).decode()
    tag = "%d_%d" % (os.getpid(), int(time.time()*1000) % 1000000)
    cmd = "echo %s | base64 -d > /tmp/probe_%s.py && python3 /tmp/probe_%s.py" % (enc, tag, tag)
    i,o,e = c.exec_command(cmd, timeout=timeout)
    return o.read().decode("utf-8","replace"), e.read().decode("utf-8","replace")

def lease_oldest(c, n=3, min_credit=100):
    src = (
        "import sqlite3,json,time\n"
        "c=sqlite3.connect(%r); c.row_factory=sqlite3.Row\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "rs=c.execute(\"select * from accounts where status='ready' and credit>=? and cooldown_until<=? order by uid asc limit ?\",(%d,time.time(),%d)).fetchall()\n"
        "out=[]\n"
        "for r in rs:\n"
        "    c.execute(\"update accounts set status='busy' where uid=?\",(r['uid'],))\n"
        "    d={k:r[k] for k in r.keys()}; d['cookies']=json.loads(d['cookies']); out.append(d)\n"
        "c.commit()\n"
        "print('JSON:'+json.dumps(out))\n"
    ) % (DB, min_credit, n)
    out,_ = rpy(c, src)
    for line in out.splitlines():
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    print("lease raw:", out[:500])
    return []

def release(c, uid, credit, ok=True):
    src = (
        "import sqlite3,time\n"
        "d=sqlite3.connect(%r)\n"
        "d.execute(\"update accounts set status='ready', credit=?, last_used=? where uid=?\",(%d,time.time(),%r))\n"
        "d.commit(); print('OK')\n"
    ) % (DB, int(credit), uid)
    rpy(c, src)

def main():
    c = ssh()
    accts = lease_oldest(c, 3, 100)
    print("leased", [a["uid"] for a in accts], flush=True)
    for a in accts:
        uid = a["uid"]
        cl = None
        try:
            cl = PippitAccount(a["cookies"], uid=uid, ports=[0], tag=a.get("tag"),
                               workspace_id=a.get("workspace_id"), space_id=a.get("space_id"))
            cl.bootstrap()
            cr = cl.credit()
            print("="*30, uid, "credit", cr, flush=True)
            for url in ("/api/biz/v1/reward_task/list?task_type=1&version=v2",
                        "/api/biz/v1/reward_task/list?version=v2"):
                try:
                    r = cl.pp.req(url, None, method="GET", tries=1, timeout=30)
                    j = r.json()
                    tasks = (j.get("data") or {}).get("reward_tasks") or []
                    print(" URL", url, "ret", j.get("ret"), "tasks", len(tasks))
                    for t in tasks:
                        keep = {k: t.get(k) for k in t if k in ("task_id","task_name","title","task_status","reward_amount",
                                "credit","reward","progress","target","task_type","expire_time","can_claim","is_finished")}
                        print("   ", json.dumps(keep, ensure_ascii=False))
                except Exception as e:
                    print(" URL", url, "ERR", repr(str(e))[:200])
            release(c, uid, cr["total"])
        except Exception as e:
            print("ACCT", uid, "FAIL", repr(str(e))[:300])
            try:
                release(c, uid, a.get("credit") or 0)
            except Exception:
                pass
    c.close()

if __name__ == "__main__":
    main()
