#!/usr/bin/env python3
"""Self-checks for labvirt's pure parsing logic. Run: python3 test_labvirt.py"""

import subprocess
import tempfile
from pathlib import Path

import labvirt


def test_version_supported():
    assert labvirt._version_supported("rhel", "8.10")
    assert labvirt._version_supported("rhel", "7.9")
    assert labvirt._version_supported("rocky", "9.5")
    assert not labvirt._version_supported("rocky", "7.9")
    assert not labvirt._version_supported("rhel", "10.10")
    assert not labvirt._version_supported("rhel", "9")
    assert not labvirt._version_supported("rhel", "abc")
    assert labvirt._version_supported("debian", "13")
    assert not labvirt._version_supported("debian", "12.5")
    assert not labvirt._version_supported("fedora", "40")


def test_load_config_quotes_and_comments():
    with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
        f.write('DEFAULT_RAM=2048          # default RAM\n'
                'ROOT_PASSWORD="pa ss#word"  # root password\n'
                'MGMT_NET=default\n'
                'ORIGINAL_DIR=""\n'
                '# comment line\n')
        f.flush()
        saved, labvirt.CONFIG_FILE = labvirt.CONFIG_FILE, Path(f.name)
        try:
            cfg = labvirt.load_config()
        finally:
            labvirt.CONFIG_FILE = saved
    assert cfg["DEFAULT_RAM"] == "2048", cfg["DEFAULT_RAM"]
    assert cfg["ROOT_PASSWORD"] == "pa ss#word", cfg["ROOT_PASSWORD"]
    assert cfg["MGMT_NET"] == "default"
    assert cfg["ORIGINAL_DIR"] == labvirt.SCRIPT_DIR / "original"
    assert cfg["KS_PATH"] is None


def test_get_mgmt_ip_parses_virsh_output():
    outputs = {
        "domiflist": (
            " Interface   Type      Source    Model    MAC\n"
            "-----------------------------------------------------------\n"
            " vnet0       network   default   virtio   52:54:00:aa:bb:cc\n"
            " vnet1       network   lab-net   virtio   52:54:00:11:22:33\n"),
        "net-dhcp-leases": (
            " Expiry Time           MAC address         Protocol   IP address           Hostname\n"
            "-----------------------------------------------------------------------------------\n"
            " 2026-10-30 14:00:00   52:54:00:AA:BB:CC   ipv4       192.168.122.57/24    vm1\n"),
    }
    saved = labvirt.virsh
    labvirt.virsh = lambda *a: subprocess.CompletedProcess(a, 0, outputs.get(a[0], ""), "")
    try:
        assert labvirt._get_mgmt_ip("vm1", "default") == "192.168.122.57"
        assert labvirt._get_mgmt_ip("vm1", "other-net") is None
    finally:
        labvirt.virsh = saved


def test_fmt_size():
    assert labvirt._fmt_size(500) == "1 KB"
    assert labvirt._fmt_size(5 * 1024) == "5 KB"
    assert labvirt._fmt_size(300 * 1_048_576) == "300 MB"
    assert labvirt._fmt_size(int(2.5 * 1_073_741_824)) == "2.5 GB"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
