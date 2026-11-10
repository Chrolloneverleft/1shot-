# Vc Manager

A Discord voice-channel capacity enforcement bot. It reads each standard voice channel's configured user limit. If a member enters a full limited channel, the bot tries to return that member to the voice channel they just left. If there is no usable previous channel, it disconnects them.


## Discord permissions

The bot needs View Channels, Connect, and Move Members in the voice channels it monitors. Its role must be high enough to move the affected members. Discord's server owner and members with roles at or above the bot's highest role cannot be moved by the bot.

## Important behavior and limitations

- Only standard voice channels with a nonzero user limit are monitored. A limit of zero means unlimited.
- The bot remembers previous channels in memory. A restart clears this history; members who join directly from being disconnected have no previous channel and will be disconnected if they exceed a limit.
- If the previous channel is full, inaccessible, or otherwise unavailable, the bot disconnects the excess member.
- This is a starter implementation. Test in a private category before enabling it across a large server. Simultaneous joins can still be timing-sensitive.
