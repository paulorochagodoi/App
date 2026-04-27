from __future__ import annotations
import threading
import time
from typing import Callable
from babymonitor.common.constants import (
    ConnectionState, CAMERA_AP_IP, API_PORT,
    P2P_CONNECT_TIMEOUT, WATCHDOG_INTERVAL, RECONNECT_DELAY,
)
from babymonitor.common.logger import get_logger
from babymonitor.network import wifi_manager
from babymonitor.network.mdns import MDNSDiscoverer

log = get_logger(__name__)


class CameraFSM(threading.Thread):
    """Connection state machine for the Camera Pi."""

    def __init__(
        self,
        ap_ssid: str,
        ap_password: str,
        fallback_ssid: str,
        fallback_password: str,
        on_state_change: Callable[[ConnectionState, str], None] | None = None,
    ):
        super().__init__(daemon=True)
        self.ap_ssid = ap_ssid
        self.ap_password = ap_password
        self.fallback_ssid = fallback_ssid
        self.fallback_password = fallback_password
        self.on_state_change = on_state_change
        self._state = ConnectionState.INIT
        self._stop_event = threading.Event()
        self.camera_ip: str = CAMERA_AP_IP

    @property
    def state(self) -> ConnectionState:
        return self._state

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        self._state = state
        log.info("State: %s %s", state.name, detail)
        if self.on_state_change:
            self.on_state_change(state, detail)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self._set_state(ConnectionState.P2P_ATTEMPT)
        while not self._stop_event.is_set():
            if self._state == ConnectionState.P2P_ATTEMPT:
                if wifi_manager.create_ap(self.ap_ssid, self.ap_password):
                    self.camera_ip = CAMERA_AP_IP
                    self._set_state(ConnectionState.P2P_CONNECTED)
                    self._set_state(ConnectionState.STREAMING, self.camera_ip)
                    self._watchdog_loop()
                else:
                    self._try_fallback()

            elif self._state == ConnectionState.RECONNECTING:
                time.sleep(RECONNECT_DELAY)
                self._set_state(ConnectionState.P2P_ATTEMPT)

    def _try_fallback(self) -> None:
        if not self.fallback_ssid:
            log.warning("No fallback WiFi configured, retrying P2P in %ds", RECONNECT_DELAY)
            time.sleep(RECONNECT_DELAY)
            return
        self._set_state(ConnectionState.FALLBACK_WIFI)
        if wifi_manager.connect_to_wifi(self.fallback_ssid, self.fallback_password):
            ip = wifi_manager.get_wlan_ip()
            self.camera_ip = ip or CAMERA_AP_IP
            self._set_state(ConnectionState.WIFI_CONNECTED, self.camera_ip)
            self._set_state(ConnectionState.STREAMING, self.camera_ip)
            self._watchdog_loop()
        else:
            log.error("Fallback WiFi failed, retrying P2P")
            self._set_state(ConnectionState.RECONNECTING)

    def _watchdog_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(WATCHDOG_INTERVAL)
            ip = wifi_manager.get_wlan_ip()
            if not ip:
                log.warning("Lost network interface, reconnecting")
                self._set_state(ConnectionState.RECONNECTING)
                return


class MonitorFSM(threading.Thread):
    """Connection state machine for the Monitor Pi."""

    def __init__(
        self,
        camera_ap_ssid: str,
        camera_ap_password: str,
        fallback_ssid: str,
        fallback_password: str,
        kiosk_url_file: str,
        default_camera_url: str,
        on_camera_url: Callable[[str], None] | None = None,
    ):
        super().__init__(daemon=True)
        self.camera_ap_ssid = camera_ap_ssid
        self.camera_ap_password = camera_ap_password
        self.fallback_ssid = fallback_ssid
        self.fallback_password = fallback_password
        self.kiosk_url_file = kiosk_url_file
        self.default_camera_url = default_camera_url
        self.on_camera_url = on_camera_url
        self._state = ConnectionState.INIT
        self._stop_event = threading.Event()
        self.camera_url: str = default_camera_url

    def stop(self) -> None:
        self._stop_event.set()

    def _write_kiosk_url(self, url: str) -> None:
        import os
        self.camera_url = url
        try:
            os.makedirs(os.path.dirname(self.kiosk_url_file), exist_ok=True)
            with open(self.kiosk_url_file, "w") as f:
                f.write(url)
        except OSError as e:
            log.warning("Could not write kiosk_url: %s", e)
        if self.on_camera_url:
            self.on_camera_url(url)

    def run(self) -> None:
        self._state = ConnectionState.P2P_ATTEMPT
        while not self._stop_event.is_set():
            log.info("State: %s", self._state.name)
            if self._state == ConnectionState.P2P_ATTEMPT:
                ok = wifi_manager.connect_to_ap(
                    self.camera_ap_ssid, self.camera_ap_password,
                    timeout=P2P_CONNECT_TIMEOUT,
                )
                if ok:
                    self._write_kiosk_url(self.default_camera_url)
                    self._state = ConnectionState.STREAMING
                    self._watchdog_loop()
                else:
                    self._try_fallback()

            elif self._state == ConnectionState.RECONNECTING:
                time.sleep(RECONNECT_DELAY)
                self._state = ConnectionState.P2P_ATTEMPT

    def _try_fallback(self) -> None:
        if not self.fallback_ssid:
            log.warning("No fallback WiFi, retrying P2P")
            time.sleep(RECONNECT_DELAY)
            return
        self._state = ConnectionState.FALLBACK_WIFI
        log.info("State: FALLBACK_WIFI")
        if wifi_manager.connect_to_wifi(self.fallback_ssid, self.fallback_password):
            self._state = ConnectionState.WIFI_CONNECTED
            log.info("State: WIFI_CONNECTED — discovering camera via mDNS")
            discoverer = MDNSDiscoverer()
            ip = discoverer.discover(timeout=30)
            if ip:
                url = f"http://{ip}:{API_PORT}"
                self._write_kiosk_url(url)
                self._state = ConnectionState.STREAMING
                self._watchdog_loop()
            else:
                log.warning("mDNS discovery failed")
                self._state = ConnectionState.RECONNECTING
        else:
            self._state = ConnectionState.RECONNECTING

    def _watchdog_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(WATCHDOG_INTERVAL)
            ip = wifi_manager.get_wlan_ip()
            if not ip:
                log.warning("Lost network, reconnecting")
                self._state = ConnectionState.RECONNECTING
                return
