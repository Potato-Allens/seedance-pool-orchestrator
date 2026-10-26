# -*- coding: utf-8 -*-
"""worker.py - account-pool workers: pick account -> HTTP submit -> poll -> download."""
import hashlib, os, re, subprocess, sys, threading, time, traceback, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import refs
import _pp_http as H
from pippit import PippitAccount, PippitError, estimate_cost

BASE = os.path.dirname(os.path.abspath(__file__))
VIDEOS = os.path.join(BASE, "data", "videos")
os.makedirs(VIDEOS, exist_ok=True)
REFS_CACHE = os.path.join(BASE, "data", "refs_cache")
os.makedirs(REFS_CACHE, exist_ok=True)
LOCKS = os.path.join(BASE, "data", "line_locks")
os.makedirs(LOCKS, exist_ok=True)
os.makedirs(LOCKS, exist_ok=True)

try:
    import psutil
except Exception:
    psutil = None



def _probe_dur(path):
    try:
        return float(H.probe(path).get("dur") or 0.0)
    except Exception:
        return 0.0


def _xfade_concat(paths, dest, xf=0.4):
    """Crossfade-concat segments so the seam dissolves instead of jump-cutting."""
    durs = [_probe_dur(p) for p in paths]
    n = len(paths)
    if n < 2 or min(durs) <= xf + 0.2:
        return None
    args = ["ffmpeg", "-y", "-v", "error"]
    for p in paths:
        args += ["-i", p]
    fc = []
    for i in range(n):
        fc.append("[%d:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                  "setsar=1,fps=30[v%d]" % (i, i))
        fc.append("[%d:a]aresample=48000,asetpts=PTS-STARTPTS[a%d]" % (i, i))
    cur_v, cur_a = "[v0]", "[a0]"
    acc = durs[0]
    for i in range(1, n):
        ov = "[vx%d]" % i
        oa = "[ax%d]" % i
        fc.append("%s[v%d]xfade=transition=fade:duration=%.3f:offset=%.3f%s"
                  % (cur_v, i, xf, max(0.0, acc - xf), ov))
        fc.append("%s[a%d]acrossfade=d=%.3f%s" % (cur_a, i, xf, oa))
        cur_v, cur_a = ov, oa
        acc = acc + durs[i] - xf
    args += ["-filter_complex", ";".join(fc), "-map", cur_v, "-map", cur_a,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", dest]
    subprocess.run(args, check=True, timeout=2400)
    return acc


def _trim_to(path, secs):
    d = _probe_dur(path)
    if d <= secs + 0.05:
        return d
    cut = path + ".trim.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-t", "%.3f" % secs,
                    "-c", "copy", cut], check=True, timeout=900)
    if abs(_probe_dur(cut) - secs) > 0.5:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-t", "%.3f" % secs,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                        "-c:a", "aac", "-b:a", "128k", cut], check=True, timeout=1800)
    os.replace(cut, path)
    return _probe_dur(path)


def _concat(paths, dest):
    """Concat segments; crossfade the seams when SPO_XFADE>0 (default 0.4s)."""
    try:
        xf = float(os.environ.get("SPO_XFADE", "0"))
    except Exception:
        xf = 0.4
    if len(paths) > 1 and xf > 0:
        try:
            acc = _xfade_concat(paths, dest, xf)
            if acc:
                return {"url": "", "asset_id": "xfade_%d" % len(paths), "thread_id": "",
                        "run_id": "", "file": dest, "bytes": os.path.getsize(dest),
                        "segments": len(paths), "xfade": xf}
        except Exception as e:
            logger.warning("xfade failed, fallback to copy concat: %r", e)
    lst = dest + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '%s'\n" % str(p).replace("'", "'\\''"))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", dest], check=True, timeout=900)
    return {"url": "", "asset_id": "concat_%d" % len(paths), "thread_id": "", "run_id": "",
            "file": dest, "bytes": os.path.getsize(dest), "segments": len(paths)}

def _local_ref(src):
    """Return a local path for a reference (local file or downloaded URL)."""
    src = str(src or "").strip()
    if not src:
        raise RuntimeError("empty reference")
    if os.path.exists(src):
        return src
    if src.startswith("http://") or src.startswith("https://"):
        h = hashlib.md5(src.encode("utf-8")).hexdigest()
        ext = os.path.splitext(urllib.parse.urlparse(src).path)[1] or ".bin"
        dst = os.path.join(REFS_CACHE, h + ext)
        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
            req = urllib.request.Request(src, headers={"User-Agent": H.UA})
            with urllib.request.urlopen(req, timeout=90) as r, open(dst, "wb") as f:
                f.write(r.read())
        return dst
    raise RuntimeError("reference not found: %s" % src[:200])


# --- ref upload circuit breaker (TikTok CDN can block this host) ---
_UPLOAD_DOWN = [0.0, ""]


class RefUploadDown(RuntimeError):
    pass


def _upload_ok():
    return time.time() >= _UPLOAD_DOWN[0]


def _upload_trip(exc):
    try:
        cool = float(os.environ.get("SPO_UPLOAD_COOLDOWN", "600"))
    except Exception:
        cool = 600.0
    _UPLOAD_DOWN[0] = time.time() + cool
    _UPLOAD_DOWN[1] = repr(exc)[:200]


_LOCK = threading.Lock()
_ACTIVE = [0]


def _pid_alive(pid):
    if not pid:
        return False
    try:
        if psutil:
            return psutil.pid_exists(pid)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def try_lock(uid):
    fp = os.path.join(LOCKS, str(uid) + ".lock")
    for _ in range(2):
        try:
            fh = os.open(fp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fh, str(os.getpid()).encode())
            os.close(fh)
            return fp
        except FileExistsError:
            try:
                pid = int(open(fp, encoding="utf-8", errors="replace").read().strip() or 0)
            except Exception:
                pid = 0
            age = time.time() - os.path.getmtime(fp)
            if _pid_alive(pid) or (pid == 0 and age < 3 * 3600):
                return None
            try:
                os.remove(fp)
            except Exception:
                return None
        except Exception:
            return None
    return None


def unlock(fp):
    if fp:
        try:
            os.remove(fp)
        except Exception:
            pass


class PoolWorker:
    def __init__(self, max_workers=None):
        self.max_workers = int(max_workers or os.environ.get("SPO_MAX_WORKERS", "2"))
        self.running = False

    def start(self):
        db.init()
        self._recover()
        self.running = True
        for i in range(self.max_workers):
            t = threading.Thread(target=self._loop, name="spo-worker-%d" % i, daemon=True)
            t.start()
        threading.Thread(target=self._refresher, name="spo-credit-refresh", daemon=True).start()
        threading.Thread(target=self._stale_guard, name="spo-stale-guard", daemon=True).start()

    def _recover(self):
        """Unstick jobs/accounts left behind by a previous crash/restart.

        Single-instance pool => every lock file at boot is stale; every job in
        'running' lost its worker. Requeue immediately instead of waiting 45min.
        """
        try:
            for fn in os.listdir(LOCKS):
                if fn.endswith(".lock"):
                    try:
                        os.remove(os.path.join(LOCKS, fn))
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            for j in db.query("SELECT id FROM jobs WHERE status='running'"):
                db.update_job(j["id"], status="queued", stage="recovered", progress=0,
                              retry_after=time.time() + 5, account_uid=None)
                db.add_event(j["id"], "recovered from pool restart")
            for a in db.query("SELECT uid FROM accounts WHERE status='busy'"):
                db.release_account(a["uid"], status="ready", cooldown=5)
        except Exception:
            pass

    def _loop(self):
        while self.running:
            try:
                job = db.next_queued()
            except Exception:
                time.sleep(5)
                continue
            if not job:
                time.sleep(3)
                continue
            try:
                self._process(job)
            except Exception as e:
                db.add_event(job["id"], "worker crash: %r" % (e,))
                try:
                    db.update_job(job["id"], status="failed", stage="crash",
                                  error=traceback.format_exc()[-2000:], completed_at=time.time())
                except Exception:
                    pass

    def _stale_guard(self):
        """Requeue jobs whose worker died (restart mid-job, hung run) - no event for STALE_SECS."""
        stale_secs = float(os.environ.get("SPO_STALE_SECS", "900"))
        while self.running:
            time.sleep(180)
            try:
                rows = db.query(
                    "SELECT j.id AS id, j.account_uid AS uid, MAX(e.ts) AS last "
                    "FROM jobs j LEFT JOIN events e ON e.job_id = j.id "
                    "WHERE j.status = 'running' GROUP BY j.id")
            except Exception:
                continue
            now = time.time()
            for r in rows:
                try:
                    last = float(r.get("last") or 0)
                except Exception:
                    last = 0.0
                if now - last < stale_secs:
                    continue
                jid = r["id"]
                gap = int(now - last)
                try:
                    if db.requeue_job(jid, error="stale %ds" % gap, max_attempts=4, delay=15):
                        db.add_event(jid, "watchdog: stale %ds -> requeued" % gap)
                    else:
                        db.update_job(jid, status="failed", stage="stale", completed_at=now,
                                      error="stale %ds" % gap)
                        db.add_event(jid, "watchdog: stale %ds -> failed (max attempts)" % gap)
                    if r.get("uid"):
                        db.release_account(r["uid"], status="ready", cooldown=120)
                except Exception as _e:
                    pass

    def _refresher(self):
        """Refresh credit for idle accounts every 10 minutes."""
        while self.running:
            time.sleep(600)
            try:
                rows = [a for a in db.list_accounts() if a.get("status") == "ready"]
            except Exception:
                continue
            low = [a for a in rows if int(a.get('credit') or 0) < 120]
            rest = [a for a in rows if int(a.get('credit') or 0) >= 120]
            rows = low + rest
            for a in rows[:40]:
                try:
                    c = PippitAccount(a["cookies"], uid=a["uid"], ports=a["ports"] or [22030, 22031, 22032], tag=a.get("tag"))
                    credit = c.credit()["total"]
                    if os.environ.get("SPO_AUTO_CLAIM", "0") == "1" and credit < 120:
                        c.claim_all(stop_at=340)
                        credit = c.credit()["total"]
                    db.set_account_credit(a["uid"], credit)
                except Exception:
                    pass
                time.sleep(0.5)

    def _process(self, job):
        jid = job["id"]
        cost = int(job.get("cost") or estimate_cost(job.get("seconds") or 30))
        db.update_job(jid, stage="picking_account", progress=2)
        acct = db.pick_account(cost if cost < 100 else 80)  # let low-credit accounts start a 30s job
        if not acct:
            db.update_job(jid, status="queued", stage="waiting_for_account", progress=0,
                          retry_after=time.time() + 60)
            db.add_event(jid, "no account with >= %d credit; retry in 60s" % cost)
            return
        uid = acct["uid"]
        fp = try_lock(uid)
        if not fp:
            db.release_account(uid, credit=acct.get("credit"), status="ready", cooldown=30)
            db.update_job(jid, status="queued", stage="account_busy", retry_after=time.time() + 20)
            db.add_event(jid, "account %s busy in another line; retry in 20s" % uid)
            return

        client = None
        credit = acct.get("credit") or 0
        spent = 0
        try:
            client = PippitAccount(acct["cookies"], uid=uid, ports=acct["ports"] or [22030, 22031, 22032],
                                   tag=acct.get("tag"), workspace_id=acct.get("workspace_id"),
                                   space_id=acct.get("space_id"))
            client.bootstrap()
            credit = client.credit()["total"]
            db.set_account_credit(uid, credit)
            if credit < cost:
                db.release_account(uid, credit=credit, status="ready", cooldown=300,
                                   error="low credit %d < %d" % (credit, cost))
                db.update_job(jid, status="queued", stage="waiting_for_account", progress=0,
                              retry_after=time.time() + 120, account_uid=None)
                db.add_event(jid, "account %s credit %d < %d" % (uid, credit, cost))
                return

            db.update_job(jid, account_uid=uid, stage="submitting", progress=5)
            db.add_event(jid, "using account %s credit=%d" % (uid, credit))

            _hb = [0.0]
            def on_progress(*args):
                try:
                    stage = str(args[0]) if args else "?"
                    aid = args[1] if len(args) > 1 else None
                    if time.time() - _hb[0] > 150:
                        _hb[0] = time.time()
                        db.add_event(jid, "hb %s" % stage[:20])
                    if stage == "submitted":
                        prog, st = 10, "submitted"
                        db.add_event(jid, "thread=%s run=%s" % (args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else ""))
                    elif stage in ("2", "3", "1"):
                        prog, st = 40, "generating"
                    else:
                        prog, st = 40, stage[:40]
                    if aid:
                        prog, st = 80, "asset_ready"
                    db.update_job(jid, progress=prog, stage=st)
                except Exception:
                    pass

            extra_parts = []
            for kind in ("image", "video", "audio"):
                src = job.get("ref_" + kind)
                if not src:
                    continue
                db.update_job(jid, stage="uploading_ref_" + kind)
                lp = _local_ref(src)
                if not _upload_ok():
                    raise RefUploadDown("upload paused: %s" % (_UPLOAD_DOWN[1] or "cdn down"))
                try:
                    info = refs.upload_asset(client.pp, lp, client.workspace_id,
                                             asset_type=refs.ASSET_TYPE[kind], log=lambda *a: None)
                except Exception as _ue:
                    _upload_trip(_ue)
                    raise RefUploadDown(repr(_ue)[:300])
                url, md = ("", {})
                if kind != "video":
                    try:
                        url, md = refs.asset_meta(client.pp, info["pippit_asset_id"])
                    except Exception:
                        url, md = ("", {})
                extra_parts.append(refs.make_part(info, asset_url=url, metadata=md))
                db.add_event(jid, "ref %s entry=%s %sx%s %.2fs %d bytes" %
                             (kind, info.get("asset_id"), info.get("width"), info.get("height"),
                              float(info.get("duration") or 0), int(info.get("size") or 0)))

            dest = os.path.join(VIDEOS, jid + ".mp4")
            prompt = job["prompt"]
            if os.environ.get("SPO_PROMPT_GUARD", "1") == "1" and "不要提问" not in prompt:
                prompt = prompt.rstrip() + ("\n\n【出片要求】直接调用视频生成工具一次成片：不要提问、不要请求确认、"
                                            "不要输出 JSON 或调用其它技能；若描述不完整，请自行补全合理的场景、主体与镜头。")

            secs = int(job.get("seconds") or 30)
            _m = str(job.get("model") or "").lower()
            if "smart" in _m or "auto" in _m or _m in ("", "default"):
                model_key, seg_cap, max_seg, multi_env, lane = None, 0.0, 0, "SPO_20_MULTI", ""
            elif "2.5" in _m or "2_5" in _m:
                model_key, seg_cap, max_seg, multi_env, lane = "Seedance_2.5", 15.0, 8, "SPO_25_MULTI", "2.5"
            elif "2.0" in _m or "2_0" in _m:
                if "fast" in _m:
                    model_key = "Seedance_2.0_fast"
                elif "mini_lite" in _m or "mini-lite" in _m or "minilite" in _m:
                    model_key = "Seedance_2.0_mini_lite"
                elif "mini" in _m:
                    model_key = "Seedance_2.0_mini"
                else:
                    model_key = "Seedance_2.0"
                seg_cap, max_seg, multi_env, lane = 10.0, 8, "SPO_20_MULTI", "2.0"
            elif "1.0" in _m or "_1_0" in _m:
                model_key, seg_cap, max_seg, multi_env, lane = "seedance_1.0", 12.0, 6, "SPO_20_MULTI", "1.0"
            else:
                model_key, seg_cap, max_seg, multi_env, lane = None, 0.0, 0, "SPO_20_MULTI", ""
            if model_key:
                db.add_event(jid, "model_key=%s" % model_key)
            # Seedance 2.5 单条被平台限到 ~13-20s（账号方案限制），30s 需多段拼接
            multi = bool(model_key) and secs > (seg_cap + 2) and os.environ.get(multi_env, "1") == "1"
            def _switch_account(reason):
                """Drop the current account and grab a fresh one for the next attempt."""
                nonlocal uid, client, fp, credit, spent
                try:
                    _cn = client.credit()["total"]
                    spent += max(0, credit - _cn)
                    credit = _cn
                except Exception:
                    pass
                try:
                    db.release_account(uid, credit=credit, status="ready", cooldown=600,
                                       failed=True, error=str(reason)[:300])
                except Exception:
                    pass
                try:
                    unlock(fp)
                except Exception:
                    pass
                for _try in range(3):
                    na = db.pick_account(cost)
                    if not na:
                        time.sleep(15)
                        continue
                    nfp = try_lock(na["uid"])
                    if not nfp:
                        db.release_account(na["uid"], status="ready", cooldown=30)
                        continue
                    try:
                        nc = PippitAccount(na["cookies"], uid=na["uid"],
                                           ports=na["ports"] or [22030, 22031, 22032],
                                           tag=na.get("tag"), workspace_id=na.get("workspace_id"),
                                           space_id=na.get("space_id"))
                        nc.bootstrap()
                    except Exception as _e:
                        unlock(nfp)
                        db.release_account(na["uid"], status="ready", cooldown=300,
                                           failed=True, error=repr(_e)[:300])
                        continue
                    uid, client, fp = na["uid"], nc, nfp
                    db.update_job(jid, account_uid=uid)
                    db.add_event(jid, "switched account -> %s (%s)" % (uid, str(reason)[:70]))
                    return True
                return False

            def _seg_run(sp, sd, seg_len, seg_parts):
                last_err = None
                for att in range(2):
                    try:
                        return client.run_video(sp, sd, seconds=seg_len,
                                                resolution=job.get("resolution") or "1080p",
                                                ratio=job.get("ratio") or "9:16",
                                                timeout=1200, on_progress=on_progress,
                                                extra_parts=(seg_parts or None),
                                                model_key=model_key)
                    except Exception as _e:
                        last_err = _e
                        db.add_event(jid, "segment attempt %d failed: %s" % (att + 1, repr(_e)[:150]))
                        if att >= 2:
                            raise
                        if seg_parts:
                            try:
                                seg_parts.clear()  # refs belong to the old account's workspace
                            except Exception:
                                pass
                        if not _switch_account(repr(_e)):
                            raise
                if last_err:
                    raise last_err

            if multi:
                try:
                    _xf = float(os.environ.get("SPO_XFADE", "0"))
                except Exception:
                    _xf = 0.0
                need = float(secs) + (_xf * 3 if _xf > 0 else 0.0)
                segs = []
                total = 0.0
                idx = 0
                db.add_event(jid, "%s multi: target %.0fs cap %.0fs/seg max %d" % (lane, need, seg_cap, max_seg))
                while total < need - 0.3 and idx < max_seg:
                    if idx > 0:
                        try:
                            credit_now = client.credit()["total"]
                            db.set_account_credit(uid, credit_now)
                            if credit_now < 60:
                                db.add_event(jid, "handoff: credit %d < 60" % credit_now)
                                if not _switch_account("credit %d < 60 mid-video" % credit_now):
                                    db.add_event(jid, "stop: no replacement account")
                                    break
                        except Exception:
                            pass
                    try:
                        _bal = client.credit()["total"]
                    except Exception:
                        _bal = 999
                    _cap = max(5, min(seg_cap, int((max(0, _bal) - 5) / 5.4)))
                    seg_len = int(round(min(float(_cap), max(5.0, need - total))))
                    if idx == 0:
                        sp = ("这是第1段，严格 %d 秒整（Strictly %d seconds of footage），单一连续镜头，"
                              "直接生成，不要询问确认。 " % (seg_len, seg_len)) + prompt
                    else:
                        sp = ("这是第%d段，严格 %d 秒整，紧接上一段结尾的画面与动作继续，"
                              "保持人物/场景/光线/镜头风格完全一致，不要重演上一段开头，直接生成，不要询问确认。 "
                              % (idx + 1, seg_len)) + prompt
                    sd = dest.replace(".mp4", ".s%d.mp4" % idx)
                    seg_parts = list(extra_parts or [])
                    if idx > 0 and _upload_ok() and os.environ.get("SPO_25_SEAM", "1") == "1":
                        try:
                            lf = dest.replace(".mp4", ".s%d.last.jpg" % (idx - 1))
                            subprocess.run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.2",
                                            "-i", segs[-1], "-frames:v", "1", "-q:v", "2", lf],
                                           check=True, timeout=180)
                            info = refs.upload_asset(client.pp, lf, client.workspace_id,
                                                     asset_type=refs.ASSET_TYPE["image"],
                                                     log=lambda *a: None)
                            u2, md2 = ("", {})
                            try:
                                u2, md2 = refs.asset_meta(client.pp, info["pippit_asset_id"])
                            except Exception:
                                pass
                            seg_parts.append(refs.make_part(info, asset_url=u2, metadata=md2))
                            sp = ("以参考图片（上一段的最后一帧）作为本段第一帧，画面、人物、光线、机位完全一致，"
                                  "从该画面继续往下演，不要重演上一段。") + sp
                            db.add_event(jid, "seam ref <- %s" % os.path.basename(segs[-1]))
                        except Exception as _e:
                            _upload_trip(_e)
                            db.add_event(jid, "seam ref failed: %s" % repr(_e)[:140])
                    db.update_job(jid, stage="segment_%d" % (idx + 1),
                                  progress=10 + int(70 * min(total, need) / need))
                    _seg_run(sp, sd, seg_len, seg_parts)
                    d = float(H.probe(sd).get("dur") or 0)
                    take = min(d, need - total)
                    if take <= 0.5:
                        db.add_event(jid, "segment %d unusable %.2fs" % (idx + 1, d))
                        try:
                            os.remove(sd)
                        except Exception:
                            pass
                        idx += 1
                        continue
                    if d > take + 0.05:
                        cut = sd + ".cut.mp4"
                        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", sd,
                                        "-t", "%.3f" % take, "-c", "copy", cut],
                                       check=True, timeout=300)
                        os.replace(cut, sd)
                        nd = float(H.probe(sd).get("dur") or 0)
                        if abs(nd - take) > 0.6:
                            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", sd,
                                            "-t", "%.3f" % take, "-c:v", "libx264",
                                            "-preset", "veryfast", "-crf", "20",
                                            "-c:a", "aac", "-b:a", "128k", cut],
                                           check=True, timeout=900)
                            os.replace(cut, sd)
                        db.add_event(jid, "trim seg%d %.2fs -> %.2fs" % (idx + 1, d, take))
                    segs.append(sd)
                    total += take
                    db.add_event(jid, "segment %d ok %.2fs kept %.2fs total %.2fs"
                                 % (idx + 1, d, take, total))
                    idx += 1
                if not segs:
                    raise RuntimeError("no usable segment generated")
                if float(total) < float(secs) - 0.6:
                    raise RuntimeError("short output %.2fs < %ss (segments=%d)"
                                       % (float(total), secs, idx))
                res = _concat(segs, dest)
                try:
                    _trim_to(dest, float(secs))
                except Exception as _e:
                    db.add_event(jid, "final trim skipped: %r" % (_e,))
                try:
                    res["credit_after"] = client.credit()["total"]
                except Exception:
                    res["credit_after"] = credit
            else:
                if secs >= 10 and not re.search(r"(\d+\s*(?:s|sec|secs|second|seconds|秒))", prompt, re.I):
                    prompt = ("%d-second video, %d shots of 5 seconds each. " % (secs, max(1, secs // 5))) + prompt
                    db.add_event(jid, "prompt duration hint added (%ds)" % secs)
                res = client.run_video(prompt, dest,
                                       seconds=secs,
                                       resolution=job.get("resolution") or "1080p",
                                       ratio=job.get("ratio") or "9:16",
                                       timeout=2400, on_progress=on_progress,
                                       extra_parts=(extra_parts or None), model_key=model_key)
            db.update_job(jid, progress=95, stage="probing")
            meta = H.probe(dest)
            db.update_job(jid, status="completed", progress=100, stage="completed",
                          account_uid=uid, thread_id=res.get("thread_id"), run_id=res.get("run_id"),
                          asset_id=res.get("asset_id"), video_path=dest, remote_url=res.get("url"),
                          completed_at=time.time(), width=meta.get("w"), height=meta.get("h"),
                          duration=meta.get("dur"), bytes=meta.get("size"),
                          cost=int(spent + max(0, credit - int(res.get("credit_after") or credit))))
            db.release_account(uid, credit=res.get("credit_after", credit), status="ready",
                               cooldown=180, done=True)
            db.add_event(jid, "completed asset=%s %dx%d %.3fs %d bytes" %
                         (res.get("asset_id"), meta.get("w"), meta.get("h"), meta.get("dur"), meta.get("size")))
        except RefUploadDown as e:
            db.add_event(jid, "ref upload unavailable: %s" % (str(e)[:200],))
            try:
                if client:
                    credit = client.credit()["total"]
            except Exception:
                pass
            db.release_account(uid, credit=credit, status="ready", cooldown=60)
            db.update_job(jid, status="queued", stage="ref_upload_wait", progress=0,
                          retry_after=max(time.time() + 30, _UPLOAD_DOWN[0]), account_uid=None)
        except Exception as e:
            err = repr(e)[:1500]
            try:
                if client:
                    credit = client.credit()["total"]
            except Exception:
                pass
            db.release_account(uid, credit=credit, status="ready", cooldown=300, failed=True, error=err)
            db.add_event(jid, "failed on %s: %s" % (uid, err))
            if db.requeue_job(jid, error=err, max_attempts=3, delay=45):
                db.add_event(jid, "requeued for another account")
            else:
                db.update_job(jid, status="failed", stage="failed", error=err, completed_at=time.time())
        finally:
            unlock(fp)


WORKER = None


def start_workers(max_workers=None):
    global WORKER
    if WORKER is None:
        WORKER = PoolWorker(max_workers)
        WORKER.start()
    return WORKER
