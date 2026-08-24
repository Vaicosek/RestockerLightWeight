#!/usr/bin/env python3
"""
Abex Stakes — land, auctions and predictions.

You stake a claim on land, stake a bid at auction, stake a prediction.

Named for what it is becoming, not only for what it does today. Today: it relays
open orders into partner servers and runs auctions. Coming: land moves here out of
core (unblocked now that ledger_v2's HTTP routes are mounted and LEDGER_TOKEN_ESTATES
can be scoped to this bot), and predictions are not built at all yet — `games` is a
reserved ledger identity with no command surface. Where the name and the code
disagree, the code is what runs.

A tiny bot you OWN and "trust" into partner Discord servers. What it does today:

  1. Pull the current open restock orders from your main Abex Tech bot's web API.
  2. Post them as one board (with a working "Claim an order" dropdown) into each
     registered channel, refreshed on a timer.
  3. When someone claims, capture their Discord ID, tell Abex Tech, and DM them an
     invite to your home server to finish and open their ticket.

It carries NO market/DB/dashboard logic — everything authoritative lives in Abex Tech.
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

import abex_embed as ab          # the house embed builder

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

# Which board(s) this deployment posts. ONE codebase, run as different branded bots:
#   SAT_FEATURES=orders   → the Abex Tech order-relay satellite (original behaviour, default)
#   SAT_FEATURES=land     → the "Abex Stakes" satellite (land board + bidding)
#   SAT_FEATURES=orders,land → both boards in every registered channel
FEATURES = {f for f in _env("SAT_FEATURES", "orders").lower().replace(" ", "").split(",") if f}
if not FEATURES:
    FEATURES = {"orders"}

CHANNELS_FILE = _env("SAT_CHANNELS_FILE", "channels.json")

intents = discord.Intents.none()
intents.guilds = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

import ticket_system as tix
tix.STAFF_ROLE_NAME = "🛡️ Staff"          # reuse this bot's existing Staff role for tickets
tix.SELF_ROLES = []                        # this bot already has its own notify panel — no duplicate
tix.install_commands(tree)                 # adds /ticket_panel and /close to this tree

import announce
announce.DEFAULT_CHANNEL = "announcements"  # this bot's setup already creates #announcements
announce.install_commands(tree)             # adds /announce

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


# ── Abex Tech API ─────────────────────────────────────────────────────────────

def _plain(name: str) -> str:
    """A role name with its emoji stripped, for echoing back in a reply.

    The roles themselves keep their emoji — they are looked up by exact name with
    `discord.utils.get(guild.roles, name=...)`, so renaming them would break the
    lookup. Only the echo is stripped.
    """
    return "".join(ch for ch in str(name or "")
                   if not (0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF
                           or ord(ch) == 0xFE0F)).strip()


async def _api_get_orders(session):
    """Fetch the open-order list from Abex Tech. Returns a list, or None on failure."""
    try:
        async with session.get(f"{API_BASE}/api/network/orders",
                               headers={"X-Network-Secret": SECRET},
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                log.warning("orders API returned %s (check NETWORK_SHARED_SECRET matches "
                            "Abex Tech's .env)", r.status)
                return None
            data = await r.json()
            return data.get("orders", []) if data.get("ok") else None
    except Exception as e:
        log.warning("orders API failed: %s", e)
        return None


async def _api_claim(session, order_id, worker_id, worker_name, guild_id):
    """Tell Abex Tech that `worker_id` claimed `order_id` from a partner server."""
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
            return await interaction.followup.send(err, ephemeral=True)

        invite = res.get("home_invite") or HOME_INVITE
        msg = res.get("message", f"You claimed order #{order_id}.")

        # DM the claimer — this bot shares their server, so DMs are allowed.
        dm_ok = False
        try:
            dm = await interaction.user.create_dm()
            body = msg
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
        await interaction.followup.send(f"Claimed order #{order_id}. {tail}", ephemeral=True)


class ClaimView(discord.ui.View):
    def __init__(self, options=None):
        super().__init__(timeout=None)
        self.add_item(ClaimSelect(options))



def _build_board(orders):
    """Return (embed, view) for the order board from Abex Tech's order list."""
    if not orders:
        # An empty board is empty: the title and one sentence, no placeholder rows.
        return ab.embed(title="Restock orders",
                        desc="No open orders right now.",
                        colour=ab.NEUTRAL), ClaimView()

    lines, options = [], []
    for o in orders[:25]:                      # Discord allows max 25 select options
        oid = int(o.get("id", 0) or 0)
        item = str(o.get("item", "item"))
        qty = int(o.get("qty", 0) or 0)
        mkt = str(o.get("market", ""))
        pay = int(o.get("pay", 0) or 0)
        # `pay` is the whole-order payment, not a unit price, so it is labelled
        # rather than given a per-piece / per-stack unit it does not have.
        paytxt = f"pay {ab.coins(pay)}" if pay else ""
        lines.append(ab.line(f"#{oid}", f"{item} x{qty:,}", mkt, paytxt))
        options.append(discord.SelectOption(label=f"#{oid} {item} x{qty}"[:100],
                                            value=str(oid),
                                            description=ab.line(mkt, paytxt)[:100] or None))
    return ab.embed(title="Restock orders",
                    desc="\n".join(lines)[:4000],
                    foot="Pick an order below to claim it.",
                    colour=ab.ACCENT), ClaimView(options)


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


# ══════════════════════════════════════════════════════════════════════════════
# LAND EXCHANGE ("Abex Stakes" satellite)
# Same thin-relay contract as orders: this bot renders a board and captures the
# clicker's real Discord ID; ALL money/valuation/escrow lives in Abex Tech and is
# reached over /api/network/land/*. Enabled when SAT_FEATURES includes "land".
# ══════════════════════════════════════════════════════════════════════════════
_land_boards: dict[int, int] = {}          # {channel_id: message_id}
_land_cache: dict[int, dict] = {}          # {listing_id: last-seen listing dict}


async def _api_get_land_listings(session):
    """Fetch active land listings from Abex Tech. Returns a list, or None on failure."""
    try:
        async with session.get(f"{API_BASE}/api/network/land/listings",
                               headers={"X-Network-Secret": SECRET},
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                log.warning("land listings API returned %s", r.status)
                return None
            data = await r.json()
            return data.get("listings", []) if data.get("ok") else None
    except Exception as e:
        log.warning("land listings API failed: %s", e)
        return None


async def _api_land_action(session, kind, listing_id, user, guild_id, amount=None):
    """POST a bid or buy to Abex Tech. `kind` is 'bid' or 'buy'. Returns the result dict."""
    payload = {"listing_id": int(listing_id),
               "bidder_id": str(user.id), "buyer_id": str(user.id),
               "bidder_name": user.display_name, "buyer_name": user.display_name,
               "source_guild_id": str(guild_id or 0)}
    if amount is not None:
        payload["amount"] = amount
    return await _api_land_post(session, kind, payload)


async def _api_land_post(session, path, payload):
    """Generic POST to /api/network/land/<path>. Returns the parsed result dict."""
    try:
        async with session.post(f"{API_BASE}/api/network/land/{path}",
                                headers={"X-Network-Secret": SECRET,
                                         "Content-Type": "application/json"},
                                json=payload,
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json()
    except Exception as e:
        log.warning("land %s API failed: %s", path, e)
        return {"ok": False, "error": "The land exchange is unreachable right now — try again shortly."}


async def _repost_land_boards():
    """Rebuild and push the land board to every registered channel — used right after a
    /sell or /cancel so the change shows immediately instead of waiting for the timer."""
    if "land" not in FEATURES:
        return
    channels = _all_channels()
    if not channels:
        return
    async with aiohttp.ClientSession() as session:
        listings = await _api_get_land_listings(session)
    if listings is None:
        return
    board = _build_land_board(listings)
    for cid in channels:
        ch = bot.get_channel(cid)
        if ch is None:
            continue
        try:
            await _push_land_board(ch, *board)
        except Exception as e:
            log.warning("board repush failed for %s: %s", cid, e)


# Optional opt-in ping roles per kind, set in this bot's .env (role IDs):
#   SAT_LAND_PING_ROLE=...  SAT_ITEM_PING_ROLE=...
_PING_ROLE = {"land": _env("SAT_LAND_PING_ROLE", ""), "item": _env("SAT_ITEM_PING_ROLE", "")}



async def _ping_new_listing(channel, kind, listing):
    rid = _PING_ROLE.get(kind, "")
    if not rid or channel is None:
        return
    try:
        what = "land" if kind == "land" else "item"
        await channel.send(
            ab.line(f"<@&{int(rid)}>", f"New {what} listing",
                    listing.get("title"), f"#{listing.get('id')}"),
            allowed_mentions=discord.AllowedMentions(roles=True))
    except Exception as e:
        log.warning("new-listing ping failed: %s", e)


def _fmt(n) -> str:
    try:
        return f"{float(n):,.0f}"
    except (TypeError, ValueError):
        return "0"



def _price_label(l) -> str:
    """The one figure a listing line leads with, always carrying its unit.

    An auction lot is priced as a whole lot, not per piece or per stack, so the
    label says which figure it is (`bid` / `reserve` / `buy now`) instead."""
    if l.get("mode") == "auction":
        cur = l.get("current_bid")
        return (f"bid {ab.coins(cur)}" if cur else f"reserve {ab.coins(l.get('reserve'))}")
    return f"buy now {ab.coins(l.get('buy_now'))}"


class BidModal(discord.ui.Modal, title="Place a bid"):

    def __init__(self, listing):
        super().__init__(timeout=300)
        self.listing = listing
        hint = listing.get("min_next_bid")
        self.amount = discord.ui.TextInput(
            label="Bid in coins",
            placeholder=(f"Minimum {ab.coins(hint)} — leave blank to bid the minimum"
                         if hint else "Amount in coins"),
            required=False, max_length=15)
        self.add_item(self.amount)


    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.amount.value or "").strip().replace(",", "")
        amount = None
        if raw:
            try:
                amount = float(raw)
            except ValueError:
                return await interaction.response.send_message(
                    "That is not a number — try again.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with aiohttp.ClientSession() as session:
            res = await _api_land_action(session, "bid", self.listing["id"], interaction.user,
                                         interaction.guild_id, amount)
        if not res or not res.get("ok"):
            return await interaction.followup.send((res or {}).get("error", "Bid failed."),
                                                   ephemeral=True)
        parts = [f"Bid placed: {ab.coins(res.get('amount'))} on listing #{self.listing['id']}"]
        ends = res.get("ends_at_epoch") or self.listing.get("ends_at_epoch")
        if ends:
            parts.append(f"closes <t:{int(ends)}:f>")
        if res.get("anti_snipe_extended"):
            parts.append("your bid extended the auction (anti-snipe)")
        parts.append("you are refunded automatically if someone outbids you")
        await interaction.followup.send(ab.line(*parts) + ".", ephemeral=True)


class ListingDetailView(discord.ui.View):
    """Ephemeral per-listing actions shown after someone picks a listing from the board."""
    def __init__(self, listing):
        super().__init__(timeout=300)
        self.listing = listing
        if listing.get("mode") == "auction":
            self.add_item(_BidButton(listing))
        if listing.get("buy_now"):
            self.add_item(_BuyButton(listing))


class _BidButton(discord.ui.Button):

    def __init__(self, listing):
        super().__init__(style=discord.ButtonStyle.primary, label="Place a bid")
        self.listing = listing

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BidModal(self.listing))


class _BuyButton(discord.ui.Button):

    def __init__(self, listing):
        # Grey, not green: green buttons are for the one main action, and buying
        # outright sits alongside bidding rather than above it.
        super().__init__(style=discord.ButtonStyle.secondary,
                         label=f"Buy now for {ab.coins(listing.get('buy_now'))}"[:80])
        self.listing = listing


    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with aiohttp.ClientSession() as session:
            res = await _api_land_action(session, "buy", self.listing["id"], interaction.user,
                                         interaction.guild_id)
        if not res or not res.get("ok"):
            return await interaction.followup.send((res or {}).get("error", "Purchase failed."),
                                                   ephemeral=True)
        await interaction.followup.send(
            f"Bought listing #{self.listing['id']} for {ab.coins(res.get('price'))} — "
            f"coins moved automatically via escrow. Check your DMs for handover details.",
            ephemeral=True)


class LandSelect(discord.ui.Select):
    def __init__(self, options=None):
        super().__init__(placeholder="View / bid on a listing…", min_values=1, max_values=1,
                         custom_id="sat_land_select",
                         options=options or [discord.SelectOption(label="No open listings", value="0")])

    async def callback(self, interaction: discord.Interaction):
        try:
            lid = int(self.values[0])
        except Exception:
            lid = 0
        listing = _land_cache.get(lid)
        if lid <= 0 or not listing:
            return await interaction.response.send_message(
                "That listing just closed or the board is mid-refresh — try again in a moment.",
                ephemeral=True)
        await interaction.response.send_message(
            embed=_build_listing_embed(listing), view=ListingDetailView(listing), ephemeral=True)


class LandView(discord.ui.View):
    def __init__(self, options=None):
        super().__init__(timeout=None)
        self.add_item(LandSelect(options))



def _kind_icon(l) -> str:
    """Retained so nothing that still calls it breaks; the house style has no
    emoji, so both this and `_kind_label` return the word."""
    return _kind_label(l)


def _kind_label(l) -> str:
    return "Land" if (l.get("kind") == "land") else "Item"



def _build_listing_embed(l) -> discord.Embed:
    kind = _kind_label(l)
    is_land = l.get("kind") == "land"

    # At most three inline tiles: four wrap to a second row on a phone.
    band = []
    if l.get("mode") == "auction":
        cur = l.get("current_bid")
        band.append(("Current bid", ab.coins(cur) if cur else "No bids yet"))
        band.append(("Reserve", ab.coins(l.get("reserve"))))
        if l.get("min_next_bid"):
            band.append(("Minimum next bid", ab.coins(l["min_next_bid"])))
        elif l.get("buy_now"):
            band.append(("Buy now", ab.coins(l["buy_now"])))
    elif l.get("buy_now"):
        band.append(("Buy now", ab.coins(l["buy_now"])))

    detail = []
    if l.get("category"):
        detail.append(("Category", l["category"]))
    if is_land and l.get("chunks"):
        detail.append(("Plot size", f"{_fmt(l['chunks'])} chunks"))
    if is_land and l.get("coords"):
        detail.append(("Coordinates", l["coords"]))
    if l.get("mode") == "auction" and l.get("buy_now") and l.get("min_next_bid"):
        detail.append(("Buy now", ab.coins(l["buy_now"])))
    if l.get("ends_at_epoch"):
        detail.append(("Closes", f"<t:{int(l['ends_at_epoch'])}:f> "
                                 f"(<t:{int(l['ends_at_epoch'])}:R>)"))

    # Land on Abex Stakes is sold outright — there is no rent and no lease, and
    # the copy must never imply one.
    foot = ("Sold outright, price is for the whole plot. Escrow settles on close."
            if is_land else
            "Price is for the whole lot. Escrow settles on close.")

    e = ab.embed(title=f"{l.get('title') or 'Listing'} #{l['id']}",
                 kicker=kind,
                 desc=(l.get("description") or "")[:1500],
                 band=band,
                 groups=[("Details", ab.rows(detail))] if detail else None,
                 foot=foot,
                 colour=ab.ACCENT)
    photos = l.get("photos") or ([l["image_url"]] if l.get("image_url") else [])
    if photos:
        e.set_image(url=str(photos[0]))
    return e



def _build_land_board(listings):
    """Return (embed, view) for the auction board and refresh the id->listing cache."""
    _land_cache.clear()
    if not listings:
        return ab.embed(title="Open listings",
                        desc="No open listings right now.",
                        colour=ab.NEUTRAL), LandView()
    lines, options = [], []
    for l in listings[:25]:
        lid = int(l.get("id", 0) or 0)
        _land_cache[lid] = l
        kind = _kind_label(l)
        extra = (f"{_fmt(l.get('chunks'))} chunks"
                 if (l.get("kind") == "land" and l.get("chunks")) else "")
        ends = f"closes <t:{int(l['ends_at_epoch'])}:R>" if l.get("ends_at_epoch") else ""
        lines.append(ab.line(f"#{lid}", l.get("title", "Listing"), kind,
                             _price_label(l), extra, ends))
        options.append(discord.SelectOption(
            label=f"#{lid} {l.get('title','Listing')}"[:100],
            value=str(lid),
            description=ab.line(kind, _price_label(l), extra)[:100]))
    return ab.embed(title="Open listings",
                    desc="\n".join(lines)[:4000],
                    foot="Pick a listing below to view it, bid, or buy. Coins are held in escrow.",
                    colour=ab.ACCENT), LandView(options)


async def _push_land_board(channel, embed, view):
    cid = channel.id
    mid = _land_boards.get(cid)
    if mid:
        try:
            msg = await channel.fetch_message(mid)
            await msg.edit(embed=embed, view=view)
            return
        except discord.NotFound:
            _land_boards.pop(cid, None)
    sent = await channel.send(embed=embed, view=view)
    _land_boards[cid] = sent.id


# ── Slash commands ───────────────────────────────────────────────────────────
@tree.command(name="setup", description="Post the restock order board in this channel")
@app_commands.checks.has_permissions(manage_guild=True)

async def setup_cmd(interaction: discord.Interaction):
    cid = interaction.channel_id
    stored = _load_stored()
    if cid in stored or cid in ENV_CHANNELS:
        return await interaction.response.send_message(
            "The board is already set up in this channel.", ephemeral=True)
    stored.append(cid)
    if not _save_stored(stored):
        return await interaction.response.send_message(
            "Could not save the channel — check the bot's file permissions.", ephemeral=True)

    what = " and ".join(sorted(FEATURES))
    await interaction.response.send_message(
        f"Board set up here ({what}) — posting it now. It refreshes automatically; "
        "run /remove to stop.", ephemeral=True)

    # Post immediately rather than waiting for the next refresh tick.
    try:
        async with aiohttp.ClientSession() as session:
            if "orders" in FEATURES:
                orders = await _api_get_orders(session)
                embed, view = _build_board(orders or [])
                await _push_board(interaction.channel, embed, view)
            if "land" in FEATURES:
                listings = await _api_get_land_listings(session)
                embed, view = _build_land_board(listings or [])
                await _push_land_board(interaction.channel, embed, view)
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
                "This channel is pinned in the bot's SAT_CHANNELS config — it has to be "
                "removed there by the bot owner.", ephemeral=True)
        return await interaction.response.send_message(
            "This channel isn't set up.", ephemeral=True)
    stored = [c for c in stored if c != cid]
    _save_stored(stored)
    _boards.pop(cid, None)
    _land_boards.pop(cid, None)
    await interaction.response.send_message(
        "Removed — the board will stop updating here.", ephemeral=True)


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
        pin = "pinned in config" if cid in ENV_CHANNELS else ""
        lines.append(ab.line(str(cid), where, pin))
    await interaction.response.send_message(
        f"The board is live in {len(ids)} channel(s):\n" + "\n".join(lines)[:1800],
        ephemeral=True)


@setup_cmd.error
@remove_cmd.error
@boards_cmd.error

async def _perm_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Managers only."
    else:
        msg = f"That did not go through: {type(error).__name__}."
        log.warning("command error: %s", error)
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ── Auction commands (Abex Stakes) — thin relays to Abex Tech ────────────────
# These live on THIS branded bot, not on Abex Tech core. Every command POSTs to the
# network API; all money/valuation/escrow happens in the brain.
_CATEGORIES = [app_commands.Choice(name=n, value=n)
               for n in ("Land", "Artificial Land", "Weapons", "Artifacts", "Other")]


@tree.command(name="sell",
              description="List anything for auction — name it, set a price, drag in photos")
@app_commands.describe(
    title="What you're selling (item or land name)",
    starting_price="Opening bid",
    buy_now="(Optional) Buy-It-Now price for an instant sale",
    photo="(Optional) drag a photo straight in",
    photo2="(Optional) a second photo",
    photo3="(Optional) a third photo",
    details="(Optional) description / condition / what's included",
    category="Pick a category — Land & Artificial Land list as land, the rest as items",
    chunks="(Land only) plot size in chunks — turns on AI valuation + company backing",
    backs_company="(Land only) market id a plot will back (65% rule) once sold",
    duration_days="(Optional) auction length in days — default from config",
)
@app_commands.choices(category=_CATEGORIES)

async def sell_cmd(interaction: discord.Interaction, title: str, starting_price: float,
                   buy_now: float = None,
                   photo: discord.Attachment = None,
                   photo2: discord.Attachment = None,
                   photo3: discord.Attachment = None,
                   details: str = None,
                   category: app_commands.Choice[str] = None,
                   chunks: float = None, backs_company: str = None,
                   duration_days: int = None):
    if "land" not in FEATURES:
        return await interaction.response.send_message(
            "This bot isn't running the auction feature.", ephemeral=True)
    atts = [a for a in (photo, photo2, photo3) if a is not None]
    for a in atts:
        if a.content_type and not a.content_type.startswith("image/"):
            return await interaction.response.send_message(
                f"{a.filename} isn't an image.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    payload = {
        "seller_id": str(interaction.user.id), "source_guild_id": str(interaction.guild_id or 0),
        "title": title, "starting_price": starting_price, "buy_now": buy_now,
        "details": details, "category": (category.value if category else None),
        "chunks": chunks, "backs_company": backs_company, "duration_days": duration_days,
        "photos": [a.url for a in atts],
    }
    async with aiohttp.ClientSession() as session:
        res = await _api_land_post(session, "create", payload)
    if not res or not res.get("ok"):
        return await interaction.followup.send(
            (res or {}).get("error", "Could not create the listing."), ephemeral=True)
    listing = res.get("listing", {})
    await _repost_land_boards()
    await _ping_new_listing(interaction.channel, res.get("kind", "item"), listing)
    tail = f"\n{res['ai_note']}" if res.get("ai_note") else ""
    await interaction.followup.send(
        f"Listed {listing.get('title')} (#{listing.get('id')}) — it is on the board now.{tail}",
        ephemeral=True)


@tree.command(name="cancel", description="Cancel your own listing (only if no bid has been placed)")
@app_commands.describe(listing_id="The # of your listing")

async def cancel_cmd(interaction: discord.Interaction, listing_id: int):
    if "land" not in FEATURES:
        return await interaction.response.send_message("This bot isn't running the auction feature.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    is_mgr = bool(getattr(getattr(interaction.user, "guild_permissions", None), "manage_guild", False))
    payload = {"listing_id": listing_id, "requester_id": str(interaction.user.id),
               "bidder_id": str(interaction.user.id), "is_manager": is_mgr,
               "source_guild_id": str(interaction.guild_id or 0)}
    async with aiohttp.ClientSession() as session:
        res = await _api_land_post(session, "cancel", payload)
    if not res or not res.get("ok"):
        return await interaction.followup.send((res or {}).get("error", "Could not cancel."),
                                               ephemeral=True)
    await _repost_land_boards()
    await interaction.followup.send(f"Cancelled listing #{listing_id}.", ephemeral=True)


@tree.command(name="auction_close", description="(Managers) Force-settle or refund a listing")
@app_commands.describe(listing_id="Listing to close",
                       refund_bidder="Cancel and refund the standing bid instead of selling")
@app_commands.checks.has_permissions(manage_guild=True)

async def auction_close_cmd(interaction: discord.Interaction, listing_id: int, refund_bidder: bool = False):
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with aiohttp.ClientSession() as session:
        res = await _api_land_post(session, "close", {"listing_id": listing_id, "refund_bidder": refund_bidder})
    if not res or not res.get("ok"):
        return await interaction.followup.send((res or {}).get("error", "Could not close."),
                                               ephemeral=True)
    await _repost_land_boards()
    # A refunded close is a contested outcome — open a private staff ticket to track it.
    if refund_bidder and interaction.guild:
        try:
            await tix.open_ticket(
                interaction.guild, interaction.user,
                subject=f"Auction dispute — listing #{listing_id}",
                body=(f"Listing #{listing_id} was closed with a refund by "
                      f"{interaction.user.mention} — {res.get('outcome')}. "
                      f"Track the refund and resolve any dispute here."),
                ping_staff=True,
                dedup_key=f"auctiondispute:{listing_id}",
            )
        except Exception as e:
            log.warning("dispute ticket for #%s failed: %s", listing_id, e)
    await interaction.followup.send(
        ab.line(f"Closed listing #{listing_id}", res.get("outcome")) + ".", ephemeral=True)


@tree.command(name="auction_config",
              description="(Managers) View or set commission %, fees and auction defaults")
@app_commands.describe(
    commission_pct="House commission % on every sale",
    listing_fee="Flat coin fee to create a listing",
    min_increment_pct="Default minimum bid raise %",
    anti_snipe_minutes="A bid within this many minutes of the end extends it",
    default_auction_days="Default auction length in days",
)
@app_commands.checks.has_permissions(manage_guild=True)

async def auction_config_cmd(interaction: discord.Interaction,
                             commission_pct: float = None, listing_fee: float = None,
                             min_increment_pct: float = None, anti_snipe_minutes: float = None,
                             default_auction_days: float = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    updates = {k: v for k, v in (
        ("commission_pct", commission_pct), ("listing_fee", listing_fee),
        ("min_increment_pct", min_increment_pct), ("anti_snipe_minutes", anti_snipe_minutes),
        ("default_auction_days", default_auction_days)) if v is not None}
    body = {"updates": updates} if updates else {}
    async with aiohttp.ClientSession() as session:
        res = await _api_land_post(session, "config", body)
    if not res or not res.get("ok"):
        return await interaction.followup.send((res or {}).get("error", "Config unavailable."),
                                               ephemeral=True)
    cfg = res.get("config", {})
    await interaction.followup.send(
        embed=ab.embed(title="Auction settings",
                       kicker="/auction_config",
                       # `ab.rows([])` returns an em-dash, and `ab.embed` only drops
                       # a group whose value is falsy — so an empty config rendered a
                       # field containing one dash. Nothing configured means no field.
                       groups=([("Settings", ab.rows([(k, cfg[k]) for k in cfg]))]
                               if cfg else []),
                       desc=("" if cfg else "Nothing is configured for this server yet."),
                       colour=ab.NEUTRAL),
        ephemeral=True)


# ── Opt-in notify panel: members self-assign a ping role with a button. Role IDs come
#    from this bot's .env (SAT_LAND_PING_ROLE / SAT_ITEM_PING_ROLE); /sell mentions them. ──
class NotifyToggleButton(discord.ui.Button):

    def __init__(self, kind, label):
        super().__init__(style=discord.ButtonStyle.secondary, label=label, custom_id=f"sat_notify_{kind}")
        self.kind = kind


    async def callback(self, interaction: discord.Interaction):
        rid = _PING_ROLE.get(self.kind, "")
        role = interaction.guild.get_role(int(rid)) if (rid and interaction.guild and str(rid).isdigit()) else None
        if not role:
            return await interaction.response.send_message(
                "That notify role isn't set up here — ask a manager.", ephemeral=True)
        member = interaction.user
        what = "land" if self.kind == "land" else "item"
        try:
            if role in getattr(member, "roles", []):
                await member.remove_roles(role, reason="auction: notify opt-out")
                await interaction.response.send_message(
                    f"Removed {role.name} — you won't be pinged for new {what} listings.",
                    ephemeral=True)
            else:
                await member.add_roles(role, reason="auction: notify opt-in")
                await interaction.response.send_message(
                    f"You'll be pinged for new {what} listings via {role.name}. "
                    "Click again to opt out.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't manage that role — a manager needs to move my role above it.",
                ephemeral=True)


class NotifyPanelView(discord.ui.View):

    def __init__(self, only_configured=False):
        super().__init__(timeout=None)
        # The custom_id (sat_notify_land / sat_notify_item) is what routes a click
        # after a restart, so relabelling these buttons is safe for panels that
        # are already posted.
        for kind, label in (("land", "Notify me about land"),
                            ("item", "Notify me about items")):
            if only_configured and not _PING_ROLE.get(kind):
                continue
            self.add_item(NotifyToggleButton(kind, label))


@tree.command(name="auction_notifypanel",
              description="(Managers) Post the opt-in 'notify me on new listings' button panel")
@app_commands.checks.has_permissions(manage_guild=True)

async def auction_notifypanel_cmd(interaction: discord.Interaction):
    if not any(_PING_ROLE.get(k) for k in ("land", "item")):
        return await interaction.response.send_message(
            "Set SAT_LAND_PING_ROLE and/or SAT_ITEM_PING_ROLE (a role ID) in this bot's "
            ".env first.", ephemeral=True)
    embed = ab.embed(
        title="New listing alerts",
        desc="Pick a button to give yourself a ping role. You are mentioned when a new "
             "listing of that type goes up; click again any time to opt out.",
        colour=ab.NEUTRAL)
    await interaction.channel.send(embed=embed, view=NotifyPanelView(only_configured=True))
    await interaction.response.send_message("Notify panel posted.", ephemeral=True)


# ── /setup_server: one-shot scaffold of the whole auction-house server (roles + a full
#    category/channel layout) via the API. Idempotent — re-running skips what exists. ─────
_SERVER_ROLES = [
    # (name, colour, mentionable, is_staff)
    ("🛡️ Staff", 0xE67E22, False, True),
    ("🏡 Land Pings", 0x2ECC71, True, False),
    ("📦 Item Pings", 0x3498DB, True, False),
]
_SERVER_LAYOUT = [
    ("📋 Information", ["welcome", "rules", "announcements", "how-it-works"], True),
    ("🔨 Auction House", ["auction-board", "list-your-item", "get-notified"], False),
    ("🤝 Transfers", ["deals"], False),
    ("🎫 Support", ["open-a-ticket"], False),
    ("💬 Community", ["off-topic"], False),
]
# channels members can read but not post in (the bot still can)
_READONLY_CHANNELS = {"welcome", "rules", "announcements", "how-it-works", "auction-board",
                      "get-notified", "open-a-ticket"}


@tree.command(name="setup_server",
              description="(Managers) Build the full auction-house layout — all roles & channels")
@app_commands.checks.has_permissions(manage_guild=True)

async def setup_server_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    me = guild.me if guild else None
    if guild is None or me is None:
        return await interaction.followup.send("Run this in the server.", ephemeral=True)
    perms = me.guild_permissions
    if not (perms.manage_channels and perms.manage_roles):
        return await interaction.followup.send(
            "I need Manage Channels and Manage Roles. Re-invite me with Administrator "
            "(or those two permissions) and run this again.", ephemeral=True)

    created = {"roles": [], "categories": [], "channels": []}
    roles = {}
    try:
        for name, colour, mentionable, is_staff in _SERVER_ROLES:
            existing = discord.utils.get(guild.roles, name=name)
            if existing:
                roles[name] = existing
                continue
            p = discord.Permissions(manage_guild=True, manage_channels=True, manage_roles=True,
                                    manage_messages=True, kick_members=True, ban_members=True,
                                    mention_everyone=True) if is_staff else discord.Permissions.none()
            r = await guild.create_role(name=name, colour=discord.Colour(colour),
                                        permissions=p, mentionable=mentionable,
                                        reason="Abex Stakes setup")
            roles[name] = r
            created["roles"].append(name)

        for cat_name, chans, _info in _SERVER_LAYOUT:
            cat = discord.utils.get(guild.categories, name=cat_name)
            if cat is None:
                cat = await guild.create_category(cat_name, reason="Abex Stakes setup")
                created["categories"].append(cat_name)
            for ch in chans:
                if discord.utils.get(guild.text_channels, name=ch):
                    continue
                overwrites = None
                if ch in _READONLY_CHANNELS:
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=False),
                        me: discord.PermissionOverwrite(send_messages=True, embed_links=True),
                    }
                await guild.create_text_channel(ch, category=cat, overwrites=overwrites, reason="Abex Stakes setup")
                created["channels"].append(ch)
    except discord.Forbidden:
        return await interaction.followup.send(
            "Ran out of permission partway — make sure my role is near the top and I have "
            "Administrator.", ephemeral=True)
    except Exception as e:
        log.warning("setup_server failed: %s", e)
        return await interaction.followup.send(
            f"Setup hit an error: {type(e).__name__}.", ephemeral=True)

    # Post the ticket panel in #open-a-ticket (once — skip if we've already posted there).
    try:
        tix_ch = discord.utils.get(guild.text_channels, name="open-a-ticket")
        if tix_ch:
            already = False
            async for m in tix_ch.history(limit=5):
                if m.author.id == me.id:
                    already = True
                    break
            if not already:
                await tix.post_ticket_panel(tix_ch)
                created["channels"].append("open-a-ticket (panel posted)")
    except Exception as e:
        log.warning("ticket panel post failed: %s", e)

    land_id = roles.get("🏡 Land Pings")
    item_id = roles.get("📦 Item Pings")
    msg = ("Auction house server built.\n"
           f"Roles created: {', '.join(_plain(r) for r in created['roles']) or 'all existed already'}\n"
           f"Categories: {len(created['categories'])} · Channels: {len(created['channels'])}\n\n"
           "Put these in this bot's .env, then restart me:\n"
           f"```\nSAT_LAND_PING_ROLE={land_id.id if land_id else ''}\n"
           f"SAT_ITEM_PING_ROLE={item_id.id if item_id else ''}\n```\n"
           "Then run /setup in #auction-board to post the live board, and "
           "/auction_notifypanel in #get-notified for the opt-in ping buttons.")
    await interaction.followup.send(msg[:1900], ephemeral=True)


@sell_cmd.error
@cancel_cmd.error
@auction_close_cmd.error
@auction_config_cmd.error
@auction_notifypanel_cmd.error
@setup_server_cmd.error

async def _auction_err(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Managers only."
    else:
        msg = f"That did not go through: {type(error).__name__}."
        log.warning("auction command error: %s", error)
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
    order_board = land_board = None
    async with aiohttp.ClientSession() as session:
        if "orders" in FEATURES:
            orders = await _api_get_orders(session)
            if orders is not None:               # None = API down; leave the last board up
                order_board = _build_board(orders)
        if "land" in FEATURES:
            listings = await _api_get_land_listings(session)
            if listings is not None:
                land_board = _build_land_board(listings)
    for cid in channels:
        ch = bot.get_channel(cid)
        if ch is None:
            continue                            # not in that server / channel gone
        try:
            if order_board is not None:
                await _push_board(ch, *order_board)
            if land_board is not None:
                await _push_land_board(ch, *land_board)
        except Exception as e:
            log.warning("post to channel %s failed: %s", cid, e)


@refresh_boards.before_loop
async def _before_refresh():
    await bot.wait_until_ready()


async def _sync_guild(guild) -> bool:
    """Copy the global commands into one guild and sync them there. Guild-scoped syncs
    show up INSTANTLY, whereas a pure global sync can take up to an hour to propagate —
    which is why we do both."""
    try:
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        return True
    except Exception as e:
        log.warning("guild sync failed for %s (%s): %s", getattr(guild, "name", "?"),
                    getattr(guild, "id", "?"), e)
        return False


@bot.event
async def on_ready():
    # Persistent views so the dropdowns keep working after a restart — only register
    # the ones this deployment actually posts (per SAT_FEATURES).
    try:
        if "orders" in FEATURES:
            bot.add_view(ClaimView())
        if "land" in FEATURES:
            bot.add_view(LandView())
            bot.add_view(NotifyPanelView())   # both buttons, for restart-safe routing
        tix.bind_views(bot)                    # re-arm ticket panel/control buttons
    except Exception:
        pass

    # Global sync (slow to appear, but covers servers we're added to later)…
    try:
        await tree.sync()
    except Exception as e:
        log.warning("global command sync failed: %s", e)
    # …plus an instant per-guild sync for every server we're already in.
    synced = 0
    for g in bot.guilds:
        if await _sync_guild(g):
            synced += 1
    log.info("slash commands synced (/setup, /remove, /boards) — instantly in %d/%d guild(s)",
             synced, len(bot.guilds))

    log.info("Abex Stakes online as %s [features: %s] — %d guild(s), %d channel(s), refresh %d min.",
             bot.user, ",".join(sorted(FEATURES)), len(bot.guilds), len(_all_channels()), REFRESH_MIN)
    try:
        import config_check
        await config_check.report(
            bot, title="Abex Stakes",
            channels={f"board[{i}]": c for i, c in enumerate(_all_channels()[:8])} or None,
            notes=[f"features: {','.join(sorted(FEATURES))}",
                   f"registered boards: {len(_all_channels())}"])
    except Exception as _cce:
        log.warning("config check skipped: %s", _cce)
    if not refresh_boards.is_running():
        refresh_boards.start()


@bot.event
async def on_guild_join(guild):
    """A new partner added the bot — push commands to them immediately so they can
    run /setup right away instead of waiting on global propagation."""
    if await _sync_guild(guild):
        log.info("joined %s (%s) — commands synced", guild.name, guild.id)


def main():
    if not TOKEN:
        raise SystemExit("SAT_BOT_TOKEN is not set — put it in .env")
    if not SECRET:
        raise SystemExit("NETWORK_SHARED_SECRET is not set — it must match Abex Tech's .env")
    if not _all_channels():
        log.warning("No channels registered yet — run /setup in a channel once the bot is online.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
