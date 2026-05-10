# Network Device Backup Tool

A Python-based tool for automating configuration backups from network devices including:

- Cisco IOS
- Cisco Nexus
- FortiGate

This tool connects to devices over SSH, collects backups, stores them on an FTP server or SCP for supported IOS devices, and writes status information for Zabbix monitoring.

---

## Features

- Backup of multiple network device types
- Support for:
  - Cisco IOS
  - Cisco Nexus
  - FortiGate
- SSH-based device interaction
- FTP backup destination
- SCP support for Cisco IOS
- Automatic remote directory creation
- Logging support
- Zabbix-friendly JSON status output
- Per-device configuration via YAML

---

## Repository Structure

```
.
├── backup.py
├── config.yaml
├── requirements.txt
└── README.md
```

---

## Requirements

This project uses:

- paramiko>=3.4.0
- PyYAML>=6.0

Install dependencies with:

```bash
pip install -r requirements.txt
```

---

## Configuration

The script uses a YAML configuration file, by default:

```
config.yaml
```

You can also specify a different config file with the `-c` or `--config` option.

### Configuration sections

#### ftp
Defines the backup destination on the FTP server:

- host
- port
- username
- password
- passive
- base_dir

#### logging
Controls log output:

- file
- level

#### zabbix
Defines the JSON status file used for Zabbix integration:

- status_file

#### devices
List of network devices to back up.

Supported device types:

- cisco_ios
- cisco_nexus
- fortigate

Per-device fields may include:

- name
- site
- host
- type
- username
- password
- port
- timeout
- read_delay
- transport for IOS (ftp or scp)
- scp settings
- vrf for Nexus
- vdom for FortiGate

---

## Usage

Run the script with the default config:

```bash
python backup.py
```

Or specify a custom config file:

```bash
python backup.py --config config.yaml
```

---

## How It Works

1. The script loads configuration from YAML.
2. It connects to each device using SSH.
3. It executes the appropriate backup workflow based on device type.
4. The resulting backup is stored on the configured FTP server.
5. For supported IOS setups, SCP can also be used.
6. Logs are written and status data can be exported for Zabbix.

---

## Backup Destination

Backups are stored in a structured remote path based on:

- site name
- device name
- hostname
- timestamp

Example structure:

```
<ftp_base_dir>/<site_name>/<device_name>/<hostname>_YYYYMMDD_HHMMSS.cfg
```

---

## Supported Device Types

### Cisco IOS

- Supports FTP backup
- Supports SCP backup
- Transport is configurable

### Cisco Nexus

- Supports backup via SSH interaction
- Can include VRF-related configuration based on config

### FortiGate

- Supports backup via SSH interaction
- Can include VDOM-related configuration based on config

---

## Logging

The tool supports configurable logging. You can define:

- log file path
- logging level

---

## Zabbix Integration

The project includes support for generating a JSON status file which can be used by Zabbix UserParameter or other monitoring integrations.

---

## Example Configuration

```yaml
ftp:
  host: 192.0.2.10
  port: 21
  username: backupuser
  password: secret
  passive: true
  base_dir: /backups

logging:
  file: backup.log
  level: INFO

zabbix:
  status_file: /var/tmp/backup_status.json

devices:
  - name: core-switch-1
    site: tehran
    host: 192.0.2.20
    type: cisco_ios
    username: admin
    password: admin123
    transport: ftp
```

---

## Contributing

Contributions are welcome. Feel free to open an issue or submit a pull request.
