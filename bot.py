import asyncio
from datetime import datetime
import os
import re
import sys
import discord
from discord.ext import commands

# --- ENVIRONMENT CONFIGURATION ---
# Safely parse the .env file BEFORE defining variables to prevent ID failures.
# This automatically strips any accidental leading or trailing spaces from your keys and values.
if os.path.exists(".env"):
    with open(".env", "r") as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

def get_env_int(key, default=0):
    val = os.environ.get(key)
    return int(val) if val and val.isdigit() else default

WAITING_ROOM_VC_ID = get_env_int("WAITING_ROOM_VC_ID")
RACE_CATEGORY_ID = get_env_int("RACE_CATEGORY_ID")

# Time (in seconds) to leave the voice room active after a race finishes.
POST_RACE_GRACE_SECONDS = 45

# Target production log path:
LOG_DIRECTORY = os.path.join(os.path.expanduser("~"), "Documents", "LoungeControl", "Server", "logs")

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

pending_groups = {}
recent_vm_names = {}
active_groups = {}
setup_tasks = {}
cleanup_tasks = {}

# Ensure required intent is explicitly enabled
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.voice_states = True
intents.message_content = True  

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
        base_username = member.name.lower()
        global_name = (member.global_name or "").lower()
        server_nick = (member.nick or "").lower()
        effective_display = member.display_name.lower()
        
        if target in (base_username, global_name, server_nick, effective_display):
            return member
    return None

async def execute_delayed_setup(group_id):
    """Waits 500ms to allow session writes to settle, then builds the grid."""
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
    await bot.wait_until_ready()
    print(f"[Production Engine] Synchronizing with active logs at: {LOG_DIRECTORY}")

    # Master loop ensures the background task NEVER dies permanently
    while True:
        try:
            global recent_vm_names
            current_path = get_active_log_path()
            while not current_path or not os.path.exists(current_path):
                await asyncio.sleep(2)
                current_path = get_active_log_path()

            print(f"[Log Monitor] Attached to file stream: {os.path.basename(current_path)}")
            file_handle = open(current_path, "r", encoding="utf-8", errors="replace")
            file_handle.seek(0, os.SEEK_END)

            capturing_vm = False
            is_valid_state = False
            
            # Sequence queue to track hardware before the group is officially created
            recent_slot_assignments = []

            while True:
                # Clear EOF lock for Windows to ensure continuous reading
                file_handle.seek(file_handle.tell())
                line = file_handle.readline()
                
                if not line:
                    active_path = get_active_log_path()
                    if active_path and active_path != current_path:
                        print(f"[Log Monitor] Sequence split detected. Shifting listener to: {os.path.basename(active_path)}")
                        file_handle.close()
                        break # Break out of inner loop to restart the file hook cleanly
                        
                    await asyncio.sleep(0.1)
                    continue

                line_lower = line.lower()

                # 1. LIVE HARDWARE DISCOVERY
                conn_match = re.search(r"Launcher\s+(RCB[\w\s]+)\s+\(([a-z0-9]+)\)\s+connected", line, re.IGNORECASE)
                if conn_match:
                    raw_name = conn_match.group(1).strip()
                    hw_code = conn_match.group(2).lower()
                    formatted_rig = raw_name.replace(" ", "_")
                    hardware_map[hw_code] = formatted_rig
                    continue

                # 2. PRE-CREATION ASSIGNMENT QUEUE
                # Captures rigs instantly as the admin assigns them in LoungeControl
                slot_match = re.search(r"launcher assinged to slot\s+([a-z0-9]+)", line, re.IGNORECASE)
                if slot_match:
                    hw_code = slot_match.group(1).lower()
                    if hw_code not in recent_slot_assignments:
                        recent_slot_assignments.append(hw_code)
                    continue

                # 3. NAME EXTRACTION FROM PRE-START [ACGroupVM] BLOCKS
                if "[ACGroupVM]" in line:
                    capturing_vm = True
                    is_valid_state = False
                    continue
                    
                if capturing_vm:
                    if not line.strip() or re.match(r"^\d{4}-\d{2}-\d{2}", line.strip()):
                        capturing_vm = False
                    elif "Group state:" in line:
                        if any(s in line for s in ["Creating", "ServerCreation", "Starting", "CarSelection", "Practice"]):
                            is_valid_state = True
                        else:
                            is_valid_state = False
                        continue
                    else:
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) >= 3 and parts[0].isdigit() and is_valid_state:
                            slot_index = int(parts[0])
                            extracted_name = parts[2]
                            
                            # Zip the extracted name dynamically to the queued hardware ID
                            if slot_index < len(recent_slot_assignments):
                                hw_code = recent_slot_assignments[slot_index]
                                recent_vm_names[hw_code] = extracted_name
                        continue

                # -------------------------------------------------------------
                # DUAL-TRIGGER ARCHITECTURE: Whichever logs first builds the room
                # -------------------------------------------------------------
                
                # TRIGGER A: THE READY CHECK (Enabled Ready Screen)
                ready_match = re.search(r"Send start Ready Check to slot (\d+),\s*([a-z0-9]+),\s*group:\s*([A-Za-z0-9_\-]+)", line, re.IGNORECASE)
                if ready_match:
                    slot_id = int(ready_match.group(1))
                    hw_code = ready_match.group(2).lower()
                    group_id = ready_match.group(3)
                    
                    rig_account = hardware_map.get(hw_code)
                    if rig_account:
                        if group_id not in pending_groups:
                            pending_groups[group_id] = {}
                            
                        driver_name = recent_vm_names.get(hw_code, rig_account)
                        pending_groups[group_id][rig_account] = driver_name
                        print(f"[Grid Staged - Method A] Mapped {rig_account} ({hw_code}) as '{driver_name}' to session {group_id}.")
                        
                        if group_id not in setup_tasks and not any(k.lower() == group_id.lower() for k in active_groups):
                            setup_tasks[group_id] = bot.loop.create_task(execute_delayed_setup(group_id))
                    continue

                # TRIGGER B: THE STATE TRANSITION (Disabled/Skipped Ready Screen)
                start_match = re.search(r"changing group state from \w+ to (?:Starting|CarSelection|Practice), group:\s*([A-Za-z0-9_\-]+)", line, re.IGNORECASE)
                if start_match:
                    group_id = start_match.group(1)
                    
                    # Scoop everyone we tracked via the VM extraction array
                    if recent_vm_names:
                        if group_id not in pending_groups:
                            pending_groups[group_id] = {}
                            
                        for hw_code, driver_name in recent_vm_names.items():
                            rig_account = hardware_map.get(hw_code)
                            if rig_account:
                                pending_groups[group_id][rig_account] = driver_name
                                print(f"[Grid Staged - Method B] Mapped {rig_account} ({hw_code}) as '{driver_name}' to session {group_id}.")
                        
                        if group_id not in setup_tasks and not any(k.lower() == group_id.lower() for k in active_groups):
                            setup_tasks[group_id] = bot.loop.create_task(execute_delayed_setup(group_id))
                            
                        # Clear buffers after successful deployment to avoid ghost lobbies
                        recent_slot_assignments.clear()
                        recent_vm_names.clear()
                    continue
                
                # -------------------------------------------------------------

                # 5. SESSION FINISHED COMMAND
                if "to finished" in line_lower:
                    finish_match = re.search(r"group:\s*([A-Za-z0-9_\-]+)", line, re.IGNORECASE)
                    if finish_match:
                        extracted_id = finish_match.group(1)
                        target_key = next((k for k in active_groups if k.lower() == extracted_id.lower()), None)
                        if target_key:
                            await schedule_cleanup(target_key, delay=POST_RACE_GRACE_SECONDS)
                    continue

                # 6. TRUE SESSION DELETION COMMAND
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
                        recent_slot_assignments.clear()

        except Exception as e:
            print(f"[CRITICAL SYSTEM RECOVERY] Log monitor encountered a fatal OS error: {e}")
            print("Restarting file stream connection in 5 seconds...")
            await asyncio.sleep(5)

async def setup_race_vc(group_id, staged_roster):
    guild = bot.guilds[0]
    category = guild.get_channel(RACE_CATEGORY_ID)
    
    if category is None:
        print(f"CRITICAL WARNING: Category ID {RACE_CATEGORY_ID} not found. Ensure it is correct in your .env file. Creating channel out of bounds.")
        
    vc_name = f"🏁 Server-{group_id}"
    race_vc = await guild.create_voice_channel(name=vc_name, category=category)
    active_groups[group_id] = {"channel_id": race_vc.id}
    
    for rig_tag, target_name in staged_roster.items():
        member = find_rig_member(guild, rig_tag)
        if member and member.voice and member.voice.channel:
            try:
                await member.move_to(race_vc)
                print(f" -> Successfully routed {rig_tag} to grid.")
                
                if target_name and target_name.lower() != rig_tag.lower():
                    # Formats the nickname to display as "(RCB 1) Christopher Hastings"
                    rig_display = rig_tag.replace("_", " ") 
                    formatted_name = f"({rig_display}) {target_name}"[:32] # 32 is the max Discord limit
                    
                    try:
                        await member.edit(nick=formatted_name)
                        print(f"    [Profile Engine] Applied display layer: {rig_tag} -> {formatted_name}")
                    except discord.Forbidden:
                        print(f"    [Security Warning] Gateway authorization blocked profile modification targeting: {rig_tag}")
                    except Exception as ex:
                        print(f"    [Execution Warning] Metadata injection failed targeting {rig_tag}: {ex}")
                        
            except Exception as e:
                print(f" -> Gateway restriction transporting {rig_tag}: {e}")

async def cleanup_race_vc(group_id):
    group_data = active_groups.pop(group_id, None)
    if not group_data:
        return
        
    guild = bot.guilds[0]
    race_vc = guild.get_channel(group_data["channel_id"])
    waiting_room_vc = guild.get_channel(WAITING_ROOM_VC_ID)
    
    if race_vc:
        for member in list(race_vc.members):
            try:
                await member.edit(nick=None) # Clears the custom tag and name, returning to baseline native RCB name
            except Exception:
                pass
            
            if waiting_room_vc:
                try:
                    await member.move_to(waiting_room_vc)
                except Exception:
                    pass
                    
        await race_vc.delete()
        print(f"Decommissioned channel parameters targeting session {group_id}")

@bot.event
async def on_ready():
    print(f"--- BOT ONLINE ---")
    print(f"User: {bot.user.name}")
    print(f"Target Category ID: {RACE_CATEGORY_ID}")
    print(f"Target Waiting Room ID: {WAITING_ROOM_VC_ID}")
    print(f"------------------")
    bot.loop.create_task(monitor_log_files())

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("CRITICAL: DISCORD_TOKEN not found in .env")
        sys.exit(1)
        
    bot.run(token)