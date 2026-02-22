# cli-test-runner

Minimal CLI tool for running Lingraphica Unity tests on Android devices. Single Python file, zero dependencies.

## Quick Start

```bash
# List connected devices
python3 run.py --list-devices

# List test categories
python3 run.py --list-categories

# Run Sanity tests (builds APK, deploys, runs, pulls results)
python3 run.py --device R5GYB3421PN --category Sanity --yes

# Skip build (reuse existing APK)
python3 run.py --device R5GYB3421PN --category Sanity --skip-build --yes

# Deploy and run only (no build)
python3 run.py --device R5GYB3421PN --deploy-only --yes

# Multiple categories
python3 run.py -d R5GYB3421PN -c Login_Section -c Dashboard_Section -y
```

## What It Does (6 steps)

1. **Build** — Calls Unity batch mode to build test APK with baked-in category
2. **Uninstall** — Removes old APK from device
3. **Install** — Pushes new APK to device
4. **Permissions** — Grants storage, camera, audio (uses `appops` for Android 16+)
5. **Launch** — Starts test activity on device
6. **Poll & Pull** — Watches for XML results, pulls when done, parses and prints summary

## Exit Codes
- `0` — All tests passed
- `1` — Failures or errors
- `130` — Interrupted (Ctrl+C)

## Output
Plain text to stdout. Results are NUnit XML saved to `results/YYYY-MM-DD/`.

## Prerequisites
- Python 3.9+
- ADB (`brew install android-platform-tools`)
- Unity 6000.3.2f1 at `/Applications/Unity/Hub/Editor/6000.3.2f1/`
- Unity project at `/Users/delliott/Documents/GitHub/lingraphica-app`
- Device with USB debugging enabled, test_account.txt and azure.txt on device

## Config
All constants are at the top of `run.py`. Edit UNITY, PROJECT, PKG paths there.
