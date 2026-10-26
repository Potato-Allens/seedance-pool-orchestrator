# -*- coding: utf-8 -*-
"""Batch driver for the pool (seedance-2.5-1080p-30s)."""

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

import argparse, json, os, sys, time, threading, random, urllib.request, urllib.error

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8788"
KEY  = os.environ.get("POOL_API_KEY", "")
OUT  = OUT_DIR
REC = _os.path.join(APP_DIR, "recon")

PROMPTS = [
 "清晨六点的老城菜市场，穿藏青布衫的老太太挑拣青菜，蒸汽从早点摊腾起，镜头从她布满皱纹的手摇到她眯眼微笑的脸，最后拉远成整条巷子的烟火气。纪实电影质感，浅景深。",
 "暴雨夜的霓虹街头，外卖骑手把雨衣掀到背后冲进楼道，水珠顺着他下巴滴落，他在楼梯间停住，抬头看手机屏幕上的一条消息，深呼吸，转身继续上楼。冷调蓝紫光，手持跟拍。",
 "沙漠公路正午，一辆掉漆的红色皮卡拖着尾尘，副驾的男孩把手臂伸出窗外，风把他的头发吹乱，镜头从轮胎低角度仰拍，切到车顶俯瞰整条无人的公路延伸到地平线。暖黄颗粒感。",
 "手术室外的走廊，年轻护士摘下口罩靠在墙上闭眼三秒，监护仪的滴答声渐远，走廊尽头有人推着担架经过，她睁眼，把口罩重新戴好走回门里。浅冷白荧光，长焦压缩感。",
 "雪山营地日出，帐篷拉链拉开，男人呼出白气，镜头跟随他把咖啡壶架在气炉上，热气升腾遮住远景的雪峰，随后切成雪峰全景。8K 风光质感，高动态范围。",
 "深夜便利店的落地窗前，穿校服的女孩趴在桌上写题，店员把一杯热牛奶放在她手边，她抬头说了声谢谢，窗外雨在玻璃上流成线。暖光与冷雨对比，35mm 胶片质感。",
 "老式理发店里，剃刀刮过泡沫的声音，老师傅的手稳得像在雕木头，镜子里映出客人闭眼的松弛表情，最后镜头拉远露出满墙的旧照片。复古黄绿色调。",
 "地铁末班车厢，只有一个人，他靠着车门看窗外隧道灯一格一格划过脸，忽然笑了一下，低头删掉手机里的一条草稿，到站起身走出。冷白与隧道黄交替，静帧式构图。",
 "稻田中央的水渠边，赤脚少年把一只纸船放进水里，蹲下看它顺流漂远，夕阳在他背后烧成一片橘红，镜头从水面仰拍他的剪影。金色时刻，柔光。",
 "凌晨四点的面馆后厨，师傅把一团面反复摔在案板上，面粉在灯下浮成雾，手腕发力时青筋凸起，最后把面甩进滚水，蒸汽扑面。高对比侧光，慢动作细节。",
 "城市天台，穿西装的男人解开领带，迎着风站到边缘，远处是整片亮着的楼群，他掏出手机又放回口袋，转身走回楼梯口。夜景大远景，冷暖混合。",
 "雨后的青石巷，穿汉服的姑娘撑着油纸伞走过，裙摆扫过水洼，倒影里是两侧白墙黛瓦，镜头贴地跟拍，最后伞微微抬起露出她的侧脸。潮湿的冷绿调。",
 "高原牧场上，藏族老人把糌粑捏成团递给孙子，孩子咬了一口皱鼻子，老人笑出满脸褶子，背景是成群的黑牦牛慢镜头走过。强日照，真实肤色。",
 "凌晨的网吧，少年戴着耳机盯着屏幕，手指飞快敲击键盘，赢下一局后猛地把椅子往后一推，仰头大笑，旁边的人拍他肩膀。RGB 蓝紫光，显示器反光。",
 "海边礁石，女人赤脚站在浪里，风把她的白衬衫吹得鼓起，她闭眼把所有重量交给海风，一个大浪打来她踉跄后退又站住，笑着抹掉脸上的水。自然光，手持。",
 "冬夜的车站站台，穿军大衣的男人提着一个旧帆布包，呼出的白气模糊了身后的灯光，广播响起时他抬头，眼神从疲惫变成期待，快步向出口走去。暖黄路灯，浅景深。",
 "拍卖行展厅，穿旗袍的鉴定师举起一只青花瓷瓶对着灯看，指尖沿着开片纹路轻轻滑过，镜头绕着瓶身一圈，最后停在她微微挑眉的表情。侧逆光，器物细节。",
 "午夜厨房，一个人煮面，水开时他关了火，坐在台面上看着面慢慢泡软，窗外是没关的霓虹招牌，他忽然想起什么，起身把碗端到窗边。低照度，颗粒感。",
 "山谷里的玻璃温室，穿围裙的女人给一排番茄苗浇水，阳光透过玻璃在水雾里散成光束，一只猫从架子上跳下来蹭她的腿，她弯腰抱起。晨光，柔和高光。",
 "赛道上，改装车过弯时轮胎冒白烟，镜头贴着地面跟拍，切到驾驶舱里车手咬牙的侧脸和不断跳动的转速表，最后冲线时他松手长出一口气。高饱和，硬光。",
]

def req(path, method="GET", body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Authorization": "Bearer " + KEY,
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return json.loads(f.read().decode("utf-8", "replace"))

def submit(prompt, seconds=30, res="1080p", ratio="9:16"):
    return req("/v1/videos", "POST", {"prompt": prompt, "model": "seedance-2.5-1080p-30s",
                                      "seconds": seconds, "resolution": res, "ratio": ratio})

def status(jid):
    return req("/v1/videos/" + jid, timeout=30)

def download(jid, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    r = urllib.request.Request(BASE + "/v1/videos/" + jid + "/content",
                               headers={"Authorization": "Bearer " + KEY})
    tmp = dest + ".part"
    with urllib.request.urlopen(r, timeout=600) as f, open(tmp, "wb") as o:
        while True:
            b = f.read(1 << 20)
            if not b: break
            o.write(b)
    os.replace(tmp, dest)
    return os.path.getsize(dest)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--inflight", type=int, default=8)
    ap.add_argument("--tag", default="a")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--seconds", type=int, default=30)
    args = ap.parse_args()

    state_path = os.path.join(REC, "state_%s.json" % args.tag)
    st = {"submitted": [], "done": [], "failed": [], "n": 0}
    if os.path.exists(state_path):
        st = json.load(open(state_path, encoding="utf-8"))

    lock = threading.Lock()
    stop = threading.Event()

    def save():
        tmp = state_path + ".tmp"
        json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, state_path)

    def worker(idx):
        while not stop.is_set():
            with lock:
                if st["n"] >= args.limit:
                    return
                n = st["n"]; st["n"] += 1
            p = PROMPTS[(n + idx * 7) % len(PROMPTS)]
            prompt = p + " 竖屏 9:16，电影级画质，无字幕无文字。"
            try:
                j = submit(prompt, seconds=args.seconds)
                jid = j["id"]
            except Exception as e:
                with lock:
                    st["failed"].append({"n": n, "err": "submit:%r" % (e,), "ts": time.time()})
                    save()
                print("SUBMIT-FAIL", n, repr(e)[:200], flush=True)
                time.sleep(5); continue
            with lock:
                st["submitted"].append({"n": n, "id": jid, "ts": time.time(), "prompt": p[:40]})
                save()
            print("SUBMIT", n, jid, flush=True)
            t0 = time.time(); last = None
            while time.time() - t0 < 3600:
                try:
                    s = status(jid)
                except Exception as e:
                    time.sleep(15); continue
                cur = (s.get("status"), s.get("progress"), s.get("stage"))
                if cur != last:
                    print("POLL", jid, cur, int(time.time() - t0), flush=True)
                    last = cur
                if s.get("status") == "completed":
                    rec = {"n": n, "id": jid, "secs": time.time() - t0,
                           "duration": s.get("duration"), "bytes": s.get("bytes"),
                           "width": s.get("width"), "height": s.get("height")}
                    if args.download:
                        try:
                            rec["local"] = download(jid, os.path.join(OUT, jid + ".mp4"))
                        except Exception as e:
                            rec["dl_err"] = repr(e)[:200]
                    with lock:
                        st["done"].append(rec); save()
                    print("DONE", json.dumps(rec, ensure_ascii=False), flush=True)
                    break
                if s.get("status") in ("failed", "cancelled"):
                    with lock:
                        st["failed"].append({"n": n, "id": jid, "err": str(s.get("error"))[:400],
                                             "secs": time.time() - t0, "ts": time.time()})
                        save()
                    print("FAIL", jid, str(s.get("error"))[:200], flush=True)
                    break
                time.sleep(15)
            else:
                with lock:
                    st["failed"].append({"n": n, "id": jid, "err": "poll timeout", "ts": time.time()})
                    save()

    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(args.inflight)]
    for t in ths: t.start()
    for t in ths: t.join()
    print("ALL DONE done=%d failed=%d" % (len(st["done"]), len(st["failed"])), flush=True)
    save()

if __name__ == "__main__":
    main()
