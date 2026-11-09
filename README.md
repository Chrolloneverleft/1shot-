# 1shot VC Guard

A Discord voice-channel capacity enforcement bot. It reads each standard voice channel's configured user limit. If a member enters a full limited channel, the bot tries to return that member to the voice channel they just left. If there is no usable previous channel, it disconnects them.

## Deploy on Railway

1. Create a GitHub repository and upload `bot.py`, `requirements.txt`, and `Dockerfile` to its root. Do **not** upload a token or `.env` file.
2. In Railway, create a project using **Deploy from GitHub Repo** and select that repository.
3. In the Railway service's **Variables** tab, add `DISCORD_TOKEN` with your Discord bot token as its value.
4. Deploy. The included Dockerfile starts the bot with `python bot.py`.
5. Check the deployment logs for `Online as ...`.

## Discord permissions

The bot needs View Channels, Connect, and Move Members in the voice channels it monitors. Its role must be high enough to move the affected members. Discord's server owner and members with roles at or above the bot's highest role cannot be moved by the bot.

## Important behavior and limitations

- Only standard voice channels with a nonzero user limit are monitored. A limit of zero means unlimited.
- The bot remembers previous channels in memory. A restart clears this history; members who join directly from being disconnected have no previous channel and will be disconnected if they exceed a limit.
- If the previous channel is full, inaccessible, or otherwise unavailable, the bot disconnects the excess member.
- This is a starter implementation. Test in a private category before enabling it across a large server. Simultaneous joins can still be timing-sensitive.
