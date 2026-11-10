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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS yeet_whitelist (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                added_by INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS voice_mute_locks (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                applied_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
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


# ==========================================
# WHITELIST DATABASE FUNCTIONS
# ==========================================

def add_yeet_whitelist(guild_id, user_id, added_by):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO yeet_whitelist (guild_id, user_id, added_by)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET added_by = excluded.added_by
        """, (guild_id, user_id, added_by))


def remove_yeet_whitelist(guild_id, user_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM yeet_whitelist WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        )
        return cursor.rowcount > 0


def get_yeet_whitelist(guild_id):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT user_id FROM yeet_whitelist WHERE guild_id = ? ORDER BY user_id",
            (guild_id,)
        ).fetchall()

    return [row[0] for row in rows]


def is_yeet_authorized(guild, user_id):
    if guild is None:
        return False

    if guild.owner_id == user_id:
        return True

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM yeet_whitelist WHERE guild_id = ? AND user_id = ?",
            (guild.id, user_id)
        ).fetchone()

    return row is not None


# ==========================================
# PERSISTENT MUTE DATABASE FUNCTIONS
# ==========================================

def add_voice_mute_lock(guild_id, user_id, applied_by):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO voice_mute_locks (guild_id, user_id, applied_by)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET applied_by = excluded.applied_by
        """, (guild_id, user_id, applied_by))


def remove_voice_mute_lock(guild_id, user_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM voice_mute_locks WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        )
        return cursor.rowcount > 0


def has_voice_mute_lock(guild_id, user_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM voice_mute_locks WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        ).fetchone()

    return row is not None


def get_voice_mute_locks(guild_id):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT user_id, applied_by, created_at FROM voice_mute_locks "
            "WHERE guild_id = ? ORDER BY created_at",
            (guild_id,)
        ).fetchall()

    return rows


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


# ==========================================
# VC YEET / PERSISTENT MUTE RUNTIME STATE
# ==========================================

YEET_DELAY_SECONDS = 0.8
YEET_MAX_CHANNELS_PER_CYCLE = 8
YEET_COOLDOWN_SECONDS = 30

YEET_TASKS = {}
YEET_COOLDOWNS = {}

MUTE_ENFORCING = set()


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
    discord.Game(name="Enforcing VC limits"),

    discord.Activity(
        type=discord.ActivityType.watching,
        name="over voice channels"
    ),

    discord.Game(name="Designed by 1shot")
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

    # First announcement occurs 20 minutes after startup.
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
            f"Test announcement sent to the voice chat "
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
# SHARED VC YEET / MUTE AUTHORIZATION
# ==========================================

def is_server_owner(interaction: discord.Interaction) -> bool:
    return (
        interaction.guild is not None
        and interaction.guild.owner_id == interaction.user.id
    )


def is_yeet_channel_eligible(target, channel):
    guild = target.guild
    bot_member = guild.me

    if bot_member is None:
        return False

    if guild.afk_channel and channel.id == guild.afk_channel.id:
        return False

    everyone_permissions = channel.permissions_for(guild.default_role)

    if not (
        everyone_permissions.view_channel
        and everyone_permissions.connect
    ):
        return False

    target_permissions = channel.permissions_for(target)

    if not (
        target_permissions.view_channel
        and target_permissions.connect
    ):
        return False

    bot_permissions = channel.permissions_for(bot_member)

    if not (
        bot_permissions.view_channel
        and bot_permissions.connect
        and bot_permissions.move_members
    ):
        return False

    if channel.user_limit and len(channel.members) >= channel.user_limit:
        return False

    return True


# ==========================================
# WHITELIST COMMANDS — SERVER OWNER ONLY
# ==========================================

whitelist_group = app_commands.Group(
    name="whitelist",
    description="Manage who can use VC Yeet and persistent voice mute."
)


@whitelist_group.command(
    name="add",
    description="Authorize a member to use VC Yeet and voice mute."
)
@app_commands.describe(member="Member to authorize")
async def whitelist_add(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not is_server_owner(interaction):
        return await interaction.response.send_message(
            "Only the server owner can manage this whitelist.",
            ephemeral=True
        )

    if member.bot:
        return await interaction.response.send_message(
            "Bots cannot be added to the whitelist.",
            ephemeral=True
        )

    if member.id == interaction.guild.owner_id:
        return await interaction.response.send_message(
            "The server owner is already authorized automatically.",
            ephemeral=True
        )

    add_yeet_whitelist(
        interaction.guild.id,
        member.id,
        interaction.user.id
    )

    await interaction.response.send_message(
        f"{member.mention} has been added to the operator whitelist.",
        ephemeral=True
    )


@whitelist_group.command(
    name="remove",
    description="Revoke a member's operator access."
)
@app_commands.describe(member="Member to remove")
async def whitelist_remove(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not is_server_owner(interaction):
        return await interaction.response.send_message(
            "Only the server owner can manage this whitelist.",
            ephemeral=True
        )

    removed = remove_yeet_whitelist(
        interaction.guild.id,
        member.id
    )

    message = (
        f"{member.mention} has been removed from the operator whitelist."
        if removed
        else f"{member.mention} was not on the whitelist."
    )

    await interaction.response.send_message(
        message,
        ephemeral=True
    )


@whitelist_group.command(
    name="list",
    description="List all authorized operators."
)
async def whitelist_list(interaction: discord.Interaction):

    if not is_server_owner(interaction):
        return await interaction.response.send_message(
            "Only the server owner can view this whitelist.",
            ephemeral=True
        )

    user_ids = get_yeet_whitelist(interaction.guild.id)

    if not user_ids:
        return await interaction.response.send_message(
            "The operator whitelist is empty. "
            "The server owner is always authorized.",
            ephemeral=True
        )

    lines = []

    for user_id in user_ids:
        member = interaction.guild.get_member(user_id)

        if member is not None:
            lines.append(f"• {member.mention} (`{user_id}`)")
        else:
            lines.append(
                f"• Unknown / departed member (`{user_id}`)"
            )

    await interaction.response.send_message(
        "**Authorized operators**\n" + "\n".join(lines),
        ephemeral=True
    )


bot.tree.add_command(whitelist_group)


# ==========================================
# VC YEET COMMANDS
# ==========================================

@bot.tree.command(
    name="vcyeet",
    description="Move a member through accessible voice channels."
)
@app_commands.guild_only()
@app_commands.describe(
    user_id="Discord user ID of target",
    cycles="Number of cycles (1–10)"
)
@app_commands.choices(
    cycles=[
        app_commands.Choice(name=str(n), value=n)
        for n in range(1, 11)
    ]
)
async def vcyeet(
    interaction: discord.Interaction,
    user_id: str,
    cycles: app_commands.Choice[int]
):

    guild = interaction.guild
    executor = interaction.user

    if not is_yeet_authorized(guild, executor.id):
        return await interaction.response.send_message(
            "You are not authorized. Ask the server owner to add you with `/whitelist add`.",
            ephemeral=True
        )

    try:
        target_id = int(user_id.strip())

    except (TypeError, ValueError):
        return await interaction.response.send_message(
            "Enter a valid numeric Discord user ID.",
            ephemeral=True
        )

    target = guild.get_member(target_id)

    if target is None:
        try:
            target = await guild.fetch_member(target_id)

        except discord.NotFound:
            return await interaction.response.send_message(
                "That user is not a member of this server.",
                ephemeral=True
            )

        except discord.HTTPException:
            return await interaction.response.send_message(
                "I couldn't retrieve that member.",
                ephemeral=True
            )

    if target.bot:
        return await interaction.response.send_message(
            "Bots cannot be targeted.",
            ephemeral=True
        )

    if target.voice is None or target.voice.channel is None:
        return await interaction.response.send_message(
            "That member is not in a voice channel.",
            ephemeral=True
        )

    task_key = (guild.id, target.id)
    existing = YEET_TASKS.get(task_key)

    if existing is not None and not existing.done():
        return await interaction.response.send_message(
            "That member already has an active sequence.",
            ephemeral=True
        )

    now = asyncio.get_running_loop().time()
    cooldown_key = (guild.id, executor.id)

    if now < YEET_COOLDOWNS.get(cooldown_key, 0):
        remaining = int(YEET_COOLDOWNS[cooldown_key] - now) + 1

        return await interaction.response.send_message(
            f"Wait {remaining} seconds before starting another sequence.",
            ephemeral=True
        )

    original_channel = target.voice.channel

    channels = [
        channel
        for channel in guild.voice_channels
        if (
            channel.id != original_channel.id
            and is_yeet_channel_eligible(target, channel)
        )
    ][:YEET_MAX_CHANNELS_PER_CYCLE]

    if not channels:
        return await interaction.response.send_message(
            "No eligible public voice channels are available.",
            ephemeral=True
        )

    count = cycles.value

    await interaction.response.send_message(
        f"VC Yeet started for **{target.display_name}**.\n"
        f"Cycles: **{count}** | "
        f"Channels per cycle: **{len(channels)}**.\n"
        "The sequence stops if the target disconnects.",
        ephemeral=True
    )

    YEET_COOLDOWNS[cooldown_key] = (
        now + YEET_COOLDOWN_SECONDS
    )

    async def run_sequence():

        try:
            for cycle in range(count):

                for channel in channels:

                    if target.voice is None or target.voice.channel is None:
                        return

                    if not is_yeet_channel_eligible(target, channel):
                        continue

                    try:
                        await target.move_to(
                            channel,
                            reason=(
                                f"VC Yeet by {executor} "
                                f"({executor.id}), cycle {cycle + 1}/{count}"
                            )
                        )

                    except (discord.Forbidden, discord.HTTPException):
                        log.exception(
                            "VC Yeet failed for target %s",
                            target.id
                        )
                        return

                    await asyncio.sleep(YEET_DELAY_SECONDS)

            if target.voice is not None and target.voice.channel is not None:

                bot_member = guild.me
                target_perms = original_channel.permissions_for(target)
                bot_perms = original_channel.permissions_for(bot_member)

                has_room = (
                    not original_channel.user_limit
                    or len(original_channel.members) < original_channel.user_limit
                )

                if (
                    target_perms.view_channel
                    and target_perms.connect
                    and bot_perms.view_channel
                    and bot_perms.move_members
                    and has_room
                ):
                    await target.move_to(
                        original_channel,
                        reason="VC Yeet completed; return to starting channel"
                    )

        except asyncio.CancelledError:
            raise

        except discord.HTTPException:
            log.exception(
                "VC Yeet sequence error for %s",
                target.id
            )

        finally:
            if YEET_TASKS.get(task_key) is asyncio.current_task():
                YEET_TASKS.pop(task_key, None)

    YEET_TASKS[task_key] = asyncio.create_task(run_sequence())


@bot.tree.command(
    name="vcyeet_stop",
    description="Stop an active VC Yeet sequence."
)
@app_commands.guild_only()
@app_commands.describe(
    user_id="Discord user ID of target"
)
async def vcyeet_stop(
    interaction: discord.Interaction,
    user_id: str
):

    guild = interaction.guild

    if not is_yeet_authorized(guild, interaction.user.id):
        return await interaction.response.send_message(
            "You are not authorized to use VC Yeet.",
            ephemeral=True
        )

    try:
        target_id = int(user_id.strip())

    except (TypeError, ValueError):
        return await interaction.response.send_message(
            "Enter a valid numeric Discord user ID.",
            ephemeral=True
        )

    task = YEET_TASKS.get((guild.id, target_id))

    if task is None or task.done():
        return await interaction.response.send_message(
            "No active sequence found for that user.",
            ephemeral=True
        )

    task.cancel()

    await interaction.response.send_message(
        f"Stopped VC Yeet for `{target_id}`.",
        ephemeral=True
    )

# ==========================================
# PERSISTENT VOICE MUTE HELPERS
# ==========================================

async def apply_persistent_mute(
    member: discord.Member,
    reason: str
) -> bool:

    if member.voice is None or member.voice.channel is None:
        return False

    if member.voice.mute:
        return True

    guild = member.guild
    bot_member = guild.me

    if bot_member is None:
        return False

    permissions = member.voice.channel.permissions_for(bot_member)

    if not permissions.mute_members:
        log.warning(
            "Missing Mute Members permission in %s",
            member.voice.channel.name
        )
        return False

    key = (guild.id, member.id)

    if key in MUTE_ENFORCING:
        return False

    MUTE_ENFORCING.add(key)

    try:
        await member.edit(
            mute=True,
            reason=reason
        )

        log.info(
            "Applied persistent server mute to %s (%s)",
            member,
            member.id
        )

        return True

    except (discord.Forbidden, discord.HTTPException):
        log.exception(
            "Could not server-mute %s (%s)",
            member,
            member.id
        )
        return False

    finally:
        MUTE_ENFORCING.discard(key)


async def enforce_mute_lock(
    member: discord.Member,
    reason: str
):
    if has_voice_mute_lock(member.guild.id, member.id):
        await apply_persistent_mute(member, reason)


async def release_voice_mute(member: discord.Member):
    """Remove the stored lock first, then lift the Discord server mute."""

    remove_voice_mute_lock(
        member.guild.id,
        member.id
    )

    if member.voice is None or member.voice.channel is None:
        return True

    bot_member = member.guild.me

    if bot_member is None:
        return False

    permissions = member.voice.channel.permissions_for(bot_member)

    if not permissions.mute_members:
        return False

    try:
        if member.voice.mute:
            await member.edit(
                mute=False,
                reason="Persistent voice mute released by authorized operator"
            )

        return True

    except (discord.Forbidden, discord.HTTPException):
        log.exception(
            "Could not unmute %s (%s)",
            member,
            member.id
        )
        return False


# ==========================================
# PERSISTENT VOICE MUTE — SINGLE MEMBER
# ==========================================

@bot.tree.command(
    name="voicemute",
    description="Apply a persistent server mute to a member."
)
@app_commands.guild_only()
@app_commands.describe(
    member="Member to restrict"
)
async def voicemute(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not is_yeet_authorized(
        interaction.guild,
        interaction.user.id
    ):
        return await interaction.response.send_message(
            "You are not authorized to manage voice mutes.",
            ephemeral=True
        )

    if member.bot:
        return await interaction.response.send_message(
            "Bots cannot be added to persistent mute locks.",
            ephemeral=True
        )

    if member.voice is None or member.voice.channel is None:
        return await interaction.response.send_message(
            "That member is not currently in a voice channel.",
            ephemeral=True
        )

    bot_permissions = member.voice.channel.permissions_for(
        interaction.guild.me
    )

    if not bot_permissions.mute_members:
        return await interaction.response.send_message(
            "I need the **Mute Members** permission in that voice channel.",
            ephemeral=True
        )

    add_voice_mute_lock(
        interaction.guild.id,
        member.id,
        interaction.user.id
    )

    success = await apply_persistent_mute(
        member,
        f"Persistent voice mute applied by {interaction.user} ({interaction.user.id})"
    )

    if success:
        await interaction.response.send_message(
            f"Persistent voice mute is now active for {member.mention}. "
            "Only an authorized operator can release it.",
            ephemeral=True
        )

    else:
        remove_voice_mute_lock(
            interaction.guild.id,
            member.id
        )

        await interaction.response.send_message(
            "I couldn't apply the server mute. "
            "Check my Mute Members permission and role hierarchy.",
            ephemeral=True
        )


# ==========================================
# PERSISTENT VOICE MUTE — SINGLE MEMBER RELEASE
# ==========================================

@bot.tree.command(
    name="voiceunmute",
    description="Release a member's persistent voice mute."
)
@app_commands.guild_only()
@app_commands.describe(
    member="Member to release"
)
async def voiceunmute(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not is_yeet_authorized(
        interaction.guild,
        interaction.user.id
    ):
        return await interaction.response.send_message(
            "You are not authorized to manage voice mutes.",
            ephemeral=True
        )

    existed = has_voice_mute_lock(
        interaction.guild.id,
        member.id
    )

    success = await release_voice_mute(member)

    if not existed:
        return await interaction.response.send_message(
            f"{member.mention} does not have a persistent mute lock.",
            ephemeral=True
        )

    if success:
        await interaction.response.send_message(
            f"Persistent mute released for {member.mention}.",
            ephemeral=True
        )

    else:
        await interaction.response.send_message(
            "The lock was removed, but I couldn't lift the server mute. "
            "Check my permissions and role hierarchy.",
            ephemeral=True
        )


# ==========================================
# BULK PERSISTENT VOICE MUTE
# CURRENT MEMBERS OF SELECTED CHANNEL ONLY
# ==========================================

@bot.tree.command(
    name="voicemute_bulk",
    description="Persistently mute everyone currently in a selected voice channel."
)
@app_commands.guild_only()
@app_commands.describe(
    channel="Voice channel whose current members should be muted"
)
async def voicemute_bulk(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel
):

    if not is_yeet_authorized(
        interaction.guild,
        interaction.user.id
    ):
        return await interaction.response.send_message(
            "You are not authorized to manage voice mutes.",
            ephemeral=True
        )

    bot_permissions = channel.permissions_for(interaction.guild.me)

    if not bot_permissions.view_channel or not bot_permissions.mute_members:
        return await interaction.response.send_message(
            "I need View Channel and Mute Members permissions in that voice channel.",
            ephemeral=True
        )

    members = [
        member
        for member in list(channel.members)
        if not member.bot
    ]

    if not members:
        return await interaction.response.send_message(
            "There are no human members in that voice channel.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    muted_count = 0
    failed = []

    for member in members:

        add_voice_mute_lock(
            interaction.guild.id,
            member.id,
            interaction.user.id
        )

        if await apply_persistent_mute(
            member,
            f"Bulk persistent voice mute by {interaction.user} ({interaction.user.id})"
        ):
            muted_count += 1

        else:
            # Keep the restriction stored so it can be enforced
            # when the bot has permission and the member is connected.
            failed.append(member.display_name)

    result = (
        f"Bulk mute processed for **{channel.name}**.\n"
        f"Persistent mute applied: **{muted_count}/{len(members)}**."
    )

    if failed:
        result += (
            "\nCould not mute immediately: "
            + ", ".join(failed[:20])
        )
        result += (
            "\nTheir restrictions remain stored and will be enforced "
            "when possible."
        )

    await interaction.followup.send(
        result,
        ephemeral=True
    )


# ==========================================
# BULK PERSISTENT VOICE UNMUTE
# ==========================================

@bot.tree.command(
    name="voiceunmute_bulk",
    description="Release persistent mute locks for current members of a voice channel."
)
@app_commands.guild_only()
@app_commands.describe(
    channel="Voice channel whose current members should be released"
)
async def voiceunmute_bulk(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel
):

    if not is_yeet_authorized(
        interaction.guild,
        interaction.user.id
    ):
        return await interaction.response.send_message(
            "You are not authorized to manage voice mutes.",
            ephemeral=True
        )

    members = [
        member
        for member in list(channel.members)
        if not member.bot
    ]

    if not members:
        return await interaction.response.send_message(
            "There are no human members in that voice channel.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    released = 0
    failed = []

    for member in members:

        existed = has_voice_mute_lock(
            interaction.guild.id,
            member.id
        )

        if not existed:
            continue

        if await release_voice_mute(member):
            released += 1

        else:
            failed.append(member.display_name)

    result = (
        f"Bulk release processed for **{channel.name}**.\n"
        f"Restrictions released: **{released}**."
    )

    if failed:
        result += (
            "\nCould not lift Discord server mute for: "
            + ", ".join(failed[:20])
        )

    await interaction.followup.send(
        result,
        ephemeral=True
    )


# ==========================================
# LIST ACTIVE PERSISTENT MUTE RESTRICTIONS
# ==========================================

@bot.tree.command(
    name="voicemute_list",
    description="List members with active persistent voice mute restrictions."
)
@app_commands.guild_only()
async def voicemute_list(
    interaction: discord.Interaction
):

    if not is_yeet_authorized(
        interaction.guild,
        interaction.user.id
    ):
        return await interaction.response.send_message(
            "You are not authorized to view persistent mute restrictions.",
            ephemeral=True
        )

    rows = get_voice_mute_locks(interaction.guild.id)

    if not rows:
        return await interaction.response.send_message(
            "There are no active persistent voice mute restrictions.",
            ephemeral=True
        )

    lines = []

    for user_id, applied_by, created_at in rows:

        member = interaction.guild.get_member(user_id)

        label = (
            member.mention
            if member
            else f"Unknown / departed member (`{user_id}`)"
        )

        lines.append(
            f"• {label} — applied by `{applied_by}` — {created_at}"
        )

    output = (
        "**Persistent voice mute restrictions**\n"
        + "\n".join(lines)
    )

    if len(output) > 1900:
        output = output[:1850] + "\n…list truncated."

    await interaction.response.send_message(
        output,
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

        # Initialize voice channel tracking.
        for guild in bot.guilds:

            for channel in guild.voice_channels:

                for member in channel.members:

                    previous_channel.setdefault(
                        key_for(member),
                        None
                    )

        log.info("Voice channels initialized")

        # Reapply persistent mute locks after bot restarts.
        for guild in bot.guilds:

            for channel in guild.voice_channels:

                for member in list(channel.members):

                    if has_voice_mute_lock(guild.id, member.id):
                        await enforce_mute_lock(
                            member,
                            "Reapplying persistent voice mute after bot startup"
                        )

        # Publish initial status.
        await bot.change_presence(
            status=discord.Status.online,
            activity=ACTIVITIES[0]
        )

        # Start status rotation.
        if not rotate_status.is_running():
            rotate_status.start()

        # Start announcements.
        if not vc_announcement.is_running():
            vc_announcement.start()

        # Register commands specifically in each server.
        for guild in bot.guilds:

            bot.tree.copy_global_to(guild=guild)

            synced = await bot.tree.sync(guild=guild)

            log.info(
                "Slash commands synced for %s: %s commands",
                guild.name,
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

    if member.bot:
        return

    # Persistent server-mute enforcement:
    # Reapply if a locked member is unmuted, or if they join
    # a voice channel while a persistent restriction is active.
    if (
        after.channel is not None
        and has_voice_mute_lock(member.guild.id, member.id)
        and not after.mute
    ):
        await enforce_mute_lock(
            member,
            "Reapplying active persistent voice mute"
        )

    # A mute-state change does not require capacity enforcement.
    if before.channel == after.channel:
        return

    key = key_for(member)

    # Ignore bot-initiated capacity-enforcement moves.
    if key in bot_moves:
        bot_moves.discard(key)
        return

    # Clear history when disconnected.
    if after.channel is None:
        previous_channel.pop(key, None)
        return

    # Remember previous channel.
    if before.channel is not None:
        previous_channel[key] = before.channel.id
    else:
        previous_channel.pop(key, None)

    destination = after.channel

    if not isinstance(destination, discord.VoiceChannel):
        return

    # Ignore unlimited channels.
    if destination.user_limit == 0:
        return

    # Allow Discord state to settle.
    await asyncio.sleep(0.35)

    async with guild_locks[member.guild.id]:

        if member.voice is None or member.voice.channel is None:
            return

        current = member.voice.channel

        if current.id != destination.id:
            return

        if current.user_limit == 0:
            return

        if len(current.members) <= current.user_limit:
            return

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
