#!/usr/bin/env python3
"""
Schedule poller for QA test runner.
Checks qa-reports API for due schedules and kicks off run.py.

Designed to be called every 5 minutes by Windows Task Scheduler.
"""

import json
import logging
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from croniter import croniter

# ── Config ──────────────────────────────────────────────────────────────────
API_BASE = "https://qa-reports.vercel.app"
SCHEDULES_URL = f"{API_BASE}/api/schedules"
POLL_WINDOW_MINUTES = 5  # Must match Task Scheduler interval
RUN_PY = Path(__file__).parent / "run.py"
LOG_DIR = Path(__file__).parent / "logs"

# Device serial lookup - maps device model to ADB serial.
# The API returns model names (e.g. "SM-X610") but run.py needs serials.
# Update this when devices change in the lab.
DEVICE_SERIALS = {}
# Populated at startup by querying adb devices


# ── Logging ─────────────────────────────────────────────────────────────────
def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"poller_{datetime.now().strftime('%Y-%m-%d')}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("poller")


# ── Device Discovery ────────────────────────────────────────────────────────
def discover_devices():
    """Query adb devices and build model→serial map."""
    try:
        r = subprocess.run(
            ["adb", "devices", "-l"], capture_output=True, text=True, timeout=10
        )
        serials = {}
        for line in r.stdout.splitlines()[1:]:
            if "device" not in line or line.strip() == "":
                continue
            parts = line.split()
            serial = parts[0]
            model = None
            for p in parts:
                if p.startswith("model:"):
                    model = p.split(":", 1)[1]
                    break
            if model:
                # ADB returns underscores (SM_X610), API uses hyphens (SM-X610)
                # Store both forms so lookups work either way
                serials[model] = serial
                alt = model.replace("_", "-")
                if alt != model:
                    serials[alt] = serial
        return serials
    except Exception as e:
        logging.getLogger("poller").warning(f"Could not discover devices: {e}")
        return {}


# ── API ─────────────────────────────────────────────────────────────────────
def fetch_schedules(log):
    """GET /api/schedules → list of schedule dicts."""
    try:
        req = urllib.request.Request(SCHEDULES_URL)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        schedules = data.get("schedules", [])
        log.info(f"Fetched {len(schedules)} schedule(s) from API")
        return schedules
    except Exception as e:
        log.error(f"Failed to fetch schedules: {e}")
        return []


def update_last_run(schedule_id, log):
    """PUT /api/schedules/<id> to update last_run_at."""
    try:
        url = f"{API_BASE}/api/schedules/{schedule_id}"
        payload = json.dumps({
            "last_run_at": datetime.now(timezone.utc).isoformat()
        }).encode()
        req = urllib.request.Request(url, data=payload, method="PUT")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info(f"Updated last_run_at for schedule {schedule_id}: HTTP {resp.status}")
    except Exception as e:
        log.warning(f"Could not update last_run_at for schedule {schedule_id}: {e}")


# ── Cron Evaluation ─────────────────────────────────────────────────────────
def is_schedule_due(schedule, now, log):
    """Check if a schedule should fire right now."""
    cron_expr = schedule.get("cron_expression")
    if not cron_expr:
        return False

    name = schedule.get("name", f"id={schedule.get('id')}")
    last_run = schedule.get("last_run_at")

    try:
        # Find the most recent time this cron should have fired
        cron = croniter(cron_expr, now)
        prev_fire = cron.get_prev(datetime)

        # It's "due" if prev_fire is within our poll window
        seconds_ago = (now - prev_fire).total_seconds()
        if seconds_ago > POLL_WINDOW_MINUTES * 60:
            return False

        # Check last_run_at to avoid double-runs
        if last_run:
            if isinstance(last_run, str):
                last_run_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            else:
                last_run_dt = last_run

            # If we already ran after this fire time, skip
            if last_run_dt.replace(tzinfo=None) >= prev_fire.replace(tzinfo=None):
                log.info(f"  [{name}] Already ran at {last_run}, skipping")
                return False

        log.info(f"  [{name}] DUE! Cron '{cron_expr}' fired at {prev_fire}, {seconds_ago:.0f}s ago")
        return True

    except Exception as e:
        log.error(f"  [{name}] Cron eval error: {e}")
        return False


# ── Execution ───────────────────────────────────────────────────────────────
def run_schedule(schedule, device_serials, log):
    """Execute run.py for a due schedule."""
    name = schedule.get("name", "?")
    devices = schedule.get("devices", [])
    categories = schedule.get("categories", [])
    schedule_id = schedule.get("id")

    if not devices or not categories:
        log.warning(f"  [{name}] No devices or categories, skipping")
        return

    for device_model in devices:
        serial = device_serials.get(device_model)
        if not serial:
            log.warning(f"  [{name}] Device {device_model} not connected, skipping")
            continue

        # Build command
        cmd = [sys.executable, str(RUN_PY), "--device", serial, "--yes"]
        for cat in categories:
            cmd.extend(["-c", cat])

        log.info(f"  [{name}] Running: {device_model} ({serial}), categories: {categories}")
        log.info(f"  Command: {' '.join(cmd)}")

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=7200,  # 2 hour max per device
                cwd=str(RUN_PY.parent),
            )
            log.info(f"  [{name}] Finished with exit code {proc.returncode}")

            # Log last 10 lines of output
            output_lines = (proc.stdout or "").strip().splitlines()
            for line in output_lines[-10:]:
                log.info(f"    | {line}")

            if proc.returncode != 0 and proc.stderr:
                for line in proc.stderr.strip().splitlines()[-5:]:
                    log.error(f"    ERR | {line}")

        except subprocess.TimeoutExpired:
            log.error(f"  [{name}] TIMEOUT after 2 hours on {device_model}")
        except Exception as e:
            log.error(f"  [{name}] Execution error: {e}")

    # Update last_run_at after all devices done
    if schedule_id:
        update_last_run(schedule_id, log)


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    log = setup_logging()
    now = datetime.now()
    log.info(f"=== Poll check at {now.strftime('%Y-%m-%d %H:%M:%S')} ===")

    # Discover connected devices
    device_serials = discover_devices()
    if device_serials:
        log.info(f"Connected devices: {device_serials}")
    else:
        log.warning("No devices connected via ADB")

    # Fetch schedules
    schedules = fetch_schedules(log)
    if not schedules:
        log.info("No schedules found, exiting")
        return

    # Check each schedule
    due_count = 0
    for sched in schedules:
        if not sched.get("enabled", True):
            continue

        name = sched.get("name", f"id={sched.get('id')}")
        log.info(f"Checking: {name} (cron: {sched.get('cron_expression')})")

        if is_schedule_due(sched, now, log):
            due_count += 1
            run_schedule(sched, device_serials, log)

    if due_count == 0:
        log.info("No schedules due right now")
    else:
        log.info(f"Executed {due_count} schedule(s)")

    log.info("=== Done ===\n")


if __name__ == "__main__":
    main()
