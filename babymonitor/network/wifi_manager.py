from __future__ import annotations
import subprocess
import time
from babymonitor.common.logger import get_logger

log = get_logger(__name__)


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def create_ap(ssid: str, password: str, interface: str = "wlan0") -> bool:
    log.info("Creating AP: %s on %s", ssid, interface)
    code, _, err = _run([
        "nmcli", "device", "wifi", "hotspot",
        "ifname", interface,
        "ssid", ssid,
        "password", password,
    ])
    if code != 0:
        log.error("Failed to create AP: %s", err)
        return False
    log.info("AP created successfully")
    return True


def connect_to_ap(ssid: str, password: str, timeout: int = 15) -> bool:
    log.info("Connecting to AP: %s", ssid)
    _run(["nmcli", "connection", "delete", ssid])
    code, _, err = _run([
        "nmcli", "device", "wifi", "connect", ssid,
        "password", password,
    ], timeout=timeout + 5)
    if code != 0:
        log.error("Failed to connect to AP %s: %s", ssid, err)
        return False
    log.info("Connected to AP: %s", ssid)
    return True


def connect_to_wifi(ssid: str, password: str, timeout: int = 30) -> bool:
    log.info("Connecting to WiFi: %s", ssid)
    _run(["nmcli", "connection", "delete", ssid])
    code, _, err = _run([
        "nmcli", "device", "wifi", "connect", ssid,
        "password", password,
    ], timeout=timeout + 5)
    if code != 0:
        log.error("Failed to connect to WiFi %s: %s", ssid, err)
        return False
    log.info("Connected to WiFi: %s", ssid)
    return True


def get_wlan_ip(interface: str = "wlan0") -> str | None:
    code, out, _ = _run(["ip", "-4", "addr", "show", interface])
    if code != 0:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            return line.split()[1].split("/")[0]
    return None


def disconnect(interface: str = "wlan0") -> None:
    _run(["nmcli", "device", "disconnect", interface])


def stop_ap() -> None:
    _run(["nmcli", "connection", "delete", "Hotspot"])
    _run(["nmcli", "connection", "delete", "BabyMonitor-AP"])
