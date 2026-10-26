# -*- coding: utf-8 -*-
"""Local worker: generate Seedance 2.5 30s from THIS machine's IP.

The pool server's egress IP is upstream-throttled (capflow over limit at >10
workers, only 2 completions/20min at the time of writing). A generation run
from a different IP succeeded in 552s while the server was saturated, so the
cap is per-egress, not global. This worker leases accounts from the shared
pool DB over SSH, generates locally, and releases them back.
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


import argparse, io, json, os, subprocess, sys, threading, time, uuid, base64
import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pippit import PippitAccount, PippitError
import refs

HOST = os.environ.get("POOL_HOST", "127.0.0.1")
PORT = int(os.environ.get("POOL_SSH_PORT", "22"))
USER = os.environ.get("POOL_SSH_USER", "root")
PWD  = os.environ.get("POOL_SSH_PASSWORD", "")
DB = os.environ.get("POOL_DB", "/opt/pippit-pool/data/pool.db")
OUT = OUT_DIR
LOG = _os.path.join(LOG_DIR, "batch_worker.log")

SHARD_PORT = 0


def set_paths(port):
    """Per-shard log + heartbeat so multiple egress shards never collide."""
    global LOG, HEARTBEAT, SHARD_PORT
    SHARD_PORT = int(port)
    suffix = "" if int(port) == 0 else "_p%d" % int(port)
    LOG = _os.path.join(LOG_DIR, "batch_worker%s.log" % suffix)
    HEARTBEAT = _os.path.join(LOG_DIR, "batch_worker%s.heartbeat.json" % suffix)


PROMPTS = [
 "清晨六点的老城菜市场，穿藏青布衫的老太太挑拣青菜，蒸汽从早点摊腾起，镜头从她布满皱纹的手摇到她眯眼微笑的脸，最后拉远成整条巷子的烟火气。纪实电影质感，浅景深。",
 "暴雨夜的霓虹街头，外卖骑手把雨衣掀到背后冲进楼道，水珠顺着他下巴滴落，他在楼梯间停住，抬头看手机屏幕上的一条消息，深呼吸，转身继续上楼。冷调蓝紫光，手持跟拍。",
 "沙漠公路正午，一辆掉漆的红色皮卡拖着尾尘，副驾的男孩把手臂伸出窗外，风把他的头发吹乱，镜头从轮胎低角度仰拍，切到车顶俯瞰整条无人公路延伸到地平线。暖黄颗粒感。",
 "手术室外的走廊，年轻护士摘下口罩靠在墙上闭眼三秒，监护仪的滴答声渐远，走廊尽头有人推着担架经过，她睁眼把口罩重新戴好走回门里。浅冷白荧光，长焦压缩感。",
 "雪山营地日出，帐篷拉链拉开，男人呼出白气，镜头跟随他把咖啡壶架在气炉上，热气升腾遮住远景的雪峰，随后切成雪峰全景。8K 风光质感，高动态范围。",
 "深夜便利店的落地窗前，穿校服的女孩趴在桌上写题，店员把一杯热牛奶放在她手边，她抬头说了声谢谢，窗外雨在玻璃上流成线。暖光与冷雨对比，35mm 胶片质感。",
 "老式理发店里，剃刀刮过泡沫的声音，老师傅的手稳得像在雕木头，镜子里映出客人闭眼的松弛表情，最后镜头拉远露出满墙的旧照片。复古黄绿色调。",
 "地铁末班车厢只有一个人，他靠着车门看窗外隧道灯一格一格划过脸，忽然笑了一下，低头删掉手机里的一条草稿，到站起身走出。冷白与隧道黄交替，静帧式构图。",
 "稻田中央的水渠边，赤脚少年把一只纸船放进水里，蹲下看它顺流漂远，夕阳在他背后烧成一片橘红，镜头从水面仰拍他的剪影。金色时刻，柔光。",
 "凌晨四点的面馆后厨，师傅把一团面反复摔在案板上，面粉在灯下浮成雾，手腕发力时青筋凸起，最后把面甩进滚水，蒸汽扑面。高对比侧光，慢动作细节。",
]


HEARTBEAT = _os.path.join(LOG_DIR, "batch_worker.heartbeat.json")
_hb_lock = threading.Lock()
_hb_state = {}


def hb(wid, stage=""):
    """Record per-worker activity; a watchdog thread flushes it to disk."""
    with _hb_lock:
        _hb_state[str(wid)] = [time.time(), stage]


def _hb_flusher():
    while True:
        try:
            with _hb_lock:
                snap = {k: list(v) for k, v in _hb_state.items()}
            tmp = HEARTBEAT + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "pid": os.getpid(), "threads": snap}, f)
            os.replace(tmp, HEARTBEAT)
        except Exception:
            pass
        time.sleep(20)


def log(msg, tag=""):
    line = time.strftime("%H:%M:%S ") + (("[%s] " % tag) if tag else "") + str(msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class Remote:
    def __init__(self, order="desc"):
        self.order = order if order in ("asc", "desc") else "desc"
        self.c = paramiko.SSHClient()
        self.c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.c.connect(HOST, PORT, USER, PWD, timeout=20)
        self.lock = threading.Lock()

    def _connect(self):
        self.c = paramiko.SSHClient()
        self.c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.c.connect(HOST, PORT, USER, PWD, timeout=20)

    def py(self, src, timeout=60):
        for attempt in range(2):
            try:
                return self._py_once(src, timeout)
            except Exception as e:
                if attempt == 1:
                    raise
                with self.lock:
                    try:
                        self._connect()
                    except Exception:
                        time.sleep(5)
        return (1, "", "reconnect failed")

    def _py_once(self, src, timeout=60):
        enc = base64.b64encode(src.encode("utf-8")).decode()
        tid = threading.get_ident()
        cmd = "echo %s | base64 -d > /tmp/_loc_%d_%d.py && python3 /tmp/_loc_%d_%d.py" % (enc, os.getpid(), tid, os.getpid(), tid)
        with self.lock:
            ch = self.c.get_transport().open_session()
            ch.settimeout(timeout)
            ch.exec_command(cmd)
            out = b""
            err = b""
            while True:
                if ch.recv_ready():
                    out += ch.recv(65536)
                if ch.recv_stderr_ready():
                    err += ch.recv_stderr(65536)
                if ch.exit_status_ready() and not ch.recv_ready() and not ch.recv_stderr_ready():
                    break
            rc = ch.recv_exit_status()
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def lease(self, min_credit=200, order=None):
        order = order if order in ("asc", "desc") else self.order
        src = (
            "import sqlite3,json,time\n"
            "c=sqlite3.connect(%r); c.row_factory=sqlite3.Row\n"
            "c.execute('BEGIN IMMEDIATE')\n"
            "r=c.execute(\"select * from accounts where status='ready' and credit>=? and cooldown_until<=? \"\n"
            "            \"order by credit " + order + ", last_used asc limit 1\",(%d,time.time())).fetchone()\n"
            "if r:\n"
            "    c.execute(\"update accounts set status='busy' where uid=?\",(r['uid'],)); c.commit()\n"
            "    d={k:r[k] for k in r.keys()}; d['cookies']=json.loads(d['cookies']); d['ports']=[0]\n"
            "    print('JSON:'+json.dumps(d,ensure_ascii=False))\n"
            "else:\n"
            "    c.commit(); print('NONE')\n"
        ) % (DB, int(min_credit))
        rc, out, err = self.py(src)
        for line in out.splitlines():
            if line.startswith("JSON:"):
                return json.loads(line[5:])
        return None

    def upload_image(self, uid, local_path, timeout=180):
        """Upload a reference image through the SERVER (its IP can reach the TOS CDN)."""
        remote_path = "/tmp/_ref_%d_%s.jpg" % (os.getpid(), uuid.uuid4().hex[:8])
        sftp = self.c.open_sftp()
        try:
            sftp.put(local_path, remote_path)
        finally:
            sftp.close()
        src = (
            "import sys,json,sqlite3\n"
            "sys.path.insert(0,'/opt/pippit-pool')\n"
            "import refs\n"
            "from pippit import PippitAccount\n"
            "c=sqlite3.connect(%r); c.row_factory=sqlite3.Row\n"
            "r=c.execute('select cookies,workspace_id,space_id,tag from accounts where uid=?',(%r,)).fetchone()\n"
            "ck=json.loads(r['cookies'])\n"
            "cl=PippitAccount(ck, uid=%r, ports=[0], tag=r['tag'], workspace_id=r['workspace_id'], space_id=r['space_id'])\n"
            "cl.bootstrap()\n"
            "info=refs.upload_asset(cl.pp, %r, cl.workspace_id, asset_type=refs.ASSET_TYPE['image'], log=lambda *a: None)\n"
            "url=''; md={}\n"
            "try:\n"
            "    url,md=refs.asset_meta(cl.pp, info['pippit_asset_id'])\n"
            "except Exception:\n"
            "    pass\n"
            "print('PART:'+json.dumps({'info':info,'url':url,'md':md}, ensure_ascii=False))\n"
        ) % (DB, uid, uid, remote_path)
        last = None
        for att in range(3):
            try:
                rc, out, err = self.py(src, timeout=timeout)
                for line in out.splitlines():
                    if line.startswith("PART:"):
                        d = json.loads(line[5:])
                        return refs.make_part(d["info"], asset_url=d.get("url") or "",
                                              metadata=d.get("md") or {})
                last = (err or out)[-200:]
            except Exception as e:
                last = repr(e)[:200]
            time.sleep(3)
        raise RuntimeError("remote upload failed: %s" % last)

    def release(self, uid, credit, done=True, failed=False, err=None, cooldown=0):
        """Give the account back. cooldown>0 parks it so a throttled account is
        not instantly re-leased (that was why shards kept hitting capflow)."""
        src = (
            "import sqlite3,time\n"
            "c=sqlite3.connect(%r)\n"
            "now=time.time()\n"
            "c.execute(\"update accounts set status='ready', credit=?, last_used=?, jobs_done=jobs_done+?, \"\n"
            "          \"jobs_failed=jobs_failed+?, last_error=?, cooldown_until=max(cooldown_until,?) where uid=?\",\n"
            "          (%d, now, %d, %d, %s, now+%d, %r))\n"
            "c.commit(); print('OK')\n"
        ) % (DB, int(credit), 1 if done else 0, 1 if failed else 0,
             ("%r" % str(err)[:300]) if err else "None", int(cooldown), uid)
        self.py(src)


def probe(p):
    try:
        o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", p], capture_output=True, text=True, timeout=60)
        return float((o.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def concat(paths, dest):
    lst = dest + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '%s'\n" % p.replace("'", "'\\''"))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", dest], check=True, timeout=900)


class Ctx:
    """Current account + ability to swap it mid-job when upstream throttles us."""

    def __init__(self, remote, acct, ports, wid):
        self.remote = remote
        self.ports = ports or [0]
        self.wid = wid
        self.uid = acct["uid"]
        self.credit = acct.get("credit") or 0
        self.tag = "w%d/%s" % (wid, self.uid[-4:])
        self.cl = PippitAccount(acct["cookies"], uid=self.uid, ports=self.ports,
                                tag=acct.get("tag"), workspace_id=acct.get("workspace_id"),
                                space_id=acct.get("space_id"))
        self.cl.bootstrap()
        self.credit = self.cl.credit()["total"]

    def refresh_credit(self):
        try:
            self.credit = self.cl.credit()["total"]
        except Exception:
            pass
        return self.credit

    def switch(self, err, cool=900):
        """Park the throttled account and lease a fresh one. True on success."""
        self.refresh_credit()
        try:
            self.remote.release(self.uid, self.credit, done=False, failed=True,
                                err=repr(err)[:200], cooldown=cool)
        except Exception:
            pass
        for _ in range(8):
            try:
                a = self.remote.lease(MIN_CREDIT)
            except Exception as e:
                log("switch lease error %r" % (str(e)[:100],), self.tag)
                time.sleep(15)
                continue
            if not a:
                time.sleep(30)
                continue
            try:
                cl = PippitAccount(a["cookies"], uid=a["uid"], ports=self.ports,
                                   tag=a.get("tag"), workspace_id=a.get("workspace_id"),
                                   space_id=a.get("space_id"))
                cl.bootstrap()
                c = cl.credit()["total"]
                if c < 60:
                    self.remote.release(a["uid"], c, done=False, cooldown=60)
                    continue
                self.uid, self.cl, self.credit = a["uid"], cl, c
                self.tag = "w%d/%s" % (self.wid, a["uid"][-4:])
                hb(self.wid, "leased:" + self.uid)
                log("switched account, credit=%d" % c, self.tag)
                return True
            except Exception as e:
                log("switch bootstrap failed %r" % (str(e)[:120],), self.tag)
                try:
                    self.remote.release(a["uid"], a.get("credit") or 0, done=False, cooldown=120)
                except Exception:
                    pass
        return False

    def handoff(self, cool=0):
        """Park a nearly-empty account (not a failure) and lease a fresh one."""
        self.refresh_credit()
        try:
            self.remote.release(self.uid, self.credit, done=False, failed=False, cooldown=cool)
        except Exception:
            pass
        for _ in range(8):
            try:
                a = self.remote.lease(MIN_CREDIT)
            except Exception as e:
                log("handoff lease error %r" % (str(e)[:100],), self.tag)
                time.sleep(15)
                continue
            if not a:
                time.sleep(30)
                continue
            try:
                cl = PippitAccount(a["cookies"], uid=a["uid"], ports=self.ports,
                                   tag=a.get("tag"), workspace_id=a.get("workspace_id"),
                                   space_id=a.get("space_id"))
                cl.bootstrap()
                c = cl.credit()["total"]
                if c < MIN_CREDIT:
                    self.remote.release(a["uid"], c, done=False, cooldown=60)
                    continue
                self.uid, self.cl, self.credit = a["uid"], cl, c
                self.tag = "w%d/%s" % (self.wid, a["uid"][-4:])
                hb(self.wid, "handoff:" + self.uid)
                log("handoff account, credit=%d" % c, self.tag)
                return True
            except Exception as e:
                log("handoff bootstrap failed %r" % (str(e)[:120],), self.tag)
                try:
                    self.remote.release(a["uid"], a.get("credit") or 0, done=False, cooldown=120)
                except Exception:
                    pass
        return False


MIN_CREDIT = int(os.environ.get("SPO_MIN_CREDIT", "80"))


THROTTLE = ("capflow", "over limit", "13002", "timeout", "12012", "429")


def is_throttle(e):
    low = str(e).lower()
    return any(k in low for k in THROTTLE)


def gen(ctx, dest, prompt, need=30.0, ratio="9:16", res="1080p"):
    segs, total, idx = [], 0.0, 0
    while total < need - 1.2 and idx < 6:
        if idx > 0 and ctx.refresh_credit() < 60:
            if ctx.handoff(cool=0):
                log("handoff ok, continuing seg%d on fresh account" % (idx + 1), ctx.tag)
            else:
                log("stop: credit<60 and no replacement", ctx.tag)
                break
        # Cap the request by the account's actual balance (output can overshoot
        # the request by ~33%, so budget 5.4 credits per requested second).
        _cap = int((max(0, ctx.credit) - 5) / 5.4)
        _cap = max(5, min(15, _cap))
        seg_len = int(round(min(float(_cap), max(5.0, need - total))))
        if idx == 0:
            sp = "这是第1段，严格 %d 秒整，单一连续镜头，直接生成，不要询问确认。%s" % (seg_len, prompt)
        else:
            sp = ("这是第%d段，严格 %d 秒整，紧接上一段结尾的画面与动作继续，保持人物/场景/光线/镜头风格完全一致，"
                  "不要重演上一段开头，直接生成，不要询问确认。%s" % (idx + 1, seg_len, prompt))
        sd = dest.replace(".mp4", ".s%d.mp4" % idx)
        parts = None
        lf = None
        if idx > 0 and segs:
            try:
                lf = dest.replace(".mp4", ".s%d.last.jpg" % (idx - 1))
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.2", "-i", segs[-1],
                                "-frames:v", "1", "-q:v", "2", lf], check=True, timeout=180)
                parts = [ctx.remote.upload_image(ctx.uid, lf)]
                sp = ("以参考图片（上一段的最后一帧）作为本段第一帧，画面、人物、光线、机位完全一致，"
                      "从该画面继续往下演，不要重演上一段。") + sp
                log("seam ref ok seg%d" % idx, ctx.tag)
            except Exception as e:
                log("seam ref failed: %r" % (e,), ctx.tag)
                parts = None
        t0 = time.time()
        d = 0.0
        last_err = None
        for _att in range(3):
            switched = 0
            last_err = None
            while True:
                try:
                    ctx.cl.run_video(sp, sd, seconds=seg_len, resolution=res, ratio=ratio,
                                     timeout=900, extra_parts=parts, model_key="Seedance_2.5")
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if switched < 4 and ctx.switch(e):
                        switched += 1
                        if parts and lf:
                            try:
                                parts = [ctx.remote.upload_image(ctx.uid, lf)]
                                log("re-uploaded seam ref for new account", ctx.tag)
                            except Exception as ue:
                                log("seam re-upload failed %r" % (str(ue)[:100],), ctx.tag)
                                parts = None
                        continue
                    break
            d = probe(sd)
            if d >= 2.5:
                break
            if _att < 2:
                if last_err is not None:
                    log("seg%d failed after switches, retry %d: %r"
                        % (idx + 1, _att + 2, str(last_err)[:90]), ctx.tag)
                else:
                    log("seg%d too short %.2fs -> retry" % (idx + 1, d), ctx.tag)
                try:
                    os.remove(sd)
                except Exception:
                    pass
                sp = ("这是一个连续的 %d 秒单镜头，画面不要切换，不要提前结束。" % seg_len) + prompt
        if d < 2.5 and segs and total >= 20.0:
            log("seg%d unsalvageable (%r) -> keep %0.2fs" % (idx + 1, str(last_err)[:60], total), ctx.tag)
            break
        if d < 2.5 and not segs and last_err is not None:
            raise last_err
        take = min(d, need - total)
        if take <= 0.5:
            log("seg%d unusable %.2fs" % (idx + 1, d), ctx.tag)
            try:
                os.remove(sd)
            except Exception:
                pass
            idx += 1
            continue
        if d > take + 0.05:
            cut = sd + ".cut.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", sd, "-t", "%.3f" % take,
                            "-c", "copy", cut], check=True, timeout=300)
            os.replace(cut, sd)
            nd = probe(sd)
            if abs(nd - take) > 0.6:
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", sd, "-t", "%.3f" % take,
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                                "-c:a", "aac", "-b:a", "128k", cut], check=True, timeout=900)
                os.replace(cut, sd)
        segs.append(sd)
        total += take
        log("seg%d ok %.2fs kept %.2fs total %.2fs (%.0fs)"
            % (idx + 1, d, take, total, time.time() - t0), ctx.tag)
        hb(ctx.wid, "seg%d" % (idx + 1))
        idx += 1
    if not segs:
        raise RuntimeError("no usable segment")
    concat(segs, dest)
    out = probe(dest)
    if out < 20.0:
        try:
            os.remove(dest)
        except Exception:
            pass
        raise RuntimeError("final too short %.2fs (need 20s)" % out)
    return out


def worker(wid, remote, budget, counter, lock, prompts, ports=None):
    fails = 0
    hb(wid, "loop")
    while True:
        hb(wid, "lease")
        with lock:
            if counter["n"] >= budget or counter["stop"]:
                return
            counter["n"] += 1
            n = counter["n"]
        try:
            acct = remote.lease(MIN_CREDIT)
        except Exception as e:
            log("lease error %r; retry in 30s" % (str(e)[:120],), "w%d" % wid)
            time.sleep(30)
            with lock:
                counter["n"] -= 1
            continue
        if not acct:
            log("no account with credit>=%d; sleep" % MIN_CREDIT, "w%d" % wid)
            time.sleep(60)
            with lock:
                counter["stop"] = True
            return
        uid = acct["uid"]
        hb(wid, "leased:" + uid)
        tag = "w%d/%s" % (wid, uid[-4:])
        jid = "loc_" + uuid.uuid4().hex[:16]
        dest = os.path.join(OUT, jid + ".mp4")
        ctx = None
        try:
            ctx = Ctx(remote, acct, ports, wid)
            tag = ctx.tag
            if ctx.credit < MIN_CREDIT:
                remote.release(ctx.uid, ctx.credit, done=False)
                log("low credit %d, released" % ctx.credit, tag)
                continue
            log("start credit=%d" % ctx.credit, tag)
            p = prompts[(n - 1) % len(prompts)]
            full_prompt = p + " 竖屏 9:16，电影级画质，无字幕无文字。"
            dur = gen(ctx, dest, full_prompt)
            c_after = ctx.refresh_credit()
            size = os.path.getsize(dest)
            try:
                json.dump({"job": jid, "uid": ctx.uid, "prompt": full_prompt, "seconds": dur,
                           "model": "seedance-2.5-1080p-30s", "ratio": "9:16", "resolution": "1080p",
                           "bytes": size, "credit_before": ctx.credit, "credit_after": c_after,
                           "used": ctx.credit - c_after, "ts": time.time(), "source": "local"},
                          open(os.path.join(OUT, jid + ".json"), "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
            except Exception:
                pass
            log("DONE %s %.2fs %dMB used=%d" % (jid, dur, size // 1048576, ctx.credit - c_after), tag)
            remote.release(ctx.uid, c_after, done=True)
            with lock:
                counter["done"] += 1
            fails = 0
        except Exception as e:
            fails += 1
            low = str(e).lower()
            throttled = any(k in low for k in THROTTLE)
            cool = 900 if throttled else 120
            log("FAIL %s %r (cooldown %ds, streak %d)" % (jid, str(e)[:200], cool, fails), tag)
            try:
                credit_now = ctx.refresh_credit() if ctx else (acct.get("credit") or 0)
                remote.release(ctx.uid if ctx else uid, credit_now, done=False, failed=True,
                               err=repr(e)[:200], cooldown=cool)
            except Exception:
                pass
            if throttled and fails >= 3:
                nap = min(300, 60 * fails)
                log("backoff %ds after %d throttled fails" % (nap, fails), tag)
                time.sleep(nap)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--ports", default="0", help="comma list of local proxy ports; 0 = direct")
    ap.add_argument("--order", default="desc", choices=["desc", "asc"], help="desc = richest accounts first, asc = drain low-credit accounts first")
    args = ap.parse_args()
    ports = [int(x) for x in str(args.ports).split(",") if x.strip() != ""]
    set_paths(ports[0])
    remote = Remote(args.order)
    counter = {"n": 0, "done": 0, "stop": False}
    lock = threading.Lock()
    threading.Thread(target=_hb_flusher, daemon=True).start()
    ths = [threading.Thread(target=worker, args=(i + 1, remote, args.limit, counter, lock, PROMPTS, ports), daemon=True)
           for i in range(args.workers)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    log("ALL DONE done=%d" % counter["done"])


if __name__ == "__main__":
    main()
