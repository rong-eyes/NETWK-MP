import socket
import threading
import time
from dataclasses import dataclass

import verbose

_HEX = set("0123456789abcdef")


def _is_hex(value, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and set(value) <= _HEX


def participants_hash(ids) -> str:
    return hashlib.sha256(json.dumps(sorted(ids), separators=(",", ":")).encode("utf-8")).hexdigest()

class _Entry:
    display_name: str
    host: str
    port: int
    last_seen: float

class LobbyResult:
    session_id: bytes          # == lobby_id, unchanged for the whole game (Section 7.2)
    my_peer_id: int            # manifest peer_id, 1..k
    manifest: dict   
