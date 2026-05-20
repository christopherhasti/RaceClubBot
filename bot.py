import asyncio
from datetime import datetime
import os
import re
import sys
import discord
from discord.ext import commands

# --- ENABLE BACKGROUND LOGGING ---
log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot-diagnostics.txt")

# Log Rotation: If the file is larger than 5MB, wipe it clean to save space
if os.path.exists(log_file_path) and os.path.getsize(log_file_path) > 5 * 1024 * 1024:
    with open(log_file_path, "w", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Log cleared (exceeded 5MB limit).\n")

# FlushLogger forces Python to write logs to the text file instantly instead of buffering them
class FlushLogger:
    def __init__(self, filename):
        self.log = open(filename, "a", encoding="utf-8")
        self.terminal = sys.stdout

    def write(self, message):
        if self.terminal:
            self.terminal.write(message)
        self.log.write(message)
        self.flush()

    def flush(self):
        if self.terminal:
            self.terminal.flush()
        self.log.flush()

sys.stdout = FlushLogger(log_file_path)
sys.stderr = sys.stdout

print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BOT PROCESS STARTED")
# ---------------------------------

# --- ENVIRONMENT CONFIGURATION ---
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

POST_RACE_GRACE_SECONDS = 45
LOG_DIRECTORY = os.path.join(os.path.expanduser("~"), "Documents", "LoungeControl", "Server", "logs")

hardware_map = {
    "e5jgr10z2sx": "RCB_1",
    "paqrvoi4k03": "RCB_2",
    "1evvndlm0yd": "RCB_3",
    "3zj4nou4i13": "RCB_4",
    "t4gz3jihqeg": "RCB_5",
    "15zakukfiqc": "RCB_6",
    "zitc4cr4wob": "RCB_7",
    "e01vipk1uou": "RCB_8",
    "dbvqlvkr2rd": "RCB_9",
    "xlhnpe0uotp": "RCB_10",
    "cj4pvupxyq1": "RCB_11"
}

pending_groups = {}
recent_vm_names = {}
active_groups = {}
setup_tasks = {}
cleanup_tasks = {}

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.voice_states = True
intents.message_content = True  

bot = commands.Bot(command_prefix="!", intents=intents)

def get_active_log_path():
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
    await asyncio.sleep(0.5)
    staged_data = pending_groups.pop(group_id, {})
    setup_tasks.pop(group_id, None)
    
    if staged_data and not any(k.lower() == group_id.lower() for k in active_groups):
        print(f"\n[Grid Action] Roster locked! Establishing private voice infrastructure for session: {group_id}")
        await setup_race_vc(group_id, staged_data)

async def schedule_cleanup(group_id, delay=0):
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

# Combines move and rename into one highly-efficient API call
async def route_and_rename(member, race_vc, new_nick, rig_tag):
    try:
        await member.edit(voice_channel=race_vc, nick=new_nick)
        print(f" -> Successfully routed and formatted {rig_tag}.")
    except Exception as e:
        if hasattr(e, 'code') and e.code == 10003:
            print(f" -> Channel deleted mid-routing for {rig_tag}. Aborting.")
        else:
            print(f" -> Gateway restriction transporting {rig_tag}: {e}")

async def setup_race_vc(group_id, staged_roster):
    guild = bot.guilds[0]
    category = guild.get_channel(RACE_CATEGORY_ID)
    
    if category is None:
        print(f"CRITICAL WARNING: Category ID {RACE_CATEGORY_ID} not found. Ensure it is correct in your .env file.")
        return
        
    vc_name = f"🏁 Server-{group_id}"
    
    existing_vc = discord.utils.get(category.voice_channels, name=vc_name)
    if existing_vc:
        race_vc = existing_vc
        print(f"[Recovery Engine] Attached to existing channel: {vc_name}")
    else:
        race_vc = await guild.create_voice_channel(name=vc_name, category=category)
        
    active_groups[group_id] = {"channel_id": race_vc.id, "setup_complete": False}
    
    try:
        tasks = []
        for rig_tag, target_name in staged_roster.items():
            member = find_rig_member(guild, rig_tag)
            if member and member.voice and member.voice.channel:
                
                # THE WAITING ROOM LOCK
                if member.voice.channel.id not in [WAITING_ROOM_VC_ID, race_vc.id]:
                    print(f" -> Skipped {rig_tag}: Currently occupied in {member.voice.channel.name}, not in Waiting Room.")
                    continue

                new_nick = member.nick
                if target_name and target_name.lower() != rig_tag.lower():
                    rig_display = rig_tag.replace("_", " ") 
                    if re.match(r"^\d+\s*RCB$", target_name, re.IGNORECASE):
                        new_nick = None
                    else:
                        new_nick = f"({rig_display}) {target_name}"[:32]
                
                # Bundle the tasks to execute concurrently
                tasks.append(route_and_rename(member, race_vc, new_nick, rig_tag))
        
        if tasks:
            await asyncio.gather(*tasks)
            
        # --- NEW: GHOST LOBBY SWEEPER ---
        # Wait a fraction of a second for Discord's member cache to update
        await asyncio.sleep(0.5)
        if len(race_vc.members) == 0:
            print(f"[Cleanup Engine] No valid drivers routed. Sweeping empty ghost lobby: {vc_name}")
            active_groups.pop(group_id, None) # Remove it from internal memory
            try:
                await race_vc.delete()
            except Exception:
                pass
            return # Exit out completely so the "finally" block doesn't unlock a deleted channel
        # --------------------------------
            
    finally:
        if group_id in active_groups:
            active_groups[group_id]["setup_complete"] = True

# Background task to pull users safely to the waiting room and strip names in one call
async def cleanup_member(member, waiting_room_vc, race_vc_id):
    try:
        if waiting_room_vc and member.voice and member.voice.channel and member.voice.channel.id == race_vc_id:
            await member.edit(nick=None, voice_channel=waiting_room_vc)
        else:
            await member.edit(nick=None)
    except Exception as e:
        print(f" -> Failed to cleanup {member.name}: {e}")

async def cleanup_race_vc(group_id):
    group_data = active_groups.pop(group_id, None)
    if not group_data:
        return
        
    guild = bot.guilds[0]
    race_vc = guild.get_channel(group_data["channel_id"])
    waiting_room_vc = guild.get_channel(WAITING_ROOM_VC_ID)
    
    if race_vc:
        tasks = [cleanup_member(m, waiting_room_vc, race_vc.id) for m in list(race_vc.members)]
        if tasks:
            await asyncio.gather(*tasks)
            
        # Give Discord's cache a split second to sync after the mass move
        await asyncio.sleep(0.5)
        
        # The Safety Shield. Physically blocks channel deletion if anyone is stranded.
        if len(race_vc.members) == 0:
            try:
                await race_vc.delete()
                print(f"Decommissioned channel parameters targeting session {group_id}")
            except Exception:
                pass
        else:
            print(f"[Safety Shield] Channel {race_vc.name} still has {len(race_vc.members)} members. Aborting deletion to prevent stranding.")

async def monitor_log_files():
    await bot.wait_until_ready()
    print(f"[Production Engine] Synchronizing with active logs at: {LOG_DIRECTORY}")

    while True:
        file_handle = None 
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
            current_vm_block = {}
            last_vm_state = {}
            active_car_applies = []
            current_group_roster = {}

            while True:
                file_handle.seek(file_handle.tell())
                line = file_handle.readline()
                
                if not line:
                    active_path = get_active_log_path()
                    if active_path and active_path != current_path:
                        print(f"[Log Monitor] Sequence split detected. Shifting listener to: {os.path.basename(active_path)}")
                        break 
                        
                    await asyncio.sleep(0.1)
                    continue

                line_lower = line.lower()

                conn_match = re.search(r"Launcher\s+(RCB[\w\s]+)\s+\(([a-z0-9]+)\)\s+connected", line, re.IGNORECASE)
                if conn_match:
                    raw_name = conn_match.group(1).strip()
                    hw_code = conn_match.group(2).lower()
                    formatted_rig = raw_name.replace(" ", "_")
                    hardware_map[hw_code] = formatted_rig
                    continue

                car_apply_match = re.search(r"Launcher\s+([a-z0-9]+)\s+car applied\s+(.+)", line, re.IGNORECASE)
                if car_apply_match:
                    hw_code = car_apply_match.group(1).lower()
                    car_name = car_apply_match.group(2).strip()
                    active_car_applies.append((hw_code, car_name))
                    continue

                if "[ACGroupVM]" in line:
                    capturing_vm = True
                    recent_vm_names = {}
                    current_vm_block = {}
                    continue
                    
                if capturing_vm:
                    if not line.strip() or re.match(r"^\d{4}-\d{2}-\d{2}", line.strip()):
                        capturing_vm = False
                        
                        items_to_remove = []
                        for slot_id, data in current_vm_block.items():
                            driver_name = data["driver"]
                            car_name = data["car"]
                            old_car = last_vm_state.get(slot_id, {}).get("car", "")
                            
                            if car_name and car_name != old_car:
                                for hw, applied_car in active_car_applies:
                                    if applied_car == car_name:
                                        current_group_roster[hw] = driver_name
                                        items_to_remove.append((hw, applied_car))
                                        break
                                        
                        for item in items_to_remove:
                            if item in active_car_applies:
                                active_car_applies.remove(item)
                                
                        last_vm_state = current_vm_block.copy()
                        
                    elif "Group state:" in line:
                        if "Creating" in line:
                            last_vm_state.clear()
                            active_car_applies.clear()
                            current_group_roster.clear()
                        continue
                    else:
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) >= 3 and parts[0].isdigit():
                            slot_index = int(parts[0])
                            extracted_name = parts[2]
                            car_name = parts[3] if len(parts) >= 4 else ""
                            
                            recent_vm_names[slot_index] = extracted_name
                            current_vm_block[slot_index] = {"driver": extracted_name, "car": car_name}
                        continue

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
                        print(f"[Grid Staged - PRIMARY] Mapped {rig_account} ({hw_code}) as '{driver_name}' to session {group_id}.")
                        
                        if group_id not in setup_tasks and not any(k.lower() == group_id.lower() for k in active_groups):
                            setup_tasks[group_id] = bot.loop.create_task(execute_delayed_setup(group_id))
                    continue

                start_match = re.search(r"changing group state from CarSelection to (?:ServerCreation|Practice), group:\s*([A-Za-z0-9_\-]+)", line, re.IGNORECASE)
                if start_match:
                    group_id = start_match.group(1)
                    
                    if current_group_roster and not any(k.lower() == group_id.lower() for k in active_groups) and group_id not in setup_tasks:
                        print(f"[Grid Staged - BACKUP] Ready Check bypassed. Using live car telemetry for session {group_id}.")
                        pending_groups[group_id] = {}
                        
                        for hw_code, driver_name in current_group_roster.items():
                            rig_account = hardware_map.get(hw_code)
                            if rig_account:
                                pending_groups[group_id][rig_account] = driver_name
                                
                        setup_tasks[group_id] = bot.loop.create_task(execute_delayed_setup(group_id))
                        
                        current_group_roster.clear()
                    continue

                if "to finished" in line_lower:
                    finish_match = re.search(r"group:\s*([A-Za-z0-9_\-]+)", line, re.IGNORECASE)
                    if finish_match:
                        extracted_id = finish_match.group(1)
                        target_key = next((k for k in active_groups if k.lower() == extracted_id.lower()), None)
                        if target_key:
                            await schedule_cleanup(target_key, delay=POST_RACE_GRACE_SECONDS)
                    continue

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

        except Exception as e:
            print(f"[CRITICAL SYSTEM RECOVERY] Log monitor encountered a fatal OS error: {e}")
            print("Restarting file stream connection in 5 seconds...")
            await asyncio.sleep(5)
            
        finally:
            if file_handle is not None and not file_handle.closed:
                try:
                    file_handle.close()
                except Exception:
                    pass

@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel and before.channel != after.channel:
        vc = before.channel
        
        if vc.name.startswith("🏁 Server-"):
            
            # Instantly strip their RCB nickname since they left the track
            try:
                if member.nick and member.nick.startswith("(RCB"):
                    bot.loop.create_task(member.edit(nick=None))
            except Exception:
                pass
            
            if len(vc.members) == 0:
                is_building = False
                for group_id, data in list(active_groups.items()):
                    if data["channel_id"] == vc.id:
                        if not data.get("setup_complete", False):
                            is_building = True
                            print(f"[Cleanup Engine] Shielded {vc.name} from deletion (Setup in progress).")
                            break
                if is_building:
                    return

                try:
                    for group_id, data in list(active_groups.items()):
                        if data["channel_id"] == vc.id:
                            active_groups.pop(group_id, None)
                            
                    await vc.delete()
                    print(f"[Cleanup Engine] Auto-cleaned empty orphaned channel: {vc.name}")
                except Exception:
                    pass

@bot.event
async def on_ready():
    print(f"--- BOT ONLINE ---")
    print(f"User: {bot.user.name}")
    print(f"Target Category ID: {RACE_CATEGORY_ID}")
    print(f"Target Waiting Room ID: {WAITING_ROOM_VC_ID}")
    
    guild = bot.guilds[0]
    category = guild.get_channel(RACE_CATEGORY_ID)
    if category:
        for vc in category.voice_channels:
            if vc.name.startswith("🏁 Server-") and len(vc.members) == 0:
                try:
                    await vc.delete()
                    print(f"[Cleanup Engine] Swept old empty channel on startup: {vc.name}")
                except Exception:
                    pass
    
    print(f"------------------")
    bot.loop.create_task(monitor_log_files())

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("CRITICAL: DISCORD_TOKEN not found in .env")
        sys.exit(1)
        
    bot.run(token)