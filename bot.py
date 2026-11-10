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

    # The actual server owner is always authorized.
    if guild.owner_id == user_id:
        return True

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM yeet_whitelist WHERE guild_id = ? AND user_id = ?",
            (guild.id, user_id)
        ).fetchone()

    return row is not None


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
# VC YEET SETTINGS / RUNTIME STATE
# ==========================================

YEET_DELAY_SECONDS = 0.8
YEET_MAX_CHANNELS_PER_CYCLE = 8
YEET_COOLDOWN_SECONDS = 30

# Active tasks, keyed by (guild_id, target_user_id).
YEET_TASKS: dict[tuple[int, int], asyncio.Task] = {}

# Executor cooldowns, keyed by (guild_id, executor_user_id).
YEET_COOLDOWNS: dict[tuple[int, int], float] = {}


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
# VC YEET — OWNER-MANAGED WHITELIST
# ==========================================

def is_server_owner(interaction: discord.Interaction) -> bool:
    return (
        interaction.guild is not None
        and interaction.guild.owner_id == interaction.user.id
    )


def is_yeet_channel_eligible(
    target: discord.Member,
    channel: discord.VoiceChannel
) -> bool:
    """Check whether the target and bot can use this voice channel."""

    guild = target.guild
    bot_member = guild.me

    if bot_member is None:
        return False

    # Do not send members to the server AFK channel.
    if guild.afk_channel and channel.id == guild.afk_channel.id:
        return False

    # Automatically skip channels restricted from @everyone.
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

    # Skip channels already at capacity.
    if channel.user_limit and len(channel.members) >= channel.user_limit:
        return False

    return True


# ==========================================
# WHITELIST COMMAND GROUP
# ==========================================

whitelist_group = app_commands.Group(
    name="whitelist",
    description="Manage who can use VC Yeet. Server owner only."
)


@whitelist_group.command(
    name="add",
    description="Authorize a member to use VC Yeet."
)
@app_commands.describe(member="Member to authorize")
async def whitelist_add(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not is_server_owner(interaction):
        return await interaction.response.send_message(
            "Only the server owner can manage the VC Yeet whitelist.",
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
        f"{member.mention} has been added to the VC Yeet whitelist.",
        ephemeral=True
    )


@whitelist_group.command(
    name="remove",
    description="Revoke a member's VC Yeet access."
)
@app_commands.describe(member="Member to remove")
async def whitelist_remove(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not is_server_owner(interaction):
        return await interaction.response.send_message(
            "Only the server owner can manage the VC Yeet whitelist.",
            ephemeral=True
        )

    removed = remove_yeet_whitelist(
        interaction.guild.id,
        member.id
    )

    if removed:
        message = (
            f"{member.mention} has been removed "
            "from the VC Yeet whitelist."
        )
    else:
        message = (
            f"{member.mention} was not on the VC Yeet whitelist."
        )

    await interaction.response.send_message(
        message,
        ephemeral=True
    )


@whitelist_group.command(
    name="list",
    description="List members authorized to use VC Yeet."
)
async def whitelist_list(interaction: discord.Interaction):

    if not is_server_owner(interaction):
        return await interaction.response.send_message(
            "Only the server owner can view the VC Yeet whitelist.",
            ephemeral=True
        )

    user_ids = get_yeet_whitelist(interaction.guild.id)

    if not user_ids:
        return await interaction.response.send_message(
            "The VC Yeet whitelist is empty. "
            "The server owner is always authorized.",
            ephemeral=True
        )

    entries = []

    for user_id in user_ids:
        member = interaction.guild.get_member(user_id)

        if member is not None:
            entries.append(f"• {member.mention} (`{user_id}`)")
        else:
            entries.append(
                f"• Unknown / departed member (`{user_id}`)"
            )

    await interaction.response.send_message(
        "**VC Yeet whitelist**\n" + "\n".join(entries),
        ephemeral=True
    )


bot.tree.add_command(whitelist_group)


# ==========================================
# VC YEET — MAIN COMMAND
# ==========================================

@bot.tree.command(
    name="vcyeet",
    description="Move a member through accessible voice channels."
)
@app_commands.guild_only()
@app_commands.describe(
    user_id="Discord user ID of the target member",
    cycles="Number of cycles (1–10)"
)
@app_commands.choices(
    cycles=[
        app_commands.Choice(name=str(number), value=number)
        for number in range(1, 11)
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
            "You are not authorized to use VC Yeet. "
            "Ask the server owner to add you with `/whitelist add`.",
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
                "I couldn't retrieve that member. Please try again.",
                ephemeral=True
            )

    if target.bot:
        return await interaction.response.send_message(
            "Bots cannot be targeted.",
            ephemeral=True
        )

    if target.voice is None or target.voice.channel is None:
        return await interaction.response.send_message(
            "That member is not currently connected to a voice channel.",
            ephemeral=True
        )

    task_key = (guild.id, target.id)
    existing_task = YEET_TASKS.get(task_key)

    if existing_task is not None and not existing_task.done():
        return await interaction.response.send_message(
            "That member already has an active VC Yeet sequence.",
            ephemeral=True
        )

    now = asyncio.get_running_loop().time()
    cooldown_key = (guild.id, executor.id)
    cooldown_until = YEET_COOLDOWNS.get(cooldown_key, 0.0)

    if now < cooldown_until:
        remaining = int(cooldown_until - now) + 1

        return await interaction.response.send_message(
            f"Please wait {remaining} seconds before starting another sequence.",
            ephemeral=True
        )

    original_channel = target.voice.channel

    # Automatically find eligible channels.
    # Locked, full, inaccessible, and AFK channels are skipped.
    eligible_channels = [
        channel
        for channel in guild.voice_channels
        if (
            channel.id != original_channel.id
            and is_yeet_channel_eligible(target, channel)
        )
    ]

    if not eligible_channels:
        return await interaction.response.send_message(
            "No eligible public voice channels are available for that member.",
            ephemeral=True
        )

    eligible_channels = eligible_channels[
        :YEET_MAX_CHANNELS_PER_CYCLE
    ]

    requested_cycles = cycles.value
    maximum_moves = len(eligible_channels) * requested_cycles

    await interaction.response.send_message(
        f"VC Yeet sequence started for **{target.display_name}**.\n"
        f"Cycles: **{requested_cycles}**\n"
        f"Eligible channels per cycle: **{len(eligible_channels)}**\n"
        f"Maximum moves: **{maximum_moves}**\n"
        "The sequence stops if the target disconnects.",
        ephemeral=True
    )

    YEET_COOLDOWNS[cooldown_key] = (
        now + YEET_COOLDOWN_SECONDS
    )

    async def run_sequence():

        try:
            for cycle_number in range(requested_cycles):

                for channel in eligible_channels:

                    # Stop if the target disconnects.
                    # The sequence will not resume if they reconnect.
                    if target.voice is None or target.voice.channel is None:
                        log.info(
                            "VC Yeet stopped because target %s disconnected.",
                            target.id
                        )
                        return

                    # Recheck permissions and capacity before every move.
                    if not is_yeet_channel_eligible(target, channel):
                        continue

                    try:
                        await target.move_to(
                            channel,
                            reason=(
                                f"VC Yeet initiated by {executor} "
                                f"({executor.id}); cycle "
                                f"{cycle_number + 1}/{requested_cycles}"
                            )
                        )

                    except (discord.Forbidden, discord.HTTPException):
                        log.exception(
                            "VC Yeet could not move target %s to channel %s",
                            target.id,
                            channel.id
                        )
                        return

                    await asyncio.sleep(YEET_DELAY_SECONDS)

            # Return the target to their starting channel if they are still
            # connected and the original channel is still accessible/free.
            if target.voice is not None and target.voice.channel is not None:

                bot_member = guild.me

                target_permissions = original_channel.permissions_for(target)
                bot_permissions = original_channel.permissions_for(bot_member)

                original_channel_has_room = (
                    not original_channel.user_limit
                    or len(original_channel.members) < original_channel.user_limit
                )

                if (
                    target_permissions.view_channel
                    and target_permissions.connect
                    and bot_permissions.view_channel
                    and bot_permissions.move_members
                    and original_channel_has_room
                ):
                    try:
                        await target.move_to(
                            original_channel,
                            reason=(
                                "VC Yeet sequence completed; "
                                "returning to starting channel"
                            )
                        )

                    except (discord.Forbidden, discord.HTTPException):
                        log.exception(
                            "Could not return target %s to original channel %s",
                            target.id,
                            original_channel.id
                        )

            log.info(
                "VC Yeet sequence completed for target %s after %s cycles.",
                target.id,
                requested_cycles
            )

        except asyncio.CancelledError:
            log.info(
                "VC Yeet sequence cancelled for target %s.",
                target.id
            )
            raise

        finally:
            current_task = YEET_TASKS.get(task_key)

            if current_task is asyncio.current_task():
                YEET_TASKS.pop(task_key, None)

    task = asyncio.create_task(run_sequence())
    YEET_TASKS[task_key] = task


# ==========================================
# VC YEET — STOP COMMAND
# ==========================================

@bot.tree.command(
    name="vcyeet_stop",
    description="Stop an active VC Yeet sequence for a member."
)
@app_commands.guild_only()
@app_commands.describe(
    user_id="Discord user ID of the target member"
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
            "No active VC Yeet sequence was found for that user.",
            ephemeral=True
        )

    task.cancel()

    await interaction.response.send_message(
        f"VC Yeet sequence stopped for user `{target_id}`.",
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

    if member.bot or before.channel == after.channel:
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


