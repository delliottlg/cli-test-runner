#!/usr/bin/env python3
"""
Minimal CLI test runner for Lingraphica Unity tests.
Single file, zero dependencies beyond Python stdlib.

Usage:
    python3 run.py --device R5GYB3421PN --category Sanity --yes
    python3 run.py --list-devices
    python3 run.py --list-categories
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
UNITY = "/Applications/Unity/Hub/Editor/6000.3.2f1/Unity.app/Contents/MacOS/Unity"
PROJECT = "/Users/delliott/Documents/GitHub/lingraphica-app"
PKG = "com.UnityTestRunner.UnityTestRunner"
ACTIVITY = f"{PKG}/com.lingraphica.LGUnityPlayerActivity"
DEVICE_RESULTS = "/sdcard/lingraphica/TestResults"
CATEGORIES_FILE = Path(__file__).parent / "categories.txt"
POLL_INTERVAL = 10  # seconds
POLL_TIMEOUT = 1800  # 30 minutes

APK_PATHS = [
    f"{PROJECT}/PlayerWithTests.apk",
    f"{PROJECT}/Library/Bee/Android/Prj/IL2CPP/Gradle/launcher/build/outputs/apk/debug/launcher-debug.apk",
]

PERMISSIONS = [
    "android.permission.READ_EXTERNAL_STORAGE",
    "android.permission.WRITE_EXTERNAL_STORAGE",
    "android.permission.CAMERA",
    "android.permission.RECORD_AUDIO",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.ACCESS_BACKGROUND_LOCATION",
]


# ── Helpers ─────────────────────────────────────────────────────────────────
def adb(args, device=None, timeout=120):
    """Run an ADB command. Returns (returncode, stdout)."""
    cmd = ["adb"]
    if device:
        cmd += ["-s", device]
    cmd += args if isinstance(args, list) else args.split()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip()


def find_devices():
    """Return list of (serial, model) tuples for connected devices."""
    _, out = adb(["devices", "-l"])
    devices = []
    for line in out.splitlines()[1:]:
        if not line.strip() or "device" not in line:
            continue
        parts = line.split()
        serial = parts[0]
        model = "Unknown"
        for p in parts:
            if p.startswith("model:"):
                model = p.split(":", 1)[1].replace("_", " ")
        devices.append((serial, model))
    return devices


def load_categories():
    """Load test categories from categories.txt."""
    if not CATEGORIES_FILE.exists():
        print(f"ERROR: {CATEGORIES_FILE} not found")
        return []
    return [l.strip() for l in CATEGORIES_FILE.read_text().splitlines() if l.strip()]


def find_apk():
    """Find the built APK, checking known paths."""
    for p in APK_PATHS:
        if os.path.isfile(p):
            return p
    return None


# ── 6-Step Workflow ─────────────────────────────────────────────────────────
def step_build(category):
    """Step 1: Build test APK via Unity batch mode."""
    print(f"\n[1/6] Building APK for {category}...")
    apk_out = APK_PATHS[0]  # PlayerWithTests.apk

    # Remove old APK
    if os.path.isfile(apk_out):
        os.remove(apk_out)

    cmd = [
        UNITY, "-batchmode", "-nographics",
        "-projectPath", PROJECT,
        "-buildTarget", "Android",
        "-runTests", "-testPlatform", "Android",
        "-testCategory", category,
        "-logFile", "-",
    ]
    print(f"  Running Unity... (this takes a while)")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

    # Unity puts APK in Gradle output, copy to project root
    gradle_apk = APK_PATHS[1]
    if os.path.isfile(gradle_apk):
        shutil.copy2(gradle_apk, apk_out)
        size_mb = os.path.getsize(apk_out) / (1024 * 1024)
        print(f"  APK built: {size_mb:.0f} MB")
        return apk_out

    # Check if it ended up at the expected root path directly
    if os.path.isfile(apk_out):
        size_mb = os.path.getsize(apk_out) / (1024 * 1024)
        print(f"  APK built: {size_mb:.0f} MB")
        return apk_out

    print(f"  ERROR: APK not found after build")
    if proc.returncode != 0:
        # Show last 20 lines of output for debugging
        lines = (proc.stdout or proc.stderr or "").splitlines()
        for line in lines[-20:]:
            print(f"  | {line}")
    return None


def step_uninstall(device):
    """Step 2: Uninstall old APK."""
    print(f"\n[2/6] Uninstalling old APK...")
    rc, out = adb(["uninstall", PKG], device)
    if rc == 0:
        print(f"  Uninstalled")
    else:
        print(f"  No previous install (ok)")


def step_install(device, apk_path):
    """Step 3: Install APK on device."""
    print(f"\n[3/6] Installing APK...")
    rc, out = adb(["install", apk_path], device, timeout=300)
    if rc != 0 or "Success" not in out:
        print(f"  ERROR: Install failed: {out}")
        return False
    print(f"  Installed successfully")
    return True


def step_permissions(device):
    """Step 4: Grant permissions (Android 16+ compatible)."""
    print(f"\n[4/6] Granting permissions...")

    # MANAGE_EXTERNAL_STORAGE via appops (Android 16+ requirement)
    rc, _ = adb(["shell", "appops", "set", PKG, "MANAGE_EXTERNAL_STORAGE", "allow"], device)
    if rc == 0:
        print(f"  MANAGE_EXTERNAL_STORAGE: ok (appops)")

    # Standard runtime permissions
    for perm in PERMISSIONS:
        rc, out = adb(["shell", "pm", "grant", PKG, perm], device)
        name = perm.split(".")[-1]
        if rc == 0:
            print(f"  {name}: ok")
        else:
            print(f"  {name}: failed ({out[:60]})")


def step_launch(device):
    """Step 5: Launch test app."""
    print(f"\n[5/6] Launching tests...")
    rc, out = adb(
        ["shell", "am", "start", "-W", "-S", "--activity-clear-top", "-n", ACTIVITY],
        device,
    )
    if rc != 0 or "Error" in out:
        print(f"  ERROR: Launch failed: {out}")
        return False
    print(f"  App launched")
    return True


def step_poll_and_pull(device, timeout=POLL_TIMEOUT):
    """Step 6: Poll for results, pull XML when ready."""
    print(f"\n[6/6] Waiting for test results...")
    start = time.time()

    while time.time() - start < timeout:
        rc, out = adb(["shell", "ls", f"{DEVICE_RESULTS}/"], device)
        if rc == 0 and ".xml" in out:
            xml_files = [f for f in out.splitlines() if f.endswith(".xml")]
            if xml_files:
                elapsed = int(time.time() - start)
                print(f"  Results found after {elapsed}s: {', '.join(xml_files)}")

                # Pull results
                local_dir = Path(__file__).parent / "results" / datetime.now().strftime("%Y-%m-%d")
                local_dir.mkdir(parents=True, exist_ok=True)
                adb(["pull", f"{DEVICE_RESULTS}/", str(local_dir)], device)

                # Return first XML file path
                return local_dir / xml_files[0]

        elapsed = int(time.time() - start)
        mins, secs = divmod(elapsed, 60)
        print(f"  Polling... ({mins}m {secs}s)", end="\r")
        time.sleep(POLL_INTERVAL)

    print(f"\n  TIMEOUT: No results after {timeout}s")
    return None


# ── Results Parser ──────────────────────────────────────────────────────────
def parse_results(xml_path):
    """Parse NUnit XML results. Returns dict with counts and failure details."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    total = int(root.get("total", 0))
    passed = int(root.get("passed", 0))
    failed = int(root.get("failed", 0))
    skipped = int(root.get("skipped", root.get("inconclusive", "0")))
    duration = float(root.get("duration", 0))

    failures = []
    for tc in root.iter("test-case"):
        if tc.get("result") == "Failed":
            name = tc.get("name", "?")
            msg_el = tc.find(".//message")
            msg = (msg_el.text or "")[:120] if msg_el is not None else ""
            failures.append((name, msg))

    # Print summary
    print(f"\n{'='*60}")
    print(f"RESULTS: {passed}/{total} passed, {failed} failed, {skipped} skipped")
    print(f"DURATION: {duration:.0f}s ({duration/60:.1f} min)")
    print(f"FILE: {xml_path}")

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for name, msg in failures:
            print(f"  FAIL: {name}")
            if msg:
                print(f"        {msg}")

    print(f"{'='*60}")

    return {"total": total, "passed": passed, "failed": failed, "skipped": skipped,
            "duration": duration, "failures": failures}


# ── Interactive Helpers ─────────────────────────────────────────────────────
def pick_device(devices):
    """Interactive device picker."""
    if len(devices) == 1:
        s, m = devices[0]
        print(f"Using device: {m} ({s})")
        return s
    print("\nDevices:")
    for i, (s, m) in enumerate(devices, 1):
        print(f"  {i}. {m} ({s})")
    while True:
        try:
            idx = int(input("Pick device: ")) - 1
            if 0 <= idx < len(devices):
                return devices[idx][0]
        except (ValueError, KeyboardInterrupt):
            return None


def pick_categories(categories):
    """Interactive category picker."""
    print("\nCategories:")
    print("  0. Sanity (default)")
    for i, c in enumerate(categories, 1):
        print(f"  {i}. {c}")
    try:
        raw = input("Pick numbers (comma-separated) or Enter for Sanity: ").strip()
        if not raw:
            return ["Sanity"]
        return [categories[int(n.strip()) - 1] for n in raw.split(",")
                if 0 < int(n.strip()) <= len(categories)]
    except (ValueError, KeyboardInterrupt, IndexError):
        return None


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Minimal Unity test runner for Android devices")
    p.add_argument("--device", "-d", help="Device serial (from adb devices)")
    p.add_argument("--category", "-c", action="append", help="Test category (repeatable)")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    p.add_argument("--skip-build", action="store_true", help="Use existing APK, skip Unity build")
    p.add_argument("--deploy-only", action="store_true", help="Deploy + run existing APK (no build)")
    p.add_argument("--list-devices", action="store_true", help="List connected devices")
    p.add_argument("--list-categories", action="store_true", help="List test categories")
    p.add_argument("--timeout", type=int, default=POLL_TIMEOUT, help="Result poll timeout in seconds")
    args = p.parse_args()

    # List commands
    if args.list_devices:
        for s, m in find_devices():
            print(f"{s}\t{m}")
        return 0

    if args.list_categories:
        for c in load_categories():
            print(c)
        return 0

    # Device selection
    devices = find_devices()
    if not devices:
        print("ERROR: No devices connected. Check USB and run: adb kill-server && adb start-server")
        return 1

    device = args.device
    if device:
        if not any(s == device for s, _ in devices):
            print(f"ERROR: Device {device} not found. Connected: {[s for s,_ in devices]}")
            return 1
    else:
        device = pick_device(devices)
        if not device:
            return 1

    device_model = next((m for s, m in devices if s == device), "Unknown")

    # Category selection
    categories = args.category or (None if not args.deploy_only else ["Sanity"])
    if not categories:
        cats = load_categories()
        categories = pick_categories(cats)
        if not categories:
            return 1

    # Confirmation
    if not args.yes:
        print(f"\nDevice: {device_model} ({device})")
        print(f"Categories: {', '.join(categories)}")
        print(f"Build: {'skip' if args.skip_build or args.deploy_only else 'yes'}")
        if input("Run? [Y/n] ").strip().lower() not in ("", "y"):
            return 1

    # Run each category
    poll_timeout = args.timeout
    all_results = {}
    for cat in categories:
        print(f"\n{'#'*60}")
        print(f"# {cat}")
        print(f"{'#'*60}")

        # Step 1: Build (unless skipped)
        apk = None
        if not args.skip_build and not args.deploy_only:
            apk = step_build(cat)
            if not apk:
                all_results[cat] = {"error": "Build failed"}
                continue
        else:
            apk = find_apk()
            if not apk:
                print("ERROR: No APK found. Run without --skip-build first.")
                all_results[cat] = {"error": "No APK"}
                continue
            print(f"\nUsing existing APK: {apk}")

        # Steps 2-5: Deploy and launch
        step_uninstall(device)
        if not step_install(device, apk):
            all_results[cat] = {"error": "Install failed"}
            continue
        step_permissions(device)
        if not step_launch(device):
            all_results[cat] = {"error": "Launch failed"}
            continue

        # Step 6: Poll and parse
        xml_path = step_poll_and_pull(device, timeout=poll_timeout)
        if xml_path and xml_path.exists():
            all_results[cat] = parse_results(xml_path)
        else:
            all_results[cat] = {"error": "No results (timeout)"}

    # Final summary
    if len(categories) > 1:
        print(f"\n{'='*60}")
        print("SUMMARY")
        for cat, r in all_results.items():
            if "error" in r:
                print(f"  {cat}: ERROR - {r['error']}")
            else:
                print(f"  {cat}: {r['passed']}/{r['total']} passed")
        print(f"{'='*60}")

    # Exit code: 0 if all categories had zero failures
    has_failures = any(
        r.get("failed", 0) > 0 or "error" in r for r in all_results.values()
    )
    return 1 if has_failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)
