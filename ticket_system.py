"""
ticket_system.py — PERMANENT runtime module for the Abex Tech bots (discord.py 2.x).

This is the half of the setup that STAYS. It owns every persistent interactive panel:
  • the ticket system  (panel → private channel → claim → close → transcript)
  • the self-roles panel (members toggle notification roles)
  • open_ticket()       (raise a staff ticket from bot code — the "ticket on an action" hook)

WHY IT'S SEPARATE FROM THE SETUP FILE
    Discord buttons only keep working after a restart if the bot re-registers their View
    (with the same custom_ids) on every startup — that's what bind_views() does. So the
    button CODE must live in a module the bot always imports. The `*_server_setup.py`
    file, by contrast, only *builds* the server once and can be deleted afterwards; the
    panels it posts keep working because THIS module re-arms them.

    setup file  → one-time: creates roles/channels, posts the panels, then delete it.
    ticket_system.py → permanent: keeps the posted panels alive forever.

WIRE IT IN  (do this once, permanently, in each bot)
    import ticket_system as tix
    tix.STAFF_ROLE_NAME = "Lead Banker"        # or "Estates Staff"
    tix.SELF_ROLES = [("Announcements", "📢 Announcements", "📢"), ...]   # per bot
    tix.install_commands(tree)                 # /ticket_panel, /close, /roles_panel
    # in on_ready:
    tix.bind_views(client)                     # re-arm ticket + self-role buttons
    await tree.sync()

OPEN A TICKET FROM CODE
    await tix.open_ticket(guild, member, subject="Loan #42",
                          body="...", ping_staff=True, dedup_key="loan:42")

REQUIRED PERMISSIONS
    Manage Channels, Manage Roles, Read Message History, Send Messages/Embeds.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands

log = logging.getLogger("ticket_system")

# ── config (override before install) ─────────────────────────────────────────
STAFF_ROLE_NAME = "Lead Banker"        # role that can see/claim/close every ticket
TICKET_CATEGORY = "🎫 Tickets"         # category new ticket channels are created under
LOG_CHANNEL = "ticket-logs"            # transcripts are posted here (staff-only)
PANEL_TITLE = "Open a ticket"
PANEL_BODY = "Need help, or want to reach staff privately? Click below and a private channel opens just for you."
ACCENT = 0x22FF7A

# Self-roles: (button label, role name, emoji). Override per bot before install.
SELF_ROLES: list[tuple[str, str, str]] = [
    ("Announcements", "📢 Announcements", "📢"),
]
ROLES_PANEL_TITLE = "Notifications"
ROLES_PANEL_BODY = "Click a button to opt in or out. Toggle any time."

# Only ever ping the roles/users we name in a message — never @everyone from arbitrary text.
_SILENT = discord.AllowedMentions(everyone=False, roles=True, users=True)


def _staff_role(guild: discord.Guild) -> discord.Role | None:
    return discord.utils.get(guild.roles, name=STAFF_ROLE_NAME)


# ── tickets ──────────────────────────────────────────────────────────────────
async def _ensure_infra(guild: discord.Guild) -> tuple[discord.CategoryChannel, discord.TextChannel | None]:
    """Make sure the Tickets category and the staff-only log channel exist."""
    cat = discord.utils.get(guild.categories, name=TICKET_CATEGORY)
    if cat is None:
        cat = await guild.create_category(TICKET_CATEGORY, reason="ticket system")
    logch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL)
    if logch is None:
        staff = _staff_role(guild)
        ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
              guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
        if staff:
            ow[staff] = discord.PermissionOverwrite(view_channel=True)
        logch = await guild.create_text_channel(LOG_CHANNEL, category=cat, overwrites=ow,
                                                reason="ticket system log")
    return cat, logch


def _find_ticket(guild: discord.Guild, dedup_key: str) -> discord.TextChannel | None:
    """An open ticket is a channel whose topic carries our dedup marker."""
    marker = f"[tkey:{dedup_key}]"
    for c in guild.text_channels:
        if c.topic and marker in c.topic:
            return c
    return None


async def open_ticket(guild: discord.Guild, opener: discord.abc.User, *,
                      subject: str, body: str = "", ping_staff: bool = True,
                      dedup_key: str | None = None) -> discord.TextChannel:
    """Create (or reuse) a private ticket channel. Returns the channel."""
    if dedup_key:
        existing = _find_ticket(guild, dedup_key)
        if existing:
            return existing

    cat, _log = await _ensure_infra(guild)
    staff = _staff_role(guild)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              manage_channels=True, read_message_history=True),
    }
    # Prefer the object we were handed if it's already a Member (interaction.user in a
    # guild is), so this works even without the members intent / an unpopulated cache.
    member = opener if isinstance(opener, discord.Member) else guild.get_member(opener.id)
    if member:
        overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                         read_message_history=True)
    if staff:
        overwrites[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                        read_message_history=True)

    base = "".join(ch for ch in opener.display_name.lower() if ch.isalnum())[:16] or "user"
    topic = f"Ticket for {opener} · {subject}"
    if dedup_key:
        topic += f" [tkey:{dedup_key}]"
    channel = await guild.create_text_channel(f"ticket-{base}", category=cat, topic=topic[:1024],
                                              overwrites=overwrites, reason=f"ticket: {subject}")

    embed = discord.Embed(title=subject, description=body or "A staff member will be with you shortly.",
                          colour=ACCENT, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Opened by {opener.display_name}")
    mention = (staff.mention + " ") if (staff and ping_staff) else ""
    opener_mention = member.mention if member else str(opener)
    await channel.send(f"{mention}{opener_mention}", embed=embed, view=TicketControlView(),
                       allowed_mentions=_SILENT)
    log.info("ticket opened: %s in %s", channel.name, guild.name)
    return channel


async def _write_transcript(channel: discord.TextChannel) -> discord.File:
    lines = [f"# Transcript — {channel.name}",
             f"# {channel.topic or ''}",
             f"# closed {datetime.now(timezone.utc).isoformat()}", ""]
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M")
        content = m.content or ""
        for e in m.embeds:
            content += f" [embed: {e.title or ''} — {(e.description or '')[:200]}]"
        for a in m.attachments:
            content += f" [file: {a.filename} {a.url}]"
        lines.append(f"[{ts}] {m.author.display_name}: {content}")
    data = "\n".join(lines).encode("utf-8")
    return discord.File(io.BytesIO(data), filename=f"{channel.name}.txt")


def _opener_ids(channel: discord.TextChannel) -> set[int]:
    """Members (non-staff, non-bot) explicitly allowed on the channel = the opener(s)."""
    ids = set()
    staff = _staff_role(channel.guild)
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Member) and not target.bot and target.id != (
                staff.id if staff else 0):
            if ow.view_channel:
                ids.add(target.id)
    return ids


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open a ticket", emoji="🎫",
                       style=discord.ButtonStyle.success, custom_id="tix:open")
    async def open(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            ch = await open_ticket(interaction.guild, interaction.user,
                                   subject="Support ticket",
                                   body=f"{interaction.user.mention} opened a ticket.",
                                   dedup_key=f"support:{interaction.user.id}")
            await interaction.followup.send(f"Your ticket: {ch.mention}", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't create channels here — I need Manage Channels.", ephemeral=True)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="✋", style=discord.ButtonStyle.primary,
                       custom_id="tix:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        staff = _staff_role(interaction.guild)
        if staff and staff not in getattr(interaction.user, "roles", []):
            await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
            return
        button.disabled = True
        button.label = f"Claimed by {interaction.user.display_name}"
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(f"✋ Claimed by {interaction.user.mention}.",
                                       allowed_mentions=_SILENT)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger,
                       custom_id="tix:close")
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await _do_close(interaction)


async def _do_close(interaction: discord.Interaction) -> None:
    """Shared close path used by both the Close button and the /close command."""
    staff = _staff_role(interaction.guild)
    is_staff = staff and staff in getattr(interaction.user, "roles", [])
    # opener may close their own; otherwise staff only
    if not is_staff and interaction.user.id not in _opener_ids(interaction.channel):
        await interaction.response.send_message("Only staff or the opener can close this.", ephemeral=True)
        return
    await interaction.response.send_message("Closing — writing the transcript…", ephemeral=True)
    try:
        transcript = await _write_transcript(interaction.channel)
        _cat, logch = await _ensure_infra(interaction.guild)
        if logch:
            await logch.send(f"Ticket **{interaction.channel.name}** closed by "
                             f"{interaction.user.mention}.", file=transcript,
                             allowed_mentions=_SILENT)
        # DM the opener a copy
        for uid in _opener_ids(interaction.channel):
            member = interaction.guild.get_member(uid)
            if member:
                try:
                    await member.send(f"Your ticket **{interaction.channel.name}** was closed.",
                                      file=await _write_transcript(interaction.channel))
                except discord.HTTPException:
                    pass
    finally:
        await interaction.channel.delete(reason="ticket closed")


# ── self-roles ───────────────────────────────────────────────────────────────
class SelfRoleButton(discord.ui.Button):
    def __init__(self, label: str, role_name: str, emoji: str):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
                         custom_id=f"vtselfrole:{role_name}")
        self.role_name = role_name

    async def callback(self, interaction: discord.Interaction):
        role = discord.utils.get(interaction.guild.roles, name=self.role_name)
        if role is None:
            await interaction.response.send_message(
                "That role doesn't exist yet — an admin can run /setup_server.", ephemeral=True)
            return
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="self-role opt-out")
                await interaction.response.send_message(f"Removed **{role.name}**.", ephemeral=True)
            else:
                await member.add_roles(role, reason="self-role opt-in")
                await interaction.response.send_message(f"Added **{role.name}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't manage that role — my role needs to be above it.", ephemeral=True)


class SelfRolesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for label, rname, emoji in SELF_ROLES:
            self.add_item(SelfRoleButton(label, rname, emoji))


# ── posting helpers (called by /setup_server or the panel commands) ───────────
async def post_ticket_panel(channel: discord.abc.Messageable) -> None:
    embed = discord.Embed(title=PANEL_TITLE, description=PANEL_BODY, colour=ACCENT)
    await channel.send(embed=embed, view=TicketPanelView())


async def post_roles_panel(channel: discord.abc.Messageable) -> None:
    embed = discord.Embed(title=ROLES_PANEL_TITLE, description=ROLES_PANEL_BODY, colour=ACCENT)
    await channel.send(embed=embed, view=SelfRolesView())


def bind_views(client: discord.Client) -> None:
    """Call in on_ready so every persistent panel keeps working after a restart."""
    client.add_view(TicketPanelView())
    client.add_view(TicketControlView())
    if SELF_ROLES:
        client.add_view(SelfRolesView())


# ── slash commands (added to your existing tree) ─────────────────────────────
@app_commands.command(name="ticket_panel", description="Post the 'open a ticket' panel here.")
@app_commands.checks.has_permissions(manage_guild=True)
async def _ticket_panel(interaction: discord.Interaction):
    await post_ticket_panel(interaction.channel)
    await interaction.response.send_message("Panel posted.", ephemeral=True)


@app_commands.command(name="roles_panel", description="Post the self-roles panel here.")
@app_commands.checks.has_permissions(manage_guild=True)
async def _roles_panel(interaction: discord.Interaction):
    await post_roles_panel(interaction.channel)
    await interaction.response.send_message("Panel posted.", ephemeral=True)


@app_commands.command(name="close", description="Close the ticket in this channel.")
async def _close(interaction: discord.Interaction):
    if not interaction.channel.name.startswith("ticket-"):
        await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        return
    await _do_close(interaction)


def install_commands(tree: app_commands.CommandTree) -> None:
    tree.add_command(_ticket_panel)
    tree.add_command(_close)
    # Only offer the self-roles panel command if this bot actually defines self-roles;
    # a bot with its own notify panel (the lands bot) leaves SELF_ROLES empty and skips it.
    if SELF_ROLES:
        tree.add_command(_roles_panel)
