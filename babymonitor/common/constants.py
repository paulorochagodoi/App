from enum import Enum, auto


class NodeRole(str, Enum):
    CAMERA = "camera"
    MONITOR = "monitor"


class ConnectionState(Enum):
    INIT = auto()
    P2P_ATTEMPT = auto()
    P2P_CONNECTED = auto()
    FALLBACK_WIFI = auto()
    WIFI_CONNECTED = auto()
    STREAMING = auto()
    RECONNECTING = auto()


CAMERA_AP_IP = "10.42.0.1"
API_PORT = 8080
MDNS_SERVICE_TYPE = "_babymonitor._tcp.local."
MDNS_SERVICE_NAME = "BabyMonitor Camera._babymonitor._tcp.local."
P2P_CONNECT_TIMEOUT = 15
WATCHDOG_INTERVAL = 5
RECONNECT_DELAY = 3
