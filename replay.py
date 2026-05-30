#!/usr/bin/env python3
"""Log replay harness for the Race Club bot.

Feeds a LoungeControl server log into a simulator that mirrors the bot's
parser logic and prints what the production bot WOULD do for each event.
No Discord API calls, no state files, no real side effects.

Usage:
    python replay.py path/to/server-log20260529.txt
    python replay.py path/to/server-log20260529.txt --verbose
    python replay.py path/to/dir/                       # all server-log*.txt files in order

Why this exists
---------------
Before this, the only way to test a bot change was to deploy and watch a
real session. With replay you can:
  - Verify a fix against a historical incident log
  - See exactly what the bot would emit at every event
  - Catch regressions before they hit production

Caveat: this script duplicates the parser regexes from bot.py. If you change
the parsing logic in bot.py, update this file too. The duplication is
intentional — keeping the production bot dependency-free is more valuable
than a single source of truth for a dev-time tool.
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

# Mirror of the bot's default hardware_map. The replay walks the same
# `Launcher RCB X (hwcode) connected` lines as the bot does to learn new
# rigs dynamically, so this just needs to be the default seed.
DEFAULT_HARDWARE_MAP = {
    "e5jgr10z2sx": "RCB_1", "paqrvoi4k03": "RCB_2", "1evvndlm0yd": "RCB_3",
    "3zj4nou4i13": "RCB_4", "t4gz3jihqeg": "RCB_5", "15zakukfiqc": "RCB_6",
    "zitc4cr4wob": "RCB_7", "e01vipk1uou": "RCB_8", "dbvqlvkr2rd": "RCB_9",
    "xlhnpe0uotp": "RCB_10", "cj4pvupxyq1": "RCB_11"
}

# Regexes — copy of the bot's parsing layer. If you change one in bot.py,
# change it here too.
RE_LINE_TIMESTAMP   = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)")
RE_SERVER_RESTART   = re.compile(r"\[INF\]\s+Starting server\b")
RE_CONNECT          = re.compile(r"Launcher\s+(RCB[\w\s]+)\s+\(([a-z0-9]+)\)\s+connected", re.IGNORECASE)
RE_CAR_APPLY        = re.compile(r"Launcher\s+([a-z0-9]+)\s+car applied\s+(.+)", re.IGNORECASE)
RE_READY_CHECK      = re.compile(
    r"Send start Ready Check to slot (\d+),\s*([a-z0-9]+),\s*group:\s*([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)
RE_BACKUP_START     = re.compile(
    r"changing group state from CarSelection to (?:ServerCreation|Practice), group:\s*([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)
RE_FINISH           = re.compile(
    r"changing group state from \w+ to Finished, group:\s*([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)
RE_REMOVED          = re.compile(r"group\s+([A-Za-z0-9_\-]+)\s+removed", re.IGNORECASE)


def _is_placeholder_name(name):
    if not name:
        return True
    if re.match(r"^\d+\s*RCB$", name, re.IGNORECASE):
        return True
    if re.match(r"^RCB_\d+$", name, re.IGNORECASE):
        return True
    return False


class Simulator:
    """Mirrors the bot's monitor_log_files state machine without doing any
    Discord I/O. Every place the real bot would call bot.loop.create_task(...)
    or member.edit(...) instead becomes an `emit()` line."""

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.hardware_map = dict(DEFAULT_HARDWARE_MAP)

        # Bot-level state (mirrors module-level dicts in bot.py).
        self.pending_groups = {}
        self.active_groups = {}  # gid -> {"managed_rigs": set, "roster": dict}
        self.setup_tasks = set()
        self.cleanup_tasks = set()

        # Per-log state (mirrors locals inside monitor_log_files).
        self.capturing_vm = False
        self.current_vm_block = {}
        self.last_vm_state = {}
        self.active_car_applies = []
        self.current_group_roster = {}
        self.current_slot_to_hw = {}
        self.recent_vm_names = {}

        self.current_timestamp = ""
        self.stats = defaultdict(int)

    def emit(self, msg, *, verbose_only=False):
        if verbose_only and not self.verbose:
            return
        prefix = f"[{self.current_timestamp}] " if self.current_timestamp else ""
        print(f"{prefix}{msg}")

    # ---- Simulated side effects (would be Discord API calls in real bot) ----

    def sim_setup_session(self, gid):
        if gid in self.active_groups:
            return
        staged = self.pending_groups.pop(gid, {})
        if not staged:
            return
        self.active_groups[gid] = {
            "managed_rigs": set(),
            "roster": dict(staged),
        }
        self.emit(f"[Grid Action] Roster locked! Establishing private voice infrastructure for session: {gid}")
        self.stats["sessions_setup"] += 1
        for rig_tag, target_name in staged.items():
            self.active_groups[gid]["managed_rigs"].add(rig_tag)
            self.emit(f"  -> [{gid}] Routed {rig_tag} → '{target_name}'", verbose_only=True)
        self.setup_tasks.discard(gid)

    def sim_schedule_cleanup(self, gid, delay):
        if gid not in self.active_groups:
            return
        self.cleanup_tasks.add(gid)
        self.emit(f"[Grid Action] Checkered flag! Session {gid} finished. Holding {delay}s post-race review...")
        # In the real bot, cleanup_race_vc fires after the delay. Simulate
        # immediate effect — the replay doesn't need real timing.
        self.cleanup_tasks.discard(gid)
        self.active_groups.pop(gid, None)
        self.stats["sessions_finished_cleanly"] += 1

    def sim_cleanup_now(self, gid, reason):
        if gid in self.active_groups:
            self.active_groups.pop(gid, None)
            self.cleanup_tasks.discard(gid)
            self.emit(f"[Cleanup] {reason}: session {gid} torn down.")
            self.stats[reason] += 1

    def sim_refresh_nicknames(self, roster_updates):
        # Mirror the bot's placeholder-guard. Only "promote" placeholder->real,
        # never swap real->real. See is_placeholder_name in bot.py.
        if not roster_updates:
            return
        for gid, data in self.active_groups.items():
            roster = data["roster"]
            for hw, driver_name in roster_updates.items():
                rig_tag = self.hardware_map.get(hw)
                if not rig_tag:
                    continue
                old_name = roster.get(rig_tag)
                if old_name == driver_name:
                    continue
                if not _is_placeholder_name(old_name):
                    self.emit(
                        f"  [Name Refresh REFUSED] {rig_tag}: '{old_name}' → '{driver_name}' "
                        f"(real→real swap, likely cross-session VM contamination)",
                        verbose_only=True,
                    )
                    self.stats["refresh_blocked"] += 1
                    continue
                if _is_placeholder_name(driver_name):
                    continue
                self.emit(f"[Name Refresh] {rig_tag}: '{old_name}' → '{driver_name}' in session {gid}")
                roster[rig_tag] = driver_name
                self.stats["refresh_applied"] += 1

    # ---- Line processing ----

    def process_line(self, line):
        self.stats["lines_processed"] += 1

        # Capture this line's timestamp if present, so subsequent emits
        # show what time the bot would have logged its action.
        ts_match = RE_LINE_TIMESTAMP.match(line)
        if ts_match:
            self.current_timestamp = ts_match.group(1)

        # --- Server restart ---
        if RE_SERVER_RESTART.search(line):
            if self.active_groups:
                orphans = list(self.active_groups.keys())
                self.emit(f"[Server Restart Detected] cleaning up {len(orphans)} orphan(s): {', '.join(orphans)}")
                self.current_slot_to_hw.clear()
                self.current_group_roster.clear()
                self.active_car_applies.clear()
                self.last_vm_state.clear()
                for gid in orphans:
                    self.sim_cleanup_now(gid, "server_restart")
            else:
                self.emit("[Server Restart Detected] (no orphan sessions)", verbose_only=True)
            return

        # --- Hardware connect ---
        m = RE_CONNECT.search(line)
        if m:
            raw_name = m.group(1).strip()
            hw_code = m.group(2).lower()
            formatted = raw_name.replace(" ", "_")
            if self.hardware_map.get(hw_code) != formatted:
                self.hardware_map[hw_code] = formatted
                self.emit(f"[Hardware Map] {hw_code} → {formatted}", verbose_only=True)
                self.stats["hw_mappings_learned"] += 1
            return

        # --- Car apply ---
        m = RE_CAR_APPLY.search(line)
        if m:
            self.active_car_applies.append((m.group(1).lower(), m.group(2).strip()))
            return

        # --- VM block start ---
        if "[ACGroupVM]" in line:
            self.capturing_vm = True
            self.current_vm_block = {}
            return

        # --- VM block content / close ---
        if self.capturing_vm:
            stripped = line.strip()
            # Block ends on blank line or a new timestamped line.
            if not stripped or RE_LINE_TIMESTAMP.match(stripped):
                self.capturing_vm = False
                self._close_vm_block()
                # The close-trigger line could itself be a timestamp line we
                # also want to dispatch. Don't recurse — caller's loop will
                # see it next iteration. Actually, the bot does NOT re-dispatch
                # in this case; it `continue`s. So just return.
                return

            if "Group state:" in line:
                if "Creating" in line:
                    self.last_vm_state.clear()
                    self.active_car_applies.clear()
                    self.current_group_roster.clear()
                    self.current_slot_to_hw.clear()
                return

            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3 and parts[0].isdigit():
                slot_idx = int(parts[0])
                driver = parts[2]
                car = parts[3] if len(parts) >= 4 else ""
                self.recent_vm_names[slot_idx] = driver
                self.current_vm_block[slot_idx] = {"driver": driver, "car": car}
            return

        # --- Ready Check (PRIMARY staging path) ---
        m = RE_READY_CHECK.search(line)
        if m:
            slot_id = int(m.group(1))
            hw_code = m.group(2).lower()
            gid = m.group(3)
            self.current_slot_to_hw[slot_id] = hw_code
            rig = self.hardware_map.get(hw_code)
            if rig:
                self.pending_groups.setdefault(gid, {})
                driver_name = self.recent_vm_names.get(slot_id, rig)
                self.pending_groups[gid][rig] = driver_name
                self.emit(f"[Grid Staged - PRIMARY] {rig} ({hw_code}) as '{driver_name}' → session {gid}",
                          verbose_only=True)
                if gid not in self.setup_tasks and gid not in self.active_groups:
                    self.setup_tasks.add(gid)
                    self.sim_setup_session(gid)
            return

        # --- BACKUP staging path ---
        m = RE_BACKUP_START.search(line)
        if m:
            gid = m.group(1)
            if self.current_group_roster and gid not in self.active_groups and gid not in self.setup_tasks:
                self.emit(f"[Grid Staged - BACKUP] Ready Check bypassed for session {gid}")
                self.pending_groups[gid] = {}
                for hw, driver_name in self.current_group_roster.items():
                    rig = self.hardware_map.get(hw)
                    if rig:
                        self.pending_groups[gid][rig] = driver_name
                self.setup_tasks.add(gid)
                self.sim_setup_session(gid)
                self.current_group_roster.clear()
            return

        # --- Finish (graceful end) ---
        m = RE_FINISH.search(line)
        if m:
            gid = m.group(1)
            if gid in self.active_groups:
                self.sim_schedule_cleanup(gid, 45)
            return

        # --- Removed (immediate teardown) ---
        if "removed" in line.lower():
            m = RE_REMOVED.search(line)
            if m:
                gid = m.group(1)
                if gid in self.active_groups:
                    self.sim_cleanup_now(gid, "session_removed")
                self.setup_tasks.discard(gid)
                self.pending_groups.pop(gid, None)
            return

    def _close_vm_block(self):
        """Mirrors the bot's VM-block close handler: correlate slot→hw, derive
        roster_updates, run the (placeholder-guarded) refresh."""
        items_to_remove = []
        roster_updates = {}
        for slot_id, data in self.current_vm_block.items():
            driver = data["driver"]
            car = data["car"]
            old_car = self.last_vm_state.get(slot_id, {}).get("car", "")

            hw_for_slot = self.current_slot_to_hw.get(slot_id)
            if hw_for_slot and driver:
                if self.current_group_roster.get(hw_for_slot) != driver:
                    self.current_group_roster[hw_for_slot] = driver
                    roster_updates[hw_for_slot] = driver
                continue

            if car and car != old_car:
                for hw, applied_car in self.active_car_applies:
                    if applied_car == car:
                        if self.current_group_roster.get(hw) != driver:
                            self.current_group_roster[hw] = driver
                            roster_updates[hw] = driver
                        items_to_remove.append((hw, applied_car))
                        break

        for item in items_to_remove:
            if item in self.active_car_applies:
                self.active_car_applies.remove(item)
        self.last_vm_state = dict(self.current_vm_block)

        if roster_updates:
            self.sim_refresh_nicknames(roster_updates)

    # ---- File / dir entrypoints ----

    def run_file(self, path):
        self.emit(f"=== Replaying {os.path.basename(path)} ===")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                self.process_line(line)

    def run_paths(self, paths):
        all_files = []
        for p in paths:
            if os.path.isdir(p):
                for name in sorted(os.listdir(p)):
                    if name.startswith("server-log") and name.endswith(".txt"):
                        all_files.append(os.path.join(p, name))
            elif os.path.isfile(p):
                all_files.append(p)
            else:
                print(f"WARN: skipping {p} (not found)", file=sys.stderr)

        for path in all_files:
            self.run_file(path)

        self.print_summary()

    def print_summary(self):
        print()
        print("=== Summary ===")
        for key in sorted(self.stats):
            print(f"  {key}: {self.stats[key]}")
        print(f"  active_groups (end of replay): {len(self.active_groups)}")
        if self.active_groups:
            for gid, d in self.active_groups.items():
                print(f"    {gid}: rigs={sorted(d['managed_rigs'])} roster={d['roster']}")


def main():
    # Windows consoles default to cp1252 and choke on the → arrow in log
    # messages; force utf-8 so output matches what the production bot emits.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="server log file(s) or directory containing server-log*.txt")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also print routed/staged/per-rig actions (default: only major events)")
    args = ap.parse_args()

    sim = Simulator(verbose=args.verbose)
    sim.run_paths(args.paths)


if __name__ == "__main__":
    main()
