#!/usr/bin/env python3
"""labvirt — RHEL / Rocky / Debian lab VM launcher"""

import os
import sys
import tty
import termios
import select
import subprocess
import socket
import time
import argparse
import shlex
import json
import tempfile
import datetime
from pathlib import Path
from textwrap import dedent

__version__    = "1.10"
__build_date__ = "30102026"

# ── Constants ─────────────────────────────────────────────────────────────────

# resolve() before parent so symlinks in ~/bin point back to the real project dir
SCRIPT_DIR      = Path(__file__).resolve().parent
USER_CONFIG     = Path.home() / ".labvirt.conf"
DEFAULT_CONFIG  = SCRIPT_DIR / "configs" / "lab.conf"
CONFIG_FILE     = USER_CONFIG if USER_CONFIG.exists() else DEFAULT_CONFIG

SUPPORTED_OS = {
    "rhel":   ["7.9", "8.10", "9.7", "9.8", "10.2"],
    "rocky":  ["8.10", "9.8", "10.2"],
    "debian": ["12", "13"],
}

# Full accepted version range per OS (CLI --version). SUPPORTED_OS above stays
# a curated pick-list for the TUI menu, which can't offer free-text entry.
SUPPORTED_OS_RANGE = {
    "rhel":   ("7.9", "10.2"),
    "rocky":  ("8.10", "10.2"),
    "debian": ("12", "13"),
}

# Kickstart is Anaconda-only: the Debian installer ignores inst.ks and waits on the console
KS_OS = ["rhel", "rocky"]

# DHCP recycles IPs across ephemeral VMs; a stale known_hosts entry makes ssh
# silently disable password auth, so never record host keys
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR"]

def _parse_version(v):
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return None

def _version_supported(os_name, version):
    lo, hi = SUPPORTED_OS_RANGE.get(os_name, (None, None))
    if lo is None:
        return False
    v, v_lo, v_hi = _parse_version(version), _parse_version(lo), _parse_version(hi)
    if v is None or v_lo is None or len(v) != len(v_lo):
        return False
    return v_lo <= v <= v_hi

OS_VARIANTS = {
    "rhel-7.9":   "rhel7.9",
    "rhel-8.10":  "rhel8-unknown",
    "rhel-9.7":   "rhel9-unknown",
    "rhel-9.8":   "rhel9-unknown",
    "rhel-10.2":  "generic",
    "rocky-8.10": "rhel8-unknown",
    "rocky-9.8":  "rhel9-unknown",
    "rocky-10.2": "generic",
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
                # shell-style: honours quotes and trailing "# comments"
                cfg[k.strip()] = (shlex.split(v, comments=True) or [""])[0]

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

def _cmd_failed(e):
    # the tool's own stderr is already on screen; the full argv can be hundreds of chars
    name = e.cmd[1] if e.cmd[0] == "sudo" else e.cmd[0]
    err(f"{name} failed (exit {e.returncode})")


def virsh(*args):
    return subprocess.run(["sudo", "virsh"] + list(args), capture_output=True, text=True)

def _exists(path):
    r = subprocess.run(["sudo", "test", "-e", str(path)], capture_output=True)
    return r.returncode == 0


def _scan(directory, pattern, sudo=True):
    """[(path, size_bytes, mtime)] of files matching pattern directly inside directory."""
    cmd = ["find", str(directory), "-maxdepth", "1", "-name", pattern,
           "-type", "f", "-printf", "%p\\t%s\\t%T@\\n"]
    r = run(cmd, sudo=sudo, capture=True, check=False)
    rows = []
    for line in sorted(r.stdout.splitlines()):
        path, size, mtime = line.rsplit("\t", 2)
        rows.append((path, int(size), float(mtime)))
    return rows


def _fmt_size(size):
    if size >= 1_073_741_824:
        return f"{size / 1_073_741_824:.1f} GB"
    if size >= 1_048_576:
        return f"{size / 1_048_576:.0f} MB"
    return f"{max(1, size // 1024)} KB"


def _backing_users(image, vms_dir):
    users = []
    for path, _, _ in _scan(vms_dir, "*.qcow2"):
        # -U: running VMs hold a write lock on their disk
        r = run(["qemu-img", "info", "-U", "--output=json", path],
                sudo=True, capture=True, check=False)
        try:
            backing = json.loads(r.stdout).get("full-backing-filename")
        except ValueError:
            continue
        if backing and Path(backing).resolve() == image.resolve():
            users.append(path)
    return users

# ── TUI menu ──────────────────────────────────────────────────────────────────

def _read_key():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        # TCSANOW: setraw's default TCSAFLUSH would discard keys already typed
        tty.setraw(fd, termios.TCSANOW)
        # os.read, not sys.stdin.buffer: a buffered read would swallow the rest
        # of an escape sequence and hide it from select()
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            if not select.select([fd], [], [], 0.05)[0]:
                return "ESC"
            # read exactly one CSI/SS3 sequence (ends at a byte in 0x40-0x7E) so
            # keys queued behind it (auto-repeat, paste) aren't swallowed
            seq = os.read(fd, 1)
            if seq in (b"[", b"O"):
                for _ in range(8):
                    seq += os.read(fd, 1)
                    if 0x40 <= seq[-1] <= 0x7E:
                        break
            if seq in (b"[A", b"OA"): return "UP"
            if seq in (b"[B", b"OB"): return "DOWN"
            return ""
        if ch in (b"\r", b"\n"):  return "ENTER"
        if ch in (b"q", b"\x03"): return "QUIT"
        return ch.decode("utf-8", errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def menu(title, options, hint="", descriptions=None):
    """Arrow-key menu. options = [(label, value), ...] or [(label, value, kind), ...].

    value=None marks a non-selectable section header. kind="danger" renders
    the label in red when not selected. descriptions is an optional
    {value: text} map shown next to the currently selected item.
    Returns the selected value or None.
    """
    descriptions = descriptions or {}
    selectable = [i for i, item in enumerate(options) if item[1] is not None]
    selected = selectable[0]
    while True:
        sys.stdout.write("\033[2J\033[H")
        print(f"\n  {C.BOLD}{C.CYAN}labvirt{C.RST}  "
              f"{C.DIM}RHEL · Rocky · Debian lab launcher{C.RST}")
        print(f"  {C.DIM}version {__version__} ({__build_date__}) by manumaiden{C.RST}")
        print(f"  {C.DIM}{'─' * 50}{C.RST}\n")
        if hint:
            print(f"  {C.YELLOW}{hint}{C.RST}\n")
        print(f"  {C.BOLD}{title}{C.RST}\n")
        for i, item in enumerate(options):
            label, value = item[0], item[1]
            kind = item[2] if len(item) > 2 else "item"
            if value is None:
                print(f"\n  {C.DIM}{C.BOLD}{label}{C.RST}")
            elif i == selected:
                desc = descriptions.get(value, "")
                desc_part = f"   {C.DIM}{desc}{C.RST}" if desc else ""
                print(f"  {C.BG_BLUE}{C.WHITE}{C.BOLD}  {label:<32}  {C.RST}{desc_part}")
            elif kind == "danger":
                print(f"    {C.RED}{label}{C.RST}")
            else:
                print(f"    {label}")
        print(f"\n  {C.DIM}↑↓ navigate   Enter select   q quit{C.RST}")
        sys.stdout.flush()

        key = _read_key()
        if key == "UP":
            idx = selectable.index(selected)
            selected = selectable[(idx - 1) % len(selectable)]
        elif key == "DOWN":
            idx = selectable.index(selected)
            selected = selectable[(idx + 1) % len(selectable)]
        elif key == "ENTER":
            return options[selected][1]
        elif key in ("QUIT", "ESC"):
            return None


def _prompt(label, default=""):
    hint = f" [{default}]" if default else ""
    val  = input(f"\n  {C.CYAN}?{C.RST}  {label}{hint}: ").strip()
    return val or default


def _pause():
    try:
        input(f"\n  {C.DIM}Press Enter to continue...{C.RST}")
    except (KeyboardInterrupt, EOFError):
        print()

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
        clones = _backing_users(dest, cfg["VMS_DIR"])
        if clones:
            print()
            warn(f"{len(clones)} VM disk(s) are linked clones of {dest.name} — "
                 f"overwriting it will corrupt them:")
            for c in clones:
                print(f"    {C.DIM}{c}{C.RST}")
        ans = input(f"\n  {C.YELLOW}⚠{C.RST}  {dest.name} already exists — overwrite? [y/N] ").strip().lower()
        if ans != "y":
            info("Aborted.")
            return

    # customize a temp copy so a failed run never leaves a half-built golden image
    tmp = dest.with_name(dest.name + ".tmp")
    info(f"Copying  {src_path.name}  →  {dest.name}")
    run(["cp", "--sparse=always", str(src_path), str(tmp)], sudo=True)

    info("Running virt-customize...")

    if os_name == "debian":
        remove_cloud_init = "dpkg -s cloud-init >/dev/null 2>&1 && apt-get remove -y cloud-init || true"
        ensure_openssh    = ("dpkg -s openssh-server >/dev/null 2>&1 "
                              "|| (apt-get update && apt-get install -y openssh-server)")
    else:
        remove_cloud_init = ("rpm -q cloud-init >/dev/null 2>&1 "
                              "&& (dnf remove -y cloud-init || yum remove -y cloud-init) || true")
        ensure_openssh    = ("rpm -q openssh-server >/dev/null 2>&1 "
                              "|| (dnf install -y openssh-server || yum install -y openssh-server)")

    # password via file: on the command line it would be readable by any local user via ps
    pw = tempfile.NamedTemporaryFile("w", prefix="labvirt-pw-")
    pw.write(cfg["ROOT_PASSWORD"])
    pw.flush()

    args = [
        "virt-customize", "-a", str(tmp), "--network",
        "--root-password", f"file:{pw.name}",
        "--run-command", remove_cloud_init,
        "--run-command", ensure_openssh,
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
            "--timezone", "UTC",
            "--run-command",
            "sed -i '/^GRUB_CMDLINE_LINUX=/s/\"$/ net.ifnames=0 biosdevname=0\"/' "
            "/etc/default/grub && update-grub",
            "--install", "bash-completion,vim",
        ]

    try:
        run(args, sudo=True)
    except BaseException:
        run(["rm", "-f", str(tmp)], sudo=True)
        raise
    finally:
        pw.close()
    run(["mv", "-f", str(tmp), str(dest)], sudo=True)
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
        # random name + 0600: a fixed /tmp path could be pre-created by another local user
        with tempfile.NamedTemporaryFile("w", prefix="labvirt-net-", suffix=".xml") as f:
            f.write(xml)
            f.flush()
            run(["virsh", "net-define", f.name], sudo=True)
        run(["virsh", "net-start",     cfg["LAB_NET"]], sudo=True)
        run(["virsh", "net-autostart", cfg["LAB_NET"]], sudo=True)
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


def _wait_for_ip(vm_name, mgmt_net):
    mgmt_ip = None
    for i in range(1, 31):
        time.sleep(2)
        mgmt_ip = _get_mgmt_ip(vm_name, mgmt_net)
        if mgmt_ip:
            break
        if i % 5 == 0:
            info(f"Still waiting... ({i * 2}s)")
    return mgmt_ip


def _print_vm_ready(vm_name, os_name, version, mgmt_ip, cfg):
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


def _wait_for_ssh_port(mgmt_ip, timeout=90):
    deadline = time.time() + timeout
    elapsed = 0
    while time.time() < deadline:
        try:
            with socket.create_connection((mgmt_ip, 22), timeout=2):
                return True
        except OSError:
            time.sleep(2)
            elapsed += 2
            if elapsed % 10 == 0:
                info(f"Still waiting for sshd... ({elapsed}s)")
    return False


def _offer_ssh(mgmt_ip):
    if not mgmt_ip or not sys.stdin.isatty():
        return
    answer = input(f"\n  {C.CYAN}?{C.RST}  Connect via SSH now? [Y/n]: ").strip().lower()
    if answer in ("", "y", "yes"):
        info("Waiting for SSH to become available...")
        if not _wait_for_ssh_port(mgmt_ip):
            warn("SSH did not come up in time — try connecting manually in a few seconds:")
            print(f"  {C.DIM}ssh root@{mgmt_ip}{C.RST}")
            return
        print()
        subprocess.run(["ssh", *SSH_OPTS, f"root@{mgmt_ip}"], check=False)


def create_vm(os_name, version, vm_name, ram, vcpus, cfg):
    base_image = cfg["ORIGINAL_DIR"] / f"{os_name}-{version}-base.qcow2"
    vm_disk    = cfg["VMS_DIR"] / f"{vm_name}.qcow2"

    if not _exists(base_image):
        err(f"Golden image not found: {base_image}")
        err(f"Run: ./labvirt.py build --os {os_name} --version {version} --src <path>")
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
    mgmt_ip = _wait_for_ip(vm_name, cfg["MGMT_NET"])
    _print_vm_ready(vm_name, os_name, version, mgmt_ip, cfg)

    if mgmt_ip:
        rhsm_user = os.environ.get("RHSM_USER", "")
        rhsm_pass = os.environ.get("RHSM_PASS", "")
        if os_name == "rhel" and rhsm_user and rhsm_pass:
            print()
            info("Registering RHEL subscription via SSH...")
            cmd = (
                f"subscription-manager register "
                f"--username {shlex.quote(rhsm_user)} --password {shlex.quote(rhsm_pass)} --auto-attach "
                f"&& (dnf install -y bash-completion vim || yum install -y bash-completion vim)"
            )
            # command via stdin, not argv: argv (with the RHSM password) is visible to ps
            subprocess.run(
                ["ssh", *SSH_OPTS, "-o", "ConnectTimeout=10", f"root@{mgmt_ip}", "bash -s"],
                input=cmd, text=True, check=False,
            )
        elif os_name == "rhel":
            print()
            warn("Register subscription manually:")
            print(f"  {C.DIM}subscription-manager register --username USER --password PASS --auto-attach")
            print(f"  dnf install -y bash-completion vim{C.RST}")

    _offer_ssh(mgmt_ip)

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
    mgmt_ip = _wait_for_ip(vm_name, cfg["MGMT_NET"])
    _print_vm_ready(vm_name, os_name, version, mgmt_ip, cfg)
    _offer_ssh(mgmt_ip)

# ── Core: destroy ─────────────────────────────────────────────────────────────

def destroy_vm(vm_name, cfg):
    if virsh("dominfo", vm_name).returncode != 0:
        err(f"VM '{vm_name}' not found.")
        sys.exit(1)

    info(f"Destroying VM: {vm_name}")

    snaps = virsh("snapshot-list", vm_name, "--name")
    snap_names = [s.strip() for s in snaps.stdout.splitlines() if s.strip()]
    if snap_names:
        warn(f"{len(snap_names)} snapshot(s) found ({', '.join(snap_names)}) — will be deleted too.")

    if "running" in virsh("dominfo", vm_name).stdout:
        run(["virsh", "destroy", vm_name], sudo=True)

    r = virsh("undefine", vm_name, "--remove-all-storage", "--snapshots-metadata")
    if r.returncode != 0:
        r = virsh("undefine", vm_name, "--snapshots-metadata")
        leftover = cfg["VMS_DIR"] / f"{vm_name}.qcow2"
        if _exists(leftover):
            run(["rm", "-f", str(leftover)], sudo=True)

    if r.returncode != 0:
        err(f"Failed to remove VM '{vm_name}': {r.stderr.strip()}")
        sys.exit(1)

    ok(f"VM '{vm_name}' removed.")

# ── Core: list ────────────────────────────────────────────────────────────────

def list_vms(cfg):
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

        ip = _get_mgmt_ip(name, cfg["MGMT_NET"]) if state == "running" else "-"
        rows.append((vm_id, name, state, ip or "-", disk))

    id_w    = max(2,  max(len(r[0]) for r in rows))
    name_w  = max(4,  max(len(r[1]) for r in rows))
    state_w = max(5,  max(len(r[2]) for r in rows))
    ip_w    = max(2,  max(len(r[3]) for r in rows))
    disk_w  = max(4,  max(len(r[4]) for r in rows))

    print(f"  {C.BOLD}{'ID':<{id_w}}  {'Name':<{name_w}}  {'State':<{state_w}}  {'IP':<{ip_w}}  {'Disk':<{disk_w}}{C.RST}")
    print(f"  {'─'*id_w}  {'─'*name_w}  {'─'*state_w}  {'─'*ip_w}  {'─'*disk_w}")
    for vm_id, name, state, ip, disk in rows:
        col = C.GREEN if state == "running" else C.DIM
        ip_col = C.GREEN if ip != "-" else C.DIM
        print(f"  {vm_id:<{id_w}}  {col}{name:<{name_w}}{C.RST}  {state:<{state_w}}  {ip_col}{ip:<{ip_w}}{C.RST}  {C.DIM}{disk}{C.RST}")
    print()

# ── Core: file listings (images / sources / isos / kickstarts) ────────────────

def _print_file_table(directory, files, empty_msg):
    if not files:
        info(empty_msg)
        print()
        return
    rows = [(Path(p).name, _fmt_size(size),
             datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"))
            for p, size, mtime in files]
    name_w = max(4, max(len(r[0]) for r in rows))
    size_w = max(4, max(len(r[1]) for r in rows))

    print(f"  {C.DIM}{directory}{C.RST}")
    print(f"  {C.BOLD}{'Name':<{name_w}}  {'Size':<{size_w}}  Modified{C.RST}")
    print(f"  {'─'*name_w}  {'─'*size_w}  {'─'*16}")
    for name, size_str, mod in rows:
        print(f"  {name:<{name_w}}  {size_str:<{size_w}}  {C.DIM}{mod}{C.RST}")
    print()


def _list_dir(cfg, key, pattern, empty_msg):
    directory = cfg.get(key)
    print()
    if not directory:
        info(f"{key} not configured.")
        print()
        return
    if not _exists(directory):
        info(f"{key} not found: {directory}")
        print()
        return
    _print_file_table(directory, _scan(directory, pattern), empty_msg)


def list_images(cfg):
    _list_dir(cfg, "ORIGINAL_DIR", "*-base.qcow2", "No golden images found.")


def list_sources(cfg):
    _list_dir(cfg, "SOURCES_DIR", "*.qcow2", "No source images found.")


def list_isos(cfg):
    _list_dir(cfg, "KS_ISO_DIR", "*.iso", "No ISO files found.")


def list_kickstarts(cfg):
    ks_cfg = cfg.get("KS_PATH")
    print()
    if not ks_cfg:
        info("KS_PATH not configured.")
        print()
        return
    if ks_cfg.is_file():
        st = ks_cfg.stat()
        _print_file_table(ks_cfg.parent, [(str(ks_cfg), st.st_size, st.st_mtime)],
                          "No kickstart files found.")
    elif ks_cfg.is_dir():
        _print_file_table(ks_cfg, _scan(ks_cfg, "*.cfg", sudo=False),
                          "No kickstart files found.")
    else:
        info(f"KS_PATH not found: {ks_cfg}")
        print()

# ── Core: about ───────────────────────────────────────────────────────────────

def show_about(cfg):
    print()
    print(f"  {C.BOLD}{C.CYAN}labvirt{C.RST}  {C.DIM}RHEL / Rocky / Debian lab VM launcher{C.RST}")
    print(f"  {C.DIM}version {__version__} ({__build_date__}) by manumaiden{C.RST}")
    print()
    print("  Spin up local KVM/libvirt VMs for ephemeral test labs —")
    print("  bonding, teaming, NIC renaming, and more.")
    print()
    print(f"  {C.BOLD}Quick start{C.RST}")
    print(f"    1. Build a golden image once per OS/version   {C.DIM}→ Build image{C.RST}")
    print(f"    2. Create VMs from it (fast linked clones)    {C.DIM}→ Create VM{C.RST}")
    print(f"    3. Or install fresh from ISO + kickstart      {C.DIM}→ Install VM from ISO+ks{C.RST}")
    print(f"    4. Tear down when done                        {C.DIM}→ Destroy VM{C.RST}")
    print()
    print(f"  {C.BOLD}Network per VM{C.RST}")
    print("    NIC1    default   192.168.122.x   management / SSH / internet")
    print("    NIC2-4  lab-net   10.10.10.x      isolated lab network (auto-created)")
    print()
    print(f"  {C.BOLD}Requirements{C.RST}")
    print("    libvirt, virt-install, virt-customize (guestfs-tools), qemu-img")
    print()
    print(f"  {C.BOLD}Config file{C.RST}")
    print(f"    {CONFIG_FILE}")
    print()
    print(f"  {C.BOLD}Supported OS{C.RST}")
    print("    RHEL 7.9-10.2 · Rocky 8.10-10.2 · Debian 12/13")
    print()


VIEWS = {
    "list":       list_vms,
    "images":     list_images,
    "sources":    list_sources,
    "isos":       list_isos,
    "kickstarts": list_kickstarts,
    "about":      show_about,
}

# ── Interactive flows ─────────────────────────────────────────────────────────

def _file_choices(directory, pattern):
    return [(f"{Path(p).name}  {C.DIM}{_fmt_size(size)}{C.RST}", p)
            for p, size, _ in _scan(directory, pattern)]


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
        choices = _file_choices(sources_dir, "*.qcow2")
        if choices:
            choices.append(("[ Enter path manually ]", "__manual__"))
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
    build_image(os_ch, ver_ch, src, cfg)


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
    create_vm(os_ch, ver_ch, vm_name, ram, vcpus, cfg)


def _interactive_ks_test(cfg):
    os_ch = menu("Select OS", [(o, o) for o in KS_OS], "Install VM from ISO+ks")
    if not os_ch:
        return
    ver_ch = menu("Select version",
                  [(v, v) for v in SUPPORTED_OS[os_ch]],
                  f"Install VM from ISO+ks — {os_ch}")
    if not ver_ch:
        return

    iso_path = None
    iso_dir  = cfg.get("KS_ISO_DIR")
    if iso_dir and _exists(iso_dir):
        choices = _file_choices(iso_dir, "*.iso")
        if choices:
            choices.append(("[ Enter path manually ]", "__manual__"))
            picked = menu("Select ISO", choices, f"KS test — {os_ch} {ver_ch}")
            if not picked:
                return
            if picked != "__manual__":
                iso_path = picked

    ks_path = None
    ks_cfg  = cfg.get("KS_PATH")
    if ks_cfg and ks_cfg.is_dir():
        choices = [(Path(p).name, p) for p, _, _ in _scan(ks_cfg, "*.cfg", sudo=False)]
        if choices:
            choices.append(("[ Enter path manually ]", "__manual__"))
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
    ks_test_vm(ks_path, iso_path, os_ch, ver_ch, vm_name, ram, vcpus, disk_gb, cfg)


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


def _main_menu_hint(cfg):
    r = virsh("list", "--name")
    running = len([n for n in r.stdout.splitlines() if n.strip()])
    label = "VM" if running == 1 else "VMs"
    return f"{running} {label} running   ·   config: {CONFIG_FILE}"


def interactive_main(cfg):
    actions = [
        ("── Provisioning ──",                   None),
        ("Build image  (OS → golden qcow2)",    "build"),
        ("Create VM    (golden → linked clone)", "create"),
        ("Install VM from ISO+ks",               "ks-test"),
        ("Destroy VM",                           "destroy", "danger"),
        ("── Info ──",                           None),
        ("List VMs",                             "list"),
        ("List images",                          "images"),
        ("List sources",                         "sources"),
        ("List ISOs",                            "isos"),
        ("List kickstarts",                      "kickstarts"),
        ("About",                                "about"),
        ("Quit",                                 "quit"),
    ]
    descriptions = {
        "build":      "labvirt build --os <os> --version <ver> --src <path>",
        "create":     "labvirt create --os <os> --version <ver> --name <name>",
        "ks-test":    "labvirt ks-test --os <os> --version <ver> --iso <iso> --ks <ks> --name <name>",
        "destroy":    "labvirt destroy <name>",
        "list":       "labvirt list",
        "images":     "labvirt images",
        "sources":    "labvirt sources",
        "isos":       "labvirt isos",
        "kickstarts": "labvirt kickstarts",
        "about":      "labvirt about",
    }
    while True:
        action = menu("Main menu", actions, _main_menu_hint(cfg), descriptions)
        if action in (None, "quit"):
            sys.stdout.write("\033[2J\033[H\n")
            break
        try:
            if action == "build":
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
            elif action in VIEWS:
                sys.stdout.write("\033[2J\033[H")
                VIEWS[action](cfg)
                _pause()
        except (KeyboardInterrupt, EOFError):
            print()
            warn("Cancelled — back to main menu.")
        except SystemExit:
            # core functions exit(1) after printing their own error
            _pause()
        except subprocess.CalledProcessError as e:
            _cmd_failed(e)
            _pause()
        except ValueError as e:
            err(f"Invalid input: {e}")
            _pause()

# ── CLI ───────────────────────────────────────────────────────────────────────

def cli_main(cfg):
    ap  = argparse.ArgumentParser(prog="labvirt.py",
                                  description="RHEL/Rocky/Debian lab VM launcher")
    ap.add_argument(
        "-v", "--version", action="version",
        version=f"labvirt version {__version__} ({__build_date__}) by manumaiden",
    )
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
    sub.add_parser("sources",     help="List raw qcow2 source images in SOURCES_DIR")
    sub.add_parser("isos",        help="List ISO files in KS_ISO_DIR")
    sub.add_parser("kickstarts",  help="List kickstart files in KS_PATH")
    sub.add_parser("about",       help="Show program info and quick start")

    pk = sub.add_parser("ks-test", help="Install a VM from ISO + kickstart")
    ks_default = cfg.get("KS_PATH")
    pk.add_argument("--ks",      default=str(ks_default) if ks_default and ks_default.is_file() else None,
                                 help="Path to ks.cfg")
    pk.add_argument("--iso",     required=True, help="Path to installation ISO")
    pk.add_argument("--os",      required=True, choices=KS_OS)
    pk.add_argument("--version", required=True)
    pk.add_argument("--name",    required=True)
    pk.add_argument("--ram",     type=int, default=int(cfg.get("DEFAULT_RAM", 2048)))
    pk.add_argument("--vcpus",   type=int, default=int(cfg.get("DEFAULT_VCPUS", 2)))
    pk.add_argument("--disk",    type=int, default=int(cfg.get("KS_DISK_SIZE", 20)), dest="disk_gb")

    args = ap.parse_args()

    if args.cmd in ("build", "create", "ks-test"):
        if not _version_supported(args.os, args.version):
            lo, hi = SUPPORTED_OS_RANGE.get(args.os, ("?", "?"))
            ap.error(f"unsupported version '{args.version}' for --os {args.os}. "
                     f"Supported range: {lo} – {hi}")

    if args.cmd == "build":
        build_image(args.os, args.version, args.src, cfg)
    elif args.cmd == "create":
        create_vm(args.os, args.version, args.name, args.ram, args.vcpus, cfg)
    elif args.cmd == "ks-test":
        if not args.ks:
            ap.error("--ks is required (or set KS_PATH to a .cfg file in config)")
        ks_test_vm(args.ks, args.iso, args.os, args.version,
                   args.name, args.ram, args.vcpus, args.disk_gb, cfg)
    elif args.cmd == "destroy":
        destroy_vm(args.name, cfg)
    elif args.cmd in VIEWS:
        VIEWS[args.cmd](cfg)
    else:
        ap.print_help()

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    if len(sys.argv) > 1:
        try:
            cli_main(cfg)
        except subprocess.CalledProcessError as e:
            _cmd_failed(e)
            sys.exit(1)
    elif sys.stdin.isatty() and sys.stdout.isatty():
        interactive_main(cfg)
    else:
        err("No arguments provided. Run with --help or in an interactive terminal.")
        sys.exit(1)


if __name__ == "__main__":
    main()
