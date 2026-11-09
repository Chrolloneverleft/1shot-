import asyncio
import logging
import os
from collections import defaultdict

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("1shot-vc-guard")

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Remember the channel a member came from, per server/member.
previous_channel: dict[tuple[int, int], int] = {}
# A move initiated by this bot must not overwrite the saved previous channel.
bot_moves: set[tuple[int, int]] = set()
# Serialize enforcement per guild so simultaneous voice events don't race each other.
guild_locks = defaultdict(asyncio.Lock)


def key_for(member: discord.Member) -> tuple[int, int]:
    return (member.guild.id, member.id)


def can_move_to(member: discord.Member, channel: discord.VoiceChannel, full_channel: discord.VoiceChannel) -> bool:
    if channel.id == full_channel.id:
        return False
    if channel.user_limit and len(channel.members) >= channel.user_limit:
        return False
    me = member.guild.me
    if me is None:
        return False
    perms = channel.permissions_for(me)
    return perms.view_channel and perms.connect and perms.move_members


async def enforce_limit(member: discord.Member, full_channel: discord.VoiceChannel) -> None:
    saved_id = previous_channel.get(key_for(member))
    target = member.guild.get_channel(saved_id) if saved_id else None

    if isinstance(target, discord.VoiceChannel) and can_move_to(member, target, full_channel):
        destination = target
        reason = f"VC capacity enforcement: {full_channel.name} is full"
    else:
        destination = None
        reason = f"VC capacity enforcement: no available previous VC for {full_channel.name}"

    # Mark the move before calling Discord; the resulting voice-state event consumes it.
    bot_moves.add(key_for(member))
    try:
        await member.move_to(destination, reason=reason)
        log.info(
            "Moved excess member %s (%s) from %s to %s",
            member,
            member.id,
            full_channel.name,
            destination.name if destination else "Disconnected",
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        bot_moves.discard(key_for(member))
        log.exception("Could not move %s from %s: %s", member, full_channel.name, exc)


@bot.event
async def on_ready():
    log.info("Online as %s (%s)", bot.user, bot.user.id)
    # Seed known current channels. No previous-channel history is available after a restart.
    for guild in bot.guilds:
        for channel in guild.voice_channels:
            for member in channel.members:
                previous_channel.setdefault(key_for(member), None)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot or before.channel == after.channel:
        return

    key = key_for(member)

    # This event is the result of a move the bot just performed. Preserve the original history.
    if key in bot_moves:
        bot_moves.discard(key)
        return

    # Save the channel the member just left. If they disconnected, clear the history.
    if after.channel is None:
        previous_channel.pop(key, None)
        return
    if before.channel is not None:
        previous_channel[key] = before.channel.id
    else:
        previous_channel.pop(key, None)

    destination = after.channel
    if not isinstance(destination, discord.VoiceChannel):
        return

    # A zero user limit means unlimited. Check only after a short settle period so
    # Discord's member list has time to reflect the join/move.
    if destination.user_limit == 0:
        return

    await asyncio.sleep(0.35)
    async with guild_locks[member.guild.id]:
        # Re-check live state; the member may have left during the delay.
        if member.voice is None or member.voice.channel is None:
            return
        current = member.voice.channel
        if current.id != destination.id or current.user_limit == 0:
            return
        if len(current.members) <= current.user_limit:
            return
        await enforce_limit(member, current)


bot.run(TOKEN)
