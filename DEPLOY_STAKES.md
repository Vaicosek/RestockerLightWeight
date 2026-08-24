# Abex Stakes — how it works + deploy

An auction house for **land and items**, built to beat the competitors on one thing:
it's dead simple. The commands live on this branded satellite bot; all the money,
valuation, escrow, and loyalty live in the Abex Tech core bot (Abex Tech) — reached over
the network API. **Main Restocker shows no auction commands at all.**

## The whole seller flow is one command (on this bot)

```
/sell  title  starting_price  [buy_now]  [photo] [photo2] [photo3]  [details]  [category]
```

Two required fields — a name and an opening price. Drag photos straight in. Optionally
set a Buy-It-Now. Pick a category from the dropdown (**Land**, **Artificial Land**,
**Weapons**, **Artifacts**, **Other** — the two land ones flip it to land-kind, turning on
the AI valuation + the 65%-rule company backing). Everyone else never types a command:
the board shows **💰 Bid** / **🛒 Buy Now** buttons.

Coins are held in escrow the instant a bid lands; the previous top bidder is refunded
automatically when outbid **and DM'd** that they were outbid. On close, coins move on
their own — seller paid net of commission, house takes its cut, and a **private transfer
room** (thread) opens with the seller + winner to coordinate the handover. No "DM the
owner to finalize" like the competitors.

## Commands on this bot

Player: `/sell`, `/cancel` (your own listing, before any bid). Participation is buttons.
Managers (Manage Server): `/auction_close`, `/auction_config`, `/auction_notifypanel`.

## Loyalty (the moat the competitors can't match)

Every completed sale awards Abex Tech loyalty points to **both** buyer and seller
(`10 + 0.0001 × price`), written to the main bot's existing loyalty system — so it shows
up in `/loyalty`. Can't be farmed; only real sales pay out. And a seller's **commission
drops automatically by loyalty tier** (1k pts → 0.5% off … 20k → 2.5% off, floor 1%) —
a real "discount at Abex Tech" with no coupon to redeem.

## Get-notified roles (opt-in)

Set role IDs in this bot's `.env` (`SAT_LAND_PING_ROLE`, `SAT_ITEM_PING_ROLE`), then run
`/auction_notifypanel` in a channel. Members click **🔔 Notify me** to self-assign the
role (click again to opt out). When a new listing of that kind is posted, the bot
@mentions the role. Make sure the bot's own role sits **above** the ping roles.

---

# Deploy

## 1. Create the Discord bot (yours to do — I can't touch the token)

1. https://discord.com/developers/applications → **New Application** → `Abex Stakes` → Create.
2. **Bot** tab → set avatar/username. Leave all three Privileged Intents **OFF**.
3. **Reset Token** → copy into this bot's `.env` as `SAT_BOT_TOKEN`. Never commit it.
4. **OAuth2 → URL Generator** → scopes `bot` + `applications.commands`; permissions
   `Send Messages` + `Embed Links` + `Use Application Commands` + `Manage Roles`
   (Manage Roles is needed for the self-assign notify panel) → invite the bot.

## 2. Turn the API on in Abex Tech (once)

In Abex Tech core's `.env`, set `NETWORK_SHARED_SECRET` to a long random value
(`python -c "import secrets;print(secrets.token_urlsafe(32))"`) and restart. All the land
endpoints (`create`, `bid`, `buy`, `cancel`, `close`, `config`, `listings`) are already
registered.

## 3. Configure + run the satellite

Copy `.env.land.example` → `.env`, fill in `SAT_FEATURES=land`, the bot token, your
dashboard URL, the **same** `NETWORK_SHARED_SECRET` as Abex Tech, and (optionally) the
notify role IDs. Then:

```bash
pip install -r requirements.txt
python app.py
```

Run `/setup` in the target channel (needs Manage Server) to post the board; `/remove`
stops it. Host it as a second Python process alongside the orders satellite (Pterodactyl
/ Wispbyte Python egg, `PY_FILE=app.py`).

## Notes / known edges

- Photos on `/sell` are passed as Discord attachment URLs (the satellite has no message to
  pin them to like a hub post would); these can expire on very long auctions. Fine for
  typical runs; re-hosting can be added if it becomes an issue.
- Transfer rooms + winner/outbid DMs are done by the **main** bot, so they land wherever
  main can reach the users (your hub / a configured `realestate:deals_channel`). For a
  pure partner-server winner not in the hub, they still get the DM + can be invited.
