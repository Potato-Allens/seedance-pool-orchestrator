# -*- coding: utf-8 -*-
"""Reaper loop: every 15 min free server disk (state-based + old completed jobs)."""

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

import glob, json, os, subprocess, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from ssh_exec import run

REC = _os.path.join(APP_DIR, "recon")
V = OUT_DIR
LOG = os.path.join(REC, "reaper.log")

def log(m):
    line = time.strftime("%H:%M:%S ") + str(m)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

while True:
    try:
        ids = set()
        for p in glob.glob(os.path.join(REC, "state_*.json")):
            try:
                st = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            for d in st.get("done", []):
                jid = d.get("id")
                if jid and os.path.exists(os.path.join(V, jid + ".mp4")):
                    ids.add(jid)
        cmds = [
            # anything older than 45 min is already downloaded locally (drivers fetch
            # within a couple of minutes); 8h retention filled the disk to 90%%.
            "find /opt/pippit-pool/data/videos -name '*.mp4' -mmin +45 -delete",
            "find /opt/pippit-pool/data/videos -name '*.last.jpg' -mmin +45 -delete",
            "find /opt/pippit-pool/data/videos -name '*.cut.mp4' -delete",
        ]
        for jid in sorted(ids):
            cmds.append("rm -f /opt/pippit-pool/data/videos/%s.mp4 /opt/pippit-pool/data/videos/%s.s?.mp4 /opt/pippit-pool/data/videos/%s.s?.last.jpg" % (jid, jid, jid))
        rc, out, err = run("; ".join(cmds) + "; du -sh /opt/pippit-pool/data/videos; df -h / | tail -1", 180)
        log("purged=%d rc=%d %s" % (len(ids), rc, out.strip().replace("\n", " | ")))
    except Exception as e:
        log("err %r" % (e,))
    time.sleep(900)
