import hashlib
import json
import random
import secrets
import socket
import threading
import time
from dataclasses import dataclass

import verbose
from node import open_lobby_socket
from protocol import (LOBBY_PORT, LOBBY_SENTINEL, LOBBY_TYPES, MAX_PLAYERS, MIN_PLAYERS, MalformedPacket,
                      MsgType, Packet, PeerInfo, Timing, decode, encode, lobby_id_from_room)

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

    def run(self, cancel: Optional[threading.Event] = None) -> Optional[LobbyResult]:
        start = time.monotonic()
        next_announce = start
        self._status(f"searching for players... (1/{self.min_players} needed)")
        try:
            while True:
                now = time.monotonic()
                if cancel is not None and cancel.is_set():
                    return self._give_up("cancelled")
                if now >= next_announce:
                    self._announce()
                    next_announce = now + self.t.t_lobby_announce_interval
                self._prune(now)
                self._recv_once()
                now = time.monotonic()
                result = self._try_close(now)
                if result is not None:
                    self._start_linger()
                    return result
                self._drive_proposals(now)
                if now - start >= self.t.t_lobby_timeout:               
                    if not self._forced:
                        if len(self.roster) < self.min_players:
                            return self._give_up("not enough players were found")
                        self._forced, self._forced_at, self._proposal = True, now, None
                        self._status("timeout reached: forcing a final vote on the current roster")
                    elif now - self._forced_at >= self.forced_wait:
                        return self._give_up("the forced vote did not reach agreement")
        except KeyboardInterrupt:
            return self._give_up("cancelled")
        finally:
            if not self._lingering:
                self.sock.close()

    def _give_up(self, reason: str) -> None:
        self._send(MsgType.LOBBY_LEAVE, {"lobby_peer_id": self.my_id})
        self._status(f"left the lobby: {reason}")
        return None

    def _send(self, msg_type: MsgType, payload: dict) -> None:
        pkt = Packet(msg_type, self.lobby_id, LOBBY_SENTINEL, 0, 0, 0, payload)
        verbose.packet("SEND", pkt, "broadcast")
        try:
            self.sock.sendto(encode(pkt), self.bcast)
        except OSError as exc:
            verbose.info(f"lobby broadcast failed: {exc}")

    def _announce(self) -> None:
        me = self.roster[self.my_id]
        me.last_seen = time.monotonic()
        self._send(MsgType.LOBBY_PRESENCE, {"lobby_peer_id": self.my_id, "display_name": me.display_name,
                                            "host": me.host, "port": me.port})

    def _send_vote(self, ids: list, h: str, now: float) -> None:
        self._proposal = (h, now)
        self.acks.setdefault(h, {})[self.my_id] = now    
        self._send(MsgType.LOBBY_START_VOTE, {"participants": ids})

    def _send_ack(self, h: str) -> None:
        self._send(MsgType.LOBBY_START_ACK, {"participants_hash": h, "lobby_peer_id": self.my_id})
