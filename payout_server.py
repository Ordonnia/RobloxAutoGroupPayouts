"""
Runs a local Flask server that can be used to instruct to payout from other applications.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import time

import pyotp
import requests
from flask import Flask, request, Response

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"


def hr(t: str) -> None:
    print(f"\n{'='*60}\n  {t}\n{'='*60}")


def check_cookie(c: str) -> dict | None:
    hr("1 · Validate cookie")
    r = requests.get("https://users.roblox.com/v1/users/authenticated",
                     headers={"Cookie": f".ROBLOSECURITY={c}", "User-Agent": UA})
    if r.status_code == 200:
        d = r.json()
        print(f"  OK — {d['name']} (id {d['id']})")
        return d
    print(f"  FAIL — {r.status_code}: {r.text[:200]}")
    return None


def check_totp(s: str) -> None:
    hr("2 · TOTP code")
    try:
        print(f"  OK — {pyotp.TOTP(s).now()}")
    except Exception as e:
        print(f"  FAIL — {e}")


def check_user(uid: int) -> None:
    hr("3 · Recipient lookup")
    r = requests.get(f"https://users.roblox.com/v1/users/{uid}")
    print(f"  {'OK — ' + r.json()['name'] if r.status_code == 200 else 'FAIL — ' + str(r.status_code)}")


def check_group(c: str, gid: int, auth_uid: int) -> None:
    hr("4 · Group ownership")
    r = requests.get(f"https://groups.roblox.com/v1/groups/{gid}",
                     headers={"Cookie": f".ROBLOSECURITY={c}", "User-Agent": UA})
    if r.status_code != 200:
        print(f"  FAIL — {r.status_code}")
        return
    d = r.json()
    oid = d.get("owner", {}).get("userId")
    print(f"  Group : {d.get('name')}")
    print(f"  Owner : {d['owner'].get('username')} (id {oid})")
    print(f"  Match : {'YES' if oid == auth_uid else 'NO — check payout permissions!'}")


def probe_payout(c: str, gid: int, uid: int, amt: int) -> dict:
    """Probe the payout endpoint (read-only, no CSRF so it won't consume a challenge)."""
    hr("5 · Probe payout endpoint (read-only)")
    h = {"Cookie": f".ROBLOSECURITY={c}", "User-Agent": UA, "Content-Type": "application/json",
         "Origin": "https://www.roblox.com", "Referer": "https://www.roblox.com/"}
    r = requests.post(f"https://groups.roblox.com/v1/groups/{gid}/payouts", headers=h,
                      json={"PayoutType": 1, "Recipients": [{"recipientId": uid, "recipientType": 0, "amount": amt}]})
    print(f"  Status: {r.status_code}")
    ct = r.headers.get("rblx-challenge-type", "")
    cid = r.headers.get("rblx-challenge-id", "")
    mb = r.headers.get("rblx-challenge-metadata", "")
    if mb:
        try:
            meta = json.loads(base64.b64decode(mb))
            print(f"  Challenge type: {ct}")
            print(f"  Challenge id  : {cid}")
            print(f"  Metadata keys : {list(meta.keys())}")
        except Exception as e:
            print(f"  Metadata error: {e}")
    return {"status": r.status_code, "type": ct, "id": cid}


def run_payout(rp, user_id: int, amount: int, run_num: int) -> (bool, str):
    hr(f"Payout #{run_num}, {user_id}, {amount}")
    t0 = time.time()
    res = rp.payout(user_id, amount)
    print(f"  Success : {res.success}")
    print(f"  Strategy: {res.strategy}")
    print(f"  Elapsed : {res.elapsed_ms:.0f} ms")
    print(f"  Message : {res.message}")
    print(f"  Wall    : {time.time() - t0:.1f}s")
    return res.success, res.message


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--probe-only", action="store_true")
    p.add_argument("--runs", type=int, default=1, help="Number of consecutive payouts")
    p.add_argument("--skip-checks", action="store_true", help="Skip diagnostic checks")
    args = p.parse_args()

    from config import GROUP_ID, ROBLOSECURITY, TWOFACTOR_SECRET
    if not ROBLOSECURITY or not GROUP_ID:
        sys.exit("Set ROBLOSECURITY and GROUP_ID in .env")

    if not args.skip_checks:
        auth = check_cookie(ROBLOSECURITY)
        if not auth:
            sys.exit(1)
        if TWOFACTOR_SECRET:
            check_totp(TWOFACTOR_SECRET)

    if args.probe_only:
        probe_payout(ROBLOSECURITY, GROUP_ID, args.user_id, args.amount)
        hr("Done (probe only)")
        return

    from roblox_payout import RobloxPayout

    print("\n\n\n--- OK! We are ready to execute Payment Server!! ---")

    app = Flask(__name__)

    @app.route("/execute-payment", methods=["POST"])
    def hello():
        data = request.json
        print(data)

        rp = RobloxPayout(ROBLOSECURITY, GROUP_ID, TWOFACTOR_SECRET)
        results, messages = [], []

        for i in range(1, args.runs + 1):
            ok, message = run_payout(rp, data['user_id'], data['amount'], i)
            results.append(ok)
            messages.append(message)
            if not ok:
                print(f"\n  *** Run #{i} FAILED — stopping. ***")
                break

        hr("Summary")
        for i, ok in enumerate(results, 1):
            print(f"  #{i}: {'SUCCESS' if ok else 'FAIL'}")
        print(f"  {sum(results)}/{len(results)} succeeded")

        return Response(json.dumps({
            "success": sum(results) == len(results),
            "results": results,
            "messages": messages
        }), status=200 if sum(results) == len(results) else 500, mimetype='application/json')

    app.run(host="127.0.0.1", port=3950)


if __name__ == "__main__":
    main()
