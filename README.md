# Abex Stakes — satellite order-relay bot

A tiny Discord bot that relays restock orders from the main **Abex Tech** bot into
partner servers, and relays claims back.

You add ("trust") this bot into each partner server. Because it is *present* in those
servers, its **Claim** dropdown actually works there — a click hands the main bot the
clicker's real Discord ID instantly. (A mirrored copy of another bot's message can't
do that: a Discord component only routes to the bot that posted it.)

It carries **no** market/DB/dashboard logic — everything authoritative lives in Abex Tech,
which is what keeps it light and safe to place in servers you don't fully control.

## How it works

1. Polls Abex Tech's `GET /api/network/orders` for the current open orders.
2. Posts a single order board per configured channel (edited in place, not spammed),
   with a **"Claim an order"** dropdown.
3. On claim → `POST /api/network/claim` with the worker's Discord ID → Abex Tech checks
   the order is still open, logs it, and pings the home worker channel.
4. The bot then DMs the worker an invite to the home server to finish their ticket.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Create a `.env` file next to `app.py` (it is gitignored — **never commit real values**):

| Variable | What it is |
| --- | --- |
| `SAT_BOT_TOKEN` | This bot's own token (Discord Developer Portal → your app → Bot). |
| `VHELPER_API_BASE` | The main Abex Tech web server, e.g. `https://your-dashboard.example.com`. |
| `NETWORK_SHARED_SECRET` | Must match `NETWORK_SHARED_SECRET` in Abex Tech's `.env`. Generate: `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `SAT_CHANNELS` | Channel IDs to post the board in — one per partner server, comma-separated. |
| `HOME_INVITE` | Invite to your home server, DM'd to a worker after they claim. |
| `SAT_REFRESH_MIN` | Board refresh interval in minutes (min 2, default 10). |

Example `.env` (placeholders only):

```dotenv
SAT_BOT_TOKEN=your-bot-token-here
VHELPER_API_BASE=https://your-dashboard.example.com
NETWORK_SHARED_SECRET=your-shared-secret-here
SAT_CHANNELS=123456789012345678,987654321098765432
HOME_INVITE=https://discord.gg/yourinvite
SAT_REFRESH_MIN=10
```

## Bot permissions

Invite it to each partner server with only:

- Send Messages
- Embed Links
- Use Application Commands

No privileged intents are required.

## Hosting (Pterodactyl / Wispbyte)

Use a **Python** egg. Upload `app.py`, `requirements.txt` and `.env` to
`/home/container/`. The default startup installs `requirements.txt` and runs
`python app.py` — set the egg's `PY_FILE` to `app.py`.

> **Security:** this repo is public. Never commit a real token or secret. If one is ever
> pushed, reset it immediately in the Discord Developer Portal — public repos are scraped
> for credentials within seconds.
