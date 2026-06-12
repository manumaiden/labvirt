#!/usr/bin/env python3
"""vmbuilder — RHEL / Rocky / Debian lab VM launcher"""

import os
import sys
import tty
import termios
import subprocess
import time
import argparse
import shlex
from pathlib import Path
from textwrap import dedent

# ── Constants ─────────────────────────────────────────────────────────────────

# resolve() before parent so symlinks in ~/bin point back to the real project dir
SCRIPT_DIR      = Path(__file__).resolve().parent
USER_CONFIG     = Path.home() / ".vmbuilder.conf"
DEFAULT_CONFIG  = SCRIPT_DIR / "configs" / "lab.conf"
CONFIG_FILE     = USER_CONFIG if USER_CONFIG.exists() else DEFAULT_CONFIG

SUPPORTED_OS = {
    "rhel":   ["7.9", "8.10", "9.7", "9.8", "10.2"],
    "rocky":  ["7.9", "8.10", "9.8", "10.2"],
    "debian": ["12", "13"],
}

OS_VARIANTS = {
    "rhel-7.9":   "rhel7.9",
    "rhel-8.10":  "rhel8-unknown",
    "rhel-9.7":   "rhel9-unknown",
    "rhel-9.8":   "rhel9-unknown",
    "rhel-10.2":  "rhel9-unknown",
    "rocky-7.9":  "rhel7.9",
    "rocky-8.10": "rhel8-unknown",
    "rocky-9.8":  "rhel9-unknown",
    "rocky-10.2": "rhel9-unknown",
    "debian-12":  "debian12",
    "debian-13":  "debian13",
}

# ── ANSI helpers ──────────────────────────────────────────────────────────────

class C:
    RST     = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    BG_BLUE = "\033[44m"

def ok(msg):   print(f"  {C.GREEN}✓{C.RST}  {msg}")
def info(msg): print(f"  {C.CYAN}→{C.RST}  {msg}")
def warn(msg): print(f"  {C.YELLOW}⚠{C.RST}  {msg}")
def err(msg):  print(f"  {C.RED}✗{C.RST}  {msg}", file=sys.stderr)

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    cfg = {}
    with open(CONFIG_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"')

    # Resolve storage paths: use lab.conf values if set, else default to script dir
    cfg["ORIGINAL_DIR"] = Path(cfg["ORIGINAL_DIR"]).expanduser() \
        if cfg.get("ORIGINAL_DIR") else SCRIPT_DIR / "original"
    cfg["VMS_DIR"] = Path(cfg["VMS_DIR"]).expanduser() \
        if cfg.get("VMS_DIR") else SCRIPT_DIR / "vms"
    cfg["SOURCES_DIR"] = Path(cfg["SOURCES_DIR"]).expanduser() \
        if cfg.get("SOURCES_DIR") else None
    cfg["KS_ISO_DIR"] = Path(cfg["KS_ISO_DIR"]).expanduser() \
        if cfg.get("KS_ISO_DIR") else None
    cfg["KS_PATH"] = Path(cfg["KS_PATH"]).expanduser() \
        if cfg.get("KS_PATH") else None

    return cfg

# ── Shell helpers ─────────────────────────────────────────────────────────────

def run(cmd, sudo=False, capture=False, check=True):
    if sudo:
        cmd = ["sudo"] + list(cmd)
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, check=check)
    return subprocess.run(cmd, check=check)

def virsh(*args):
    return subprocess.run(["sudo", "virsh"] + list(args), capture_output=True, text=True)

def _exists(path):
    r = subprocess.run(["sudo", "test", "-e", str(path)], capture_output=True)
    return r.returncode == 0

# ── TUI menu ──────────────────────────────────────────────────────────────────

def _read_key():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.buffer.read(1)
        if ch == b"\x1b":
            rest = sys.stdin.buffer.read(2)
            if rest == b"[A": return "UP"
            if rest == b"[B": return "DOWN"
            return "ESC"
        if ch in (b"\r", b"\n"):  return "ENTER"
        if ch in (b"q", b"\x03"): return "QUIT"
        return ch.decode("utf-8", errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def menu(title, options, hint=""):
    """Arrow-key menu. options = [(label, value), ...]. Returns value or None."""
    selected = 0
    while True:
        sys.stdout.write("\033[2J\033[H")
        print(f"\n  {C.BOLD}{C.CYAN}vmbuilder{C.RST}  "
              f"{C.DIM}RHEL · Rocky · Debian lab launcher{C.RST}")
        print(f"  {C.DIM}{'─' * 50}{C.RST}\n")
        if hint:
            print(f"  {C.YELLOW}{hint}{C.RST}\n")
        print(f"  {C.BOLD}{title}{C.RST}\n")
        for i, (label, _) in enumerate(options):
            if i == selected:
                print(f"  {C.BG_BLUE}{C.WHITE}{C.BOLD}  {label:<32}  {C.RST}")
            else:
                print(f"    {label}")
        print(f"\n  {C.DIM}↑↓ navigate   Enter select   q quit{C.RST}")
        sys.stdout.flush()

        key = _read_key()
        if key == "UP":
            selected = (selected - 1) % len(options)
        elif key == "DOWN":
            selected = (selected + 1) % len(options)
        elif key == "ENTER":
            return options[selected][1]
        elif key in ("QUIT", "ESC"):
            return None


def _prompt(label, default=""):
    hint = f" [{default}]" if default else ""
    val  = input(f"\n  {C.CYAN}?{C.RST}  {label}{hint}: ").strip()
    return val or default


def _pause():
    input(f"\n  {C.DIM}Press Enter to continue...{C.RST}")

# ── Core: build ───────────────────────────────────────────────────────────────

def build_image(os_name, version, src, cfg):
    src_path = Path(src).expanduser()
    if not src_path.is_file():
        err(f"Source image not found: {src_path}")
        sys.exit(1)

    original_dir = cfg["ORIGINAL_DIR"]
    dest = original_dir / f"{os_name}-{version}-base.qcow2"
    run(["mkdir", "-p", str(original_dir)], sudo=True)

    if _exists(dest):
        ans = input(f"\n  {C.YELLOW}⚠{C.RST}  {dest.name} already exists — overwrite? [y/N] ").strip().lower()
        if ans != "y":
            info("Aborted.")
            return

    info(f"Copying  {src_path.name}  →  {dest.name}")
    run(["cp", "--sparse=always", str(src_path), str(dest)], sudo=True)

    info("Running virt-customize...")

    args = [
        "virt-customize", "-a", str(dest),
        "--root-password", f"password:{cfg['ROOT_PASSWORD']}",
        "--uninstall", "cloud-init",
        "--run-command",
        "sed -i '/^#*PermitRootLogin/d' /etc/ssh/sshd_config "
        "&& echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config",
    ]

    if os_name in ("rhel", "rocky"):
        args += ["--run-command",
                 "grubby --update-kernel=ALL --remove-args=net.ifnames=0"]
        if os_name == "rocky":
            args += ["--install", "bash-completion,vim"]
    elif os_name == "debian":
        args += [
            "--run-command",
            "sed -i '/^GRUB_CMDLINE_LINUX=/s/\"$/ net.ifnames=0 biosdevname=0\"/' "
            "/etc/default/grub && update-grub",
            "--install", "bash-completion,vim",
        ]

    run(args, sudo=True)
    print()
    ok(f"Golden image ready: {dest}")

    if os_name == "rhel":
        print()
        warn("RHEL requires an active subscription for packages.")
        print(f"  {C.DIM}After first boot:")
        print(f"    subscription-manager register --username USER --password PASS --auto-attach")
        print(f"    yum install -y bash-completion vim   # RHEL 7")
        print(f"    dnf install -y bash-completion vim   # RHEL 8/9/10")
        print(f"  Or export RHSM_USER / RHSM_PASS before 'create' for auto-registration.{C.RST}")

# ── Core: create ──────────────────────────────────────────────────────────────

def _ensure_lab_net(cfg):
    r = virsh("net-info", cfg["LAB_NET"])
    if r.returncode != 0:
        info(f"Creating isolated network {cfg['LAB_NET']} ({cfg['LAB_NET_IP']}/24)")
        xml = dedent(f"""\
            <network>
              <name>{cfg['LAB_NET']}</name>
              <forward mode='none'/>
              <bridge name='{cfg['LAB_NET_BRIDGE']}' stp='on' delay='0'/>
              <ip address='{cfg['LAB_NET_IP']}' netmask='{cfg['LAB_NET_NETMASK']}'>
                <dhcp>
                  <range start='{cfg['LAB_NET_DHCP_START']}' end='{cfg['LAB_NET_DHCP_END']}'/>
                </dhcp>
              </ip>
            </network>""")
        xml_path = Path("/tmp/vmbuilder-lab-net.xml")
        xml_path.write_text(xml)
        run(["virsh", "net-define",    str(xml_path)], sudo=True)
        run(["virsh", "net-start",     cfg["LAB_NET"]], sudo=True)
        run(["virsh", "net-autostart", cfg["LAB_NET"]], sudo=True)
        xml_path.unlink(missing_ok=True)
        ok(f"Network {cfg['LAB_NET']} created")
    elif not any(l.strip().startswith("Active:") and "yes" in l for l in r.stdout.splitlines()):
        run(["virsh", "net-start", cfg["LAB_NET"]], sudo=True)
        ok(f"Network {cfg['LAB_NET']} started")


def _get_mgmt_ip(vm_name, mgmt_net):
    r = virsh("domiflist", vm_name)
    mac = None
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[2] == mgmt_net:
            mac = parts[4]
            break
    if not mac:
        return None
    r = virsh("net-dhcp-leases", mgmt_net)
    for line in r.stdout.splitlines():
        if mac.lower() in line.lower():
            for part in line.split():
                if "/" in part and part[0].isdigit():
                    return part.split("/")[0]
    return None


def create_vm(os_name, version, vm_name, ram, vcpus, cfg):
    base_image = cfg["ORIGINAL_DIR"] / f"{os_name}-{version}-base.qcow2"
    vm_disk    = cfg["VMS_DIR"] / f"{vm_name}.qcow2"

    if not _exists(base_image):
        err(f"Golden image not found: {base_image}")
        err(f"Run: ./vmbuilder.py build --os {os_name} --version {version} --src <path>")
        sys.exit(1)

    if virsh("dominfo", vm_name).returncode == 0:
        err(f"VM '{vm_name}' already exists. Destroy it first.")
        sys.exit(1)

    run(["mkdir", "-p", str(cfg["VMS_DIR"])], sudo=True)
    _ensure_lab_net(cfg)

    info(f"Creating linked clone: {vm_disk.name}")
    run(["qemu-img", "create", "-f", "qcow2",
         "-b", str(base_image), "-F", "qcow2", str(vm_disk)], sudo=True)

    os_variant = OS_VARIANTS.get(f"{os_name}-{version}", "generic")

    info(f"Starting  {vm_name}  [{os_name} {version} | {ram} MB | {vcpus} vCPU]")
    info(f"NIC1: {cfg['MGMT_NET']} (management)   NIC2-4: {cfg['LAB_NET']} (lab 10.10.10.0/24)")

    run([
        "virt-install",
        "--name",       vm_name,
        "--memory",     str(ram),
        "--vcpus",      str(vcpus),
        "--disk",       f"{vm_disk},format=qcow2,bus=virtio",
        "--import",
        "--os-variant", os_variant,
        "--network",    f"network={cfg['MGMT_NET']},model=virtio",
        "--network",    f"network={cfg['LAB_NET']},model=virtio",
        "--network",    f"network={cfg['LAB_NET']},model=virtio",
        "--network",    f"network={cfg['LAB_NET']},model=virtio",
        "--graphics",   "none",
        "--noautoconsole",
    ], sudo=True)

    info("Waiting for DHCP lease on management network...")
    mgmt_ip = None
    for i in range(1, 31):
        time.sleep(2)
        mgmt_ip = _get_mgmt_ip(vm_name, cfg["MGMT_NET"])
        if mgmt_ip:
            break
        if i % 5 == 0:
            info(f"Still waiting... ({i * 2}s)")

    print()
    if mgmt_ip:
        ok("VM ready!")
        print(f"\n  {'VM':<10}: {C.BOLD}{vm_name}{C.RST}")
        print(f"  {'OS':<10}: {os_name} {version}")
        print(f"  {'SSH':<10}: {C.GREEN}ssh root@{mgmt_ip}{C.RST}")
        print(f"  {'Password':<10}: {cfg['ROOT_PASSWORD']}")

        rhsm_user = os.environ.get("RHSM_USER", "")
        rhsm_pass = os.environ.get("RHSM_PASS", "")
        if os_name == "rhel" and rhsm_user and rhsm_pass:
            print()
            info("Registering RHEL subscription via SSH...")
            cmd = (
                f"subscription-manager register "
                f"--username {shlex.quote(rhsm_user)} --password {shlex.quote(rhsm_pass)} --auto-attach "
                f"&& dnf install -y bash-completion vim"
            )
            subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=10", f"root@{mgmt_ip}", cmd],
                check=False,
            )
        elif os_name == "rhel":
            print()
            warn("Register subscription manually:")
            print(f"  {C.DIM}subscription-manager register --username USER --password PASS --auto-attach")
            print(f"  dnf install -y bash-completion vim{C.RST}")
    else:
        warn("VM started but IP not detected yet.")
        info(f"Check with: virsh net-dhcp-leases {cfg['MGMT_NET']}")

# ── Core: ks-test ─────────────────────────────────────────────────────────────

def ks_test_vm(ks_path, iso_path, os_name, version, vm_name, ram, vcpus, disk_gb, cfg):
    ks_path  = Path(ks_path).expanduser()
    iso_path = Path(iso_path).expanduser()

    if not ks_path.is_file():
        err(f"Kickstart file not found: {ks_path}")
        sys.exit(1)
    if not iso_path.is_file():
        err(f"ISO not found: {iso_path}")
        sys.exit(1)

    if virsh("dominfo", vm_name).returncode == 0:
        err(f"VM '{vm_name}' already exists. Destroy it first.")
        sys.exit(1)

    run(["mkdir", "-p", str(cfg["VMS_DIR"])], sudo=True)
    _ensure_lab_net(cfg)

    vm_disk = cfg["VMS_DIR"] / f"{vm_name}.qcow2"
    info(f"Creating disk: {vm_disk.name} ({disk_gb} GB)")
    run(["qemu-img", "create", "-f", "qcow2", str(vm_disk), f"{disk_gb}G"], sudo=True)

    os_variant  = OS_VARIANTS.get(f"{os_name}-{version}", "generic")
    ks_filename = ks_path.name

    info(f"Starting installation: {vm_name}  [{os_name} {version} | {ram} MB | {vcpus} vCPU]")
    info(f"NIC1: {cfg['MGMT_NET']} (management)   NIC2-4: {cfg['LAB_NET']} (lab 10.10.10.0/24)")
    info("Console attached — installation output will appear below.")
    print()

    run([
        "virt-install",
        "--name",          vm_name,
        "--memory",        str(ram),
        "--vcpus",         str(vcpus),
        "--disk",          f"{vm_disk},format=qcow2,bus=virtio",
        "--location",      str(iso_path),
        "--initrd-inject", str(ks_path),
        "--extra-args",    f"inst.ks=file:/{ks_filename} console=ttyS0",
        "--os-variant",    os_variant,
        "--network",       f"network={cfg['MGMT_NET']},model=virtio",
        "--network",       f"network={cfg['LAB_NET']},model=virtio",
        "--network",       f"network={cfg['LAB_NET']},model=virtio",
        "--network",       f"network={cfg['LAB_NET']},model=virtio",
        "--nographics",
    ], sudo=True)

    info("Waiting for DHCP lease on management network...")
    mgmt_ip = None
    for i in range(1, 31):
        time.sleep(2)
        mgmt_ip = _get_mgmt_ip(vm_name, cfg["MGMT_NET"])
        if mgmt_ip:
            break
        if i % 5 == 0:
            info(f"Still waiting... ({i * 2}s)")

    print()
    if mgmt_ip:
        ok("VM ready!")
        print(f"\n  {'VM':<10}: {C.BOLD}{vm_name}{C.RST}")
        print(f"  {'OS':<10}: {os_name} {version}")
        print(f"  {'SSH':<10}: {C.GREEN}ssh root@{mgmt_ip}{C.RST}")
        print(f"  {'Password':<10}: {cfg['ROOT_PASSWORD']}")
    else:
        warn("VM started but IP not detected yet.")
        info(f"Check with: virsh net-dhcp-leases {cfg['MGMT_NET']}")

# ── Core: destroy ─────────────────────────────────────────────────────────────

def destroy_vm(vm_name, cfg):
    if virsh("dominfo", vm_name).returncode != 0:
        err(f"VM '{vm_name}' not found.")
        sys.exit(1)

    info(f"Destroying VM: {vm_name}")

    if "running" in virsh("dominfo", vm_name).stdout:
        run(["virsh", "destroy", vm_name], sudo=True)

    r = virsh("undefine", vm_name, "--remove-all-storage")
    if r.returncode != 0:
        virsh("undefine", vm_name)
        leftover = cfg["VMS_DIR"] / f"{vm_name}.qcow2"
        if _exists(leftover):
            run(["rm", "-f", str(leftover)], sudo=True)

    ok(f"VM '{vm_name}' removed.")

# ── Core: list ────────────────────────────────────────────────────────────────

def list_vms():
    r = virsh("list", "--all", "--name")
    names = [n.strip() for n in r.stdout.splitlines() if n.strip()]
    print()
    if not names:
        info("No VMs found.")
        print()
        return

    rows = []
    for name in names:
        info_r = virsh("dominfo", name)
        state, vm_id = "unknown", "-"
        for line in info_r.stdout.splitlines():
            if line.startswith("State:"):
                state = line.split(":", 1)[1].strip()
            elif line.startswith("Id:"):
                vm_id = line.split(":", 1)[1].strip()

        blk_r = virsh("domblklist", name)
        disk = "-"
        for line in blk_r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "vda":
                disk = parts[1]
                break

        rows.append((vm_id, name, state, disk))

    id_w    = max(2,  max(len(r[0]) for r in rows))
    name_w  = max(4,  max(len(r[1]) for r in rows))
    state_w = max(5,  max(len(r[2]) for r in rows))
    disk_w  = max(4,  max(len(r[3]) for r in rows))

    print(f"  {C.BOLD}{'ID':<{id_w}}  {'Name':<{name_w}}  {'State':<{state_w}}  {'Disk':<{disk_w}}{C.RST}")
    print(f"  {'─'*id_w}  {'─'*name_w}  {'─'*state_w}  {'─'*disk_w}")
    for vm_id, name, state, disk in rows:
        col = C.GREEN if state == "running" else C.DIM
        print(f"  {vm_id:<{id_w}}  {col}{name:<{name_w}}{C.RST}  {state:<{state_w}}  {C.DIM}{disk}{C.RST}")
    print()

# ── Core: images ──────────────────────────────────────────────────────────────

def list_images(cfg):
    original_dir = cfg["ORIGINAL_DIR"]
    print()
    if not _exists(original_dir):
        info(f"ORIGINAL_DIR not found: {original_dir}")
        print()
        return

    r = subprocess.run(
        ["sudo", "find", str(original_dir), "-maxdepth", "1", "-name", "*-base.qcow2", "-type", "f"],
        capture_output=True, text=True,
    )
    files = sorted(p.strip() for p in r.stdout.splitlines() if p.strip())
    if not files:
        info("No golden images found.")
        print()
        return

    import datetime
    rows = []
    for f in files:
        sr = subprocess.run(["sudo", "stat", "-c", "%s %Y", f], capture_output=True, text=True)
        try:
            size_b, mtime = sr.stdout.strip().split()
            size     = int(size_b)
            size_str = f"{size / 1_073_741_824:.1f} GB" if size >= 1_073_741_824 \
                       else f"{size / 1_048_576:.0f} MB"
            mod      = datetime.datetime.fromtimestamp(int(mtime)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            size_str, mod = "?", "?"
        rows.append((Path(f).name, size_str, mod))

    name_w = max(4, max(len(r[0]) for r in rows))
    size_w = max(4, max(len(r[1]) for r in rows))

    print(f"  {C.BOLD}{'Name':<{name_w}}  {'Size':<{size_w}}  Modified{C.RST}")
    print(f"  {'─'*name_w}  {'─'*size_w}  {'─'*16}")
    for name, size_str, mod in rows:
        print(f"  {name:<{name_w}}  {size_str:<{size_w}}  {C.DIM}{mod}{C.RST}")
    print()

# ── Core: isos ────────────────────────────────────────────────────────────────

def list_isos(cfg):
    iso_dir = cfg.get("KS_ISO_DIR")
    print()
    if not iso_dir:
        info("KS_ISO_DIR not configured.")
        print()
        return
    if not _exists(iso_dir):
        info(f"KS_ISO_DIR not found: {iso_dir}")
        print()
        return

    r = subprocess.run(
        ["sudo", "find", str(iso_dir), "-maxdepth", "1", "-name", "*.iso", "-type", "f"],
        capture_output=True, text=True,
    )
    files = sorted(p.strip() for p in r.stdout.splitlines() if p.strip())
    if not files:
        info("No ISO files found.")
        print()
        return

    import datetime
    rows = []
    for f in files:
        sr = subprocess.run(["sudo", "stat", "-c", "%s %Y", f], capture_output=True, text=True)
        try:
            size_b, mtime = sr.stdout.strip().split()
            size     = int(size_b)
            size_str = f"{size / 1_073_741_824:.1f} GB" if size >= 1_073_741_824 \
                       else f"{size / 1_048_576:.0f} MB"
            mod      = datetime.datetime.fromtimestamp(int(mtime)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            size_str, mod = "?", "?"
        rows.append((Path(f).name, size_str, mod))

    name_w = max(4, max(len(r[0]) for r in rows))
    size_w = max(4, max(len(r[1]) for r in rows))

    print(f"  {C.BOLD}{'Name':<{name_w}}  {'Size':<{size_w}}  Modified{C.RST}")
    print(f"  {'─'*name_w}  {'─'*size_w}  {'─'*16}")
    for name, size_str, mod in rows:
        print(f"  {name:<{name_w}}  {size_str:<{size_w}}  {C.DIM}{mod}{C.RST}")
    print()

# ── Interactive flows ─────────────────────────────────────────────────────────

def _list_sources(sources_dir):
    r = subprocess.run(
        ["sudo", "find", str(sources_dir), "-maxdepth", "1", "-name", "*.qcow2", "-type", "f"],
        capture_output=True, text=True,
    )
    files = sorted(p.strip() for p in r.stdout.splitlines() if p.strip())
    result = []
    for f in files:
        sr = subprocess.run(["sudo", "stat", "-c", "%s", f], capture_output=True, text=True)
        try:
            size = int(sr.stdout.strip())
            size_str = f"{size / 1_073_741_824:.1f} GB" if size >= 1_073_741_824 \
                       else f"{size / 1_048_576:.0f} MB"
        except ValueError:
            size_str = "?"
        result.append((f"{Path(f).name}  {C.DIM}{size_str}{C.RST}", f))
    return result


def _list_isos(iso_dir):
    r = subprocess.run(
        ["sudo", "find", str(iso_dir), "-maxdepth", "1", "-name", "*.iso", "-type", "f"],
        capture_output=True, text=True,
    )
    files = sorted(p.strip() for p in r.stdout.splitlines() if p.strip())
    result = []
    for f in files:
        sr = subprocess.run(["sudo", "stat", "-c", "%s", f], capture_output=True, text=True)
        try:
            size = int(sr.stdout.strip())
            size_str = f"{size / 1_073_741_824:.1f} GB" if size >= 1_073_741_824 \
                       else f"{size / 1_048_576:.0f} MB"
        except ValueError:
            size_str = "?"
        result.append((f"{Path(f).name}  {C.DIM}{size_str}{C.RST}", f))
    return result


def _list_ks_files(ks_dir):
    r = subprocess.run(
        ["find", str(ks_dir), "-maxdepth", "1", "-name", "*.cfg", "-type", "f"],
        capture_output=True, text=True,
    )
    files = sorted(p.strip() for p in r.stdout.splitlines() if p.strip())
    return [(Path(f).name, f) for f in files]


def _interactive_build(cfg):
    os_ch = menu("Select OS", [(o, o) for o in SUPPORTED_OS], "Build golden image")
    if not os_ch:
        return
    ver_ch = menu("Select version",
                  [(v, v) for v in SUPPORTED_OS[os_ch]],
                  f"Build golden image — {os_ch}")
    if not ver_ch:
        return

    src = None
    sources_dir = cfg.get("SOURCES_DIR")
    if sources_dir and _exists(sources_dir):
        choices = _list_sources(sources_dir)
        if choices:
            choices.append(("[ Inserisci path manualmente ]", "__manual__"))
            picked = menu("Select source qcow2", choices,
                          f"Build golden image — {os_ch} {ver_ch}")
            if not picked:
                return
            if picked != "__manual__":
                src = picked

    if src is None:
        sys.stdout.write("\033[2J\033[H")
        src = _prompt("Source qcow2 path")
        if not src:
            warn("No path provided.")
            return

    print()
    try:
        build_image(os_ch, ver_ch, src, cfg)
    except SystemExit:
        pass


def _interactive_create(cfg):
    os_ch = menu("Select OS", [(o, o) for o in SUPPORTED_OS], "Create VM")
    if not os_ch:
        return
    ver_ch = menu("Select version",
                  [(v, v) for v in SUPPORTED_OS[os_ch]],
                  f"Create VM — {os_ch}")
    if not ver_ch:
        return
    sys.stdout.write("\033[2J\033[H")
    vm_name = _prompt("VM name", f"{os_ch}-{ver_ch}-01")
    ram     = int(_prompt("RAM (MB)", cfg.get("DEFAULT_RAM", "2048")))
    vcpus   = int(_prompt("vCPUs",    cfg.get("DEFAULT_VCPUS", "2")))
    print()
    try:
        create_vm(os_ch, ver_ch, vm_name, ram, vcpus, cfg)
    except SystemExit:
        pass


def _interactive_ks_test(cfg):
    os_ch = menu("Select OS", [(o, o) for o in SUPPORTED_OS], "KS test")
    if not os_ch:
        return
    ver_ch = menu("Select version",
                  [(v, v) for v in SUPPORTED_OS[os_ch]],
                  f"KS test — {os_ch}")
    if not ver_ch:
        return

    iso_path = None
    iso_dir  = cfg.get("KS_ISO_DIR")
    if iso_dir and _exists(iso_dir):
        choices = _list_isos(iso_dir)
        if choices:
            choices.append(("[ Inserisci path manualmente ]", "__manual__"))
            picked = menu("Select ISO", choices, f"KS test — {os_ch} {ver_ch}")
            if not picked:
                return
            if picked != "__manual__":
                iso_path = picked

    ks_path = None
    ks_cfg  = cfg.get("KS_PATH")
    if ks_cfg and ks_cfg.is_dir():
        choices = _list_ks_files(ks_cfg)
        if choices:
            choices.append(("[ Inserisci path manualmente ]", "__manual__"))
            picked = menu("Select kickstart", choices, f"KS test — {os_ch} {ver_ch}")
            if not picked:
                return
            if picked != "__manual__":
                ks_path = picked

    sys.stdout.write("\033[2J\033[H")
    if iso_path is None:
        iso_path = _prompt("ISO path")
        if not iso_path:
            warn("No ISO path provided.")
            return

    if ks_path is None:
        default = str(ks_cfg) if ks_cfg and ks_cfg.is_file() else ""
        ks_path = _prompt("Kickstart path", default)
        if not ks_path:
            warn("No kickstart path provided.")
            return

    vm_name = _prompt("VM name", f"ks-test-{os_ch}-{ver_ch}")
    ram     = int(_prompt("RAM (MB)", cfg.get("DEFAULT_RAM", "2048")))
    vcpus   = int(_prompt("vCPUs",    cfg.get("DEFAULT_VCPUS", "2")))
    disk_gb = int(_prompt("Disk (GB)", str(cfg.get("KS_DISK_SIZE", "20"))))
    print()
    try:
        ks_test_vm(ks_path, iso_path, os_ch, ver_ch, vm_name, ram, vcpus, disk_gb, cfg)
    except SystemExit:
        pass


def _interactive_destroy(cfg):
    r = virsh("list", "--all", "--name")
    names = [n.strip() for n in r.stdout.splitlines() if n.strip()]
    if not names:
        sys.stdout.write("\033[2J\033[H")
        warn("No VMs found.")
        _pause()
        return
    choice = menu("Select VM to destroy", [(n, n) for n in names], "Destroy VM")
    if not choice:
        return
    sys.stdout.write("\033[2J\033[H")
    confirm = input(
        f"\n  {C.RED}Destroy '{choice}'? This cannot be undone. [y/N]{C.RST} "
    ).strip().lower()
    print()
    if confirm == "y":
        destroy_vm(choice, cfg)
    else:
        info("Aborted.")
    _pause()


def _interactive_list():
    sys.stdout.write("\033[2J\033[H")
    list_vms()
    _pause()


def _interactive_list_images(cfg):
    sys.stdout.write("\033[2J\033[H")
    list_images(cfg)
    _pause()


def _interactive_list_isos(cfg):
    sys.stdout.write("\033[2J\033[H")
    list_isos(cfg)
    _pause()


def interactive_main(cfg):
    actions = [
        ("Build image  (OS → golden qcow2)",    "build"),
        ("Create VM    (golden → linked clone)", "create"),
        ("Install VM from ISO+ks",               "ks-test"),
        ("Destroy VM",                           "destroy"),
        ("List VMs",                             "list"),
        ("List images",                          "images"),
        ("List ISOs",                            "isos"),
        ("Quit",                                 "quit"),
    ]
    while True:
        action = menu("Main menu", actions)
        if action in (None, "quit"):
            sys.stdout.write("\033[2J\033[H\n")
            break
        elif action == "build":
            _interactive_build(cfg)
            _pause()
        elif action == "create":
            _interactive_create(cfg)
            _pause()
        elif action == "ks-test":
            _interactive_ks_test(cfg)
            _pause()
        elif action == "destroy":
            _interactive_destroy(cfg)
        elif action == "list":
            _interactive_list()
        elif action == "images":
            _interactive_list_images(cfg)
        elif action == "isos":
            _interactive_list_isos(cfg)

# ── CLI ───────────────────────────────────────────────────────────────────────

def cli_main(cfg):
    ap  = argparse.ArgumentParser(prog="vmbuilder.py",
                                  description="RHEL/Rocky/Debian lab VM launcher")
    sub = ap.add_subparsers(dest="cmd")

    pb = sub.add_parser("build",   help="Build a golden image")
    pb.add_argument("--os",      required=True, choices=list(SUPPORTED_OS))
    pb.add_argument("--version", required=True)
    pb.add_argument("--src",     required=True)

    pc = sub.add_parser("create",  help="Create a VM from a golden image")
    pc.add_argument("--os",      required=True, choices=list(SUPPORTED_OS))
    pc.add_argument("--version", required=True)
    pc.add_argument("--name",    required=True)
    pc.add_argument("--ram",     type=int, default=int(cfg.get("DEFAULT_RAM", 2048)))
    pc.add_argument("--vcpus",   type=int, default=int(cfg.get("DEFAULT_VCPUS", 2)))

    pd = sub.add_parser("destroy", help="Destroy a VM")
    pd.add_argument("name")

    sub.add_parser("list",   help="List all VMs")
    sub.add_parser("images", help="List golden images in ORIGINAL_DIR")
    sub.add_parser("isos",   help="List ISO files in KS_ISO_DIR")

    pk = sub.add_parser("ks-test", help="Install a VM from ISO + kickstart")
    pk.add_argument("--ks",      default=str(cfg["KS_PATH"]) if cfg.get("KS_PATH") else None,
                                 help="Path to ks.cfg")
    pk.add_argument("--iso",     required=True, help="Path to installation ISO")
    pk.add_argument("--os",      required=True, choices=list(SUPPORTED_OS))
    pk.add_argument("--version", required=True)
    pk.add_argument("--name",    required=True)
    pk.add_argument("--ram",     type=int, default=int(cfg.get("DEFAULT_RAM", 2048)))
    pk.add_argument("--vcpus",   type=int, default=int(cfg.get("DEFAULT_VCPUS", 2)))
    pk.add_argument("--disk",    type=int, default=int(cfg.get("KS_DISK_SIZE", 20)), dest="disk_gb")

    args = ap.parse_args()

    if args.cmd == "build":
        build_image(args.os, args.version, args.src, cfg)
    elif args.cmd == "create":
        create_vm(args.os, args.version, args.name, args.ram, args.vcpus, cfg)
    elif args.cmd == "ks-test":
        if not args.ks:
            ap.error("--ks is required (or set KS_PATH in config)")
        ks_test_vm(args.ks, args.iso, args.os, args.version,
                   args.name, args.ram, args.vcpus, args.disk_gb, cfg)
    elif args.cmd == "destroy":
        destroy_vm(args.name, cfg)
    elif args.cmd == "list":
        list_vms()
    elif args.cmd == "images":
        list_images(cfg)
    elif args.cmd == "isos":
        list_isos(cfg)
    else:
        ap.print_help()

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    if len(sys.argv) > 1:
        cli_main(cfg)
    elif sys.stdin.isatty() and sys.stdout.isatty():
        interactive_main(cfg)
    else:
        err("No arguments provided. Run with --help or in an interactive terminal.")
        sys.exit(1)


if __name__ == "__main__":
    main()
