"""
VPS Discord Bot — setup.py
Interactive first-run configurator.
Run: python3 setup.py
"""

import json
import os
import sys
import sqlite3
import getpass
import time
import subprocess
import re

CONFIG_FILE = "bot_config.json"
DB = "panel.db"

BOLD  = "\033[1m"
GREEN = "\033[32m"
CYAN  = "\033[36m"
WARN  = "\033[33m"
ERR   = "\033[31m"
RST   = "\033[0m"

def p(msg, color=""):    print(f"{color}{msg}{RST}")
def ok(msg):             p(f"  ✓  {msg}", GREEN)
def warn(msg):           p(f"  !  {msg}", WARN)
def err(msg):            p(f"  ✗  {msg}", ERR)
def banner(msg):         p(f"\n{'─'*54}\n  {msg}\n{'─'*54}", CYAN)
def ask(prompt, default=""):
    if default:
        raw = input(f"  {prompt} [{default}]: ").strip()
        return raw if raw else default
    return input(f"  {prompt}: ").strip()


def ask_int(prompt, default, lo, hi):
    while True:
        raw = ask(prompt, str(default))
        try:
            v = int(raw)
            if lo <= v <= hi:
                return v
            print(f"    Must be {lo}–{hi}")
        except ValueError:
            print("    Enter a number")


def load_existing_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def init_db():
    db = sqlite3.connect(DB)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS discord_users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id  TEXT UNIQUE NOT NULL,
        username    TEXT NOT NULL,
        joined_at   INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vps (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id      TEXT NOT NULL,
        container_id    TEXT NOT NULL,
        ssh_host        TEXT,
        ssh_port        INTEGER,
        ssh_password    TEXT,
        sshx_url        TEXT,
        pinggy_url      TEXT,
        status          TEXT DEFAULT 'creating',
        created_at      INTEGER NOT NULL,
        last_regen      INTEGER DEFAULT 0,
        kvm_enabled     INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS vps_requests (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id  TEXT NOT NULL UNIQUE,
        username    TEXT NOT NULL,
        note        TEXT DEFAULT '',
        status      TEXT DEFAULT 'pending',
        created_at  INTEGER NOT NULL,
        reviewed_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS panel_config (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    db.commit()
    db.close()


def set_db_config(key: str, value: str):
    db = sqlite3.connect(DB)
    db.execute(
        "INSERT INTO panel_config(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    db.commit()
    db.close()


def validate_discord_token(token: str) -> bool:
    """Basic format check for a Discord bot token."""
    # Discord tokens are roughly: [24chars].[6chars].[38chars] or newer format
    return len(token) > 50 and "." in token


def validate_discord_id(id_str: str) -> bool:
    """Discord IDs are 17–19 digit numbers."""
    return id_str.isdigit() and 17 <= len(id_str) <= 19


def run_systemd_service(bot_dir: str):
    """Create and enable a systemd service for the bot."""
    python_path = sys.executable
    service_content = f"""[Unit]
Description=VPS Discord Bot
After=network.target lxd.service
Wants=lxd.service

[Service]
Type=simple
User=root
WorkingDirectory={bot_dir}
ExecStart={python_path} -u bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""
    service_path = "/etc/systemd/system/vps-discord-bot.service"
    try:
        with open(service_path, "w") as f:
            f.write(service_content)
        subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
        subprocess.run(["systemctl", "enable", "vps-discord-bot"], check=True, capture_output=True)
        subprocess.run(["systemctl", "start", "vps-discord-bot"], check=True, capture_output=True)
        return True, service_path
    except Exception as e:
        return False, str(e)


def main():
    banner("VPS Discord Bot — First-Run Setup")
    p("This wizard configures your VPS hosting Discord bot.\n")

    existing = load_existing_config()
    config = dict(existing)  # Start from existing, overwrite with new answers

    # ── Bot identity ──────────────────────────────────────────────────────────
    banner("Bot Identity")

    bot_name = ask("Bot display name", existing.get("bot_name", "VPS Bot"))
    while not bot_name:
        bot_name = ask("Cannot be empty. Bot name")
    config["bot_name"] = bot_name

    owner_name = ask("Owner/provider name (shown in embeds)", existing.get("owner_name", "Admin"))
    config["owner_name"] = owner_name if owner_name else "Admin"

    logo_url = ask(
        "Bot logo URL (direct image URL for embed thumbnail, leave blank to skip)",
        existing.get("logo_url", "")
    )
    config["logo_url"] = logo_url

    # ── Discord token ─────────────────────────────────────────────────────────
    banner("Discord Bot Token")
    p("Get your token from https://discord.com/developers/applications")
    p("Create an application → Bot → Reset Token\n")

    if existing.get("discord_token"):
        p("  A token is already configured.")
        change = ask("Change it? [y/N]", "n").lower()
        if change in ("y", "yes"):
            token = getpass.getpass("  New Discord bot token: ").strip()
            while not validate_discord_token(token):
                err("Token looks invalid (must be 50+ characters with dots).")
                token = getpass.getpass("  Discord bot token: ").strip()
            config["discord_token"] = token
        # else keep existing
    else:
        token = getpass.getpass("  Discord bot token: ").strip()
        while not validate_discord_token(token):
            err("Token looks invalid (must be 50+ characters with dots).")
            token = getpass.getpass("  Discord bot token: ").strip()
        config["discord_token"] = token

    ok("Token saved.")

    # ── Admin Discord IDs ─────────────────────────────────────────────────────
    banner("Admin Discord User IDs")
    p("Admins can: view all VPS, delete, suspend, approve requests, grant VPS.")
    p("Get a Discord user's ID: Enable Developer Mode → Right-click user → Copy ID\n")

    if existing.get("admin_ids"):
        p(f"  Current admins: {existing['admin_ids']}")
        change = ask("Add more admins? [y/N]", "n").lower()
        if change in ("y", "yes"):
            new_ids = []
        else:
            new_ids = None
    else:
        new_ids = []

    if new_ids is not None:
        while True:
            admin_id = ask("Admin Discord user ID (blank to finish)")
            if not admin_id:
                break
            if validate_discord_id(admin_id):
                new_ids.append(admin_id)
                ok(f"Added admin: {admin_id}")
            else:
                warn("Invalid Discord ID. Must be 17–19 digits.")

        existing_admins = existing.get("admin_ids", [])
        config["admin_ids"] = list(set([str(i) for i in existing_admins] + new_ids))

    if not config.get("admin_ids"):
        warn("No admin IDs set. Admin commands will be inaccessible.")
    else:
        ok(f"Admin IDs: {config['admin_ids']}")

    # ── Provision mode ────────────────────────────────────────────────────────
    banner("VPS Provisioning Mode")
    p("  1) Self-service   — users run /vps create and get a VPS instantly")
    p("  2) Admin-provision — users request a VPS; admin approves via /admin approve\n")

    current_mode = existing.get("provision_mode", "self_service")
    while True:
        choice = ask(
            f"Choose [1/2] (current: {'1' if current_mode == 'self_service' else '2'})",
            "1" if current_mode == "self_service" else "2"
        )
        if choice == "1":
            config["provision_mode"] = "self_service"
            ok("Mode: Self-service (users get VPS instantly)")
            break
        elif choice == "2":
            config["provision_mode"] = "admin_provision"
            ok("Mode: Admin-provision (admin approves each request)")
            break
        else:
            warn("Enter 1 or 2")

    # ── Per-VPS resource limits ───────────────────────────────────────────────
    banner("Per-VPS Resource Limits")
    p("These limits apply to every VPS created on this node.\n")

    cpu  = ask_int(
        "CPU cores per VPS (1–32)",
        int(existing.get("vps_cpu_cores", 4)), 1, 32
    )
    ram  = ask_int(
        "RAM GB per VPS (1–256)",
        int(existing.get("vps_ram_gb", 4)), 1, 256
    )
    disk = ask_int(
        "Disk GB per VPS (5–2000)",
        int(existing.get("vps_disk_gb", 80)), 5, 2000
    )

    config["vps_cpu_cores"] = cpu
    config["vps_ram_gb"]    = ram
    config["vps_disk_gb"]   = disk
    ok(f"Each VPS: {cpu} vCPU / {ram}GB RAM / {disk}GB disk")

    # ── Pinggy token (optional) ───────────────────────────────────────────────
    banner("Pinggy Token (Optional)")
    p("Pinggy provides public SSH tunnels for each VPS (free tier available).")
    p("Sign up at https://pinggy.io for a persistent token.")
    p("Without a token, Pinggy free tunnels still work but URLs change on restart.\n")

    pinggy_token = ask(
        "Pinggy token (leave blank for anonymous free tier)",
        existing.get("pinggy_token", "")
    )
    config["pinggy_token"] = pinggy_token
    if pinggy_token:
        ok("Pinggy token saved.")
    else:
        p("  Using anonymous Pinggy tunnels (URL resets on each VPS restart).", WARN)

    # ── Write config ──────────────────────────────────────────────────────────
    banner("Saving Configuration")

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    ok(f"Config written to {CONFIG_FILE}")

    # ── Init DB ───────────────────────────────────────────────────────────────
    init_db()
    set_db_config("provision_mode", config["provision_mode"])
    set_db_config("vps_cpu_cores", str(config["vps_cpu_cores"]))
    set_db_config("vps_ram_gb",    str(config["vps_ram_gb"]))
    set_db_config("vps_disk_gb",   str(config["vps_disk_gb"]))
    ok("Database initialized.")

    # ── Systemd service ───────────────────────────────────────────────────────
    banner("Background Service")
    p("Install the bot as a systemd service so it restarts automatically?\n")

    bot_dir = os.path.dirname(os.path.abspath(__file__))
    install_service = ask("Install systemd service? [Y/n]", "y").lower()

    if install_service in ("", "y", "yes"):
        if os.geteuid() != 0:
            warn("Not running as root — skipping systemd setup.")
            warn("Run 'sudo python3 setup.py' or install the service manually.")
            p(f"\n  Manual install: copy the service file to /etc/systemd/system/", CYAN)
        else:
            ok_flag, result = run_systemd_service(bot_dir)
            if ok_flag:
                ok(f"Service installed and started: vps-discord-bot.service")
                ok("Check status: sudo systemctl status vps-discord-bot")
                ok("View logs: sudo journalctl -u vps-discord-bot -f")
            else:
                warn(f"Service install failed: {result}")
                warn("Start manually: python3 bot.py")
    else:
        p("\n  Run the bot manually: python3 bot.py", CYAN)

    # ── Done ──────────────────────────────────────────────────────────────────
    banner("Setup Complete!")
    ok(f"Bot name: {config['bot_name']}")
    ok(f"Owner: {config['owner_name']}")
    ok(f"Mode: {config['provision_mode']}")
    ok(f"Resources: {config['vps_cpu_cores']} vCPU / {config['vps_ram_gb']}GB RAM / {config['vps_disk_gb']}GB disk")
    ok(f"Admins: {len(config.get('admin_ids', []))} configured")

    p(f"\n{'─'*54}", CYAN)
    p("  Quick command reference:", BOLD)
    p("  /vps create       — Create a VPS (users)")
    p("  /vps status       — Check VPS + control buttons")
    p("  /vps credentials  — Get SSH credentials again")
    p("  /admin list       — View all VPS (admins)")
    p("  /admin requests   — Pending requests (admins)")
    p("  /admin approve    — Approve a request (admins)")
    p("  /admin grant      — Manually grant a VPS (admins)")
    p(f"{'─'*54}\n", CYAN)

    if install_service not in ("", "y", "yes") or os.geteuid() != 0:
        p("  To start the bot: python3 bot.py\n", GREEN)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Setup cancelled.")
        sys.exit(0)
