"""Install/remove the current user's launchd schedule for a built Stock King.app."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--app", default="/Applications/Stock King.app")
parser.add_argument("--remove", action="store_true")
args = parser.parse_args()
if sys.platform != "darwin":
    raise SystemExit("Requires macOS")
label = "com.stockking.research"
domain = f"gui/{os.getuid()}"
agents = Path.home() / "Library/LaunchAgents"
slots = ("0920", "1030", "1455", "review", "weekly")
if args.remove:
    for suffix in ("",) + tuple(f".{slot}" for slot in slots):
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}{suffix}"], check=False)
        (agents / f"{label}{suffix}.plist").unlink(missing_ok=True)
    raise SystemExit(0)
app = Path(args.app).expanduser().resolve()
script = app / "Contents/Resources/macos-schedule.sh"
if not script.is_file() or not (app / "Contents/MacOS/Stock King").is_file():
    raise SystemExit("Build and install Stock King.app first")
logs = Path.home() / "Library/Logs/Stock King"
logs.mkdir(parents=True, exist_ok=True)
agents.mkdir(parents=True, exist_ok=True)
for slot in slots:
    task = f"{label}.{slot}"
    plist = agents / f"{task}.plist"
    payload = {"Label": task, "ProgramArguments": ["/bin/sh", str(script), str(app), slot],
               "StartInterval": 30, "RunAtLoad": True,
               "StandardOutPath": str(logs / f"schedule-{slot}.log"),
               "StandardErrorPath": str(logs / f"schedule-{slot}-error.log")}
    subprocess.run(["launchctl", "bootout", f"{domain}/{task}"], check=False)
    plist.write_bytes(plistlib.dumps(payload))
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
print("Installed: 09:20, 10:30, 14:55 picks; 15:30 review; Friday 15:45 learning (Shanghai time).")
