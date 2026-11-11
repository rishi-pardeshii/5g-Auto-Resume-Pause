"""Pause/resume Free Download Manager based on the Android phone's radio type.

The script uses only the Python standard library and the adb.exe next to this
file.  On the first run, put the mouse over FDM's pause/resume button and
press F8, then confirm whether the download is currently running.  The same
coordinate is used for both states of the button.

Example:
    python main.py
    python main.py --device SERIAL
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import ctypes.wintypes
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


LOGGER = logging.getLogger("fdm-5g-controller")
ADB_TIMEOUT_SECONDS = 8
F8_VIRTUAL_KEY = 0x77
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEY_PRESSED_MASK = 0x8000

# FDM has used both names in different releases.  Extra names can be supplied
# through --process-name when an installation uses a custom executable name.
DEFAULT_FDM_PROCESS_NAMES = ("fdm.exe", "free download manager.exe")
NETWORK_FAILURE_LIMIT = 3


class ControllerError(RuntimeError):
    """An error that makes it unsafe to continue controlling FDM."""


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class Config:
    adb_path: Path
    device: Optional[str]
    interval: float
    click_delay: float
    process_names: tuple[str, ...]


def run_command(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a command without inheriting a console window or shell."""
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControllerError(f"Command failed: {' '.join(command)}: {exc}") from exc


def adb_base_command(config: Config) -> list[str]:
    command = [str(config.adb_path)]
    if config.device:
        command.extend(["-s", config.device])
    return command


def find_adb() -> Path:
    bundled = Path(__file__).resolve().with_name("adb.exe")
    if bundled.is_file():
        return bundled
    path_adb = shutil.which("adb.exe")
    if path_adb:
        return Path(path_adb)
    raise ControllerError("adb.exe was not found next to main.py or on PATH")


def choose_device(adb_path: Path, requested: Optional[str]) -> str:
    result = run_command([str(adb_path), "devices"], ADB_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise ControllerError(result.stderr.strip() or "adb devices failed")

    devices = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            devices.append(fields[0])

    if requested:
        if requested not in devices:
            raise ControllerError(
                f"Requested device {requested!r} is not connected or not authorized"
            )
        return requested
    if not devices:
        raise ControllerError("No authorized Android device is connected through adb")
    if len(devices) > 1:
        raise ControllerError(
            "More than one Android device is connected; rerun with --device SERIAL"
        )
    return devices[0]


def get_network_type(config: Config) -> str:
    """Read the Android radio type used to decide whether downloads run."""
    result = run_command(
        adb_base_command(config) + ["shell", "getprop", "gsm.network.type"],
        ADB_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ControllerError(
            result.stderr.strip() or "Unable to read gsm.network.type through adb"
        )
    network_type = " ".join(result.stdout.split())
    if not network_type:
        raise ControllerError("Android returned an empty gsm.network.type value")
    return network_type


def is_5g(network_type: str) -> bool:
    """Return true only when gsm.network.type reports NR_SA."""
    return network_type.strip().upper().split(",", 1)[0].strip() == "NR_SA"


def wait_for_button_location() -> Point:
    if sys.platform != "win32":
        raise ControllerError("Mouse calibration requires Windows")
    user32 = ctypes.windll.user32
    print("Hover the mouse over FDM's pause/resume button, then press F8.")
    print("Press Ctrl+C to cancel.")
    was_down = False
    while True:
        down = bool(user32.GetAsyncKeyState(F8_VIRTUAL_KEY) & KEY_PRESSED_MASK)
        if down and not was_down:
            point = ctypes.wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                raise ControllerError("Windows could not read the mouse position")
            LOGGER.info("Captured button location: (%d, %d)", point.x, point.y)
            return Point(point.x, point.y)
        was_down = down
        time.sleep(0.03)


def ask_initial_download_state() -> bool:
    try:
        answer = input("Is the download currently running? [Y/n]: ").strip().casefold()
    except EOFError as exc:
        raise ControllerError("A console is required for the initial download-state question") from exc
    if answer not in ("", "y", "yes", "n", "no"):
        raise ControllerError("Please answer the initial state question with y or n")
    return answer in ("", "y", "yes")


def click(point: Point, delay: float) -> None:
    user32 = ctypes.windll.user32
    previous = ctypes.wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(previous)):
        raise ControllerError("Windows could not read the current mouse position")
    try:
        if not user32.SetCursorPos(point.x, point.y):
            raise ControllerError("Windows could not move the mouse to the FDM button")
        time.sleep(delay)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    finally:
        if not user32.SetCursorPos(previous.x, previous.y):
            raise ControllerError("Windows could not restore the previous mouse position")


def process_ids(process_names: Iterable[str]) -> list[str]:
    result = run_command(["tasklist", "/FO", "CSV", "/NH"], ADB_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise ControllerError(result.stderr.strip() or "Could not enumerate Windows processes")
    wanted = {name.casefold() for name in process_names}
    ids = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) >= 2 and row[0].casefold() in wanted:
            ids.append(row[1])
    return ids


def close_fdm(process_names: Iterable[str]) -> None:
    ids = process_ids(process_names)
    for pid in ids:
        result = run_command(["taskkill", "/PID", pid, "/T", "/F"], ADB_TIMEOUT_SECONDS)
        if result.returncode != 0:
            LOGGER.error("Could not close FDM process %s: %s", pid, result.stderr.strip())


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", help="adb serial, required when multiple devices are connected")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between checks (default: 1)")
    parser.add_argument("--click-delay", type=float, default=0.15, help="seconds to wait before clicking (default: 0.15)")
    parser.add_argument("--process-name", action="append", dest="process_names", metavar="NAME",
                        help="additional FDM executable name; may be supplied more than once")
    args = parser.parse_args()
    if args.interval <= 0 or args.click_delay < 0:
        parser.error("--interval must be positive and --click-delay cannot be negative")
    names = tuple(dict.fromkeys(DEFAULT_FDM_PROCESS_NAMES + tuple(args.process_names or ())))
    return Config(find_adb(), args.device, args.interval, args.click_delay, names)


def run(config: Config) -> None:
    device = choose_device(config.adb_path, config.device)
    config = Config(config.adb_path, device, config.interval, config.click_delay, config.process_names)
    button = wait_for_button_location()
    download_running = ask_initial_download_state()
    LOGGER.info("Monitoring %s. 5G resumes downloads; LTE/non-5G pauses them.", device)

    network_failures = 0
    while True:
        try:
            network_type = get_network_type(config)
            should_resume = is_5g(network_type)
            network_failures = 0
            LOGGER.info("Phone network type: %s (%s)", network_type, "5G" if should_resume else "not 5G")
        except ControllerError:
            network_failures += 1
            LOGGER.exception("Could not read the phone network state (%d/%d)", network_failures, NETWORK_FAILURE_LIMIT)
            if network_failures >= NETWORK_FAILURE_LIMIT:
                raise
            time.sleep(config.interval)
            continue

        if should_resume != download_running:
            click(button, config.click_delay)
            download_running = should_resume
            LOGGER.info("FDM button clicked: %s", "resume" if should_resume else "pause")
        time.sleep(config.interval)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config: Optional[Config] = None
    try:
        config = parse_args()
        run(config)
    except KeyboardInterrupt:
        LOGGER.info("Stopped by user; FDM was left running.")
        return 0
    except Exception as exc:
        LOGGER.error("Fatal error: %s", exc)
        try:
            close_fdm(config.process_names if config is not None else DEFAULT_FDM_PROCESS_NAMES)
        except Exception:
            LOGGER.exception("The fail-safe could not close Free Download Manager")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())