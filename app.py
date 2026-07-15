#!/usr/bin/env python3
"""
RestockerLightweight — satellite order-relay bot.

A tiny bot you OWN and "trust" into partner Discord servers. Its ONLY job:

  1. Pull the current open restock orders from your main V Helper bot's web API.
  2. Post them as one board (with a working "Claim an order" dropdown) into each
     registered channel, refreshed on a timer.
  3. When someone claims, capture their Discord ID, tell V Helper, and DM them an
     invite to your home server to finish and open their ticket.

It carries NO market/DB/dashboard logic — everything authoritative lives in V Helper.
That's what keeps it lightweight and safe to place in servers you don't fully control.

Why this works when a mirrored post doesn't: a Discord component only routes to the bot
that POSTED it. Because THIS bot is present in each partner server, its dropdown really
works there, so a click hands you the clicker's real Discord ID instantly.

── Registering channels ────────────────────────────────────────────────────────
Two ways, and they stack:

  * /setup   — run it in the channel you want the board in (needs Manage Server).
               Saved to channels.json, no restart needed. This is the easy way.
  * SAT_CHANNELS — a comma-separated list in .env, always active. Good for seeding.

── Setup ───────────────────────────────────────────────────────────────────────
    pip install -r requirements.txt
    python app.py

Invite the bot with: Send Messages + Embed Links + Use Application Commands.
No privileged intents needed.
"""
import os
import json
import logging

import aiohttp
import discord
from discord import app_commands
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
ENV_CHANNELS = [int(c) for c in _env("SAT_CHANNELS", "").replace(" ", "").split(",")
                if c.strip().isdigit()]

CHANNELS_FILE = _env("SAT_CHANNELS_FILE", "channels.json")

intents = discord.Intents.none()
intents.guilds = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# {channel_id: message_id} — one board per channel, edited in place rather than spammed.
_boards: dict[int, int] = {}


# ── Registered channels (channels.json + SAT_CHANNELS) ───────────────────────
def _load_stored() -> list:
    try:
        with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
            return [int(x) for x in json.load(f)]
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("couldn't read %s: %s", CHANNELS_FILE, e)
        return []


def _save_stored(ids) -> bool:
    try:
        with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted({int(i) for i in ids}), f)
        return True
    except Exception as e:
        log.warning("couldn't write %s: %s", CHANNELS_FILE, e)
        return False


def _all_channels() -> list:
    """Every channel we post the board in: env-seeded + /setup-registered."""
    return sorted(set(ENV_CHANNELS) | set(_load_stored()))


# ── V Helper API ─────────────────────────────────────────────────────────────
async def _api_get_orders(session):
    """Fetch the open-order list from V Helper. Returns a list, or None on failure."""
    try:
        async with session.get(f"{API_BASE}/api/network/orders",
                               headers={"X-Network-Secret": SECRET},
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                log.warning("orders API returned %s (check NETWORK_SHARED_SECRET matches "
                            "V Helper's .env)", r.status)
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


async def _push_board(channel, embed, view):
    """Post or edit this channel's single board message."""
    cid = channel.id
    mid = _boards.get(cid)
    if mid:
        try:
            msg = await channel.fetch_message(mid)
            await msg.edit(embed=embed, view=view)
            return
        except discord.NotFound:
            _boards.pop(cid, None)
    sent = await channel.send(embed=embed, view=view)
    _boards[cid] = sent.id


# ── Slash commands ───────────────────────────────────────────────────────────
@tree.command(name="setup", description="Post the restock order board in this channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_cmd(interaction: discord.Interaction):
    cid = interaction.channel_id
    stored = _load_stored()
    if cid in stored or cid in ENV_CHANNELS:
        return await interaction.response.send_message(
            "✅ The order board is already set up in this channel.", ephemeral=True)
    stored.append(cid)
    if not _save_stored(stored):
        return await interaction.response.send_message(
            "⚠️ Couldn't save the channel — check the bot's file permissions.", ephemeral=True)

    await interaction.response.send_message(
        "✅ Order board set up here — posting it now. It refreshes automatically; "
        "run `/remove` to stop.", ephemeral=True)

    # Post immediately rather than waiting for the next refresh tick.
    try:
        async with aiohttp.ClientSession() as session:
            orders = await _api_get_orders(session)
        embed, view = _build_board(orders or [])
        await _push_board(interaction.channel, embed, view)
    except Exception as e:
        log.warning("initial board post failed in %s: %s", cid, e)


@tree.command(name="remove", description="Stop posting the restock order board in this channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def remove_cmd(interaction: discord.Interaction):
    cid = interaction.channel_id
    stored = _load_stored()
    if cid not in stored:
        if cid in ENV_CHANNELS:
            return await interaction.response.send_message(
                "⚠️ This channel is pinned in the bot's `SAT_CHANNELS` config — it has to be "
                "removed there by the bot owner.", ephemeral=True)
        return await interaction.response.send_message(
            "This channel isn't set up.", ephemeral=True)
    stored = [c for c in stored if c != cid]
    _save_stored(stored)
    _boards.pop(cid, None)
    await interaction.response.send_message(
        "✅ Removed — the board will stop updating here.", ephemeral=True)


@tree.command(name="boards", description="List every channel the order board is posted in")
@app_commands.checks.has_permissions(manage_guild=True)
async def boards_cmd(interaction: discord.Interaction):
    ids = _all_channels()
    if not ids:
        return await interaction.response.send_message("No channels set up yet.", ephemeral=True)
    lines = []
    for cid in ids:
        ch = bot.get_channel(cid)
        where = f"{ch.guild.name} · #{ch.name}" if ch and ch.guild else "unknown / no access"
        pin = " *(config)*" if cid in ENV_CHANNELS else ""
        lines.append(f"• `{cid}` — {where}{pin}")
    await interaction.response.send_message(
        f"**Order board is live in {len(ids)} channel(s):**\n" + "\n".join(lines)[:1800],
        ephemeral=True)


@setup_cmd.error
@remove_cmd.error
@boards_cmd.error
async def _perm_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "⛔ You need the **Manage Server** permission to do that."
    else:
        msg = f"⚠️ {type(error).__name__}: {error}"
        log.warning("command error: %s", error)
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ── Refresh loop ─────────────────────────────────────────────────────────────
@tasks.loop(minutes=REFRESH_MIN)
async def refresh_boards():
    channels = _all_channels()
    if not channels:
        return
    async with aiohttp.ClientSession() as session:
        orders = await _api_get_orders(session)
    if orders is None:
        return                                  # API down — leave the last board up
    embed, view = _build_board(orders)
    for cid in channels:
        ch = bot.get_channel(cid)
        if ch is None:
            continue                            # not in that server / channel gone
        try:
            await _push_board(ch, embed, view)
        except Exception as e:
            log.warning("post to channel %s failed: %s", cid, e)


@refresh_boards.before_loop
async def _before_refresh():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    # Persistent view so the dropdown keeps working after a restart.
    try:
        bot.add_view(ClaimView())
    except Exception:
        pass
    try:
        await tree.sync()
        log.info("slash commands synced (/setup, /remove, /boards)")
    except Exception as e:
        log.warning("command sync failed: %s", e)
    log.info("Satellite online as %s — %d guild(s), %d channel(s), refresh %d min.",
             bot.user, len(bot.guilds), len(_all_channels()), REFRESH_MIN)
    if not refresh_boards.is_running():
        refresh_boards.start()


def main():
    if not TOKEN:
        raise SystemExit("SAT_BOT_TOKEN is not set — put it in .env")
    if not SECRET:
        raise SystemExit("NETWORK_SHARED_SECRET is not set — it must match V Helper's .env")
    if not _all_channels():
        log.warning("No channels registered yet — run /setup in a channel once the bot is online.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
