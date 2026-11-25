import queue
import random
import socket
import threading
import time
from functools import partial
from typing import Callable, NamedTuple, Optional

import verbose
from channel import Channel
from protocol import (DIRECTED_TYPES, LOBBY_PORT, LOBBY_TYPES, UNRELIABLE_TYPES, MalformedPacket, MsgType,
                      Packet, PeerInfo, Timing, decode, encode)


def local_ip() -> str:
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

def open_game_socket(host: str = "0.0.0.0", port: int = 0) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.bind((host, port))
    s.settimeout(0.2)
    return s


class Incoming(NamedTuple):
    peer_id: int
    msg_type: MsgType
    payload: dict
    packet: Packet


class Node:
    def __init__(self, sock: socket.socket, session_id: bytes, my_id: int, manifest: dict,
                 timing: Optional[Timing] = None, drop_rate: float = 0.0,
                 rng: Optional[random.Random] = None,
                 on_channel_failed: Optional[Callable[[int], None]] = None,
                 on_malformed: Optional[Callable[[tuple, str], None]] = None):
        self.sock, self.session_id, self.my_id = sock, session_id, my_id
        self.manifest: dict = dict(manifest)                
        self.t = timing or Timing()
        self.drop_rate, self._rng = drop_rate, rng or random.Random()   
        self.on_channel_failed, self.on_malformed = on_channel_failed, on_malformed
        self.peer_ids = sorted(p for p in self.manifest if p != my_id)
        self.channels = {pid: Channel(pid, my_id, session_id, partial(self._transmit, pid), self.t,
                                      on_failed=self._channel_failed) for pid in self.peer_ids}
        self.live = set(self.peer_ids)                       
        self.failed_channels: set = set()                    
        self.inbox: "queue.Queue[Incoming]" = queue.Queue()  
        self._cond = threading.Condition()
        self._hello_acked: set = set()
        self._hello_sent: set = set()
        self._running = False
        self._threads: list = []

    def start(self) -> None:
        self._running = True
        for fn in (self._recv_loop, self._timer_loop):
            th = threading.Thread(target=fn, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._running = False
        for th in self._threads:
            th.join(timeout=1)
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, peer_id: int, msg_type: MsgType, payload: dict) -> bool:
        ch = self.channels[peer_id]
        if msg_type in UNRELIABLE_TYPES:
            return ch.send_unreliable(msg_type, payload)
        return ch.send(msg_type, payload)

    def broadcast(self, msg_type: MsgType, payload: dict) -> list:
        if msg_type in DIRECTED_TYPES or msg_type in LOBBY_TYPES:
            raise ValueError(f"{msg_type.name} is not a game broadcast message")
        return [pid for pid in sorted(self.live) if self.send(pid, msg_type, payload)]

    def ping(self, peer_id: int) -> bool:
        return self.send(peer_id, MsgType.PING, {"sent_at_ms": time.monotonic_ns() // 1_000_000})

    def mark_disconnected(self, peer_id: int) -> None:
        self.live.discard(peer_id)

    def recv(self, timeout: Optional[float] = None) -> Optional[Incoming]:
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def _transmit(self, peer_id: int, pkt: Packet) -> None:
        info: PeerInfo = self.manifest[peer_id]
        verbose.packet("SEND", pkt, f"peer {peer_id}")
        try:
            self.sock.sendto(encode(pkt), (info.host, info.port))
        except OSError as exc:      
            verbose.info(f"sendto peer {peer_id} failed: {exc}")

    def _channel_failed(self, peer_id: int) -> None:
        self.failed_channels.add(peer_id)
        verbose.info(f"channel to peer {peer_id} FAILED after {self.t.retry_max} timeouts")
        if self.on_channel_failed:
            self.on_channel_failed(peer_id)

    def _recv_loop(self) -> None:
        while self._running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue             
            except OSError:
                if self._running:
                    continue
                break
            if self.drop_rate and self._rng.random() < self.drop_rate:
                continue
            self._handle_datagram(data, addr)

    def _timer_loop(self) -> None:
        while self._running:
            for ch in self.channels.values():
                ch.tick()
            time.sleep(0.02)

    def _handle_datagram(self, data: bytes, addr: tuple) -> None:
        try:
            pkt = decode(data)
        except MalformedPacket as exc:
            verbose.info(f"MALFORMED datagram from {addr[0]}:{addr[1]}: {exc}")
            if self.on_malformed:
                self.on_malformed(addr, str(exc))
            return
        if pkt.session_id != self.session_id or pkt.msg_type in LOBBY_TYPES:
            return                  
        ch = self.channels.get(pkt.sender)
        if ch is None or pkt.sender not in self.live:
            verbose.info(f"ignoring datagram from unknown/disconnected peer id {pkt.sender}")
            return
        verbose.packet("RECV", pkt, f"peer {pkt.sender}")
        for p in ch.on_packet(pkt):
            self._dispatch(pkt.sender, p)

    def _dispatch(self, pid: int, pkt: Packet) -> None:
        t, body = pkt.msg_type, pkt.payload or {}
        ch = self.channels[pid]
        if t == MsgType.PING:
            echo = body.get("sent_at_ms")
            if isinstance(echo, int):
                self.send(pid, MsgType.PONG, {"echo_sent_at_ms": echo})  
            else:
                self._malformed(pid, "PING without integer sent_at_ms")
        elif t == MsgType.PONG:
            echo = body.get("echo_sent_at_ms")
            if isinstance(echo, int):
                rtt = max((time.monotonic_ns() // 1_000_000 - echo) / 1000.0, 0.0005)
                ch.note_rtt(rtt)
                ch.last_pong = time.monotonic()
                self.inbox.put(Incoming(pid, MsgType.PONG, body, pkt))
            else:
                self._malformed(pid, "PONG without integer echo_sent_at_ms")
        elif t == MsgType.HELLO:
            self._send_hello(pid)                   
            self.send(pid, MsgType.HELLO_ACK, {})
        elif t == MsgType.HELLO_ACK:
            with self._cond:
                self._hello_acked.add(pid)
                self._cond.notify_all()
        else:
            self.inbox.put(Incoming(pid, MsgType(t), body, pkt))

    def _malformed(self, pid: int, reason: str) -> None:
        verbose.info(f"MALFORMED payload from peer {pid}: {reason}")
        if self.on_malformed:
            self.on_malformed((self.manifest[pid].host, self.manifest[pid].port), reason)

    def _send_hello(self, pid: int) -> None:
        with self._cond:
            if pid in self._hello_sent:
                return
            self._hello_sent.add(pid)
        self.send(pid, MsgType.HELLO, {})

    def handshake(self, timeout: Optional[float] = None) -> set:
        
        timeout = self.t.t_join_timeout if timeout is None else timeout
        for pid in self.peer_ids:
            self._send_hello(pid)
        deadline = time.monotonic() + timeout
        with self._cond:
            while not set(self.peer_ids) <= self._hello_acked:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            return set(self._hello_acked)
