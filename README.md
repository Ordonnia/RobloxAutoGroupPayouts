# Roblox Auto Group Payouts

Automatically send Roblox group payouts via API. Works with the new `chef` challenge that Roblox now returns instead of `twostepverification`.

**No browser automation. No clicking. Just fast API calls.**

---

## What You Need

1. **Python 3.10+** installed ([download here](https://www.python.org/downloads/))
2. The **group owner's** `.ROBLOSECURITY` cookie
3. The **group owner's** TOTP secret (the 2FA authenticator secret key)
4. Your **group ID** (the number in your group's URL)

---

## Setup (Step by Step)

### 1. Download this repo

Click the green **Code** button above, then **Download ZIP**. Extract it somewhere.

Or if you have git:

```
git clone https://github.com/PandyOnGit/RobloxAutoGroupPayouts.git
cd RobloxAutoGroupPayouts
```

### 2. Install dependencies

Open a terminal/command prompt in the folder and run:

```
pip install -r requirements.txt
```

### 3. Create your `.env` file

Copy `.env.example` to `.env`:

```
copy .env.example .env
```

Now open `.env` in any text editor and fill in your values:

```
ROBLOSECURITY=_|WARNING:-DO-NOT-SHARE-THIS...your-full-cookie-here
GROUP_ID=12345678
TWOFACTOR_SECRET=JBSWY3DPEHPK3PXP
```

**Where to find these:**

- **ROBLOSECURITY** — Open Roblox in your browser, press F12, go to Application > Cookies > `.ROBLOSECURITY`. Copy the entire value.
- **GROUP_ID** — Go to your group page. The number in the URL is your group ID. Example: `roblox.com/groups/12345678/` means your ID is `12345678`.
- **TWOFACTOR_SECRET** — This is the secret key you got when you set up the authenticator app. If you lost it, you'll need to disable 2FA and re-enable it to get a new one. When Roblox shows you the QR code, there's usually a "can't scan?" link that reveals the text secret.

---

## Usage

### Send a payout

```
python roblox_payout.py --user-id 123456789 --amount 100
```

Replace `123456789` with the **recipient's Roblox user ID** and `100` with the **amount of Robux**.

### Run diagnostics first (optional)

To check if your cookie, TOTP, and group are set up correctly without actually sending Robux:

```
python test_payout.py --user-id 123456789 --amount 1 --probe-only
```

### Send multiple test payouts in a row

```
python test_payout.py --user-id 123456789 --amount 1 --runs 3
```

This sends 3 payouts of 1 Robux each, with automatic cooldowns between them.

---

## How It Works

1. Gets a CSRF token
2. Tries the payout — Roblox returns a `chef` challenge
3. Sends the challenge to Roblox's continue endpoint
4. Retries the payout with proof headers — **done**

If Roblox rate-limits the session (`blocksession`), it automatically waits and retries.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `CSRF failed. Cookie invalid?` | Your `.ROBLOSECURITY` cookie expired. Get a fresh one from your browser. |
| `TOTP verify failed` | Your `TWOFACTOR_SECRET` is wrong or belongs to a different account. Make sure it matches the account that owns the cookie. |
| `blocksession` / `AutomatedTampering` | Roblox rate limit. The script auto-retries. If it keeps failing, wait a few minutes. |
| `Challenge failed to authorize` | Usually means the session was flagged. Wait 2-3 minutes and try again. |
| Payout says success but no Robux received | Make sure the recipient is in the group and meets Roblox's payout eligibility. |

---

## Important

- **Never share your `.ROBLOSECURITY` cookie** — it's a full login token.
- **Never commit your `.env` file** — it's in `.gitignore` for a reason.
- The cookie owner **must** have payout permissions (usually the group owner).
- The `TWOFACTOR_SECRET` **must** be for the **same account** as the cookie.
