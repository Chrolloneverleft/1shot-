
import asyncio
import logging
import os
from collections import defaultdict

import discord
from discord.ext import commands, tasks


# ==========================================
# CONFIGURATION
# ==========================================

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable")


# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("1shot-vc-guard")


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

previous_channel: dict[tuple[int, int], int] = {}

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

    bot_moves.add(key_for(member))

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
            destination.name if destination else "Disconnected",
        )

    except (discord.Forbidden, discord.HTTPException) as exc:

        bot_moves.discard(key_for(member))

        log.exception(
            "Could not move %s from %s: %s",
            member,
            full_channel.name,
            exc
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


@rotate_status.before_loop
async def before_rotate_status():

    await bot.wait_until_ready()


# ==========================================
# BOT READY EVENT
# ==========================================

@bot.event
async def on_ready():

    log.info(
        "Online as %s (%s)",
        bot.user,
        bot.user.id
    )

    # Initialize existing voice channel members
    for guild in bot.guilds:

        for channel in guild.voice_channels:

            for member in channel.members:

                previous_channel.setdefault(
                    key_for(member),
                    None
                )

    # Publish first status immediately
    await bot.change_presence(
        status=discord.Status.online,
        activity=ACTIVITIES[0]
    )

    log.info("Initial bot status published")

    # Start automatic status rotation
    if not rotate_status.is_running():

        rotate_status.start()

        log.info("Rotating status system started")


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

    # Clear history if member disconnects
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

    # Allow Discord member state to settle
    await asyncio.sleep(0.35)

    async with guild_locks[member.guild.id]:

        # Verify member is still connected
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
# START BOT
# ==========================================

bot.run(TOKEN)
