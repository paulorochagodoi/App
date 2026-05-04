from __future__ import annotations
import os
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


class BaseFSM(threading.Thread):
    """Shared connection state machine logic for both Pi nodes."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._state = ConnectionState.INIT
        self._stop_event = threading.Event()

    @property
    def state(self) -> ConnectionState:
        return self._state

    def stop(self) -> None:
        self._stop_event.set()

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        self._state = state
        log.info("State → %s %s", state.name, detail)
        self._on_state_changed(state, detail)

    # ── Hooks for subclasses ────────────────────────────────────────────────

    def _on_state_changed(self, state: ConnectionState, detail: str) -> None:
        """Called after every state transition. Override to react."""

    def _do_p2p_attempt(self) -> bool:
        """Try to establish P2P connection. Return True on success."""
        raise NotImplementedError

    def _on_p2p_connected(self) -> None:
        """Called once P2P is up. Sets state and starts watchdog."""
        raise NotImplementedError

    def _try_fallback(self) -> None:
        """Attempt fallback WiFi when P2P fails."""
        raise NotImplementedError

    # ── Common loop ─────────────────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(WATCHDOG_INTERVAL)
            if not wifi_manager.get_wlan_ip():
                log.warning("Lost network interface — reconnecting")
                self._set_state(ConnectionState.RECONNECTING)
                return

    def run(self) -> None:
        self._set_state(ConnectionState.P2P_ATTEMPT)
        while not self._stop_event.is_set():
            try:
                if self._state == ConnectionState.P2P_ATTEMPT:
                    if self._do_p2p_attempt():
                        self._on_p2p_connected()
                        self._watchdog_loop()
                    else:
                        self._try_fallback()
                elif self._state == ConnectionState.RECONNECTING:
                    time.sleep(RECONNECT_DELAY)
                    self._set_state(ConnectionState.P2P_ATTEMPT)
            except Exception:
                log.exception(
                    "Unhandled exception in FSM — retrying in %ds", RECONNECT_DELAY
                )
                time.sleep(RECONNECT_DELAY)
                self._state = ConnectionState.P2P_ATTEMPT


class CameraFSM(BaseFSM):
    """Connection FSM for the Camera Pi (creates the AP)."""

    def __init__(
        self,
        ap_ssid: str,
        ap_password: str,
        fallback_ssid: str,
        fallback_password: str,
        on_state_change: Callable[[ConnectionState, str], None] | None = None,
    ) -> None:
        super().__init__()
        self.ap_ssid = ap_ssid
        self.ap_password = ap_password
        self.fallback_ssid = fallback_ssid
        self.fallback_password = fallback_password
        self._on_state_change_cb = on_state_change
        self.camera_ip: str = CAMERA_AP_IP

    def _on_state_changed(self, state: ConnectionState, detail: str) -> None:
        if self._on_state_change_cb:
            self._on_state_change_cb(state, detail)

    def _do_p2p_attempt(self) -> bool:
        return wifi_manager.create_ap(self.ap_ssid, self.ap_password)

    def _on_p2p_connected(self) -> None:
        self.camera_ip = CAMERA_AP_IP
        self._set_state(ConnectionState.P2P_CONNECTED)
        self._set_state(ConnectionState.STREAMING, self.camera_ip)

    def _try_fallback(self) -> None:
        if not self.fallback_ssid:
            log.warning("No fallback WiFi configured — retrying P2P in %ds", RECONNECT_DELAY)
            time.sleep(RECONNECT_DELAY)
            self._set_state(ConnectionState.RECONNECTING)
            return
        self._set_state(ConnectionState.FALLBACK_WIFI)
        if wifi_manager.connect_to_wifi(self.fallback_ssid, self.fallback_password):
            ip = wifi_manager.get_wlan_ip()
            self.camera_ip = ip or CAMERA_AP_IP
            self._set_state(ConnectionState.WIFI_CONNECTED, self.camera_ip)
            self._set_state(ConnectionState.STREAMING, self.camera_ip)
            self._watchdog_loop()
        else:
            log.error("Fallback WiFi failed — retrying P2P")
            self._set_state(ConnectionState.RECONNECTING)


class MonitorFSM(BaseFSM):
    """Connection FSM for the Monitor Pi (joins the Camera AP)."""

    def __init__(
        self,
        camera_ap_ssid: str,
        camera_ap_password: str,
        fallback_ssid: str,
        fallback_password: str,
        kiosk_url_file: str,
        default_camera_url: str,
        on_camera_url: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.camera_ap_ssid = camera_ap_ssid
        self.camera_ap_password = camera_ap_password
        self.fallback_ssid = fallback_ssid
        self.fallback_password = fallback_password
        self.kiosk_url_file = kiosk_url_file
        self.default_camera_url = default_camera_url
        self._on_camera_url_cb = on_camera_url
        self.camera_url: str = default_camera_url

    def _do_p2p_attempt(self) -> bool:
        return wifi_manager.connect_to_ap(
            self.camera_ap_ssid, self.camera_ap_password,
            timeout=P2P_CONNECT_TIMEOUT,
        )

    def _on_p2p_connected(self) -> None:
        self._write_kiosk_url(self.default_camera_url)
        self._set_state(ConnectionState.STREAMING)

    def _write_kiosk_url(self, url: str) -> None:
        self.camera_url = url
        try:
            os.makedirs(os.path.dirname(self.kiosk_url_file), exist_ok=True)
            with open(self.kiosk_url_file, "w") as f:
                f.write(url)
        except OSError as e:
            log.warning("Could not write kiosk_url: %s", e)
        if self._on_camera_url_cb:
            self._on_camera_url_cb(url)

    def _try_fallback(self) -> None:
        if not self.fallback_ssid:
            log.warning("No fallback WiFi — retrying P2P in %ds", RECONNECT_DELAY)
            time.sleep(RECONNECT_DELAY)
            self._set_state(ConnectionState.RECONNECTING)
            return
        self._set_state(ConnectionState.FALLBACK_WIFI)
        if wifi_manager.connect_to_wifi(self.fallback_ssid, self.fallback_password):
            self._set_state(ConnectionState.WIFI_CONNECTED)
            log.info("Discovering camera via mDNS")
            ip = MDNSDiscoverer().discover(timeout=30)
            if ip:
                url = f"http://{ip}:{API_PORT}"
                self._write_kiosk_url(url)
                self._set_state(ConnectionState.STREAMING)
                self._watchdog_loop()
            else:
                log.warning("mDNS discovery failed")
                self._set_state(ConnectionState.RECONNECTING)
        else:
            self._set_state(ConnectionState.RECONNECTING)
