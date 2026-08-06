# vmbuilder

Spin up local RHEL/Rocky/Debian lab VMs in seconds.

## Supported OS

| Distro      | Version                    |
|-------------|----------------------------|
| RHEL        | 7.9, 8.10, 9.7, 9.8, 10.2 |
| Rocky Linux | 7.9, 8.10, 9.8, 10.2      |
| Debian      | 12 (Bookworm), 13 (Trixie) |

## Requirements

- Linux host with KVM enabled
- `libvirt`, `virt-install`, `virt-customize` (`guestfs-tools` package)
- `qemu-img`
- Python 3.6+
- qcow2 images downloaded from the Red Hat / Rocky / Debian portal

## Installation

```bash
cd ~/claudeproject/vmbuilder
./install.sh
source ~/.bashrc   # first time only, if ~/bin wasn't already in PATH
```

Creates a `~/bin/vmbuilder` → `vmbuilder.py` symlink. After installation the
command is available from any directory.

## Usage

### Interactive menu

```bash
vmbuilder
```

Navigate with arrow keys ↑↓, Enter to select, `q` to quit.

### Direct CLI

```bash
# Step 1 — Build the golden image (once per OS version)
vmbuilder build --os rocky  --version 9.8  --src ~/downloads/Rocky-9.8-x86_64.qcow2
vmbuilder build --os rhel   --version 9.8  --src ~/downloads/rhel-9.8-x86_64.qcow2
vmbuilder build --os debian --version 12   --src ~/downloads/debian-12-amd64.qcow2

# Step 2 — Create a VM
vmbuilder create --os rocky --version 9.8 --name testlab-01
vmbuilder create --os rhel  --version 8.10 --name rhel-test --ram 4096 --vcpus 4

# Fresh install from ISO + kickstart (without a golden image)
vmbuilder ks-test --os rocky --version 9.8 \
  --iso ~/isos/Rocky-9.8-x86_64-dvd.iso \
  --ks kickstarts/ks_rocky9.cfg \
  --name ks-rocky9

# List VMs (with management IP for running VMs)
vmbuilder list

# List golden images in ORIGINAL_DIR
vmbuilder images

# List raw qcow2 source images in SOURCES_DIR
vmbuilder sources

# List ISO files in KS_ISO_DIR
vmbuilder isos

# List kickstart files in KS_PATH
vmbuilder kickstarts

# Destroy a VM
vmbuilder destroy testlab-01
```

## What gets applied to golden images (build)

| Operation                         | RHEL | Rocky | Debian |
|------------------------------------|:----:|:-----:|:------:|
| Root password (`Test1234!`)        | ✓    | ✓     | ✓      |
| Remove cloud-init                 | ✓    | ✓     | ✓      |
| PermitRootLogin yes               | ✓    | ✓     | ✓      |
| NIC naming kernel arg             | ✓    | ✓     | ✓      |
| bash-completion + vim             | ¹    | ✓     | ✓      |

¹ RHEL requires an active subscription — packages must be installed after boot.

## Network topology (per VM)

| NIC  | Network | IP                   | Purpose                 |
|------|---------|----------------------|--------------------------|
| NIC1 | default | 192.168.122.x (DHCP) | Management / SSH        |
| NIC2 | lab-net | 10.10.10.x (DHCP)    | Lab                     |
| NIC3 | lab-net | 10.10.10.x (DHCP)    | Bonding / teaming slave |
| NIC4 | lab-net | 10.10.10.x (DHCP)    | Bonding / teaming slave |

The `lab-net` network (isolated, 10.10.10.0/24) is created automatically if it
doesn't exist yet.

## RHEL — Automatic subscription (optional)

```bash
export RHSM_USER="user"
export RHSM_PASS="password"
vmbuilder create --os rhel --version 9.8 --name rhel-test
```

With these variables set, registration and installation of `bash-completion`
and `vim` run automatically via SSH after boot.

## Configuration

`install.sh` automatically copies the config to `~/.vmbuilder.conf` on first
install. Make all changes there — the file in the repo (`configs/lab.conf`)
stays untouched even after a `git pull`.

```bash
DEFAULT_RAM=2048          # default RAM for VMs (MB) — fallback if unset: 2048
DEFAULT_VCPUS=2           # default vCPUs — fallback if unset: 2
ROOT_PASSWORD="password"  # root password applied during build

MGMT_NET="default"        # libvirt management network (must already exist)
LAB_NET="lab-net"         # libvirt lab network (auto-created if missing)
LAB_NET_BRIDGE="virbr-lab"    # bridge name used when LAB_NET is auto-created
LAB_NET_IP="10.10.10.1"       # gateway IP used when LAB_NET is auto-created
LAB_NET_NETMASK="255.255.255.0"
LAB_NET_DHCP_START="10.10.10.10"
LAB_NET_DHCP_END="10.10.10.200"

# Image paths — directories requiring sudo are supported transparently
# Leave empty = folders next to the script (original/ and vms/)
ORIGINAL_DIR="/path/golden"
VMS_DIR="/path/vms"

# Folder scanned for raw qcow2 sources during 'build'
# If set, shows a selection menu instead of the manual prompt.
# Only affects the interactive TUI — the direct CLI (`build --src ...`)
# always requires an explicit path.
SOURCES_DIR="/path/sources"

# Folder scanned for ISOs during 'ks-test'
# If set, shows a selection menu instead of the manual prompt
KS_ISO_DIR="/path/iso"

# Kickstart for 'ks-test':
#   directory → TUI menu of the *.cfg files inside
#   file      → pre-fills the prompt (and becomes the CLI default for --ks)
#   empty     → always prompts manually; --ks becomes mandatory in the CLI
KS_PATH="/path/ks"

# Default disk size (GB) for VMs created with ks-test — fallback if unset: 20
KS_DISK_SIZE=20
```

### Config lookup

1. `~/.vmbuilder.conf` — user config (created by `install.sh`)
2. `<repo>/configs/lab.conf` — fallback/template

This is a **whole-file selection, not a per-key merge**: if `~/.vmbuilder.conf`
exists, it is read in full and `configs/lab.conf` is not consulted at all —
even for keys missing from the user config. Any variable without a hardcoded
fallback in the code (e.g. `MGMT_NET`, `LAB_NET*`, `ROOT_PASSWORD`) must be
present in whichever file is actually loaded.

### Storage on privileged paths

If `ORIGINAL_DIR` or `VMS_DIR` live on directories that require `sudo`
(separate mount points, `/mnt/...`, etc.), the script handles this
automatically — all disk and network operations run with `sudo`.

## A note on sudo

The script uses `sudo` internally for all privileged operations:

- `virsh` — network and VM management
- `qemu-img` — linked clone disk creation
- `virt-install` / `virt-customize` — provisioning
- `mkdir` / `cp` / `rm` — storage path operations

You do not need to run `vmbuilder` with `sudo` — the prefix is added
automatically where needed.

## Project structure

```
vmbuilder/
├── vmbuilder.py        # Single entry point (TUI menu + CLI)
├── install.sh          # Installs vmbuilder into ~/bin, creates ~/.vmbuilder.conf
├── configs/
│   └── lab.conf        # Configuration template (do not edit directly)
├── kickstarts/
│   ├── ks_rhel8.cfg    # RHEL 8 kickstart — minimal server
│   ├── ks_rhel9.cfg    # RHEL 9 kickstart — minimal server
│   ├── ks_rhel10.cfg   # RHEL 10 kickstart — minimal server
│   ├── ks_rocky8.cfg   # Rocky Linux 8 kickstart — minimal server
│   ├── ks_rocky9.cfg   # Rocky Linux 9 kickstart — minimal server
│   └── ks_rocky10.cfg  # Rocky Linux 10 kickstart — minimal server
├── original/           # Provisioned golden images (default)
└── vms/                # Cloned VM disks (default, created at runtime)
```
