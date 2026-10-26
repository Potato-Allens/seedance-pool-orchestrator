# -*- coding: utf-8 -*-
"""claim_credits.py - finish + claim the one-off bonus tasks on an existing account.

The upstream platform grants a fixed welcome quota per new account; part of it is
delivered through "reward tasks" (authorize push, link email, download/export ...).
There is no daily refresh - verified by probing aged accounts: the same four task
ids come back with status=2 (already claimed).

Usage:
    python src/accounts/claim_credits.py --cookies cookies.json
    python src/accounts/claim_credits.py --cookies cookies.json --ports 0,7899 --json out.json
"""
import argparse
import json
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_ROOT = _os.path.abspath(_os.path.join(_HERE, "..", ".."))
_SRV = _os.path.join(_ROOT, "src", "server")
if _SRV not in _sys.path:
    _sys.path.insert(0, _SRV)
_os.environ.setdefault("PP_HOME", _ROOT)

from pippit import PippitAccount  # noqa: E402

TASK_IDS = (10010, 10008, 10011, 10005)


def load_cookies(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    if isinstance(data, list):
        return {c["name"]: c["value"] for c in data if c.get("name")}
    return dict(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", required=True, help="cookies.json (dict or DevTools list)")
    ap.add_argument("--uid", default=None)
    ap.add_argument("--workspace-id", default=None)
    ap.add_argument("--space-id", default=None)
    ap.add_argument("--ports", default="0", help="comma list of local proxy ports; 0 = direct")
    ap.add_argument("--json", default=None, help="write a result snapshot here")
    args = ap.parse_args()

    ports = [int(x) for x in str(args.ports).split(",") if x.strip()]
    cl = PippitAccount(load_cookies(args.cookies), uid=args.uid, ports=ports,
                       workspace_id=args.workspace_id, space_id=args.space_id)
    cl.bootstrap()

    before = cl.credit()
    print("credit before:", before)

    extra = []
    for tid in TASK_IDS:
        row = {"task": tid}
        for path in ("/api/biz/v1/reward_task/finish", "/api/biz/v1/reward_task/claim"):
            try:
                r = cl.pp.req(path, {"task_id": tid}, tries=1, timeout=20)
                j = r.json()
                row[path.rsplit("/", 1)[-1]] = {"ret": j.get("ret"), "errmsg": j.get("errmsg")}
            except Exception as e:  # noqa: BLE001
                row[path.rsplit("/", 1)[-1]] = {"error": str(e)[:200]}
        extra.append(row)

    after = cl.credit()
    print("credit after :", after)
    print("delta        :", after["total"] - before["total"])

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"before": before, "after": after, "tasks": extra}, f,
                      ensure_ascii=False, indent=1)
        print("written:", args.json)


if __name__ == "__main__":
    main()
