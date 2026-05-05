from __future__ import annotations
import socket
import threading
import time
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf
from babymonitor.common.constants import MDNS_SERVICE_TYPE, MDNS_SERVICE_NAME, API_PORT
from babymonitor.common.logger import get_logger

log = get_logger(__name__)


class MDNSAdvertiser:
    def __init__(self, ip: str, port: int = API_PORT):
        self._ip = ip
        self._port = port
        self._zeroconf: Zeroconf | None = None
        self._info: ServiceInfo | None = None

    def start(self) -> None:
        self._zeroconf = Zeroconf()
        self._info = ServiceInfo(
            MDNS_SERVICE_TYPE,
            MDNS_SERVICE_NAME,
            addresses=[socket.inet_aton(self._ip)],
            port=self._port,
            properties={"version": "1"},
        )
        self._zeroconf.register_service(self._info)
        log.info("mDNS advertised at %s:%d", self._ip, self._port)

    def stop(self) -> None:
        if self._zeroconf and self._info:
            self._zeroconf.unregister_service(self._info)
            self._zeroconf.close()
            self._zeroconf = None
        log.info("mDNS advertiser stopped")


class MDNSDiscoverer:
    def __init__(self):
        self._found_ip: str | None = None
        self._event = threading.Event()
        self._zeroconf: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None

    def discover(self, timeout: int = 30) -> str | None:
        self._zeroconf = Zeroconf()
        self._browser = ServiceBrowser(self._zeroconf, MDNS_SERVICE_TYPE, self)
        log.info("mDNS discovery started (timeout %ds)", timeout)
        self._event.wait(timeout=timeout)
        self.stop()
        return self._found_ip

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info:
            ip = socket.inet_ntoa(info.addresses[0])
            log.info("mDNS found camera at %s:%d", ip, info.port)
            self._found_ip = ip
            self._event.set()

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def stop(self) -> None:
        if self._zeroconf:
            self._zeroconf.close()
            self._zeroconf = None
