

import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from collections import defaultdict
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands


# ==========================================
# CONFIGURATION
# ==========================================

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable")

ANNOUNCEMENT_MESSAGE = (
    "Enforcing VC limits actively ~ designed by 1shot"
)

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "1shot_vc_guard.db"


# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("1shot-vc-guard")


# ==========================================
# DATABASE
# ==========================================

def init_database():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS announcements (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            )
        """)


def set_announcement_channel(guild_id, channel_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO announcements (guild_id, channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET channel_id = excluded.channel_id
        """, (guild_id, channel_id))


def get_announcement_channel(guild_id):
    with sqlite3.connect(DB_PATH) as conn:
        result = conn.execute(
            "SELECT channel_id FROM announcements WHERE guild_id = ?",
            (guild_id,)
        ).fetchone()

    return result[0] if result else None


def remove_announcement_channel(guild_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM announcements WHERE guild_id = ?",
            (guild_id,)
        )
        return cursor.rowcount > 0


init_database()


# ==========================================
# DISCORD BOT SETUP
# ==========================================

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ==========================================
# VOICE CHANNEL TRACKING
# ==========================================

previous_channel: dict[tuple[int, int], Optional[int]] = {}

bot_moves: set[tuple[int, int]] = set()

guild_locks = defaultdict(asyncio.Lock)


def key_for(member: discord.Member) -> tuple[int, int]:
    return (member.guild.id, member.id)


def can_move_to(
    member: discord.Member,
    channel: discord.VoiceChannel,
    full_channel: discord.VoiceChannel
) -> bool:

    if channel.id == full_channel.id:
        return False

    if channel.user_limit and len(channel.members) >= channel.user_limit:
        return False

    me = member.guild.me

    if me is None:
        return False

    perms = channel.permissions_for(me)

    return (
        perms.view_channel
        and perms.connect
        and perms.move_members
    )


# ==========================================
# VC CAPACITY ENFORCEMENT
# ==========================================

async def enforce_limit(
    member: discord.Member,
    full_channel: discord.VoiceChannel
) -> None:

    saved_id = previous_channel.get(key_for(member))

    target = (
        member.guild.get_channel(saved_id)
        if saved_id else None
    )

    if (
        isinstance(target, discord.VoiceChannel)
        and can_move_to(member, target, full_channel)
    ):
        destination = target

        reason = (
            f"VC capacity enforcement: "
            f"{full_channel.name} is full"
        )

    else:
        destination = None

        reason = (
            f"VC capacity enforcement: "
            f"no available previous VC for {full_channel.name}"
        )

    key = key_for(member)

    bot_moves.add(key)

    try:
        await member.move_to(
            destination,
            reason=reason
        )

        log.info(
            "Moved excess member %s (%s) from %s to %s",
            member,
            member.id,
            full_channel.name,
            destination.name if destination else "Disconnected"
        )

    except (discord.Forbidden, discord.HTTPException):

        bot_moves.discard(key)

        log.exception(
            "Could not move %s from %s",
            member,
            full_channel.name
        )


# ==========================================
# ROTATING BOT ACTIVITIES
# ==========================================

ACTIVITIES = [

    discord.Game(
        name="Enforcing VC limits"
    ),

    discord.Activity(
        type=discord.ActivityType.watching,
        name="over voice channels"
    ),

    discord.Game(
        name="Designed by 1shot"
    )

]

activity_index = 0


@tasks.loop(seconds=15)
async def rotate_status():

    global activity_index

    activity = ACTIVITIES[activity_index]

    try:

        await bot.change_presence(
            status=discord.Status.online,
            activity=activity
        )

        log.info(
            "Bot status changed to: %s",
            activity.name
        )

        activity_index = (
            activity_index + 1
        ) % len(ACTIVITIES)

    except discord.HTTPException:

        log.exception("Could not update bot presence")


@rotate_status.before_loop
async def before_rotate_status():

    await bot.wait_until_ready()


@rotate_status.error
async def rotate_status_error(error):

    log.exception(
        "Status rotation task failed",
        exc_info=error
    )


# ==========================================
# AUTOMATED VOICE CHAT ANNOUNCEMENTS
# ==========================================

@tasks.loop(minutes=20)
async def vc_announcement():

    for guild in bot.guilds:

        channel_id = get_announcement_channel(guild.id)

        if channel_id is None:
            continue

        channel = guild.get_channel(channel_id)

        if not isinstance(channel, discord.VoiceChannel):
            log.warning(
                "Configured voice channel missing in %s",
                guild.name
            )
            continue

        try:

            await channel.send(ANNOUNCEMENT_MESSAGE)

            log.info(
                "Announcement sent to voice chat: %s in %s",
                channel.name,
                guild.name
            )

        except discord.Forbidden:

            log.error(
                "No permission to send messages in voice chat: %s",
                channel.name
            )

        except discord.HTTPException:

            log.exception(
                "Announcement failed in %s",
                guild.name
            )


@vc_announcement.before_loop
async def before_vc_announcement():

    await bot.wait_until_ready()

    # First announcement after 20 minutes
    await asyncio.sleep(20 * 60)


@vc_announcement.error
async def vc_announcement_error(error):

    log.exception(
        "Announcement task failed",
        exc_info=error
    )


# ==========================================
# ANNOUNCEMENT PERMISSION CHECK
# ==========================================

def is_server_admin(interaction: discord.Interaction) -> bool:

    return (
        interaction.guild is not None
        and interaction.user.guild_permissions.administrator
    )


# ==========================================
# SET ANNOUNCEMENT VOICE CHANNEL
# ==========================================

@bot.tree.command(
    name="announcement_set",
    description="Set the voice channel for automatic announcements."
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def announcement_set(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel
):

    if not is_server_admin(interaction):
        return await interaction.response.send_message(
            "Administrator permission required.",
            ephemeral=True
        )

    permissions = channel.permissions_for(interaction.guild.me)

    if not permissions.view_channel or not permissions.send_messages:
        return await interaction.response.send_message(
            "I need View Channel and Send Messages permissions "
            "in that voice channel's text chat.",
            ephemeral=True
        )

    set_announcement_channel(
        interaction.guild.id,
        channel.id
    )

    await interaction.response.send_message(
        f"**Announcement channel configured.**\n"
        f"Messages will be sent in the voice chat of "
        f"**{channel.name}** every 20 minutes.",
        ephemeral=True
    )

    log.info(
        "Announcement channel set to %s in %s",
        channel.name,
        interaction.guild.name
    )


# ==========================================
# TEST ANNOUNCEMENT
# ==========================================

@bot.tree.command(
    name="announcement_test",
    description="Send a test VC announcement immediately."
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def announcement_test(
    interaction: discord.Interaction
):

    if not is_server_admin(interaction):
        return await interaction.response.send_message(
            "Administrator permission required.",
            ephemeral=True
        )

    channel_id = get_announcement_channel(interaction.guild.id)

    if channel_id is None:
        return await interaction.response.send_message(
            "No announcement channel configured. "
            "Use /announcement_set first.",
            ephemeral=True
        )

    channel = interaction.guild.get_channel(channel_id)

    if not isinstance(channel, discord.VoiceChannel):
        return await interaction.response.send_message(
            "Configured voice channel no longer exists.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    try:

        await channel.send(ANNOUNCEMENT_MESSAGE)

        await interaction.followup.send(
            f"Test announcement sent to voice chat "
            f"of **{channel.name}**.",
            ephemeral=True
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "I don't have permission to send messages in that voice chat.",
            ephemeral=True
        )

    except discord.HTTPException:

        log.exception("Test announcement failed")

        await interaction.followup.send(
            "Failed to send the test announcement.",
            ephemeral=True
        )


# ==========================================
# ANNOUNCEMENT STATUS
# ==========================================

@bot.tree.command(
    name="announcement_status",
    description="Check the configured announcement voice channel."
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def announcement_status(
    interaction: discord.Interaction
):

    if not is_server_admin(interaction):
        return await interaction.response.send_message(
            "Administrator permission required.",
            ephemeral=True
        )

    channel_id = get_announcement_channel(interaction.guild.id)

    if channel_id is None:
        return await interaction.response.send_message(
            "Automatic announcements are not configured.",
            ephemeral=True
        )

    channel = interaction.guild.get_channel(channel_id)

    if not isinstance(channel, discord.VoiceChannel):
        message = (
            "The saved voice channel no longer exists. "
            "Use /announcement_set to configure another."
        )
    else:
        message = (
            f"**Announcement voice channel:** {channel.name}\n"
            f"**Frequency:** Every 20 minutes\n"
            f"**Message:** {ANNOUNCEMENT_MESSAGE}"
        )

    await interaction.response.send_message(
        message,
        ephemeral=True
    )


# ==========================================
# DISABLE ANNOUNCEMENTS
# ==========================================

@bot.tree.command(
    name="announcement_disable",
    description="Disable automatic announcements in this server."
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def announcement_disable(
    interaction: discord.Interaction
):

    if not is_server_admin(interaction):
        return await interaction.response.send_message(
            "Administrator permission required.",
            ephemeral=True
        )

    removed = remove_announcement_channel(interaction.guild.id)

    message = (
        "Automatic announcements have been disabled."
        if removed
        else "No announcement channel was configured."
    )

    await interaction.response.send_message(
        message,
        ephemeral=True
    )


# ==========================================
# BOT READY EVENT
# ==========================================

@bot.event
async def on_ready():

    log.info("Bot ready event started")

    log.info(
        "Online as %s (%s)",
        bot.user,
        bot.user.id
    )

    try:

        # Initialize voice channel tracking
        for guild in bot.guilds:

            for channel in guild.voice_channels:

                for member in channel.members:

                    previous_channel.setdefault(
                        key_for(member),
                        None
                    )

        log.info("Voice channels initialized")

        # Publish initial status
        await bot.change_presence(
            status=discord.Status.online,
            activity=ACTIVITIES[0]
        )

        # Start status rotation
        if not rotate_status.is_running():
            rotate_status.start()

        # Start announcements
        if not vc_announcement.is_running():
            vc_announcement.start()

        # Synchronise slash commands
        synced = await bot.tree.sync()

        log.info(
            "Slash commands synced: %s commands",
            len(synced)
        )

        log.info("1shot VC Guard fully operational.")

    except Exception:

        log.exception("ERROR INSIDE on_ready")


# ==========================================
# VOICE STATE UPDATE EVENT
# ==========================================

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState
):

    if member.bot or before.channel == after.channel:
        return

    key = key_for(member)

    # Ignore bot-initiated moves
    if key in bot_moves:

        bot_moves.discard(key)
        return

    # Clear history when disconnected
    if after.channel is None:

        previous_channel.pop(key, None)
        return

    # Remember previous channel
    if before.channel is not None:

        previous_channel[key] = before.channel.id

    else:

        previous_channel.pop(key, None)

    destination = after.channel

    if not isinstance(destination, discord.VoiceChannel):
        return

    # Ignore unlimited channels
    if destination.user_limit == 0:
        return

    # Allow Discord state to settle
    await asyncio.sleep(0.35)

    async with guild_locks[member.guild.id]:

        # Check whether member is still connected
        if member.voice is None or member.voice.channel is None:
            return

        current = member.voice.channel

        if current.id != destination.id:
            return

        if current.user_limit == 0:
            return

        # Check channel capacity
        if len(current.members) <= current.user_limit:
            return

        # Move excess member
        await enforce_limit(member, current)


# ==========================================
# BOT ERROR HANDLING
# ==========================================

@bot.event
async def on_error(event, *args, **kwargs):

    log.exception(
        "Unhandled error in event: %s",
        event
    )


# ==========================================
# START BOT
# ==========================================

log.info("Starting 1shot VC Guard...")

bot.run(TOKEN)

