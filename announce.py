"""
announce.py — /announce for any Abex Tech bot (discord.py 2.x).

Drops into the Bank bot, Abex Stakes, or Abex Tech core. Like ticket_system.py
this is a PERMANENT module (keep it), but it registers no persistent views — the preview
buttons are short-lived by design, so nothing here breaks across a restart.

THE SHAPE (this is deliberate)
    Discord modals cannot hold pickers, and button views cannot hold free text. An
    announcement needs BOTH: a channel to post to (picker) and multi-line body (free text).
    So it's sequenced:
        /announce  → channel comes from the slash option (native channel picker, no IDs
                     to type) → a modal takes the title + body (real free text)
                   → an ephemeral PREVIEW with Post / Cancel, so nothing goes public
                     until you've seen exactly what it looks like.

WIRE IT IN
    import announce
    announce.install_commands(tree)      # adds /announce
    # optional:
    announce.DEFAULT_CHANNEL = "announcements"
    announce.ACCENT = 0x22FF7A

PERMISSIONS
    The command is gated to Manage Server. Pinging @everyone additionally needs the bot
    to have Mention Everyone in the target channel; without it the post still goes out,
    just without the ping.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands

log = logging.getLogger("announce")

DEFAULT_CHANNEL = "announcements"      # used when the command is run without a channel
ACCENT = 0x22FF7A
FOOTER = ""                            # e.g. "Abex Tech" — appended to every announcement


def _resolve_channel(guild: discord.Guild,
                     given: discord.TextChannel | None) -> discord.TextChannel | None:
    if given is not None:
        return given
    return discord.utils.get(guild.text_channels, name=DEFAULT_CHANNEL)


def _build_embed(title: str, body: str) -> discord.Embed:
    e = discord.Embed(title=title or None, description=body, colour=ACCENT)
    if FOOTER:
        e.set_footer(text=FOOTER)
    return e


class _PreviewView(discord.ui.View):
    """Ephemeral confirm step: nothing is public until Post is clicked."""

    def __init__(self, channel: discord.TextChannel, embed: discord.Embed, ping: str):
        super().__init__(timeout=600)
        self.channel = channel
        self.embed = embed
        self.ping = ping

    @discord.ui.button(label="Post it", emoji="📣", style=discord.ButtonStyle.success)
    async def post(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        content, mentions = "", discord.AllowedMentions.none()
        if self.ping == "everyone":
            content = "@everyone"
            mentions = discord.AllowedMentions(everyone=True, roles=False, users=False)
        elif self.ping.startswith("role:"):
            content = f"<@&{self.ping[5:]}>"
            mentions = discord.AllowedMentions(everyone=False, roles=True, users=False)
        try:
            msg = await self.channel.send(content=content, embed=self.embed,
                                          allowed_mentions=mentions)
        except discord.Forbidden:
            await interaction.followup.send(
                f"I can't post in {self.channel.mention} — I need Send Messages "
                f"and Embed Links there.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.followup.send(f"Posted in {self.channel.mention} — {msg.jump_url}",
                                        ephemeral=True)
        log.info("announcement posted in #%s", self.channel.name)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled — nothing was posted.",
                                                embed=None, view=None)
        self.stop()


class _AnnounceModal(discord.ui.Modal, title="New announcement"):
    """The only part that is genuinely free text."""

    head = discord.ui.TextInput(label="Title", required=False, max_length=256,
                                placeholder="Abex Tech — corporate action")
    body = discord.ui.TextInput(label="Announcement", style=discord.TextStyle.paragraph,
                                required=True, max_length=4000,
                                placeholder="Write it exactly as you want it to read.")

    def __init__(self, channel: discord.TextChannel, ping: str):
        super().__init__()
        self._channel = channel
        self._ping = ping

    async def on_submit(self, interaction: discord.Interaction):
        embed = _build_embed(str(self.head), str(self.body))
        where = self._channel.mention
        note = {"everyone": " and ping **@everyone**"}.get(self._ping, "")
        if self._ping.startswith("role:"):
            note = f" and ping <@&{self._ping[5:]}>"
        await interaction.response.send_message(
            content=f"Preview — this will post to {where}{note}. Nothing is public yet.",
            embed=embed, view=_PreviewView(self._channel, embed, self._ping),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@app_commands.command(name="announce", description="Write an announcement, preview it, then post it.")
@app_commands.describe(channel=f"Where to post (default: #{DEFAULT_CHANNEL})",
                       ping="Who to ping with it (default: nobody)",
                       role="The role to ping, if you chose 'a role'")
@app_commands.choices(ping=[
    app_commands.Choice(name="nobody", value="none"),
    app_commands.Choice(name="@everyone", value="everyone"),
    app_commands.Choice(name="a role", value="role"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def _announce(interaction: discord.Interaction,
                    channel: discord.TextChannel | None = None,
                    ping: app_commands.Choice[str] | None = None,
                    role: discord.Role | None = None):
    target = _resolve_channel(interaction.guild, channel)
    if target is None:
        await interaction.response.send_message(
            f"I couldn't find a #{DEFAULT_CHANNEL} channel — pass one with `channel:`.",
            ephemeral=True)
        return

    mode = ping.value if ping else "none"
    if mode == "role":
        if role is None:
            await interaction.response.send_message(
                "Pick the role too (`role:`) when you choose 'a role'.", ephemeral=True)
            return
        mode = f"role:{role.id}"

    # A modal must be the FIRST response to the interaction — never defer before this.
    await interaction.response.send_modal(_AnnounceModal(target, mode))


@_announce.error
async def _err(interaction: discord.Interaction, error):
    msg = ("You need Manage Server to announce."
           if isinstance(error, app_commands.MissingPermissions) else f"Failed: {error}")
    send = (interaction.followup.send if interaction.response.is_done()
            else interaction.response.send_message)
    await send(msg, ephemeral=True)


def install_commands(tree: app_commands.CommandTree) -> None:
    tree.add_command(_announce)
