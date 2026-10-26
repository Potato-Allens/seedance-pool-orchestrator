# -*- coding: utf-8 -*-
"""Per-model / per-resolution cost matrix (credits per produced second)."""

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

import argparse, json, os, subprocess, sys, time
from pippit import PippitAccount
import probe_rewards as R

OUT = _os.path.join(OUT_DIR, "model_cost.jsonl")
DEST = _os.path.join(OUT_DIR, "model_cost")

def probe(p):
    o = subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries",
                        "stream=width,height,duration","-show_entries","format=duration",
                        "-of","json",p], capture_output=True, text=True, timeout=60)
    try:
        j = json.loads(o.stdout)
        st = (j.get("streams") or [{}])[0]
        d = st.get("duration") or (j.get("format") or {}).get("duration")
        return float(d or 0), st.get("width"), st.get("height")
    except Exception:
        return 0.0, None, None

def log(rec):
    with open(OUT,"a",encoding="utf-8") as f:
        f.write(json.dumps(rec,ensure_ascii=False)+"\n")
    print(json.dumps(rec,ensure_ascii=False), flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--wire", default="720p")
    ap.add_argument("--secs", type=int, default=15)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--attempts", type=int, default=3)
    a = ap.parse_args()
    os.environ["PP_WIRE_RES"] = a.wire
    os.makedirs(DEST, exist_ok=True)
    c = R.ssh()
    for k in range(a.attempts):
        acct = None
        for i in range(30):
            accts = R.lease_oldest(c, 1, 100)
            if accts:
                acct = accts[0]; break
            time.sleep(10)
        if not acct:
            log({"model":a.model,"wire":a.wire,"err":"no_account"}); return
        uid = acct["uid"]
        cl = PippitAccount(acct["cookies"], uid=uid, ports=[a.port], tag=acct.get("tag"),
                           workspace_id=acct.get("workspace_id"), space_id=acct.get("space_id"))
        t0=time.time(); c0=acct.get("credit") or 0
        try:
            cl.bootstrap(); c0 = cl.credit()["total"]
            dest = os.path.join(DEST, "%s_%s_%s.mp4" % (a.model.replace(".","_"), a.wire, time.strftime("%H%M%S")))
            r = cl.run_video("A slow dolly shot through a rainy neon alley at night, 15 seconds, single continuous shot, cinematic.",
                             dest, seconds=a.secs, resolution="1080p", ratio="9:16", timeout=1800,
                             model_key=a.model)
            c1 = cl.credit()["total"]
            d,w,h = probe(dest)
            rec = {"model":a.model,"wire":a.wire,"req":a.secs,"port":a.port,"uid":uid,"try":k,
                   "dt":round(time.time()-t0,1),"out_s":round(d,3),"w":w,"h":h,
                   "c0":c0,"c1":c1,"used":c0-c1,
                   "cr_per_s": (round((c0-c1)/d,3) if d>0 else None),
                   "bytes": r.get("bytes"),"file":dest}
            log(rec)
            R.release(c, uid, c1)
            c.close(); return
        except Exception as e:
            msg = str(e)[:200]
            try: c1 = cl.credit()["total"]
            except Exception: c1 = c0
            log({"model":a.model,"wire":a.wire,"req":a.secs,"port":a.port,"uid":uid,"try":k,
                 "dt":round(time.time()-t0,1),"err":msg,"c0":c0,"c1":c1,"used":c0-c1})
            R.release(c, uid, c1, ok=False)
            time.sleep(30)
    c.close()

main()
