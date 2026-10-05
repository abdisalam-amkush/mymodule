#!/usr/bin/env python3
"""Install a Magisk-style module ZIP from Termux or an Android shell.

Detects APatch, KernelSU/KernelSU Next, or Magisk and delegates installation to
that manager's own command-line installer. Reboots only after a successful
installation unless --no-reboot is supplied.

Examples:
  python flash_module.py /sdcard/Download/AlwaysStrong-v1.0.4-ecomkyc.zip
  python flash_module.py --yes /sdcard/Download/module.zip
  python flash_module.py --manager magisk --no-reboot /sdcard/Download/module.zip
  python flash_module.py --dry-run /sdcard/Download/module.zip
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Manager:
    key: str
    title: str
    binary: str


def run_root(command: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    """Run one shell command through Android root and collect its output."""
    try:
        return subprocess.run(
            ["su", "-c", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError("No su binary was found. Run this on a rooted Android device.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("The root command timed out.")


def root_ok() -> bool:
    result = run_root("id -u", timeout=15)
    return result.returncode == 0 and result.stdout.strip() == "0"


def root_find_binary(name: str, extra_paths: tuple[str, ...] = ()) -> Optional[str]:
    """Find an executable from the root shell without relying on Termux PATH."""
    candidates = " ".join(shlex.quote(p) for p in extra_paths)
    command = (
        f"p=$(command -v {shlex.quote(name)} 2>/dev/null); "
        f"if [ -n \"$p\" ] && [ -x \"$p\" ]; then printf '%s' \"$p\"; exit 0; fi; "
        f"for p in {candidates}; do "
        f"[ -x \"$p\" ] && {{ printf '%s' \"$p\"; exit 0; }}; "
        f"done; exit 1"
    )
    result = run_root(command, timeout=15)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def detect_managers() -> dict[str, Manager]:
    """Return all detected manager CLIs; APatch is preferred over KSU compatibility."""
    found: dict[str, Manager] = {}

    apd = root_find_binary("apd", ("/data/adb/apd", "/data/adb/apd/apd"))
    if apd:
        found["apatch"] = Manager("apatch", "APatch", apd)

    ksud = root_find_binary("ksud", ("/data/adb/ksud", "/data/adb/ksu/bin/ksud"))
    if ksud:
        found["kernelsu"] = Manager("kernelsu", "KernelSU / KernelSU Next", ksud)

    magisk = root_find_binary("magisk", ("/sbin/.magisk/magisk", "/debug_ramdisk/magisk"))
    if magisk:
        found["magisk"] = Manager("magisk", "Magisk", magisk)

    return found


def choose_manager(found: dict[str, Manager], requested: str) -> Manager:
    if requested != "auto":
        if requested not in found:
            raise RuntimeError(f"Requested manager '{requested}' was not detected.")
        return found[requested]

    # APatch can expose KSU compatibility binaries, so it must win when present.
    for key in ("apatch", "kernelsu", "magisk"):
        if key in found:
            return found[key]
    raise RuntimeError(
        "No supported manager CLI was found. Supported: APatch (apd), "
        "KernelSU/KernelSU Next (ksud), and Magisk (magisk)."
    )


def read_module_id(zip_path: Path) -> str:
    """Read the root-level module ID for display only; installer validates it again."""
    with zipfile.ZipFile(zip_path) as archive:
        try:
            data = archive.read("module.prop").decode("utf-8", "replace")
        except KeyError:
            raise RuntimeError("This ZIP has no root-level module.prop; it is not a standard module ZIP.")
    for line in data.splitlines():
        if line.startswith("id=") and line[3:].strip():
            return line[3:].strip()
    return "unknown"


def install_command(manager: Manager, zip_path: Path) -> str:
    quoted_zip = shlex.quote(str(zip_path))
    binary = shlex.quote(manager.binary)
    if manager.key == "magisk":
        return f"{binary} --install-module {quoted_zip}"
    # APatch and KernelSU expose the same module-install command shape.
    return f"{binary} module install {quoted_zip}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install a Magisk-style module ZIP through the detected Android root manager."
    )
    parser.add_argument("zip", type=Path, help="Path to the module ZIP, e.g. /sdcard/Download/module.zip")
    parser.add_argument(
        "--manager",
        choices=("auto", "apatch", "kernelsu", "magisk"),
        default="auto",
        help="Manager to use (default: auto; APatch, then KernelSU, then Magisk).",
    )
    parser.add_argument("--no-reboot", action="store_true", help="Install but do not reboot afterward.")
    parser.add_argument("--reboot-delay", type=int, default=3, help="Seconds to wait before rebooting (default: 3).")
    parser.add_argument("--yes", action="store_true", help="Do not ask for installation confirmation.")
    parser.add_argument("--dry-run", action="store_true", help="Detect and show the selected command without installing.")
    args = parser.parse_args()

    if args.reboot_delay < 0 or args.reboot_delay > 60:
        parser.error("--reboot-delay must be between 0 and 60 seconds")

    zip_path = args.zip.expanduser().resolve()
    if not zip_path.is_file():
        parser.error(f"ZIP not found: {zip_path}")
    if not zipfile.is_zipfile(zip_path):
        parser.error(f"Not a readable ZIP file: {zip_path}")

    try:
        module_id = read_module_id(zip_path)
        if not root_ok():
            raise RuntimeError("Root access was denied. Grant root access to Termux/shell and try again.")
        found = detect_managers()
        manager = choose_manager(found, args.manager)
        command = install_command(manager, zip_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    detected = ", ".join(m.title for m in found.values()) or "none"
    print(f"Module ZIP : {zip_path}")
    print(f"Module ID  : {module_id}")
    print(f"Detected   : {detected}")
    print(f"Using      : {manager.title}")

    if args.dry_run:
        print("Dry run only; no installation or reboot was performed.")
        return 0

    if not args.yes:
        answer = input("Install this module now? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled. Nothing was installed.")
            return 0

    print("\nInstalling through the detected manager...\n")
    result = run_root(command, timeout=180)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        print(f"\nERROR: {manager.title} installer failed (exit code {result.returncode}).", file=sys.stderr)
        print("The device was not rebooted.", file=sys.stderr)
        return result.returncode or 1

    print("\nInstallation completed successfully.")
    if args.no_reboot:
        print("Reboot skipped because --no-reboot was supplied.")
        return 0

    if args.reboot_delay:
        print(f"Rebooting in {args.reboot_delay} second(s)...")
        time.sleep(args.reboot_delay)
    reboot = run_root("sync; reboot", timeout=15)
    if reboot.returncode != 0:
        print("WARNING: Module installed, but automatic reboot failed. Reboot manually.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
