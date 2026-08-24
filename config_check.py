"""
config_check.py — boot-time configuration self-check for any Abex Tech bot.

WHY THIS EXISTS
    The bank ships doctor.py, which does these checks well — but it runs as
    `python doctor.py`, and there is NO SHELL on the Wisp/Pterodactyl panel. So the
    checks could never run where they actually matter. This module does the same work
    from inside on_ready, using the connection the bot already has, and prints the
    result to the console you CAN read from the panel.

    It also exists because every one of these failures is otherwise SILENT: posting to
    a channel is fire-and-forget (best-effort, logged and swallowed), so a channel ID
    left pointing at a deleted server looks exactly like "nothing happened today".

WHAT IT DOES
    Read-only. Resolves every configured guild/channel id, checks the bot's permissions
    in each channel, and prints one block. It never writes to Discord, never raises into
    your startup path, and never moves anything.

USE
    import config_check
    # inside on_ready, after the bot is connected:
    await config_check.report(
        bot, title="Abex Bank",
        guilds={"BANK_GUILD_ID": GUILD_ID},
        channels={"NEW_ACCOUNT_CHANNEL_ID": NEW_ACCOUNT_CHANNEL_ID,
                  "LOAN_PROPOSALS_CHANNEL_ID": LOAN_PROPOSALS_CHANNEL_ID,
                  "BOT_LOG_CHANNEL_ID": BOT_LOG_CHANNEL_ID},
        notes=["Members intent: " + ("on" if bot.intents.members else "OFF")],
    )
"""
from __future__ import annotations

import logging

import discord

log = logging.getLogger("config_check")

OK, WARN, BAD = "✅", "⚠️ ", "❌"
# Permissions a channel we POST to must grant us.
_NEED = ("view_channel", "send_messages", "embed_links")


def _line(sym: str, msg: str) -> str:
    return f"  {sym} {msg}"


async def _check_channel(client: discord.Client, label: str, cid) -> tuple[str, str]:
    """Resolve one channel id and check we can actually post in it."""
    raw = str(cid or "").strip()
    if not raw or raw == "0":
        return WARN, f"{label} not set — those posts are disabled"
    try:
        ch = client.get_channel(int(raw)) or await client.fetch_channel(int(raw))
    except ValueError:
        return BAD, f"{label}={raw} is not a valid id"
    except discord.NotFound:
        return BAD, (f"{label}={raw} DOES NOT EXIST — deleted channel or deleted server. "
                     f"Re-point it at the new server.")
    except discord.Forbidden:
        return BAD, f"{label}={raw} exists but the bot cannot see it (missing access)"
    except Exception as e:
        return BAD, f"{label}={raw} unreachable: {type(e).__name__}"
    guild = getattr(ch, "guild", None)
    where = f"#{getattr(ch, 'name', '?')}" + (f" in '{guild.name}'" if guild else "")
    if guild is None:
        return OK, f"{label} → {where}"
    perms = ch.permissions_for(guild.me)
    # A category is a container, not somewhere we post — only visibility matters.
    need = ("view_channel",) if isinstance(ch, discord.CategoryChannel) else _NEED
    missing = [p for p in need if not getattr(perms, p, False)]
    if missing:
        return WARN, f"{label} → {where} — MISSING {', '.join(missing)}"
    return OK, f"{label} → {where}"


async def report(client: discord.Client, *, title: str = "config",
                 guilds: dict | None = None, channels: dict | None = None,
                 notes: list[str] | None = None) -> list[str]:
    """Print the config block. Returns the lines (also handy for a /diagnose command)."""
    out: list[str] = [f"── {title}: startup config check " + "─" * max(0, 30 - len(title))]
    try:
        gl = client.guilds
        out.append(_line(OK if gl else BAD,
                         f"in {len(gl)} server(s): " + (", ".join(f"'{g.name}'" for g in gl[:6])
                                                        or "NONE — the bot is not in any server")))
        for label, gid in (guilds or {}).items():
            raw = str(gid or "").strip()
            if not raw or raw == "0":
                out.append(_line(WARN, f"{label} not set"))
                continue
            try:
                g = client.get_guild(int(raw))
            except ValueError:
                out.append(_line(BAD, f"{label}={raw} is not a valid id"))
                continue
            out.append(_line(OK, f"{label} → '{g.name}'") if g else
                       _line(BAD, f"{label}={raw} — the bot is NOT in that server "
                                  f"(deleted, or never invited with applications.commands)"))
        for label, cid in (channels or {}).items():
            sym, msg = await _check_channel(client, label, cid)
            out.append(_line(sym, msg))
        for n in (notes or []):
            out.append(_line("·", n))
        bad_n = sum(1 for l in out if BAD in l)
        warn_n = sum(1 for l in out if WARN in l)
        out.append(f"── {'all good' if not (bad_n or warn_n) else f'{bad_n} broken, {warn_n} warning(s)'} "
                   + "─" * 24)
    except Exception as e:                       # never break startup over a health check
        out.append(_line(BAD, f"config check itself failed: {type(e).__name__}: {e}"))
    for l in out:
        log.info("%s", l)
        print(l, flush=True)                     # Wisp console shows print() reliably
    return out
