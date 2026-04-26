#!/usr/bin/env python3
"""
Cursor ID Reset Daemon + --revert (preserve IDs)
- --revert : Restore all storage.json settings from latest backup, EXCEPT the 4 telemetry IDs.
- Normal start: tray icon + auto‑reset daemon.
"""

import os
import sys
import json
import uuid
import shutil
import hashlib
import logging
import platform
import threading
import tempfile
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

import pystray
from PIL import Image, ImageDraw

# ----------------------------------------------------------------------
#  Configuration
# ----------------------------------------------------------------------
CONFIG_FILE = Path.home() / ".cursor_reset_daemon.json"
LOG_FILE = Path(tempfile.gettempdir()) / "cursor_reset_daemon.log"
RESET_INTERVAL_MINUTES = 30
LOCK_FILE = Path(tempfile.gettempdir()) / "cursor_reset_daemon.lock"

# ----------------------------------------------------------------------
#  Logging
# ----------------------------------------------------------------------
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filemode="a",
)

def log_info(msg: str) -> None:
    print(f"[INFO] {msg}")
    logging.info(msg)

def log_error(msg: str) -> None:
    print(f"[ERROR] {msg}")
    logging.error(msg)

# ----------------------------------------------------------------------
#  Single instance lock
# ----------------------------------------------------------------------
def acquire_lock() -> bool:
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(LOCK_FILE, flags)
        with os.fdopen(fd, 'w') as f:
            f.write(str(os.getpid()))
        return True
    except FileExistsError:
        try:
            with open(LOCK_FILE, 'r') as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            log_info(f"Another instance is running (PID {pid}), exiting.")
            return False
        except (OSError, ValueError, ProcessLookupError):
            LOCK_FILE.unlink(missing_ok=True)
            return acquire_lock()
    except Exception as e:
        log_error(f"Lock acquisition failed: {e}")
        return False

def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception as e:
        log_error(f"Failed to remove lock file: {e}")

# ----------------------------------------------------------------------
#  Cursor storage path detection
# ----------------------------------------------------------------------
def get_storage_path() -> Path:
    system = platform.system()
    try:
        if system == "Windows":
            base_path = os.getenv("APPDATA")
        elif system == "Darwin":
            base_path = str(Path.home() / "Library/Application Support")
        else:
            base_path = str(Path.home() / ".config")
        storage_path = Path(base_path) / "Cursor/User/globalStorage/storage.json"
        if not storage_path.exists():
            raise FileNotFoundError(f"storage.json not found at {storage_path}")
        return storage_path
    except Exception as e:
        raise RuntimeError(f"Failed to locate storage.json: {e}")

# ----------------------------------------------------------------------
#  Backup & ID generation (original logic)
# ----------------------------------------------------------------------
def create_backup(storage_path: Path, suffix: str = "") -> Path:
    """Create a timestamped backup, return its path."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if suffix:
        backup_path = storage_path.parent / f"storage.json.backup_{timestamp}_{suffix}"
    else:
        backup_path = storage_path.parent / f"storage.json.backup_{timestamp}"
    shutil.copy2(storage_path, backup_path)
    log_info(f"Backup created: {backup_path}")
    return backup_path

def generate_new_ids() -> Dict[str, str]:
    return {
        "telemetry.machineId": hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest(),
        "telemetry.macMachineId": hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest(),
        "telemetry.devDeviceId": str(uuid.uuid4()),
        "telemetry.sqmId": f"{{{uuid.uuid4()}}}",
    }

def reset_cursor_ids(storage_path: Path) -> bool:
    try:
        with open(storage_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        create_backup(storage_path)
        new_ids = generate_new_ids()
        data.update(new_ids)
        with open(storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log_info("Cursor IDs reset successfully.")
        for key in new_ids:
            log_info(f"  Updated {key}")
        return True
    except Exception as e:
        log_error(f"Failed to reset IDs: {e}")
        return False

# ----------------------------------------------------------------------
#  NEW: Revert except IDs
# ----------------------------------------------------------------------
def revert_except_ids(storage_path: Path) -> bool:
    """
    Restore all keys from the latest backup EXCEPT the four telemetry IDs.
    Current IDs are preserved.
    """
    # Find the most recent backup file
    backup_dir = storage_path.parent
    backups = sorted(backup_dir.glob("storage.json.backup_*"), key=os.path.getmtime, reverse=True)
    if not backups:
        log_error("No backup found. Nothing to revert.")
        return False

    latest_backup = backups[0]
    log_info(f"Using latest backup: {latest_backup}")

    try:
        # Read current storage.json
        with open(storage_path, "r", encoding="utf-8") as f:
            current_data = json.load(f)

        # Read backup
        with open(latest_backup, "r", encoding="utf-8") as f:
            backup_data = json.load(f)

        # Preserve the current telemetry IDs
        preserved_ids = {
            "telemetry.machineId": current_data.get("telemetry.machineId"),
            "telemetry.macMachineId": current_data.get("telemetry.macMachineId"),
            "telemetry.devDeviceId": current_data.get("telemetry.devDeviceId"),
            "telemetry.sqmId": current_data.get("telemetry.sqmId"),
        }

        # Merge: start with backup data, then overwrite with preserved IDs
        merged_data = backup_data.copy()
        for key, value in preserved_ids.items():
            if value is not None:
                merged_data[key] = value

        # Create a backup of the current state BEFORE reverting
        create_backup(storage_path, suffix="before_revert")

        # Write merged data
        with open(storage_path, "w", encoding="utf-8") as f:
            json.dump(merged_data, f, indent=2)

        log_info("Revert completed: all settings restored from backup except telemetry IDs.")
        log_info("Current IDs remain unchanged.")
        return True

    except Exception as e:
        log_error(f"Failed to revert: {e}")
        return False

# ----------------------------------------------------------------------
#  Config management
# ----------------------------------------------------------------------
def load_config() -> Dict[str, Any]:
    default = {"enabled": True, "interval_minutes": RESET_INTERVAL_MINUTES}
    if not CONFIG_FILE.exists():
        return default
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        log_error(f"Failed to load config: {e}")
        return default

def save_config(config: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        log_error(f"Failed to save config: {e}")

# ----------------------------------------------------------------------
#  Tray icon
# ----------------------------------------------------------------------
def create_icon_image(color: str) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=color, outline="black", width=2)
    return img

# ----------------------------------------------------------------------
#  Startup integration
# ----------------------------------------------------------------------
def add_to_startup() -> None:
    script_path = Path(sys.argv[0]).resolve()
    system = platform.system()
    try:
        if system == "Windows":
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            )
            winreg.SetValueEx(key, "CursorResetDaemon", 0, winreg.REG_SZ, str(script_path))
            winreg.CloseKey(key)
            log_info("Added to Windows startup.")
        elif system == "Darwin":
            plist_dir = Path.home() / "Library/LaunchAgents"
            plist_dir.mkdir(parents=True, exist_ok=True)
            plist_path = plist_dir / "com.user.cursorreset.plist"
            plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.cursorreset</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{script_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>"""
            plist_path.write_text(plist_content, encoding="utf-8")
            os.system(f"launchctl load {plist_path}")
            log_info("Added to macOS startup.")
        else:
            autostart_dir = Path.home() / ".config/autostart"
            autostart_dir.mkdir(parents=True, exist_ok=True)
            desktop_path = autostart_dir / "cursor-reset-daemon.desktop"
            desktop_content = f"""[Desktop Entry]
Type=Application
Name=Cursor Reset Daemon
Exec={sys.executable} {script_path}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
"""
            desktop_path.write_text(desktop_content, encoding="utf-8")
            os.chmod(desktop_path, 0o755)
            log_info("Added to Linux startup.")
    except Exception as e:
        log_error(f"Failed to add to startup: {e}")

# ----------------------------------------------------------------------
#  Daemon class (tray + scheduler)
# ----------------------------------------------------------------------
class CursorResetDaemon:
    def __init__(self):
        self.config = load_config()
        self.enabled = self.config.get("enabled", True)
        self.interval_minutes = self.config.get("interval_minutes", RESET_INTERVAL_MINUTES)
        self.timer: Optional[threading.Timer] = None
        self.icon: Optional[pystray.Icon] = None
        self.storage_path: Optional[Path] = None
        try:
            self.storage_path = get_storage_path()
            log_info(f"Storage file found: {self.storage_path}")
        except Exception as e:
            log_error(f"Cannot find Cursor storage.json: {e}")

    def _run_reset(self) -> None:
        if not self.enabled:
            return
        if self.storage_path is None:
            log_error("Cannot reset: storage.json path unknown.")
            return
        log_info("Performing scheduled reset...")
        reset_cursor_ids(self.storage_path)
        if self.enabled:
            self._schedule_reset()

    def _schedule_reset(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
        interval_seconds = self.interval_minutes * 60
        self.timer = threading.Timer(interval_seconds, self._run_reset)
        self.timer.daemon = True
        self.timer.start()
        log_info(f"Next reset scheduled in {self.interval_minutes} minute(s).")

    def stop_scheduler(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def set_enabled(self, enabled: bool) -> None:
        if self.enabled == enabled:
            return
        self.enabled = enabled
        self.config["enabled"] = enabled
        save_config(self.config)
        if enabled:
            log_info("Auto-reset ENABLED.")
            if self.storage_path:
                reset_cursor_ids(self.storage_path)
            self._schedule_reset()
        else:
            log_info("Auto-reset DISABLED.")
            self.stop_scheduler()
        if self.icon:
            new_color = "green" if self.enabled else "red"
            self.icon.icon = create_icon_image(new_color)

    def reset_now(self) -> None:
        if self.storage_path is None:
            log_error("Cannot reset: storage.json not found.")
            return
        log_info("Manual reset triggered.")
        reset_cursor_ids(self.storage_path)

    def quit(self) -> None:
        log_info("Shutting down.")
        self.stop_scheduler()
        if self.icon:
            self.icon.stop()
        release_lock()
        sys.exit(0)

    def run(self) -> None:
        if self.storage_path is None:
            tooltip = "Cursor storage.json not found! Check installation."
        else:
            tooltip = f"Cursor Reset Daemon\nAuto-reset: {'ON' if self.enabled else 'OFF'}\nInterval: {self.interval_minutes} min"
        icon_color = "green" if self.enabled else "red"
        image = create_icon_image(icon_color)

        menu = pystray.Menu(
            pystray.MenuItem(
                f"Auto-reset (now {'ON' if self.enabled else 'OFF'})",
                lambda icon, item: self.set_enabled(not self.enabled),
                checked=lambda item: self.enabled,
            ),
            pystray.MenuItem("Reset Now", lambda icon, item: self.reset_now()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda icon, item: self.quit()),
        )

        self.icon = pystray.Icon("cursor_reset_daemon", image, tooltip, menu)
        if self.enabled:
            self._schedule_reset()
        self.icon.run()

# ----------------------------------------------------------------------
#  Main entry point with --revert support
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Cursor ID Reset Daemon")
    parser.add_argument("--revert", action="store_true", help="Revert storage.json from latest backup, preserving current telemetry IDs")
    args = parser.parse_args()

    if args.revert:
        # Revert mode: no daemon, no tray, no lock needed (single action)
        print("Revert mode: restoring all settings except telemetry IDs...")
        try:
            storage_path = get_storage_path()
            if revert_except_ids(storage_path):
                print("✅ Revert completed successfully.")
            else:
                print("❌ Revert failed (check log for details).")
        except Exception as e:
            print(f"❌ Error: {e}")
        sys.exit(0)

    # Normal daemon mode
    if not acquire_lock():
        sys.exit(1)
    add_to_startup()
    daemon = CursorResetDaemon()
    try:
        daemon.run()
    except KeyboardInterrupt:
        daemon.quit()
    except Exception as e:
        log_error(f"Unhandled exception: {e}")
        release_lock()
        sys.exit(1)

if __name__ == "__main__":
    main()
