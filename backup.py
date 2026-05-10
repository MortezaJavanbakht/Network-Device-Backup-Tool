#!/usr/bin/env python3
"""
Network Device Backup Tool
Supports: Cisco IOS, Cisco Nexus, FortiGate
Backup destination: FTP server
Monitoring: Zabbix UserParameter (JSON status file)
"""

import sys
import json
import ftplib
import logging
import argparse
import traceback
from datetime import datetime
from pathlib import Path

import paramiko
import yaml

# ─────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────

def setup_logging(log_file: str, log_level: str = "INFO") -> logging.Logger:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger = logging.getLogger("network_backup")
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler — force UTF-8 so special chars work on Windows
    import io
    ch = logging.StreamHandler(
        io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "buffer") else sys.stdout
    )
    ch.setLevel(level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ─────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────
# SSH helper
# ─────────────────────────────────────────────


def ssh_interactive(host: str, port: int, username: str, password: str,
                    commands: list, timeout: int = 30,
                    read_delay: float = 2.0, expect_done: str = None,
                    logger=None) -> str:
    """
    Interactive SSH shell.
    Sends each command and waits read_delay seconds.
    If expect_done is set, keeps reading until that string appears in output
    (used to wait for 'copy ftp:' confirmation messages).
    """
    import time
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    output_buf = ""
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        shell = client.invoke_shell(width=250, height=10000)
        time.sleep(1)
        shell.recv(65535)  # drain banner

        for cmd in commands:
            shell.send(cmd + "\n")
            time.sleep(read_delay)

        # If waiting for a completion marker (e.g. after copy ftp:)
        if expect_done:
            wait_limit = timeout
            waited = 0
            while waited < wait_limit:
                if shell.recv_ready():
                    chunk = shell.recv(65535).decode("utf-8", errors="replace")
                    output_buf += chunk
                    if expect_done in output_buf:
                        break
                time.sleep(1)
                waited += 1
        else:
            # Drain remaining output
            while shell.recv_ready():
                output_buf += shell.recv(65535).decode("utf-8", errors="replace")
                time.sleep(0.3)

    finally:
        client.close()
    return output_buf


# ─────────────────────────────────────────────
# FTP path builder (also used by Cisco copy cmd)
# ─────────────────────────────────────────────

def build_remote_path(ftp_cfg: dict, device: dict, timestamp: datetime) -> str:
    """
    Layout: <ftp_base_dir>/<site_name>/<device_name>/<hostname>_YYYYMMDD_HHMMSS.cfg
    Example: backups/istanbul-dc/core-switch-01/core-switch-01_20250815_143000.cfg
    """
    base      = ftp_cfg.get("base_dir", "backups").rstrip("/")
    site      = device.get("site", "default-site").replace(" ", "_")
    hostname  = device.get("name", device["host"]).replace(" ", "_")
    ts_str    = timestamp.strftime("%Y%m%d_%H%M%S")
    filename  = f"{hostname}_{ts_str}.cfg"
    return f"{base}/{site}/{hostname}/{filename}"


def ensure_ftp_dirs(ftp_cfg: dict, remote_path: str, logger) -> None:
    """
    Pre-create the remote directory tree on the FTP server so that
    'copy running-config ftp:' from the device succeeds without errors.
    """
    host     = ftp_cfg["host"]
    port     = ftp_cfg.get("port", 21)
    username = ftp_cfg["username"]
    password = ftp_cfg["password"]
    passive  = ftp_cfg.get("passive", True)

    parts = remote_path.replace("\\", "/").split("/")[:-1]  # dirs only
    logger.debug(f"FTP pre-creating dirs: {'/'.join(parts)}")

    with ftplib.FTP() as ftp:
        ftp.connect(host, port, timeout=30)
        ftp.login(username, password)
        ftp.set_pasv(passive)
        for d in parts:
            if not d:
                continue
            try:
                ftp.cwd(d)
            except ftplib.error_perm:
                ftp.mkd(d)
                ftp.cwd(d)


def verify_ftp_file(ftp_cfg: dict, remote_path: str, logger) -> int:
    """
    Connect to FTP and return the size in bytes of the uploaded file.
    Returns 0 if the file does not exist.
    """
    host     = ftp_cfg["host"]
    port     = ftp_cfg.get("port", 21)
    username = ftp_cfg["username"]
    password = ftp_cfg["password"]
    passive  = ftp_cfg.get("passive", True)

    with ftplib.FTP() as ftp:
        ftp.connect(host, port, timeout=30)
        ftp.login(username, password)
        ftp.set_pasv(passive)
        try:
            size = ftp.size(remote_path)
            return size if size else 0
        except ftplib.error_perm:
            return 0


# ─────────────────────────────────────────────
# Device backup functions
# ─────────────────────────────────────────────

def backup_cisco_ios(device: dict, ftp_cfg: dict, remote_path: str,
                     timestamp: datetime, logger) -> int:
    """
    Cisco IOS: device pushes config via FTP or SCP depending on
    the 'transport' property per device (default: ftp).
    """
    transport = device.get("transport", "ftp").lower()
    if transport == "scp":
        return _backup_cisco_ios_scp(device, ftp_cfg, remote_path, logger)
    else:
        return _backup_cisco_ios_ftp(device, ftp_cfg, remote_path, logger)


def _backup_cisco_ios_ftp(device: dict, ftp_cfg: dict, remote_path: str, logger) -> int:
    """IOS backup via: copy running-config ftp://user:pass@host/path"""
    host     = device["host"]
    ftp_host = ftp_cfg["host"]
    ftp_user = ftp_cfg["username"]
    ftp_pass = ftp_cfg["password"]

    logger.info(f"[{host}] Connecting (Cisco IOS) -- will push via copy ftp:")
    ensure_ftp_dirs(ftp_cfg, remote_path, logger)

    ftp_url  = f"ftp://{ftp_user}:{ftp_pass}@{ftp_host}/{remote_path}"
    commands = [
        "terminal length 0",
        f"copy running-config {ftp_url}",
        "",   # confirm: "Address or name of remote host [x.x.x.x]?"
        "",   # confirm: "Destination filename [path]?"
    ]

    output = ssh_interactive(
        host=host,
        port=device.get("port", 22),
        username=device["username"],
        password=device["password"],
        commands=commands,
        timeout=device.get("timeout", 60),
        read_delay=device.get("read_delay", 3.0),
        expect_done="bytes copied",
        logger=logger,
    )
    logger.debug(f"[{host}] copy ftp output:\n{output.strip()}")

    if "bytes copied" not in output:
        logger.warning(f"[{host}] 'bytes copied' not found in output -- verifying via FTP...")

    size = verify_ftp_file(ftp_cfg, remote_path, logger)
    if size == 0:
        raise RuntimeError(
            f"copy running-config ftp: completed but file not found on FTP: {remote_path}"
        )
    return size


def _backup_cisco_ios_scp(device: dict, ftp_cfg: dict, remote_path: str, logger) -> int:
    """
    IOS backup via: copy running-config scp://user:pass@host/full_os_path

    The 'scp' block in device config must define:
      host:     same as FTP server
      username: OS user with write access to the FTP root directory
      password: OS user password
      path:     OS path that is the FTP server's home/root directory
                e.g. /mnt/ftp  -> full path = /mnt/ftp/<remote_path>

    IOS interactive prompts during SCP:
      1. "Address or name of remote host [x]?"  -> Enter
      2. "Destination username [user]?"          -> Enter
      3. "Destination filename [path]?"          -> Enter
      4. Host key confirmation (first time)      -> "yes"

    Python pre-creates dirs via SFTP and verifies the file after transfer.
    """
    host    = device["host"]
    scp_cfg = device.get("scp", {})

    scp_host  = scp_cfg.get("host",     ftp_cfg["host"])
    scp_port  = scp_cfg.get("port",     22)
    scp_user  = scp_cfg.get("username")
    scp_pass  = scp_cfg.get("password")
    scp_path  = scp_cfg.get("path", "").rstrip("/")   # e.g. /mnt/ftp

    if not scp_user or not scp_pass:
        raise ValueError(
            f"[{host}] SCP transport requires 'scp.username' and 'scp.password' in config"
        )

    # Full absolute path on the OS: /mnt/ftp/backups/site/device/file.cfg
    full_os_path = f"{scp_path}/{remote_path}" if scp_path else remote_path

    logger.info(f"[{host}] Connecting (Cisco IOS) -- will push via copy scp: -> {scp_host}:{full_os_path}")

    # Pre-create destination directory on the SCP server via SFTP
    _ensure_sftp_dirs(scp_host, scp_port, scp_user, scp_pass, full_os_path, logger)

    scp_url  = f"scp://{scp_user}:{scp_pass}@{scp_host}/{full_os_path}"
    commands = [
        "terminal length 0",
        f"copy running-config {scp_url}",
        "",      # confirm: "Address or name of remote host?"
        "",      # confirm: "Destination username?"
        "",      # confirm: "Destination filename?"
        "yes",   # accept SSH host key if prompted (first connection)
    ]

    output = ssh_interactive(
        host=host,
        port=device.get("port", 22),
        username=device["username"],
        password=device["password"],
        commands=commands,
        timeout=device.get("timeout", 90),
        read_delay=device.get("read_delay", 3.0),
        expect_done="bytes copied",
        logger=logger,
    )
    logger.debug(f"[{host}] copy scp output:\n{output.strip()}")

    if "bytes copied" not in output:
        logger.warning(f"[{host}] 'bytes copied' not found in output -- verifying via SFTP...")

    size = _verify_sftp_file(scp_host, scp_port, scp_user, scp_pass, full_os_path, logger)
    if size == 0:
        raise RuntimeError(
            f"copy running-config scp: completed but file not found on SCP server: {full_os_path}"
        )
    return size


def _ensure_sftp_dirs(host: str, port: int, username: str, password: str,
                      remote_path: str, logger) -> None:
    """Create directory tree on SCP/SFTP server via Paramiko SFTP."""
    dirs = remote_path.replace("\\", "/").split("/")[:-1]
    logger.debug(f"SFTP pre-creating dirs: {'/'.join(dirs)}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=port, username=username, password=password,
                       timeout=30, look_for_keys=False, allow_agent=False)
        sftp    = client.open_sftp()
        current = ""
        for d in dirs:
            if not d:
                continue
            current = f"{current}/{d}" if current else d
            try:
                sftp.stat(current)
            except FileNotFoundError:
                sftp.mkdir(current)
        sftp.close()
    finally:
        client.close()


def _verify_sftp_file(host: str, port: int, username: str, password: str,
                      remote_path: str, logger) -> int:
    """Return file size on SCP/SFTP server, 0 if not found."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=port, username=username, password=password,
                       timeout=30, look_for_keys=False, allow_agent=False)
        sftp = client.open_sftp()
        try:
            return sftp.stat(remote_path).st_size or 0
        except FileNotFoundError:
            return 0
        finally:
            sftp.close()
    finally:
        client.close()


def backup_cisco_nexus(device: dict, ftp_cfg: dict, remote_path: str,
                        timestamp: datetime, logger) -> int:
    """
    Cisco Nexus: device pushes config directly to FTP via
      copy running-config ftp://user:pass@host/path vrf management
    Returns file size in bytes after verification.
    """
    host     = device["host"]
    ftp_host = ftp_cfg["host"]
    ftp_user = ftp_cfg["username"]
    ftp_pass = ftp_cfg["password"]

    logger.info(f"[{host}] Connecting (Cisco Nexus) — will push via copy ftp:")

    ensure_ftp_dirs(ftp_cfg, remote_path, logger)

    ftp_url = f"ftp://{ftp_user}:{ftp_pass}@{ftp_host}/{remote_path}"
    # Nexus requires specifying VRF for out-of-band management (commonly 'management')
    vrf = device.get("vrf", "management")

    commands = [
        "terminal length 0",
        f"copy running-config {ftp_url} vrf {vrf}",
        "",   # confirm prompt
    ]

    output = ssh_interactive(
        host=host,
        port=device.get("port", 22),
        username=device["username"],
        password=device["password"],
        commands=commands,
        timeout=device.get("timeout", 60),
        read_delay=device.get("read_delay", 2.0),
        expect_done="Copy complete",   # Nexus prints "Copy complete" on success
        logger=logger,
    )

    logger.debug(f"[{host}] copy output:\n{output.strip()}")

    size = verify_ftp_file(ftp_cfg, remote_path, logger)
    if size == 0:
        raise RuntimeError(
            f"copy running-config ftp: completed but file not found on FTP: {remote_path}"
        )
    return size


def backup_fortigate(device: dict, ftp_cfg: dict, remote_path: str,
                     timestamp: datetime, logger) -> int:
    """
    FortiGate: device pushes config directly to FTP via:
      execute backup config ftp <filename> <ftp_host>:<ftp_port> <user> <password>

    Official syntax (Fortinet docs):
      execute backup config ftp <backup_filename> <ftp_server>[:<ftp_port>] [<user>] [<password>]

    Waits for 'Send config file' confirmation in SSH output,
    then verifies the file landed on FTP.
    Returns file size in bytes.
    """
    host     = device["host"]
    ftp_host = ftp_cfg["host"]
    ftp_port = ftp_cfg.get("port", 21)
    ftp_user = ftp_cfg["username"]
    ftp_pass = ftp_cfg["password"]

    logger.info(f"[{host}] Connecting (FortiGate) — will push via execute backup config ftp:")

    # Pre-create directory tree on FTP so FortiGate doesn't fail on missing path
    ensure_ftp_dirs(ftp_cfg, remote_path, logger)

    # FortiGate needs just the filename portion; path must be relative to FTP root
    # We pass the full relative path as the filename argument
    cmd = (
        f"execute backup config ftp "
        f"{remote_path} "
        f"{ftp_host}:{ftp_port} "
        f"{ftp_user} "
        f"{ftp_pass}"
    )

    if device.get("vdom", False):
        logger.debug(f"[{host}] VDOM mode enabled — prepending 'config vdom / edit root'")
        commands = [
            "config vdom",
            "edit root",
            cmd,
        ]
    else:
        commands = [cmd]

    output = ssh_interactive(
        host=host,
        port=device.get("port", 22),
        username=device["username"],
        password=device["password"],
        commands=commands,
        timeout=device.get("timeout", 60),
        read_delay=device.get("read_delay", 3.0),
        expect_done="Send config file",   # FortiGate prints "Send config file to ftp server OK."
        logger=logger,
    )

    logger.debug(f"[{host}] execute backup output:\n{output.strip()}")

    if "Send config file" not in output:
        logger.warning(f"[{host}] Expected 'Send config file' not found in output — verifying via FTP...")

    # Verify the file exists on FTP
    size = verify_ftp_file(ftp_cfg, remote_path, logger)
    if size == 0:
        raise RuntimeError(
            f"execute backup config ftp completed but file not found on FTP: {remote_path}"
        )
    return size


DEVICE_HANDLERS = {
    "cisco_ios":   backup_cisco_ios,
    "cisco_nexus": backup_cisco_nexus,
    "fortigate":   backup_fortigate,
}



# ─────────────────────────────────────────────
# Zabbix status file
# ─────────────────────────────────────────────

def write_zabbix_status(status_file: str, results: list) -> None:
    """
    Write a JSON status file consumed by Zabbix UserParameter.
    Zabbix UserParameter examples are printed at end of run.

    File format:
    {
      "last_run": "2025-08-15T14:30:00",
      "total": 5,
      "success": 4,
      "failed": 1,
      "devices": [
        {"name": "core-sw1", "type": "cisco_ios", "status": "ok",
         "timestamp": "...", "remote_path": "..."},
        {"name": "fw01",     "type": "fortigate", "status": "failed",
         "error": "Connection refused", "timestamp": "..."}
      ]
    }
    """
    path = Path(status_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    total   = len(results)
    success = sum(1 for r in results if r["status"] == "ok")
    failed  = total - success

    data = {
        "last_run": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "total":   total,
        "success": success,
        "failed":  failed,
        "devices": results,
    }
    with open(status_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ─────────────────────────────────────────────
# Main backup runner
# ─────────────────────────────────────────────

def run_backup(config: dict, logger: logging.Logger) -> list:
    ftp_cfg     = config["ftp"]
    devices     = config["devices"]
    status_file = config.get("zabbix", {}).get("status_file", "/var/tmp/network_backup_status.json")
    results     = []
    timestamp   = datetime.now()

    for device in devices:
        host     = device["host"]
        dev_name = device.get("name", host)
        dev_type = device["type"]
        result   = {"name": dev_name, "type": dev_type,
                    "host": host, "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%S")}

        if dev_type not in DEVICE_HANDLERS:
            msg = f"Unknown device type '{dev_type}' — skipping"
            logger.warning(f"[{host}] {msg}")
            result.update({"status": "skipped", "error": msg})
            results.append(result)
            continue

        try:
            remote_path = build_remote_path(ftp_cfg, device, timestamp)
            handler     = DEVICE_HANDLERS[dev_type]
            size_bytes  = handler(device, ftp_cfg, remote_path, timestamp, logger)

            result.update({"status": "ok", "remote_path": remote_path,
                            "size_bytes": size_bytes})
            logger.info(f"[{dev_name}] Backup SUCCESSFUL ({size_bytes} bytes) -> {remote_path}")

        except Exception as e:
            error_detail = traceback.format_exc()
            result.update({"status": "failed", "error": str(e)})
            logger.error(f"[{dev_name}] Backup FAILED: {e}")
            logger.debug(f"[{dev_name}] Traceback:\n{error_detail}")

        results.append(result)

    write_zabbix_status(status_file, results)
    logger.info(f"Zabbix status written -> {status_file}")
    return results


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Network device backup tool")
    parser.add_argument("-c", "--config", default="config.yaml",
                        help="Path to config YAML file (default: config.yaml)")
    parser.add_argument("-d", "--device", default=None,
                        help="Backup only this device name (optional)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Connect and fetch config but do NOT upload to FTP")
    args = parser.parse_args()

    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"ERROR: Config file not found: {args.config}")
        sys.exit(1)

    # Setup logger
    log_cfg = config.get("logging", {})
    logger  = setup_logging(
        log_file  = log_cfg.get("file", "logs/backup.log"),
        log_level = log_cfg.get("level", "INFO"),
    )

    # Filter single device if requested
    if args.device:
        config["devices"] = [d for d in config["devices"]
                             if d.get("name") == args.device or d.get("host") == args.device]
        if not config["devices"]:
            logger.error(f"No device found with name/host '{args.device}'")
            sys.exit(1)

    if args.dry_run:
        logger.info("=== DRY RUN MODE — FTP upload disabled ===")
        # Monkey-patch FTP upload
        import network_backup.backup as self_mod
        self_mod.ftp_upload = lambda *a, **kw: logger.info("DRY RUN: would upload to FTP")

    logger.info(f"=== Backup started — {len(config['devices'])} device(s) ===")
    results = run_backup(config, logger)

    # Summary
    ok     = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "failed")
    logger.info(f"=== Backup finished — OK: {ok}  FAILED: {failed} ===")

    # Print Zabbix UserParameter hints once
    status_file = config.get("zabbix", {}).get("status_file", "/var/tmp/network_backup_status.json")
    print("\n" + "="*60)
    print("ZABBIX UserParameter configuration (add to zabbix_agentd.conf):")
    print("="*60)
    print(f'UserParameter=netbackup.last_run,     python3 -c "import json; d=json.load(open(\'{status_file}\')); print(d[\'last_run\'])"')
    print(f'UserParameter=netbackup.total,         python3 -c "import json; d=json.load(open(\'{status_file}\')); print(d[\'total\'])"')
    print(f'UserParameter=netbackup.success,       python3 -c "import json; d=json.load(open(\'{status_file}\')); print(d[\'success\'])"')
    print(f'UserParameter=netbackup.failed,        python3 -c "import json; d=json.load(open(\'{status_file}\')); print(d[\'failed\'])"')
    print(f'UserParameter=netbackup.status_json,   cat {status_file}')
    print("="*60 + "\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
