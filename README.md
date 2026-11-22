# SpotiRPC

Show Spotify playback on your Discord profile without linking your Spotify account.

SpotiRPC runs on Linux and needs Python 3.9+, `busctl`, and the Spotify and Discord desktop apps.

1. Create an app in the [Discord Developer Portal](https://discord.com/developers/applications) and copy its Application ID.
2. Create a `.env` file next to `spotirpc.py` with `DISCORD_APPLICATION_ID=your_application_id`.
3. Open Spotify and Discord, enable Discord Activity Sharing, and run `python3 spotirpc.py`.

Press Ctrl+C to stop. Pause Spotify to show the Idle card.
