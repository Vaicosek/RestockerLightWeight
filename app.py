#!/usr/bin/env python3
"""
RestockerLightweight — satellite order-relay bot.

A tiny bot you OWN and "trust" into partner Discord servers. Its ONLY job:

  1. Pull the current open restock orders from your main V Helper bot's web API.
  2. Post them as one board (with a working "Claim an order" dropdown) into a
     configured channel in each partner server, refreshed on a timer.
  3. When someone claims, capture their Discord ID, tell V Helper, and DM them an
     invite to your home server to finish and open their ticket.

It carries NO market/DB/dashboard logic — everything authoritative lives in V Helper.
That's what keeps it lightweight and safe to add to servers you don't fully control.

Why this works when a mirrored post doesn't: a Discord button only routes to the bot
that POSTED it. Because THIS bot is present in each partner server, its dropdown
actually works there, so a click hands you the clicker's real Discord ID instantly.

── Setup ────────────────────────────────────────────────────────────────────────
Put a `.env` next to this file (copy .env.example). Then:

    pip install -r requirements.txt
    python app.py

Invite the bot to each partner server with only: Send Messages + Embed Links +
Use Application Commands. No privileged intents needed.
"""
import os
import logging

import aiohttp
import discord
from discord.ext import tasks

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

log = logging.getLogger("satellite")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _env(name, default=""):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


TOKEN       = _env("SAT_BOT_TOKEN")
API_BASE    = _env("VHELPER_API_BASE", "https://dashboard.vaicosmarket.com").rstrip("/")
SECRET      = _env("NETWORK_SHARED_SECRET")
HOME_INVITE = _env("HOME_INVITE")
REFRESH_MIN = max(2, int(_env("SAT_REFRESH_MIN", "10") or "10"))
CHANNELS    = [int(c) for c in _env("SAT_CHANNELS", "").replace(" ", "").split(",")
               if c.strip().isdigit()]

intents = discord.Intents.none()
intents.guilds = True
bot = discord.Client(intents=intents)

# {channel_id: message_id} — we keep ONE board per channel and edit it each refresh
# instead of spamming new messages.
_boards: dict[int, int] = {}


# ── V Helper API ─────────────────────────────────────────────────────────────
async def _api_get_orders(session):
    """Fetch the open-order list from V Helper. Returns a list, or None on failure."""
    try:
        async with session.get(f"{API_BASE}/api/network/orders",
                               headers={"X-Network-Secret": SECRET},
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                log.warning("orders API returned %s", r.status)
                return None
            data = await r.json()
            return data.get("orders", []) if data.get("ok") else None
    except Exception as e:
        log.warning("orders API failed: %s", e)
        return None


async def _api_claim(session, order_id, worker_id, worker_name, guild_id):
    """Tell V Helper that `worker_id` claimed `order_id` from a partner server."""
    try:
        async with session.post(f"{API_BASE}/api/network/claim",
                                headers={"X-Network-Secret": SECRET,
                                         "Content-Type": "application/json"},
                                json={"order_id": order_id,
                                      "worker_id": str(worker_id),
                                      "worker_name": worker_name,
                                      "source_guild_id": str(guild_id)},
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json()
    except Exception as e:
        log.warning("claim API failed: %s", e)
        return {"ok": False, "error": "The order system is unreachable right now — try again shortly."}


# ── Claim dropdown (persistent) ──────────────────────────────────────────────
class ClaimSelect(discord.ui.Select):
    def __init__(self, options=None):
        super().__init__(placeholder="Claim an order…", min_values=1, max_values=1,
                         custom_id="sat_claim_select",
                         options=options or [discord.SelectOption(label="No open orders", value="0")])

    async def callback(self, interaction: discord.Interaction):
        try:
            order_id = int(self.values[0])
        except Exception:
            order_id = 0
        if order_id <= 0:
            return await interaction.response.send_message("Nothing to claim right now.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with aiohttp.ClientSession() as session:
            res = await _api_claim(session, order_id, interaction.user.id,
                                   interaction.user.display_name,
                                   interaction.guild_id or 0)
        if not res or not res.get("ok"):
            err = (res or {}).get("error", "That order is no longer available.")
            return await interaction.followup.send(f"❌ {err}", ephemeral=True)

        invite = res.get("home_invite") or HOME_INVITE
        msg = res.get("message", f"You claimed order #{order_id}.")

        # DM the claimer — this bot shares their server, so DMs are allowed.
        dm_ok = False
        try:
            dm = await interaction.user.create_dm()
            body = f"✅ {msg}"
            if invite:
                body += f"\n\nFinish it and open your ticket here: {invite}"
            await dm.send(body)
            dm_ok = True
        except Exception:
            pass

        if dm_ok:
            tail = "Check your DMs to finish."
        elif invite:
            tail = f"Join to finish: {invite}"
        else:
            tail = "A manager will reach out."
        await interaction.followup.send(f"✅ Claimed order #{order_id}. {tail}", ephemeral=True)


class ClaimView(discord.ui.View):
    def __init__(self, options=None):
        super().__init__(timeout=None)
        self.add_item(ClaimSelect(options))


def _build_board(orders):
    """Return (embed, view) for the order board from V Helper's order list."""
    embed = discord.Embed(title="🧰 Restock orders — workers wanted",
                          color=discord.Color.green())
    if not orders:
        embed.description = "No open orders right now — check back soon."
        return embed, ClaimView()

    lines, options = [], []
    for o in orders[:25]:                      # Discord allows max 25 select options
        oid = int(o.get("id", 0) or 0)
        item = str(o.get("item", "item"))
        qty = int(o.get("qty", 0) or 0)
        mkt = str(o.get("market", ""))
        pay = int(o.get("pay", 0) or 0)
        paytxt = f" — {pay:,}¢" if pay else ""
        lines.append(f"**#{oid}** {item} ×{qty} · {mkt}{paytxt}")
        options.append(discord.SelectOption(label=f"#{oid} {item} ×{qty}"[:100],
                                            value=str(oid),
                                            description=(f"{mkt}{paytxt}")[:100] or None))
    embed.description = "\n".join(lines)[:4000]
    embed.set_footer(text="Pick an order below to claim it — you'll be DM'd how to finish.")
    return embed, ClaimView(options)


# ── Refresh loop ─────────────────────────────────────────────────────────────
@tasks.loop(minutes=REFRESH_MIN)
async def refresh_boards():
    if not CHANNELS:
        return
    async with aiohttp.ClientSession() as session:
        orders = await _api_get_orders(session)
    if orders is None:
        return                                  # API down — leave the last board up
    embed, view = _build_board(orders)
    for cid in CHANNELS:
        ch = bot.get_channel(cid)
        if ch is None:
            continue
        try:
            mid = _boards.get(cid)
            if mid:
                try:
                    msg = await ch.fetch_message(mid)
                    await msg.edit(embed=embed, view=view)
                    continue
                except discord.NotFound:
                    _boards.pop(cid, None)
            sent = await ch.send(embed=embed, view=view)
            _boards[cid] = sent.id
        except Exception as e:
            log.warning("post to channel %s failed: %s", cid, e)


@refresh_boards.before_loop
async def _before_refresh():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    # Register the persistent view so the dropdown keeps working after a restart.
    try:
        bot.add_view(ClaimView())
    except Exception:
        pass
    log.info("Satellite online as %s — relaying to %d channel(s) every %d min.",
             bot.user, len(CHANNELS), REFRESH_MIN)
    if not refresh_boards.is_running():
        refresh_boards.start()


def main():
    if not TOKEN:
        raise SystemExit("SAT_BOT_TOKEN is not set — put it in .env")
    if not SECRET:
        raise SystemExit("NETWORK_SHARED_SECRET is not set — it must match V Helper's .env")
    if not CHANNELS:
        log.warning("SAT_CHANNELS is empty — the bot will idle until you add channel IDs.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
