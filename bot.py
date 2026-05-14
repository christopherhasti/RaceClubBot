import asyncio
from datetime import datetime
import os
import re
import sys
import discord
from discord.ext import commands

# Safely load internal server infrastructure IDs from the local uncommitted environment parameters
WAITING_ROOM_VC_ID = int(os.getenv("WAITING_ROOM_VC_ID", 0))
RACE_CATEGORY_ID = int(os.getenv("RACE_CATEGORY_ID", 0))

# Time (in seconds) to leave the voice room active after a race finishes.
POST_RACE_GRACE_SECONDS = 45

# Target production log path:
LOG_DIRECTORY = os.path.join(os.path.expanduser("~"), "Documents", "LoungeControl", "Server", "logs")
# Note: Set to "." if testing locally inside the active script folder

# Complete hardware layout pre-populated with permanent identifiers
hardware_map = {
    "e5jgr10z2sx": "RCB_1",   # Rig 1
    "paqrvoi4k03": "RCB_2",   # Rig 2
    "1evvndlm0yd": "RCB_3",   # Rig 3
    "3zj4nou4i13": "RCB_4",   # Rig 4
    "t4gz3jihqeg": "RCB_5",   # Rig 5
    "15zakukfiqc": "RCB_6",   # Rig 6
    "zitc4cr4wob": "RCB_7",   # Rig 7
    "e01vipk1uou": "RCB_8",   # Rig 8
    "dbvqlvkr2rd": "RCB_9",   # Rig 9
    "xlhnpe0uotp": "RCB_10",  # Rig 10
    "cj4pvupxyq1": "RCB_11"   # Rig 11
}

# Roster staging tracker: {group_id: {"RCB_1": "Real Name", "RCB_2": "Real Name"}}
pending_groups = {}

# Ephemeral mapping buffer parsing recent multi-line [ACGroupVM] driver block states
# Maps physical zero-indexed session slots to extracted string names: {0: "Adam LaBarbera"}
recent_vm_names = {}

# Active session channels: {group_id: {"channel_id": int}}
active_groups = {}

# Active delayed setup tasks: {group_id: Task}
setup_tasks = {}

# Active delayed cleanup tasks: {group_id: Task}
cleanup_tasks = {}

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

def get_active_log_path():
    """Scans the directory and returns the most recently modified text log for today."""
    today_str = datetime.now().strftime("%Y%m%d")
    prefix = f"server-log{today_str}"
    
    if not os.path.exists(LOG_DIRECTORY):
        return None
        
    candidates = []
    try:
        for filename in os.listdir(LOG_DIRECTORY):
            if filename.startswith(prefix) and filename.endswith(".txt"):
                candidates.append(os.path.join(LOG_DIRECTORY, filename))
    except Exception:
        return None
        
    if not candidates:
        return None
        
    return max(candidates, key=os.path.getmtime)

def find_rig_member(guild, rig_name):
    """
    Deep identity evaluation: Sweeps base username, global display name, 
    server nickname, and effective display name to bypass nickname masking.
    """
    target = rig_name.lower()
    for member in guild.members:
        # Check underlying immutable identity attributes alongside active overlays
        base_username = member.name.lower()
        global_name = (member.global_name or "").lower()
        server_nick = (member.nick or "").lower()
        effective_display = member.display_name.lower()
        
        if target in (base_username, global_name, server_nick, effective_display):
            return member
    return None

async def execute_delayed_setup(group_id):
    """Waits 500ms to allow back-to-back Ready Check log writes to settle, then builds the grid."""
    await asyncio.sleep(0.5)
    staged_data = pending_groups.pop(group_id, {})
    setup_tasks.pop(group_id, None)
    
    # Case-insensitive verification avoids generating duplicate rooms
    if staged_data and not any(k.lower() == group_id.lower() for k in active_groups):
        print(f"\n[Grid Action] Roster locked! Establishing private voice infrastructure early for session: {group_id}")
        await setup_race_vc(group_id, staged_data)

async def schedule_cleanup(group_id, delay=0):
    """Schedules channel teardown after a defined grace period unless overridden."""
    if group_id in cleanup_tasks:
        return

    async def task_wrapper():
        if delay > 0:
            print(f"[Grid Action] Checkered flag! Session {group_id} finished. Holding voice room open for {delay}s post-race review...")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
        await cleanup_race_vc(group_id)
        cleanup_tasks.pop(group_id, None)

    cleanup_tasks[group_id] = bot.loop.create_task(task_wrapper())

async def monitor_log_files():
    global recent_vm_names
    await bot.wait_until_ready()
    print(f"[Production Engine] Synchronizing with active logs at: {LOG_DIRECTORY}")

    current_path = get_active_log_path()
    while not current_path or not os.path.exists(current_path):
        await asyncio.sleep(2)
        current_path = get_active_log_path()

    print(f"[Log Monitor] Attached to file stream: {os.path.basename(current_path)}")
    file_handle = open(current_path, "r", encoding="utf-8", errors="replace")
    file_handle.seek(0, os.SEEK_END)

    capturing_vm = False

    while True:
        line = file_handle.readline()
        
        # Intra-day split sequence monitoring
        if not line:
            active_path = get_active_log_path()
            if active_path and active_path != current_path:
                print(f"[Log Monitor] Sequence split detected. Shifting listener to: {os.path.basename(active_path)}")
                file_handle.close()
                current_path = active_path
                file_handle = open(current_path, "r", encoding="utf-8", errors="replace")
                continue
                
            await asyncio.sleep(0.1)
            continue

        line_lower = line.lower()

        # 1. LIVE HARDWARE DISCOVERY (Backup fallback for unmapped replacement hardware)
        conn_match = re.search(r"Launcher\s+(RCB[\w\s]+)\s+\(([a-z0-9]+)\)\s+connected", line, re.IGNORECASE)
        if conn_match:
            raw_name = conn_match.group(1).strip()
            hw_code = conn_match.group(2).lower()
            formatted_rig = raw_name.replace(" ", "_")
            hardware_map[hw_code] = formatted_rig
            continue

        # 2. NAME EXTRACTION FROM PRE-START [ACGroupVM] BLOCKS
        # Captures multi-line outputs like: "0, Launcher, Adam LaBarbera, Audi R8 GT3 Evo 2"
        if "[ACGroupVM]" in line:
            capturing_vm = True
            recent_vm_names = {}
            continue
            
        if capturing_vm:
            if not line.strip() or re.match(r"^\d{4}-\d{2}-\d{2}", line.strip()):
                capturing_vm = False
            elif "Group state:" in line:
                continue
            else:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3 and parts[0].isdigit():
                    slot_index = int(parts[0])
                    extracted_name = parts[2]
                    recent_vm_names[slot_index] = extracted_name
                continue

        # 3. EARLY ROSTER STAGING & RAPID SETUP TRIGGER
        # Intercepts: "Send start Ready Check to slot 0, e5jgr10z2sx, group: LobbyA"
        ready_match = re.search(r"Send start Ready Check to slot (\d+),\s*([a-z0-9]+),\s*group:\s*([A-Za-z0-9_\-]+)", line, re.IGNORECASE)
        if ready_match:
            slot_id = int(ready_match.group(1))
            hw_code = ready_match.group(2).lower()
            group_id = ready_match.group(3)
            
            rig_account = hardware_map.get(hw_code)
            if rig_account:
                if group_id not in pending_groups:
                    pending_groups[group_id] = {}
                    
                driver_name = recent_vm_names.get(slot_id, rig_account)
                pending_groups[group_id][rig_account] = driver_name
                print(f"[Grid Staged] Mapped {rig_account} ({hw_code}) as '{driver_name}' to session {group_id}.")
                
                if group_id not in setup_tasks and not any(k.lower() == group_id.lower() for k in active_groups):
                    setup_tasks[group_id] = bot.loop.create_task(execute_delayed_setup(group_id))
            continue

        # 4. SESSION FINISHED COMMAND (Trigger Post-Race Grace Period Teardown)
        if "to finished" in line_lower:
            finish_match = re.search(r"group:\s*([A-Za-z0-9_\-]+)", line, re.IGNORECASE)
            if finish_match:
                extracted_id = finish_match.group(1)
                target_key = next((k for k in active_groups if k.lower() == extracted_id.lower()), None)
                if target_key:
                    await schedule_cleanup(target_key, delay=POST_RACE_GRACE_SECONDS)
            continue

        # 5. TRUE SESSION DELETION COMMAND (Overrides stopped bypass to delete lobby when dashboard clears)
        # Note: Purposely bypasses "to stopped" lines to preserve troubleshooting comms for admin staff
        if "removed" in line_lower:
            teardown_match = re.search(r"group\s+([A-Za-z0-9_\-]+)\s+removed", line, re.IGNORECASE)
            if teardown_match:
                extracted_id = teardown_match.group(1)
                target_key = next((k for k in active_groups if k.lower() == extracted_id.lower()), None)
                
                if target_key:
                    if target_key in cleanup_tasks:
                        cleanup_tasks[target_key].cancel()
                        cleanup_tasks.pop(target_key, None)
                        
                    print(f"\n[Grid Action] Complete session removal detected. Purging workspace layout immediately: {target_key}")
                    bot.loop.create_task(cleanup_race_vc(target_key))
                
                if extracted_id in setup_tasks:
                    setup_tasks[extracted_id].cancel()
                    setup_tasks.pop(extracted_id, None)
                pending_groups.pop(extracted_id, None)

async def setup_race_vc(group_id, staged_roster):
    guild = bot.guilds[0]
    category = guild.get_channel(RACE_CATEGORY_ID)
    
    vc_name = f"🏁 Server-{group_id}"
    race_vc = await guild.create_voice_channel(name=vc_name, category=category)
    active_groups[group_id] = {"channel_id": race_vc.id}
    
    for rig_tag, target_name in staged_roster.items():
        member = find_rig_member(guild, rig_tag)
        if member and member.voice and member.voice.channel:
            try:
                await member.move_to(race_vc)
                print(f" -> Successfully routed {rig_tag} to grid.")
                
                # Apply custom identity display overlay natively via Approach A
                if target_name and target_name.lower() != rig_tag.lower():
                    try:
                        await member.edit(nick=target_name)
                        print(f"    [Profile Engine] Applied display layer: {rig_tag} -> {target_name}")
                    except discord.Forbidden:
                        print(f"    [Security Warning] Gateway authorization blocked profile modification targeting: {rig_tag}")
                    except Exception as ex:
                        print(f"    [Execution Warning] Metadata injection failed targeting {rig_tag}: {ex}")
                        
            except Exception as e:
                print(f" -> Gateway restriction transporting {rig_tag}: {e}")
        else:
            pass

async def cleanup_race_vc(group_id):
    group_data = active_groups.pop(group_id, None)
    if not group_data:
        return
        
    guild = bot.guilds[0]
    race_vc = guild.get_channel(group_data["channel_id"])
    waiting_room_vc = guild.get_channel(WAITING_ROOM_VC_ID)
    
    if race_vc:
        for member in list(race_vc.members):
            # 1. Native Display Name Restoration via Approach A
            # Passing nick=None safely unbinds custom string overlays directly on Discord servers,
            # returning profiles cleanly to baseline account labels without local caching dictionaries.
            try:
                await member.edit(nick=None)
                print(f"    [Profile Engine] Restored baseline display mapping for ID: {member.id}")
            except Exception:
                pass
            
            # 2. Base Return Routing
            if waiting_room_vc:
                try:
                    await member.move_to(waiting_room_vc)
                except Exception:
                    pass
                    
        await race_vc.delete()
        print(f"Decommissioned channel parameters targeting session {group_id}")

@bot.event
async def on_ready():
    print(f"Gateway interface ready. Operating profile: {bot.user.name}")
    bot.loop.create_task(monitor_log_files())

if __name__ == "__main__":
    token = None
    if os.path.exists(".env"):
        with open(".env", "r") as f:
            for line in f:
                if line.startswith("DISCORD_TOKEN="):
                    token = line.split("=", 1)[1].strip()
    
    if not token:
        print("CRITICAL: Failed to evaluate target DISCORD_TOKEN configuration.")
        sys.exit(1)
        
    bot.run(token)