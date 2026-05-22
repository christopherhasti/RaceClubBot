# LoungeControl Discord Bot

A Discord bot that automatically manages private voice channels for sim racing sessions. It monitors a sim racing server's log files in real time, detects when a race group is forming, creates a dedicated voice channel, moves the correct drivers into it with formatted nicknames, and tears everything down when the session ends.

---

## Features

- **Automatic session detection** — Watches live log files for ready checks and group state changes
- **Dual detection paths** — Uses ready-check events as the primary signal, with a car-telemetry fallback if the ready check is bypassed
- **Private voice channel management** — Creates a `🏁 Server-<GroupID>` voice channel per session, moves drivers in, and deletes it when the race finishes
- **Driver nickname formatting** — Renames members to `(RCB X) DriverName` for the duration of their session
- **Post-race grace period** — Holds the channel open for 45 seconds after a race ends for review
- **Rate-limit safe** — Sequential API calls with a 0.6 s delay between each move to avoid Discord HTTP 429s
- **Ghost lobby sweeper** — Immediately deletes channels where no drivers were successfully routed
- **Startup cleanup** — Sweeps any empty `🏁 Server-` channels left over from a previous run
- **Persistent hardware map** — Dynamically learned hardware codes are saved to `hardware_map.json` and survive bot restarts
- **Log rotation** — Diagnostic log file is wiped when it exceeds 5 MB

---

## Requirements

- Python 3.10+
- `discord.py` (`pip install discord.py`)
- A Discord bot token with the following intents enabled in the [Developer Portal](https://discord.com/developers/applications):
  - Server Members Intent
  - Message Content Intent

---

## Configuration

Create a `.env` file in the same directory as `bot.py`:

```env
DISCORD_TOKEN=your_bot_token_here
GUILD_ID=123456789012345678
WAITING_ROOM_VC_ID=123456789012345678
RACE_CATEGORY_ID=123456789012345678
```

| Variable            | Description                                                              |
|---------------------|--------------------------------------------------------------------------|
| `DISCORD_TOKEN`     | Your Discord bot token                                                   |
| `GUILD_ID`          | The ID of your Discord server                                            |
| `WAITING_ROOM_VC_ID`| The voice channel ID where drivers wait between races                    |
| `RACE_CATEGORY_ID`  | The category ID under which race voice channels will be created          |

The log directory is hardcoded to `~/Documents/LoungeControl/Server/logs`. Update `LOG_DIRECTORY` in the script if your server logs live elsewhere.

---

## Hardware Map

The `hardware_map` dictionary in the script defines the default mapping of hardware codes (from the sim server) to rig names:

```python
hardware_map = {
    "e5jgr10z2sx": "RCB_1",
    "paqrvoi4k03": "RCB_2",
    ...
}
```

When a rig connects and the bot learns a new hardware code, it is saved immediately to `hardware_map.json` in the same directory. On the next startup, these saved codes are merged on top of the defaults, so dynamically registered rigs are never lost across restarts.

---

## Running the Bot

```bash
python bot.py
```

Diagnostic output is written both to the terminal and to `bot-diagnostics.txt` in the same directory.

---

## How It Works

```
Sim server writes to log file
         │
         ▼
   Log monitor reads new lines
         │
    ┌────┴──────────────────────┐
    │                           │
Ready Check detected        Car telemetry fallback
(primary path)              (if ready check missed)
    │                           │
    └────────────┬──────────────┘
                 │
         pending_groups filled
                 │
                 ▼
      execute_delayed_setup (0.5 s delay)
                 │
                 ▼
    Create 🏁 Server-<GroupID> VC
                 │
                 ▼
    Move + rename each driver (sequential, 0.6 s gap)
                 │
                 ▼
    Race runs...
                 │
                 ▼
    "to finished" detected → 45 s grace period
                 │
    "removed" detected → immediate teardown
                 │
                 ▼
    Move drivers back to Waiting Room, strip nicks
                 │
                 ▼
    Delete race VC
```

---

## File Structure

```
.
├── bot.py                  # Main bot script
├── .env                    # Environment config (not committed)
├── hardware_map.json       # Auto-generated, persists learned hardware codes
└── bot-diagnostics.txt     # Auto-generated diagnostic log
```

---

## Known Limitations

- **No admin commands** — There are no `!commands` for manual intervention (e.g., forcing a cleanup or checking active sessions).
- **Log directory is hardcoded** — Must be changed in the source if your log path differs.
- **Session state is not persisted** — If the bot restarts mid-race, active session tracking is lost. Hardware codes are safe (saved to `hardware_map.json`), but any in-progress voice channel will not be automatically cleaned up.
