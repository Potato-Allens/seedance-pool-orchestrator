# -*- coding: utf-8 -*-
"""Nail down ratio codes (13 vs 6) and output dimensions with 5s cheap probes."""

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
import pippit
from pippit import PippitAccount
import probe_rewards as R

OUT=_os.path.join(OUT_DIR, "ratio.jsonl"); DEST=_os.path.join(OUT_DIR, "ratio")

def probe(p):
    o=subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height,duration",
                      "-show_entries","format=duration","-of","json",p],capture_output=True,text=True,timeout=60)
    j=json.loads(o.stdout or "{}"); st=(j.get("streams") or [{}])[0]
    return float(st.get("duration") or (j.get("format") or {}).get("duration") or 0), st.get("width"), st.get("height")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--code",required=True); ap.add_argument("--secs",type=int,default=5)
    ap.add_argument("--port",type=int,default=0); ap.add_argument("--min_credit",type=int,default=100)
    a=ap.parse_args()
    os.makedirs(DEST,exist_ok=True)
    c=R.ssh()
    for k in range(2):
        accts=R.lease_oldest(c,1,a.min_credit)
        if not accts: time.sleep(20); continue
        acct=accts[0]; uid=acct["uid"]
        cl=PippitAccount(acct["cookies"],uid=uid,ports=[a.port],tag=acct.get("tag"),
                         workspace_id=acct.get("workspace_id"),space_id=acct.get("space_id"))
        t0=time.time()
        try:
            cl.bootstrap(); c0=cl.credit()["total"]
            key="code_"+a.code; pippit.RATIO_CODE[key]=int(a.code)
            dest=os.path.join(DEST,"ratio_%s_%s.mp4"%(a.code,time.strftime("%H%M%S")))
            cl.run_video("A slow dolly shot down a rainy neon alley, cinematic.",dest,seconds=a.secs,
                         ratio=key,timeout=1200,model_key="Seedance_2.5")
            c1=cl.credit()["total"]; d,w,h=probe(dest)
            rec={"code":a.code,"req":a.secs,"uid":uid,"dt":round(time.time()-t0,1),"out_s":round(d,3),
                 "w":w,"h":h,"used":c0-c1}
            open(OUT,"a",encoding="utf-8").write(json.dumps(rec,ensure_ascii=False)+"\n"); print(rec,flush=True)
            R.release(c,uid,c1); c.close(); return
        except Exception as e:
            try: c1=cl.credit()["total"]
            except Exception: c1=c0
            rec={"code":a.code,"uid":uid,"try":k,"dt":round(time.time()-t0,1),"err":str(e)[:160],"used":c0-c1}
            open(OUT,"a",encoding="utf-8").write(json.dumps(rec,ensure_ascii=False)+"\n"); print(rec,flush=True)
            R.release(c,uid,c1,ok=False); time.sleep(20)
    c.close()

main()
