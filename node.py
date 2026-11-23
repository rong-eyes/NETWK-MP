import queue
import threading
import time

import verbose

from channel import Channel
from protocol import (DIRECTED_TYPES, LOBBY_PORT, LOBBY_TYPES, UNRELIABLE_TYPES, MalformedPacket, MsgType,
                      Packet, PeerInfo, Timing, decode, encode)


def local_ip() -> str:
    """Best guess at this machine's LAN address (connect() on UDP sends nothing)."""
    for target in ("8.8.8.8", "10.255.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 9))
            return s.getsockname()[0]
        except OSError:
            continue
        finally:
            s.close()
    return "127.0.0.1"


def open_lobby_socket(port: int = LOBBY_PORT) -> socket.socket:
    """Every idle client binds this same port at once, so BOTH reuse options are needed:
    SO_REUSEADDR is what Windows/Linux want, macOS also needs SO_REUSEPORT."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", port))
    s.settimeout(0.05)
    return s
