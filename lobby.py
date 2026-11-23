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

@dataclass
class _Entry:
    display_name: str
    host: str
    port: int
    last_seen: float

@dataclass
class LobbyResult:
    session_id: bytes          
    my_peer_id: int            
    manifest: dict   

class Lobby:
    def __init__(self, room_code: str, display_name: str, game_host: str, game_port: int, *,
                 timing: Optional[Timing] = None, bcast_addr: str = "255.255.255.255",
                 lobby_port: int = LOBBY_PORT, min_players: int = MIN_PLAYERS,
                 max_players: int = MAX_PLAYERS, on_status: Optional[Callable[[str], None]] = None,
                 drop_rate: float = 0.0, rng: Optional[random.Random] = None):
        self.t = timing or Timing()
        self.lobby_id = lobby_id_from_room(room_code)
        self.my_id = secrets.token_hex(16)              
        self.min_players, self.max_players = min_players, max_players
        self.bcast = (bcast_addr, lobby_port)
        self.on_status = on_status or (lambda s: None)
        self.drop_rate, self._rng = drop_rate, rng or random.Random()
        self.sock = open_lobby_socket(lobby_port)
        now = time.monotonic()
        self.roster = {self.my_id: _Entry(display_name, game_host, game_port, now)}
        self.last_change = now                          
        self.acks: dict = {}                            
        self._proposal: Optional[tuple] = None          
        self._forced = False
        self._forced_at = 0.0
        self.forced_wait = 3 * self.t.t_lobby_quiet     
        self.ack_ttl = 4 * self.t.t_lobby_quiet
        self.linger = 2 * self.t.t_lobby_quiet
        self._frozen = False
        self._lingering = False
