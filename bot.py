"""
VPS Discord Bot — bot.py
Full VPS hosting bot: LXD containers + Pinggy SSH tunnels.
Credentials DM'd to users. Admin provisioning + self-service modes.
"""

import os
import re
import time
import sqlite3
import secrets
import string
import asyncio
import threading
import subprocess
import json
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands

from vps import (
    create_vps_container,
    destroy_vps,
    suspend_vps,
    unsuspend_vps,
    start_vps,
    stop_vps,
    reinstall_vps,
    get_container_stats,
    can_create_vps,
    can_allocate_disk,
    get_free_vps_cpu,
    get_free_vps_ram,
    get_free_vps_disk,
    kvm_available,
    exec_command,
    regen_sshx,
    sync_status,
)
from monitor import start_monitor
import queue_manager as queue_mgr

DB = "panel.db"
CONFIG_FILE = "bot_config.json"


# ── Config loader ────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        raise RuntimeError(f"{CONFIG_FILE} not found. Run python3 setup.py first.")
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
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


def get_panel_config() -> dict:
    db = get_db()
    cfg = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM panel_config").fetchall()}
    db.close()
    return cfg


def set_panel_config(key: str, value: str):
    db = get_db()
    db.execute(
        "INSERT INTO panel_config(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    db.commit()
    db.close()


def get_provision_mode() -> str:
    return get_panel_config().get("provision_mode", "self_service")


def is_admin_discord(discord_id: str) -> bool:
    cfg = load_config()
    admins = cfg.get("admin_ids", [])
    return str(discord_id) in [str(a) for a in admins]


# ── Pinggy SSH tunnel ─────────────────────────────────────────────────────────

class PinggyTunnel:
    """
    Launches a Pinggy reverse SSH tunnel inside a container.
    Public SSH access via: ssh root@HOST -p PORT

    Pinggy prints forwarded addresses like:
      tcp://rndfoo.a.pinggy.link:PORT
    or in older output:
      Forwarding TCP connections from tcp://HOST:PORT
    or just:
      HOST:PORT
    We match all three patterns.
    """

    # All known Pinggy URL patterns
    _URL_PATTERNS = [
        # tcp://something.a.pinggy.link:12345
        re.compile(r"tcp://([a-zA-Z0-9.\-]+\.pinggy\.(?:link|io)):(\d+)", re.IGNORECASE),
        # tcp://something.a.pinggy.online:12345  (older domains)
        re.compile(r"tcp://([a-zA-Z0-9.\-]+):(\d{4,5})", re.IGNORECASE),
        # Forwarding TCP connections from tcp://HOST:PORT
        re.compile(r"[Ff]orwarding.*?([a-zA-Z0-9.\-]+):(\d{4,5})", re.IGNORECASE),
        # bare HOST:PORT on its own line
        re.compile(r"^([a-zA-Z0-9.\-]+\.(?:pinggy\.link|pinggy\.io|pinggy\.online)):(\d{4,5})$",
                   re.MULTILINE | re.IGNORECASE),
    ]

    @staticmethod
    def _parse_url(log: str) -> tuple[str, int] | None:
        for pattern in PinggyTunnel._URL_PATTERNS:
            m = pattern.search(log)
            if m:
                host = m.group(1).strip()
                port = int(m.group(2).strip())
                if 1024 <= port <= 65535:
                    return host, port
        return None

    @staticmethod
    def install_and_start(container_id: str, pinggy_token: str = None) -> dict:
        """
        Ensures openssh-server + ssh client are installed, starts Pinggy tunnel.
        Returns {"host": str, "port": int, "url": str, "ssh_command": str} or {"error": str}
        """
        from vps import _get_container, _exec, _apt_install_with_retry, _CONTAINER_ENV, _fix_container_dns

        inst = _get_container(container_id)
        if not inst:
            return {"error": "Container not found"}

        env = _CONTAINER_ENV

        # ── Fix DNS before any network call ──────────────────────────────────
        _fix_container_dns(inst)

        # ── Ensure openssh-server is running ─────────────────────────────────
        sshd_check = _exec(inst, ["bash", "-c",
                                   "command -v sshd && echo PRESENT || echo MISSING"],
                           environment=env)
        if "MISSING" in (sshd_check.stdout or ""):
            print("[pinggy] installing openssh-server...")
            _apt_install_with_retry(inst, "openssh-server", env)

        # Start sshd regardless — idempotent
        _exec(inst, ["bash", "-c",
                     "service ssh start 2>/dev/null || systemctl start ssh 2>/dev/null || true"],
              environment=env)

        # Verify sshd is actually listening on port 22
        for _ in range(10):
            check = _exec(inst, ["bash", "-c", "ss -tnlp | grep ':22' || netstat -tnlp 2>/dev/null | grep ':22'"],
                          environment=env)
            if check.stdout and ":22" in check.stdout:
                break
            time.sleep(1)
        else:
            print("[pinggy] WARNING: sshd may not be listening on port 22")

        # ── Ensure openssh-client (ssh binary) ───────────────────────────────
        ssh_check = _exec(inst, ["bash", "-c", "command -v ssh && echo PRESENT || echo MISSING"],
                          environment=env)
        if "MISSING" in (ssh_check.stdout or ""):
            print("[pinggy] installing openssh-client...")
            _apt_install_with_retry(inst, "openssh-client", env)

        # ── Kill any existing Pinggy tunnel ───────────────────────────────────
        _exec(inst, ["bash", "-c",
                     "pkill -f 'a.pinggy.io' 2>/dev/null; "
                     "pkill -f 'pinggy' 2>/dev/null; "
                     "rm -f /tmp/pinggy.log; sleep 1; true"],
              environment=env)

        # ── Build Pinggy SSH command ──────────────────────────────────────────
        if pinggy_token:
            remote_target = f"{pinggy_token}+tcp@a.pinggy.io"
        else:
            remote_target = "tcp@a.pinggy.io"

        tunnel_cmd = (
            f"nohup ssh -p 443 "
            f"-R0:localhost:22 "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"-o ServerAliveInterval=25 "
            f"-o ServerAliveCountMax=5 "
            f"-o ExitOnForwardFailure=yes "
            f"-o LogLevel=VERBOSE "
            f"-o BatchMode=yes "
            f"{remote_target} "
            f"> /tmp/pinggy.log 2>&1 & echo $!"
        )

        launch = _exec(inst, ["bash", "-c", tunnel_cmd], environment=env)
        pid = (launch.stdout or "").strip()
        print(f"[pinggy] tunnel launched pid={pid}")

        # ── Wait for Pinggy to print the forwarded address ───────────────────
        for tick in range(30):
            time.sleep(1)
            log_r = _exec(inst, ["bash", "-c", "cat /tmp/pinggy.log 2>/dev/null || true"],
                          environment=env)
            log = log_r.stdout or ""

            if log.strip():
                result = PinggyTunnel._parse_url(log)
                if result:
                    host, port = result
                    url = f"tcp://{host}:{port}"
                    ssh_command = f"ssh root@{host} -p {port}"
                    print(f"[pinggy] tunnel ready after {tick+1}s: {ssh_command}")
                    return {
                        "host": host,
                        "port": port,
                        "url": url,
                        "ssh_command": ssh_command,
                    }

                # Print log every 5s for debugging
                if (tick + 1) % 5 == 0:
                    print(f"[pinggy] waiting {tick+1}s, log tail: {log.strip()[-400:]}")

        # Last-ditch: dump the full log so we can debug
        final_log = _exec(inst, ["bash", "-c", "cat /tmp/pinggy.log 2>/dev/null || true"],
                          environment=env)
        print(f"[pinggy] TIMEOUT — full log:\n{(final_log.stdout or '(empty)')[:1000]}")
        return {"error": "Pinggy tunnel timed out — no forwarded address found in 30s"}

    @staticmethod
    def regen_tunnel(container_id: str, pinggy_token: str = None) -> dict:
        return PinggyTunnel.install_and_start(container_id, pinggy_token)


# ── Password change helper ────────────────────────────────────────────────────

def change_vps_password(container_id: str, new_password: str) -> bool:
    from vps import _get_container, _exec
    inst = _get_container(container_id)
    if not inst:
        return False
    result = _exec(inst, ["bash", "-c", f"echo 'root:{new_password}' | chpasswd"])
    return result.exit_code == 0


# ── VPS build workers ─────────────────────────────────────────────────────────

def _build_vps(vps_id: int, discord_id: str):
    """Queue worker: creates container + Pinggy tunnel, DMs credentials."""
    db = get_db()
    db.execute("UPDATE vps SET status='creating' WHERE id=?", (vps_id,))
    db.commit()
    db.close()

    cfg = load_config()
    pinggy_token = cfg.get("pinggy_token", "")

    try:
        cid, sshx_url = create_vps_container(f"vps-{discord_id[:8]}")
        from vps import _create_raw, get_free_vps_cpu, get_free_vps_ram, get_free_vps_disk
        # Pull the password from the container config (stored during _create_raw)
        # We re-generate and set it here for reliability
        from vps import generate_password, _get_container, _set_root_password_with_retry
        inst = _get_container(cid)
        password = generate_password(16)
        _set_root_password_with_retry(inst, password)

        # Launch Pinggy tunnel
        tunnel = PinggyTunnel.install_and_start(cid, pinggy_token or None)

        ssh_host = tunnel.get("host", "")
        ssh_port = tunnel.get("port", 0)
        pinggy_url = tunnel.get("url", "")
        tunnel_error = tunnel.get("error", "")

        db = get_db()
        db.execute(
            "UPDATE vps SET container_id=?,ssh_host=?,ssh_port=?,ssh_password=?,"
            "sshx_url=?,pinggy_url=?,status='running' WHERE id=?",
            (cid, ssh_host, ssh_port, password,
             sshx_url if sshx_url != "__terminal_only__" else "",
             pinggy_url, vps_id)
        )
        db.commit()
        db.close()

        # DM credentials to user
        asyncio.run_coroutine_threadsafe(
            _dm_credentials(discord_id, cid, password, ssh_host, ssh_port,
                            pinggy_url, sshx_url, tunnel_error),
            bot.loop
        )

    except Exception as e:
        db = get_db()
        db.execute("UPDATE vps SET status='failed',container_id='failed' WHERE id=?", (vps_id,))
        db.commit()
        db.close()
        asyncio.run_coroutine_threadsafe(
            _dm_error(discord_id, str(e)),
            bot.loop
        )


async def _dm_credentials(discord_id: str, container_id: str, password: str,
                           ssh_host: str, ssh_port: int, pinggy_url: str,
                           sshx_url: str, tunnel_error: str = ""):
    cfg = load_config()
    bot_name    = cfg.get("bot_name", "VPS Bot")
    owner_name  = cfg.get("owner_name", "Admin")
    logo_url    = cfg.get("logo_url", "")

    try:
        user = await bot.fetch_user(int(discord_id))
    except Exception:
        return

    embed = discord.Embed(
        title=f"🖥️ Your VPS is Ready!",
        description=(
            f"Your VPS has been provisioned and is ready to use.\n"
            f"**Keep these credentials safe — they won't be shown again.**"
        ),
        color=0x00FF88,
        timestamp=datetime.now(timezone.utc)
    )

    if logo_url:
        embed.set_thumbnail(url=logo_url)

    embed.add_field(name="📦 Container ID", value=f"`{container_id}`", inline=False)
    embed.add_field(name="👤 Username", value="`root`", inline=True)
    embed.add_field(name="🔑 Password", value=f"`{password}`", inline=True)

    if ssh_host and ssh_port:
        embed.add_field(
            name="🔌 SSH Command",
            value=f"`ssh root@{ssh_host} -p {ssh_port}`",
            inline=False
        )
        embed.add_field(name="🌐 Host", value=f"`{ssh_host}`", inline=True)
        embed.add_field(name="🔒 Port", value=f"`{ssh_port}`", inline=True)
    elif tunnel_error:
        embed.add_field(
            name="⚠️ SSH Tunnel",
            value=f"Tunnel unavailable: {tunnel_error}\nUse the browser terminal instead.",
            inline=False
        )

    if sshx_url and sshx_url != "__terminal_only__":
        embed.add_field(name="🖥️ Browser Terminal (sshx)", value=f"[Open Terminal]({sshx_url})", inline=False)

    embed.set_footer(text=f"{bot_name} · Provided by {owner_name}")

    view = CredentialsView(discord_id)
    try:
        await user.send(embed=embed, view=view)
    except discord.Forbidden:
        pass  # User has DMs closed


async def _dm_error(discord_id: str, error: str):
    try:
        user = await bot.fetch_user(int(discord_id))
        embed = discord.Embed(
            title="❌ VPS Provisioning Failed",
            description=f"Your VPS could not be created.\n```{error[:1800]}```",
            color=0xFF4444
        )
        await user.send(embed=embed)
    except Exception:
        pass


# ── Discord Bot setup ─────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ── Views / Buttons ───────────────────────────────────────────────────────────

class CredentialsView(discord.ui.View):
    def __init__(self, discord_id: str):
        super().__init__(timeout=None)
        self.discord_id = discord_id

    @discord.ui.button(label="🔑 Change Password", style=discord.ButtonStyle.secondary, custom_id="change_pass")
    async def change_password_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("❌ This button is not for you.", ephemeral=True)
            return
        modal = ChangePasswordModal(self.discord_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🖥️ sshx Terminal", style=discord.ButtonStyle.primary, custom_id="open_sshx")
    async def sshx_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("❌ This button is not for you.", ephemeral=True)
            return
        db = get_db()
        vps = db.execute(
            "SELECT sshx_url, status FROM vps WHERE discord_id=? AND status='running'",
            (self.discord_id,)
        ).fetchone()
        db.close()
        if not vps:
            await interaction.response.send_message("❌ No running VPS found.", ephemeral=True)
            return
        url = vps["sshx_url"]
        if not url:
            await interaction.response.send_message(
                "⚠️ Browser terminal is not available for your VPS. Use the SSH command instead.",
                ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🖥️ **Browser Terminal**\n[Click here to open]({url})",
            ephemeral=True
        )


class ChangePasswordModal(discord.ui.Modal, title="Change VPS Password"):
    new_password = discord.ui.TextInput(
        label="New Password",
        placeholder="Minimum 8 characters",
        min_length=8,
        max_length=64
    )

    def __init__(self, discord_id: str):
        super().__init__()
        self.discord_id = discord_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = get_db()
        vps = db.execute(
            "SELECT container_id FROM vps WHERE discord_id=? AND status='running'",
            (self.discord_id,)
        ).fetchone()
        db.close()

        if not vps:
            await interaction.followup.send("❌ No running VPS found.", ephemeral=True)
            return

        new_pass = str(self.new_password)
        success = await asyncio.to_thread(change_vps_password, vps["container_id"], new_pass)
        if success:
            # Update DB
            db = get_db()
            db.execute("UPDATE vps SET ssh_password=? WHERE discord_id=?", (new_pass, self.discord_id))
            db.commit()
            db.close()
            await interaction.followup.send(
                f"✅ Password changed successfully!\n**New password:** `{new_pass}`",
                ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Failed to change password. Try again later.", ephemeral=True)


class VPSControlView(discord.ui.View):
    """Buttons for VPS management — sent with /vps status."""

    def __init__(self, discord_id: str, vps_id: int):
        super().__init__(timeout=300)
        self.discord_id = discord_id
        self.vps_id = vps_id

    def _check_owner(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.discord_id or is_admin_discord(str(interaction.user.id))

    @discord.ui.button(label="🔑 Change Password", style=discord.ButtonStyle.secondary)
    async def change_pass(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_owner(interaction):
            await interaction.response.send_message("❌ Not your VPS.", ephemeral=True)
            return
        modal = ChangePasswordModal(self.discord_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🖥️ sshx", style=discord.ButtonStyle.primary)
    async def open_sshx(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_owner(interaction):
            await interaction.response.send_message("❌ Not your VPS.", ephemeral=True)
            return
        db = get_db()
        vps = db.execute("SELECT sshx_url FROM vps WHERE id=?", (self.vps_id,)).fetchone()
        db.close()
        url = vps["sshx_url"] if vps else None
        if not url:
            await interaction.response.send_message("⚠️ sshx not available for this VPS.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🖥️ **Browser Terminal:** [Open sshx]({url})", ephemeral=True
        )

    @discord.ui.button(label="🔄 Regen Pinggy", style=discord.ButtonStyle.secondary)
    async def regen_pinggy_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_owner(interaction):
            await interaction.response.send_message("❌ Not your VPS.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        db = get_db()
        vps = db.execute("SELECT * FROM vps WHERE id=?", (self.vps_id,)).fetchone()
        db.close()
        if not vps:
            await interaction.followup.send("❌ VPS not found.", ephemeral=True)
            return

        await interaction.followup.send("🔄 Regenerating Pinggy tunnel... ~30s.", ephemeral=True)
        cfg = load_config()
        pinggy_token = cfg.get("pinggy_token", "")
        result = await asyncio.to_thread(
            PinggyTunnel.regen_tunnel, vps["container_id"], pinggy_token or None
        )
        if "error" in result:
            await interaction.followup.send(f"❌ Failed: {result['error']}", ephemeral=True)
            return

        host = result["host"]
        port = result["port"]
        ssh_cmd = result.get("ssh_command", f"ssh root@{host} -p {port}")
        db = get_db()
        db.execute("UPDATE vps SET ssh_host=?,ssh_port=?,pinggy_url=? WHERE id=?",
                   (host, port, result["url"], self.vps_id))
        db.commit()
        db.close()
        await interaction.followup.send(
            f"✅ **New Pinggy tunnel:**\n`{ssh_cmd}`\n"
            f"Password: `{vps['ssh_password']}`",
            ephemeral=True
        )

    @discord.ui.button(label="🔄 Regen sshx", style=discord.ButtonStyle.secondary)
    async def regen_sshx_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_owner(interaction):
            await interaction.response.send_message("❌ Not your VPS.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        db = get_db()
        vps = db.execute("SELECT container_id, last_regen FROM vps WHERE id=?", (self.vps_id,)).fetchone()
        db.close()
        if not vps:
            await interaction.followup.send("❌ VPS not found.", ephemeral=True)
            return
        if time.time() - (vps["last_regen"] or 0) < 30:
            await interaction.followup.send("⏳ Wait 30s between regenerations.", ephemeral=True)
            return
        new_url = await asyncio.to_thread(regen_sshx, vps["container_id"])
        if new_url:
            db = get_db()
            db.execute("UPDATE vps SET sshx_url=?,last_regen=? WHERE id=?",
                       (new_url, int(time.time()), self.vps_id))
            db.commit()
            db.close()
            await interaction.followup.send(f"✅ New sshx link: {new_url}", ephemeral=True)
        else:
            await interaction.followup.send("❌ Failed to regenerate sshx link.", ephemeral=True)

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger)
    async def stop_vps_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_owner(interaction):
            await interaction.response.send_message("❌ Not your VPS.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        db = get_db()
        vps = db.execute("SELECT container_id FROM vps WHERE id=?", (self.vps_id,)).fetchone()
        db.close()
        if not vps:
            await interaction.followup.send("❌ VPS not found.", ephemeral=True)
            return
        await asyncio.to_thread(stop_vps, vps["container_id"])
        db = get_db()
        db.execute("UPDATE vps SET status='stopped' WHERE id=?", (self.vps_id,))
        db.commit()
        db.close()
        await interaction.followup.send("✅ VPS stopped.", ephemeral=True)

    @discord.ui.button(label="▶️ Start", style=discord.ButtonStyle.success)
    async def start_vps_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_owner(interaction):
            await interaction.response.send_message("❌ Not your VPS.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        db = get_db()
        vps = db.execute("SELECT container_id FROM vps WHERE id=?", (self.vps_id,)).fetchone()
        db.close()
        if not vps:
            await interaction.followup.send("❌ VPS not found.", ephemeral=True)
            return
        await asyncio.to_thread(start_vps, vps["container_id"])
        db = get_db()
        db.execute("UPDATE vps SET status='running' WHERE id=?", (self.vps_id,))
        db.commit()
        db.close()
        await interaction.followup.send("✅ VPS started.", ephemeral=True)


class AdminVPSView(discord.ui.View):
    """Admin-only control buttons for a user's VPS."""

    def __init__(self, vps_id: int, target_discord_id: str):
        super().__init__(timeout=300)
        self.vps_id = vps_id
        self.target_discord_id = target_discord_id

    @discord.ui.button(label="🗑️ Delete VPS", style=discord.ButtonStyle.danger)
    async def delete_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_discord(str(interaction.user.id)):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        db = get_db()
        vps = db.execute("SELECT container_id FROM vps WHERE id=?", (self.vps_id,)).fetchone()
        db.close()
        if not vps:
            await interaction.followup.send("❌ VPS not found.", ephemeral=True)
            return
        await asyncio.to_thread(destroy_vps, vps["container_id"])
        db = get_db()
        db.execute("DELETE FROM vps WHERE id=?", (self.vps_id,))
        db.commit()
        db.close()
        await interaction.followup.send(f"✅ VPS `{vps['container_id']}` deleted.", ephemeral=True)

    @discord.ui.button(label="⏸️ Suspend", style=discord.ButtonStyle.secondary)
    async def suspend_vps_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_discord(str(interaction.user.id)):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        db = get_db()
        vps = db.execute("SELECT container_id FROM vps WHERE id=?", (self.vps_id,)).fetchone()
        db.close()
        if not vps:
            await interaction.followup.send("❌ VPS not found.", ephemeral=True)
            return
        await asyncio.to_thread(suspend_vps, vps["container_id"])
        db = get_db()
        db.execute("UPDATE vps SET status='suspended' WHERE id=?", (self.vps_id,))
        db.commit()
        db.close()
        await interaction.followup.send("✅ VPS suspended.", ephemeral=True)

    @discord.ui.button(label="▶️ Unsuspend", style=discord.ButtonStyle.success)
    async def unsuspend_vps_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_discord(str(interaction.user.id)):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        db = get_db()
        vps = db.execute("SELECT container_id FROM vps WHERE id=?", (self.vps_id,)).fetchone()
        db.close()
        if not vps:
            await interaction.followup.send("❌ VPS not found.", ephemeral=True)
            return
        await asyncio.to_thread(unsuspend_vps, vps["container_id"])
        db = get_db()
        db.execute("UPDATE vps SET status='running' WHERE id=?", (self.vps_id,))
        db.commit()
        db.close()
        await interaction.followup.send("✅ VPS unsuspended.", ephemeral=True)


# ── Slash Commands ────────────────────────────────────────────────────────────

@tree.command(name="vps", description="Create or manage your VPS")
@app_commands.describe(action="Action to perform")
@app_commands.choices(action=[
    app_commands.Choice(name="create", value="create"),
    app_commands.Choice(name="status", value="status"),
    app_commands.Choice(name="delete", value="delete"),
    app_commands.Choice(name="reinstall", value="reinstall"),
    app_commands.Choice(name="credentials", value="credentials"),
    app_commands.Choice(name="regen_pinggy", value="regen_pinggy"),
    app_commands.Choice(name="regen_sshx", value="regen_sshx"),
])
async def vps_command(interaction: discord.Interaction, action: str):
    discord_id = str(interaction.user.id)
    cfg = load_config()
    bot_name   = cfg.get("bot_name", "VPS Bot")
    logo_url   = cfg.get("logo_url", "")

    if action == "create":
        await _handle_create(interaction, discord_id, cfg)

    elif action == "status":
        await _handle_status(interaction, discord_id, logo_url, bot_name)

    elif action == "delete":
        await _handle_delete(interaction, discord_id)

    elif action == "reinstall":
        await _handle_reinstall(interaction, discord_id)

    elif action == "credentials":
        await _handle_credentials(interaction, discord_id, logo_url, bot_name)

    elif action == "regen_pinggy":
        await _handle_regen_pinggy(interaction, discord_id)

    elif action == "regen_sshx":
        await _handle_regen_sshx_cmd(interaction, discord_id)


async def _handle_create(interaction: discord.Interaction, discord_id: str, cfg: dict):
    await interaction.response.defer(ephemeral=True)
    db = get_db()

    # Check existing VPS
    existing = db.execute(
        "SELECT id, status FROM vps WHERE discord_id=?", (discord_id,)
    ).fetchone()
    if existing and existing["status"] not in ("failed", "deleted"):
        db.close()
        await interaction.followup.send(
            "❌ You already have a VPS. Use `/vps status` to check it.", ephemeral=True
        )
        return

    provision_mode = get_provision_mode()

    if provision_mode == "admin_provision":
        # Check for existing request
        req = db.execute(
            "SELECT status FROM vps_requests WHERE discord_id=?", (discord_id,)
        ).fetchone()
        if req and req["status"] == "pending":
            db.close()
            await interaction.followup.send(
                "⏳ Your VPS request is **pending**. Wait for admin approval.", ephemeral=True
            )
            return

        # Submit request
        db.execute(
            "INSERT OR REPLACE INTO vps_requests(discord_id,username,note,status,created_at) "
            "VALUES(?,?,?,?,?)",
            (discord_id, str(interaction.user), "", "pending", int(time.time()))
        )
        db.commit()
        db.close()

        # Notify admins
        await _notify_admins_new_request(interaction.user)

        await interaction.followup.send(
            "📨 **VPS request submitted!**\nAn admin will review and provision your VPS. "
            "You'll receive credentials via DM when it's ready.",
            ephemeral=True
        )
        return

    # Self-service mode
    # Capacity check
    allowed, count = can_create_vps()
    if not allowed:
        db.close()
        await interaction.followup.send(
            "❌ Server is at capacity. Try again later.", ephemeral=True
        )
        return

    disk_ok, _, _, disk_msg = can_allocate_disk(get_free_vps_disk())
    if not disk_ok:
        db.close()
        await interaction.followup.send(
            f"❌ Disk budget reached. {disk_msg or ''}", ephemeral=True
        )
        return

    # Register discord user
    db.execute(
        "INSERT OR IGNORE INTO discord_users(discord_id,username,joined_at) VALUES(?,?,?)",
        (discord_id, str(interaction.user), int(time.time()))
    )

    # Create VPS record
    cur = db.execute(
        "INSERT INTO vps(discord_id,container_id,status,created_at) VALUES(?,?,?,?)",
        (discord_id, "pending", "queued", int(time.time()))
    )
    db.commit()
    vps_id = cur.lastrowid
    db.close()

    # Queue build
    queue_mgr.enqueue(discord_id, vps_id)

    cfg = load_config()
    pos = queue_mgr.get_position(vps_id)
    await interaction.followup.send(
        f"✅ **VPS queued!** Position: `#{pos}`\n"
        f"⏳ Provisioning takes 2–5 minutes. Credentials will be **DM'd** to you when ready.\n"
        f"Make sure your DMs are open!",
        ephemeral=True
    )


async def _handle_status(interaction: discord.Interaction, discord_id: str,
                          logo_url: str, bot_name: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    vps = db.execute(
        "SELECT * FROM vps WHERE discord_id=? ORDER BY created_at DESC LIMIT 1",
        (discord_id,)
    ).fetchone()
    db.close()

    if not vps:
        await interaction.followup.send(
            "❌ You don't have a VPS. Use `/vps create` to get one.", ephemeral=True
        )
        return

    status_emoji = {
        "running": "🟢",
        "stopped": "🔴",
        "creating": "🟡",
        "queued": "⏳",
        "failed": "❌",
        "suspended": "🟠",
    }.get(vps["status"], "⚪")

    embed = discord.Embed(
        title=f"🖥️ Your VPS",
        color=0x00BFFF,
        timestamp=datetime.now(timezone.utc)
    )
    if logo_url:
        embed.set_thumbnail(url=logo_url)

    embed.add_field(name="Status", value=f"{status_emoji} {vps['status'].capitalize()}", inline=True)
    embed.add_field(name="Container", value=f"`{vps['container_id']}`", inline=True)

    if vps["status"] == "queued":
        pos = queue_mgr.get_position(vps["id"])
        embed.add_field(name="Queue Position", value=f"`#{pos}`", inline=True)

    if vps["ssh_host"] and vps["ssh_port"]:
        embed.add_field(
            name="SSH",
            value=f"`ssh root@{vps['ssh_host']} -p {vps['ssh_port']}`",
            inline=False
        )

    if vps["pinggy_url"]:
        embed.add_field(name="Pinggy URL", value=f"`{vps['pinggy_url']}`", inline=False)

    if vps["sshx_url"]:
        embed.add_field(name="Browser Terminal", value=f"[sshx]({vps['sshx_url']})", inline=True)

    embed.add_field(
        name="Created",
        value=f"<t:{vps['created_at']}:R>",
        inline=True
    )
    embed.set_footer(text=bot_name)

    view = VPSControlView(discord_id, vps["id"]) if vps["status"] == "running" else None
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def _handle_delete(interaction: discord.Interaction, discord_id: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    vps = db.execute(
        "SELECT * FROM vps WHERE discord_id=? AND status NOT IN ('failed','deleted') "
        "ORDER BY created_at DESC LIMIT 1",
        (discord_id,)
    ).fetchone()
    db.close()

    if not vps:
        await interaction.followup.send("❌ No active VPS found.", ephemeral=True)
        return

    # Confirm view
    view = ConfirmDeleteView(discord_id, vps["id"], vps["container_id"])
    await interaction.followup.send(
        f"⚠️ **Are you sure?**\nThis will permanently delete your VPS `{vps['container_id']}`.\n"
        f"**All data will be lost.**",
        view=view,
        ephemeral=True
    )


async def _handle_reinstall(interaction: discord.Interaction, discord_id: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    vps = db.execute(
        "SELECT * FROM vps WHERE discord_id=? AND status='running' "
        "ORDER BY created_at DESC LIMIT 1",
        (discord_id,)
    ).fetchone()
    db.close()

    if not vps:
        await interaction.followup.send("❌ No running VPS found.", ephemeral=True)
        return

    db = get_db()
    db.execute("UPDATE vps SET status='creating' WHERE id=?", (vps["id"],))
    db.commit()
    db.close()

    threading.Thread(
        target=_reinstall_worker,
        args=(vps["id"], vps["container_id"], discord_id),
        daemon=True
    ).start()

    await interaction.followup.send(
        "🔄 **Reinstalling VPS...** New credentials will be DM'd to you when done.",
        ephemeral=True
    )


async def _handle_credentials(interaction: discord.Interaction, discord_id: str,
                                logo_url: str, bot_name: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    vps = db.execute(
        "SELECT * FROM vps WHERE discord_id=? AND status='running' "
        "ORDER BY created_at DESC LIMIT 1",
        (discord_id,)
    ).fetchone()
    db.close()

    if not vps:
        await interaction.followup.send("❌ No running VPS found.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🔑 Your VPS Credentials",
        description="**Keep these safe! Do not share them.**",
        color=0xFFAA00,
        timestamp=datetime.now(timezone.utc)
    )
    if logo_url:
        embed.set_thumbnail(url=logo_url)

    embed.add_field(name="Username", value="`root`", inline=True)
    embed.add_field(name="Password", value=f"`{vps['ssh_password']}`", inline=True)

    if vps["ssh_host"] and vps["ssh_port"]:
        embed.add_field(
            name="SSH Command",
            value=f"`ssh root@{vps['ssh_host']} -p {vps['ssh_port']}`",
            inline=False
        )

    if vps["sshx_url"]:
        embed.add_field(name="Browser Terminal", value=f"[Open sshx]({vps['sshx_url']})", inline=True)

    embed.set_footer(text=f"{bot_name} · Sent only to you")

    view = CredentialsView(discord_id)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def _handle_regen_pinggy(interaction: discord.Interaction, discord_id: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    vps = db.execute(
        "SELECT * FROM vps WHERE discord_id=? AND status='running' "
        "ORDER BY created_at DESC LIMIT 1",
        (discord_id,)
    ).fetchone()
    db.close()

    if not vps:
        await interaction.followup.send("❌ No running VPS found.", ephemeral=True)
        return

    await interaction.followup.send("🔄 Regenerating Pinggy tunnel... this takes ~30s.", ephemeral=True)

    cfg = load_config()
    pinggy_token = cfg.get("pinggy_token", "")

    result = await asyncio.to_thread(
        PinggyTunnel.regen_tunnel, vps["container_id"], pinggy_token or None
    )

    if "error" in result:
        await interaction.followup.send(f"❌ Pinggy regen failed: {result['error']}", ephemeral=True)
        return

    host = result["host"]
    port = result["port"]
    url  = result["url"]
    ssh_cmd = result.get("ssh_command", f"ssh root@{host} -p {port}")

    db = get_db()
    db.execute(
        "UPDATE vps SET ssh_host=?,ssh_port=?,pinggy_url=? WHERE id=?",
        (host, port, url, vps["id"])
    )
    db.commit()
    db.close()

    cfg2 = load_config()
    embed = discord.Embed(
        title="🔄 Pinggy Tunnel Regenerated",
        color=0x00FF88,
    )
    embed.add_field(name="SSH Command", value=f"`{ssh_cmd}`", inline=False)
    embed.add_field(name="Host", value=f"`{host}`", inline=True)
    embed.add_field(name="Port", value=f"`{port}`", inline=True)
    embed.add_field(name="Username", value="`root`", inline=True)
    embed.add_field(name="Password", value=f"`{vps['ssh_password']}`", inline=True)
    embed.set_footer(text=cfg2.get("bot_name", "VPS Bot"))

    await interaction.followup.send(embed=embed, ephemeral=True)


async def _handle_regen_sshx_cmd(interaction: discord.Interaction, discord_id: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    vps = db.execute(
        "SELECT * FROM vps WHERE discord_id=? AND status='running' "
        "ORDER BY created_at DESC LIMIT 1",
        (discord_id,)
    ).fetchone()
    db.close()

    if not vps:
        await interaction.followup.send("❌ No running VPS found.", ephemeral=True)
        return

    if time.time() - (vps["last_regen"] or 0) < 30:
        await interaction.followup.send("⏳ Wait 30s between regenerations.", ephemeral=True)
        return

    await interaction.followup.send("🔄 Regenerating sshx terminal link... up to 45s.", ephemeral=True)

    new_url = await asyncio.to_thread(regen_sshx, vps["container_id"])
    if new_url:
        db = get_db()
        db.execute("UPDATE vps SET sshx_url=?,last_regen=? WHERE id=?",
                   (new_url, int(time.time()), vps["id"]))
        db.commit()
        db.close()
        await interaction.followup.send(
            f"✅ **sshx terminal regenerated!**\n[Open Browser Terminal]({new_url})",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            "❌ sshx link could not be regenerated. The container is still accessible via SSH.",
            ephemeral=True
        )


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, discord_id: str, vps_id: int, container_id: str):
        super().__init__(timeout=60)
        self.discord_id = discord_id
        self.vps_id = vps_id
        self.container_id = container_id

    @discord.ui.button(label="✅ Yes, Delete It", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("❌ Not your VPS.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(destroy_vps, self.container_id)
        db = get_db()
        db.execute("DELETE FROM vps WHERE id=?", (self.vps_id,))
        db.commit()
        db.close()
        self.stop()
        await interaction.followup.send("✅ VPS deleted.", ephemeral=True)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.send_message("✅ Cancelled.", ephemeral=True)


# ── Admin Commands ────────────────────────────────────────────────────────────

@tree.command(name="admin", description="Admin VPS management")
@app_commands.describe(
    action="Admin action",
    user_id="Target user's Discord ID (17–19 digit number — right-click user → Copy ID)",
    note="Optional note"
)
@app_commands.choices(action=[
    app_commands.Choice(name="list", value="list"),
    app_commands.Choice(name="grant", value="grant"),
    app_commands.Choice(name="delete", value="delete"),
    app_commands.Choice(name="suspend", value="suspend"),
    app_commands.Choice(name="unsuspend", value="unsuspend"),
    app_commands.Choice(name="requests", value="requests"),
    app_commands.Choice(name="approve", value="approve"),
    app_commands.Choice(name="deny", value="deny"),
    app_commands.Choice(name="config", value="config"),
])
async def admin_command(interaction: discord.Interaction, action: str,
                        user_id: str = None, note: str = ""):
    if not is_admin_discord(str(interaction.user.id)):
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return

    # Validate user_id format when provided
    if user_id is not None:
        user_id = user_id.strip().lstrip("<@!").rstrip(">")  # strip @mention format too
        if not user_id.isdigit():
            await interaction.response.send_message(
                "❌ `user_id` must be a numeric Discord user ID (17–19 digits).\n"
                "Enable Developer Mode → right-click any user → **Copy User ID**.",
                ephemeral=True
            )
            return

    # Resolve display name for actions that need it
    target_name = user_id or "unknown"
    if user_id:
        try:
            target_user = await bot.fetch_user(int(user_id))
            target_name = str(target_user)
        except Exception:
            pass

    needs_user = {"grant", "delete", "suspend", "unsuspend", "approve", "deny"}
    if action in needs_user and not user_id:
        await interaction.response.send_message(
            f"❌ `/admin {action}` requires a `user_id`.\n"
            f"Right-click the user in Discord → **Copy User ID**, then paste it.",
            ephemeral=True
        )
        return

    if action == "list":
        await _admin_list(interaction)
    elif action == "grant":
        await _admin_grant(interaction, user_id, target_name)
    elif action == "delete":
        await _admin_delete(interaction, user_id)
    elif action == "suspend":
        await _admin_suspend(interaction, user_id)
    elif action == "unsuspend":
        await _admin_unsuspend(interaction, user_id)
    elif action == "requests":
        await _admin_list_requests(interaction)
    elif action == "approve":
        await _admin_approve_request(interaction, user_id, target_name)
    elif action == "deny":
        await _admin_deny_request(interaction, user_id)
    elif action == "config":
        await _admin_config(interaction)


async def _admin_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    vpses = db.execute("SELECT * FROM vps ORDER BY created_at DESC").fetchall()
    db.close()

    if not vpses:
        await interaction.followup.send("📋 No VPS records found.", ephemeral=True)
        return

    embed = discord.Embed(title="📋 All VPS Instances", color=0x00BFFF)
    for vps in vpses[:20]:  # Discord embed field limit
        status_emoji = {"running": "🟢", "stopped": "🔴", "failed": "❌",
                        "suspended": "🟠", "creating": "🟡"}.get(vps["status"], "⚪")
        val = (
            f"Status: {status_emoji} {vps['status']}\n"
            f"Container: `{vps['container_id']}`\n"
        )
        if vps["ssh_host"] and vps["ssh_port"]:
            val += f"SSH: `{vps['ssh_host']}:{vps['ssh_port']}`\n"
        embed.add_field(
            name=f"User `{vps['discord_id']}`",
            value=val,
            inline=False
        )

    if len(vpses) > 20:
        embed.set_footer(text=f"Showing 20 of {len(vpses)} VPS instances")

    await interaction.followup.send(embed=embed, ephemeral=True)


async def _admin_grant(interaction: discord.Interaction, target_id: str, target_name: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    existing = db.execute(
        "SELECT id FROM vps WHERE discord_id=? AND status NOT IN ('failed','deleted')",
        (target_id,)
    ).fetchone()
    db.close()

    if existing:
        await interaction.followup.send(
            f"❌ <@{target_id}> already has a VPS.", ephemeral=True
        )
        return

    allowed, _ = can_create_vps()
    if not allowed:
        await interaction.followup.send("❌ Server at capacity.", ephemeral=True)
        return

    db = get_db()
    cur = db.execute(
        "INSERT INTO vps(discord_id,container_id,status,created_at) VALUES(?,?,?,?)",
        (target_id, "pending", "queued", int(time.time()))
    )
    db.commit()
    vps_id = cur.lastrowid
    db.close()

    queue_mgr.enqueue(target_id, vps_id)

    await interaction.followup.send(
        f"✅ VPS provisioning started for <@{target_id}>. "
        f"Credentials will be DM'd when ready.",
        ephemeral=True
    )


async def _admin_delete(interaction: discord.Interaction, target_id: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    vps = db.execute(
        "SELECT * FROM vps WHERE discord_id=? AND status NOT IN ('failed','deleted') "
        "ORDER BY created_at DESC LIMIT 1",
        (target_id,)
    ).fetchone()
    db.close()

    if not vps:
        await interaction.followup.send(f"❌ No active VPS for <@{target_id}>.", ephemeral=True)
        return

    view = AdminVPSView(vps["id"], target_id)
    await interaction.followup.send(
        f"🗑️ Deleting VPS for <@{target_id}>: `{vps['container_id']}`",
        view=view,
        ephemeral=True
    )


async def _admin_suspend(interaction: discord.Interaction, target_id: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    vps = db.execute(
        "SELECT * FROM vps WHERE discord_id=? AND status='running'",
        (target_id,)
    ).fetchone()
    db.close()

    if not vps:
        await interaction.followup.send(f"❌ No running VPS for <@{target_id}>.", ephemeral=True)
        return

    await asyncio.to_thread(suspend_vps, vps["container_id"])
    db = get_db()
    db.execute("UPDATE vps SET status='suspended' WHERE id=?", (vps["id"],))
    db.commit()
    db.close()
    await interaction.followup.send(f"✅ VPS suspended for <@{target_id}>.", ephemeral=True)


async def _admin_unsuspend(interaction: discord.Interaction, target_id: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    vps = db.execute(
        "SELECT * FROM vps WHERE discord_id=? AND status='suspended'",
        (target_id,)
    ).fetchone()
    db.close()

    if not vps:
        await interaction.followup.send(f"❌ No suspended VPS for <@{target_id}>.", ephemeral=True)
        return

    await asyncio.to_thread(unsuspend_vps, vps["container_id"])
    db = get_db()
    db.execute("UPDATE vps SET status='running' WHERE id=?", (vps["id"],))
    db.commit()
    db.close()
    await interaction.followup.send(f"✅ VPS unsuspended for <@{target_id}>.", ephemeral=True)


async def _admin_list_requests(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    requests = db.execute(
        "SELECT * FROM vps_requests WHERE status='pending' ORDER BY created_at ASC"
    ).fetchall()
    db.close()

    if not requests:
        await interaction.followup.send("📋 No pending VPS requests.", ephemeral=True)
        return

    embed = discord.Embed(title="📨 Pending VPS Requests", color=0xFFAA00)
    for req in requests[:20]:
        embed.add_field(
            name=f"User: {req['username']} (`{req['discord_id']}`)",
            value=(
                f"Submitted: <t:{req['created_at']}:R>\n"
                f"Note: {req['note'] or '(none)'}\n"
                f"Use `/admin approve user:@{req['username']}` or `/admin deny user:@{req['username']}`"
            ),
            inline=False
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _admin_approve_request(interaction: discord.Interaction, target_id: str, target_name: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    req = db.execute(
        "SELECT * FROM vps_requests WHERE discord_id=? AND status='pending'",
        (target_id,)
    ).fetchone()
    db.close()

    if not req:
        await interaction.followup.send(f"❌ No pending request for <@{target_id}>.", ephemeral=True)
        return

    allowed, _ = can_create_vps()
    if not allowed:
        await interaction.followup.send("❌ Server at capacity.", ephemeral=True)
        return

    db = get_db()
    cur = db.execute(
        "INSERT INTO vps(discord_id,container_id,status,created_at) VALUES(?,?,?,?)",
        (target_id, "pending", "queued", int(time.time()))
    )
    db.commit()
    vps_id = cur.lastrowid
    db.execute(
        "UPDATE vps_requests SET status='approved',reviewed_at=? WHERE discord_id=?",
        (int(time.time()), target_id)
    )
    db.commit()
    db.close()

    queue_mgr.enqueue(target_id, vps_id)

    await interaction.followup.send(
        f"✅ Approved VPS for <@{target_id}>. Credentials will be DM'd when ready.",
        ephemeral=True
    )

    # DM user about approval
    try:
        user = await bot.fetch_user(int(target_id))
        await user.send(
            f"🎉 **Your VPS request has been approved!**\n"
            f"Provisioning is starting now. Credentials will arrive via DM in 2–5 minutes."
        )
    except Exception:
        pass


async def _admin_deny_request(interaction: discord.Interaction, target_id: str):
    await interaction.response.defer(ephemeral=True)
    db = get_db()
    db.execute(
        "UPDATE vps_requests SET status='denied',reviewed_at=? WHERE discord_id=?",
        (int(time.time()), target_id)
    )
    db.commit()
    db.close()
    await interaction.followup.send(f"✅ Request denied for <@{target_id}>.", ephemeral=True)

    try:
        user = await bot.fetch_user(int(target_id))
        await user.send("❌ Your VPS request has been denied. Contact an admin for more info.")
    except Exception:
        pass


async def _admin_config(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cfg = get_panel_config()
    embed = discord.Embed(title="⚙️ Panel Configuration", color=0x888888)
    embed.add_field(name="Provision Mode", value=cfg.get("provision_mode", "self_service"), inline=True)
    embed.add_field(name="CPU per VPS", value=f"{cfg.get('vps_cpu_cores', '4')} cores", inline=True)
    embed.add_field(name="RAM per VPS", value=f"{cfg.get('vps_ram_gb', '4')} GB", inline=True)
    embed.add_field(name="Disk per VPS", value=f"{cfg.get('vps_disk_gb', '80')} GB", inline=True)

    view = AdminConfigView()
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class AdminConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="🔄 Toggle Provision Mode", style=discord.ButtonStyle.secondary)
    async def toggle_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_discord(str(interaction.user.id)):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        current = get_provision_mode()
        new_mode = "admin_provision" if current == "self_service" else "self_service"
        set_panel_config("provision_mode", new_mode)
        await interaction.response.send_message(
            f"✅ Provision mode changed to: **{new_mode}**", ephemeral=True
        )


# ── Admin notification helper ─────────────────────────────────────────────────

async def _notify_admins_new_request(user: discord.User):
    cfg = load_config()
    admin_ids = cfg.get("admin_ids", [])
    for admin_id in admin_ids:
        try:
            admin_user = await bot.fetch_user(int(admin_id))
            await admin_user.send(
                f"📨 **New VPS Request**\n"
                f"User: {user} (`{user.id}`)\n"
                f"Use `/admin requests` to review.\n"
                f"Approve with `/admin approve user:@{user.name}`"
            )
        except Exception:
            pass


# ── Reinstall worker ──────────────────────────────────────────────────────────

def _reinstall_worker(vps_id: int, old_container_id: str, discord_id: str):
    cfg = load_config()
    pinggy_token = cfg.get("pinggy_token", "")
    try:
        new_cid, sshx_url = reinstall_vps(old_container_id, f"vps-{discord_id[:8]}")
        from vps import generate_password, _get_container, _set_root_password_with_retry
        inst = _get_container(new_cid)
        password = generate_password(16)
        _set_root_password_with_retry(inst, password)

        tunnel = PinggyTunnel.install_and_start(new_cid, pinggy_token or None)
        ssh_host = tunnel.get("host", "")
        ssh_port = tunnel.get("port", 0)
        pinggy_url = tunnel.get("url", "")

        db = get_db()
        db.execute(
            "UPDATE vps SET container_id=?,ssh_host=?,ssh_port=?,ssh_password=?,"
            "sshx_url=?,pinggy_url=?,status='running' WHERE id=?",
            (new_cid, ssh_host, ssh_port, password,
             sshx_url if sshx_url != "__terminal_only__" else "",
             pinggy_url, vps_id)
        )
        db.commit()
        db.close()

        asyncio.run_coroutine_threadsafe(
            _dm_credentials(discord_id, new_cid, password, ssh_host, ssh_port,
                            pinggy_url, sshx_url),
            bot.loop
        )
    except Exception as e:
        db = get_db()
        db.execute("UPDATE vps SET status='failed' WHERE id=?", (vps_id,))
        db.commit()
        db.close()
        asyncio.run_coroutine_threadsafe(_dm_error(discord_id, str(e)), bot.loop)


# ── Queue worker wrapper ──────────────────────────────────────────────────────

def _queue_build_wrapper(vps_id: int, discord_id: str):
    _build_vps(vps_id, discord_id)


# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    cfg = load_config()
    bot_name = cfg.get("bot_name", "VPS Bot")
    print(f"{'='*50}")
    print(f"  {bot_name} is online")
    print(f"  Logged in as: {bot.user} ({bot.user.id})")
    print(f"  Provision mode: {get_provision_mode()}")
    print(f"{'='*50}")

    try:
        synced = await tree.sync()
        print(f"  Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"  Failed to sync commands: {e}")

    # Set bot presence
    logo = cfg.get("bot_name", "VPS Bot")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"VPS Instances | /vps"
        )
    )

    # Start background monitor
    start_monitor()
    queue_mgr.start_queue_worker(_queue_build_wrapper)
    print("  Monitor + queue worker started")


@bot.event
async def on_guild_join(guild: discord.Guild):
    cfg = load_config()
    bot_name = cfg.get("bot_name", "VPS Bot")
    owner_name = cfg.get("owner_name", "Admin")
    logo_url = cfg.get("logo_url", "")

    # Find a suitable channel to send welcome
    channel = None
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            channel = ch
            break

    if not channel:
        return

    embed = discord.Embed(
        title=f"👋 Thanks for adding {bot_name}!",
        description=(
            f"**{bot_name}** is a VPS hosting bot — provision and manage "
            f"Linux containers directly from Discord.\n\n"
            f"**Getting started:**\n"
            f"• `/vps create` — Request a free VPS\n"
            f"• `/vps status` — Check your VPS status + controls\n"
            f"• `/vps credentials` — Retrieve your login credentials\n"
        ),
        color=0x00FF88
    )
    if logo_url:
        embed.set_thumbnail(url=logo_url)
    embed.set_footer(text=f"Provided by {owner_name}")
    await channel.send(embed=embed)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    cfg = load_config()
    token = cfg.get("discord_token")
    if not token:
        raise RuntimeError("No discord_token in bot_config.json. Run python3 setup.py first.")
    bot.run(token)
