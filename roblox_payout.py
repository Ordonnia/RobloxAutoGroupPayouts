"""
Roblox Group Payout — pure API, zero browser dependencies.

Flow (4 HTTP calls on clean session):
  1. POST /v2/logout                              -> CSRF token
  2. POST /v1/groups/{id}/payouts                  -> 403 + chef challenge
  3. POST /challenge/v1/continue  (chef metadata)  -> 200 (empty = passed)
  4. POST /v1/groups/{id}/payouts  (proof headers)  -> 200

When chef continue returns empty, the challenge is done and the payout retry
succeeds immediately.  When it returns 2FA or blocksession, the session is
flagged — we wait and retry the entire flow from scratch.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass

import pyotp
import requests
from requests import Session

logger = logging.getLogger("roblox_payout")

PAYOUT_COOLDOWN = 90


@dataclass
class PayoutResult:
    success: bool
    message: str
    strategy: str = ""
    elapsed_ms: float = 0.0


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _totp(secret: str) -> str:
    return pyotp.TOTP(secret).now()


def _b64d(raw: str) -> dict:
    return json.loads(base64.b64decode(raw))


def _b64e(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def get_username(user_id: int) -> str:
    r = requests.get(
        f"https://users.roblox.com/v1/users/{user_id}",
        headers={"User-Agent": _UA},
    )
    r.raise_for_status()
    return r.json()["name"]


_last_payout_time: float = 0.0


class RobloxPayout:
    def __init__(self, roblosecurity: str, group_id: int, twofactor_secret: str):
        self.group_id = group_id
        self.twofactor_secret = twofactor_secret

        self.s = Session()
        self.s.cookies.set(".ROBLOSECURITY", roblosecurity, domain=".roblox.com")
        self.s.headers.update({
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/",
        })

    # -- primitives --------------------------------------------------------

    def _csrf(self) -> None:
        self.s.headers.pop("X-CSRF-TOKEN", None)
        r = self.s.post("https://auth.roblox.com/v2/logout")
        t = r.headers.get("X-CSRF-TOKEN") or r.headers.get("x-csrf-token")
        if not t:
            raise RuntimeError(f"CSRF failed (HTTP {r.status_code}). Cookie invalid?")
        self.s.headers["X-CSRF-TOKEN"] = t
        logger.info("CSRF OK (%s...)", t[:12])

    def _payout_post(self, user_id: int, amount: int) -> requests.Response:
        try:
            return self.s.post(
                f"https://groups.roblox.com/v1/groups/{self.group_id}/payouts",
                json={
                    "PayoutType": 1,
                    "Recipients": [{"recipientId": user_id, "recipientType": 0, "amount": amount}],
                },
            )
        except requests.ConnectionError:
            logger.warning("Connection dropped, retrying with fresh socket...")
            self.s.close()
            return self.s.post(
                f"https://groups.roblox.com/v1/groups/{self.group_id}/payouts",
                json={
                    "PayoutType": 1,
                    "Recipients": [{"recipientId": user_id, "recipientType": 0, "amount": amount}],
                },
            )

    def _continue(self, challenge_id: str, challenge_type: str, metadata: dict) -> requests.Response:
        logger.debug("POST /continue  cid=%s  type=%s  keys=%s",
                      challenge_id[:30], challenge_type, list(metadata.keys()))
        return self.s.post(
            "https://apis.roblox.com/challenge/v1/continue",
            json={
                "challengeId": challenge_id,
                "challengeType": challenge_type,
                "challengeMetadata": json.dumps(metadata),
            },
        )

    def _verify_totp(self, user_id: str, challenge_id: str) -> str:
        code = _totp(self.twofactor_secret)
        logger.info("TOTP code=%s  user=%s  cid=%s", code, user_id, challenge_id[:30])
        r = self.s.post(
            f"https://twostepverification.roblox.com/v1/users/{user_id}/challenges/authenticator/verify",
            json={"actionType": "Generic", "challengeId": challenge_id, "code": code},
        )
        body = r.json()
        logger.info("TOTP verify -> %s  %s", r.status_code, json.dumps(body)[:150])
        if "errors" in body:
            raise RuntimeError(f"TOTP verify failed: {body['errors'][0]['message']}")
        return body["verificationToken"]

    # -- main flow ---------------------------------------------------------

    def payout(self, user_id: int, amount: int, _retries: int = 3) -> PayoutResult:
        global _last_payout_time
        t0 = time.perf_counter()

        # Enforce cooldown between payouts
        if _last_payout_time > 0:
            since = time.time() - _last_payout_time
            if since < PAYOUT_COOLDOWN:
                wait = PAYOUT_COOLDOWN - since
                logger.info("Cooldown: %.0fs since last, waiting %.0fs ...", since, wait)
                time.sleep(wait)

        self._csrf()

        r = self._payout_post(user_id, amount)
        logger.info("Payout -> %s  body=%s", r.status_code, r.text[:200])

        if r.status_code == 200:
            _last_payout_time = time.time()
            return PayoutResult(True, "Sent (no challenge).", "direct", _ms(t0))

        ctype = (r.headers.get("rblx-challenge-type") or "").lower()
        cid = r.headers.get("rblx-challenge-id", "")
        cmeta_b64 = r.headers.get("rblx-challenge-metadata", "")

        if not ctype or not cid:
            return self._err(r, "api", t0)

        logger.info("Challenge: type=%s  id=%s  meta_b64_len=%d", ctype, cid, len(cmeta_b64))

        # blocksession: session is temporarily flagged, wait and retry
        if ctype == "blocksession":
            return self._handle_blocksession(user_id, amount, _retries, t0)

        # twostepverification without chef wrapper (legacy path)
        if ctype == "twostepverification":
            return self._solve_2fa_direct(user_id, amount, cid, cmeta_b64, t0)

        # chef challenge
        if ctype == "chef":
            return self._solve_chef(user_id, amount, cid, cmeta_b64, t0, _retries)

        return PayoutResult(False, f"Unknown challenge: {ctype}", "api", _ms(t0))

    # -- chef flow ---------------------------------------------------------

    def _solve_chef(
        self, user_id: int, amount: int,
        outer_cid: str, chef_meta_b64: str, t0: float,
        _retries: int,
    ) -> PayoutResult:
        chef_meta = _b64d(chef_meta_b64)
        logger.info("Chef keys: %s", list(chef_meta.keys()))

        rc = self._continue(outer_cid, "chef", chef_meta)
        cont = rc.json()
        next_type = cont.get("challengeType", "")
        next_meta = cont.get("challengeMetadata", "")
        logger.info("Chef continue -> %s (%d bytes)  next='%s'  meta_len=%d",
                     rc.status_code, len(rc.content), next_type, len(next_meta or ""))

        if rc.status_code != 200:
            return PayoutResult(
                False, f"Chef continue HTTP {rc.status_code}: {rc.text[:200]}",
                "api-chef", _ms(t0),
            )

        # EMPTY: chef passed, retry payout immediately
        if not next_type and not next_meta:
            logger.info("Chef PASSED (empty) -> retrying payout with proof headers")
            self.s.headers.update({
                "rblx-challenge-id": outer_cid,
                "rblx-challenge-type": "twostepverification",
                "rblx-challenge-metadata": chef_meta_b64,
            })
            r2 = self._payout_post(user_id, amount)
            self._clean_headers()
            logger.info("Retry -> %s  %s", r2.status_code, r2.text[:200])

            if r2.status_code == 200:
                global _last_payout_time
                _last_payout_time = time.time()
                return PayoutResult(True, "Sent (chef->empty->retry).", "api-chef", _ms(t0))
            return self._err(r2, "api-chef", t0)

        # 2FA sub-challenge: try to solve it
        if next_type == "twostepverification" and next_meta:
            logger.info("Chef -> 2FA sub-challenge")
            return self._solve_2fa_sub(
                user_id, amount, outer_cid, chef_meta_b64, next_meta, t0, _retries,
            )

        # blocksession or anything else: session flagged, wait and retry fresh
        logger.warning("Chef continue returned '%s' -> session flagged, will wait and retry", next_type)
        return self._handle_blocksession(user_id, amount, _retries, t0)

    def _solve_2fa_sub(
        self, user_id: int, amount: int,
        outer_cid: str, chef_meta_b64: str,
        tfa_meta_raw: str, t0: float, _retries: int,
    ) -> PayoutResult:
        """Solve the 2FA sub-challenge that chef unlocked."""
        tfa_meta = json.loads(tfa_meta_raw) if isinstance(tfa_meta_raw, str) else tfa_meta_raw
        tfa_user = str(tfa_meta["userId"])
        tfa_cid = tfa_meta["challengeId"]
        logger.info("2FA: user=%s  cid=%s  meta_keys=%s", tfa_user, tfa_cid, list(tfa_meta.keys()))

        try:
            vtoken = self._verify_totp(tfa_user, tfa_cid)
        except RuntimeError as exc:
            return PayoutResult(False, str(exc), "api-chef-2fa", _ms(t0))

        logger.info("TOTP OK  token=%s...", vtoken[:16])

        # Fill the token into the FULL metadata (matching browser behavior)
        tfa_meta["verificationToken"] = vtoken
        tfa_meta["rememberDevice"] = False

        rc2 = self._continue(outer_cid, "twostepverification", tfa_meta)
        cont2 = rc2.json()
        c2_type = cont2.get("challengeType", "")
        c2_meta = cont2.get("challengeMetadata", "")
        logger.info("2FA continue -> %s (%d bytes)  type='%s'  meta_len=%d",
                     rc2.status_code, len(rc2.content), c2_type, len(c2_meta or ""))

        # Build TOTP proof for retry headers
        tfa_proof = _b64e({
            "rememberDevice": False,
            "actionType": "Generic",
            "verificationToken": vtoken,
            "challengeId": tfa_cid,
        })

        # If 2FA continue returned blocksession, the challenge is poisoned.
        # Wait and retry fresh instead of hammering failed retries.
        if c2_type == "blocksession":
            logger.warning("2FA continue -> blocksession (session flagged)")
            logger.warning("Challenge is poisoned. Waiting before fresh retry...")
            return self._handle_blocksession(user_id, amount, _retries, t0)

        # Otherwise try to retry the payout with proof
        for proof, label in [(tfa_proof, "totp"), (chef_meta_b64, "chef-meta")]:
            for rtype in ["twostepverification", "chef"]:
                self.s.headers.update({
                    "rblx-challenge-id": outer_cid,
                    "rblx-challenge-type": rtype,
                    "rblx-challenge-metadata": proof,
                })
                r2 = self._payout_post(user_id, amount)
                self._clean_headers()
                logger.info("Retry(%s,%s) -> %s  %s", label, rtype, r2.status_code, r2.text[:150])

                if r2.status_code == 200:
                    global _last_payout_time
                    _last_payout_time = time.time()
                    return PayoutResult(True, f"Sent ({label},{rtype}).", "api-chef-2fa", _ms(t0))

                new_ct = (r2.headers.get("rblx-challenge-type") or "").lower()
                if new_ct:
                    logger.info("New challenge '%s' -> aborting retry", new_ct)
                    return self._err(r2, "api-chef-2fa", t0)

        return self._err(r2, "api-chef-2fa", t0)

    # -- blocksession handler (shared) ------------------------------------

    def _handle_blocksession(
        self, user_id: int, amount: int, _retries: int, t0: float,
    ) -> PayoutResult:
        if _retries <= 0:
            return PayoutResult(
                False,
                "Session blocked (AutomatedTampering). Wait a few minutes.",
                "api", _ms(t0),
            )
        wait = 90
        logger.warning("Session flagged -> waiting %ds then fresh retry (%d left) ...", wait, _retries)
        time.sleep(wait)
        # Close stale connections so the retry uses fresh TCP sockets
        self.s.close()
        return self.payout(user_id, amount, _retries - 1)

    # -- legacy 2FA-only (no chef wrapper) --------------------------------

    def _solve_2fa_direct(
        self, user_id: int, amount: int,
        outer_cid: str, meta_b64: str, t0: float,
    ) -> PayoutResult:
        meta = _b64d(meta_b64)
        tfa_cid = meta["challengeId"]
        tfa_user = str(meta["userId"])

        try:
            vtoken = self._verify_totp(tfa_user, tfa_cid)
        except RuntimeError as exc:
            return PayoutResult(False, str(exc), "api-totp", _ms(t0))

        tfa_proof = {
            "rememberDevice": False,
            "actionType": "Generic",
            "verificationToken": vtoken,
            "challengeId": tfa_cid,
        }
        self._continue(outer_cid, "twostepverification", tfa_proof)

        self.s.headers.update({
            "rblx-challenge-id": outer_cid,
            "rblx-challenge-type": "twostepverification",
            "rblx-challenge-metadata": _b64e(tfa_proof),
        })
        r2 = self._payout_post(user_id, amount)
        self._clean_headers()

        if r2.status_code == 200:
            global _last_payout_time
            _last_payout_time = time.time()
            return PayoutResult(True, "Sent (TOTP direct).", "api-totp", _ms(t0))

        return self._err(r2, "api-totp", t0)

    # -- util --------------------------------------------------------------

    def _clean_headers(self) -> None:
        for h in ("rblx-challenge-id", "rblx-challenge-type", "rblx-challenge-metadata"):
            self.s.headers.pop(h, None)

    @staticmethod
    def _err(r: requests.Response, strat: str, t0: float) -> PayoutResult:
        try:
            e = r.json()["errors"][0]["message"]
        except Exception:
            e = r.text[:300]
        return PayoutResult(False, f"HTTP {r.status_code}: {e}", strat, _ms(t0))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    p = argparse.ArgumentParser(description="Roblox Group Payout (pure API)")
    p.add_argument("--user-id", type=int, required=True)
    p.add_argument("--amount", type=int, required=True)
    args = p.parse_args()

    from config import ROBLOSECURITY, GROUP_ID, TWOFACTOR_SECRET
    rp = RobloxPayout(ROBLOSECURITY, GROUP_ID, TWOFACTOR_SECRET)
    res = rp.payout(args.user_id, args.amount)

    print(f"\nSuccess  : {res.success}")
    print(f"Strategy : {res.strategy}")
    print(f"Elapsed  : {res.elapsed_ms:.0f} ms")
    print(f"Message  : {res.message}")


if __name__ == "__main__":
    main()
