import socket
import threading
import struct
import json

#Article used in the making of this: 
#[BOILERPLATE]https://abdesol.medium.com/udp-protocol-with-a-header-implementation-in-python-b3d8dae9a74b
#[MULTICAST]https://stackoverflow.com/questions/603852/how-do-you-udp-multicast-in-python
#[PEER CLASS]https://blog.devgenius.io/implementing-peer-to-peer-data-exchange-in-python-8e69513489af

"""
Binded to 0.0.0.0 so that it may receiove broadcast and multicast traffic
port=7777 becuase it is the defined default udp port
mcast_group='239.1.1.1' because it is a recommened multicast IP address for programs like these
"""
class Peer: 
    def __init__(self, host='0.0.0.0', port=7777, mcast_group='239.1.1.1'):
        self.host = host
        self.port = port
        self.mcast_group = mcast_group
        self.peers = set()

        #setting up the UDP socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

        #SO_REUSEPORT only exists in macOS, SO_REUSEADDR is on windows. This is a safety check but iirc they serve the same purpose
        if hasattr(socket, 'SO_REUSEADDR'):
            self.sock.setsockopt(socket.SOL_SOCKET , socket.SO_REUSEADDR,1)
        elif hasattr(socket, 'SO_REUSEPORT'):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.REUSEPORT, 1)

        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        self.sock.bind((self.host, self.port))

        #4s4s is a format string us3ed to tell the socket which multicast grp and IP addr
        if hasattr(socket,'IP_ADD_MEMBERSHIP'): #joining multicast group, subject to change as idk how gossip works yet
            mreq = struct.pack("4s4s", socket.inet_aton(self.mcast_group), socket.inet_aton('0.0.0.0'))
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        if hasattr(socket, 'IP_MULTICAST_TTL'):
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1) #1 means it stays inside the LAN netowkr


        #BELOW ARE FUNCTIONS USED FOR SENDING MESSAGES

        """
        message -> subject to change later as we start to define the different message types using JSON (LOBBY_START_VOTE, etc.)
        we can do it as such:
            msg_type = {
                        content
            }

            packet = json.dumps(msg_type).encode('utf-8') //not sure about the utf-8 part
            self.sock.sendto(packet, -----)
        """

        def unicast_msg(self, message, peer_ip, peer_port):
            #subject to change
            self.sock.sendto(message.encode(), (peer_ip, peer_port))

        def broadcast_msg(self, message):
            #subject to change
            self.sock.sendto(message.encode(), ('255.255.255.255', self.port))

        def gossip_msg(self, message):
            self.sock.sendto(message.encode(), (self.mcast_group, self.port))

        def listen(self):
            while True:
                #this one accepts any peer it hears, should be modified
                data, addr = self.sock.recvfrom(1024)
                self.peers.add(addr)

        """
        sample code to filter traffic:

        def listen(self):
        while True:
            data, addr = self.sock.recvfrom(1024)
            try:
                msg = json.loads(data.decode('utf-8'))
                msg_type = msg.get("type")

                # Only register new peers if they broadcast LOBBY_PRESENCE
                if msg_type == "LOBBY_PRESENCE":
                    if addr not in self.peers:
                        self.peers.add(addr)
                        print(f"New peer discovered: {addr}")

                # Only process other commands if the sender is already an accepted peer
                elif addr in self.peers:
                    if msg_type == "LOBBY_START_VOTE":
                        print(f"Vote started by {addr}: {msg.get('data')}")
                    elif msg_type == "LOBBY_LEAVE":
                        self.peers.discard(addr)
                        print(f"Peer left: {addr}")

            except (json.JSONDecodeError, UnicodeDecodeError):
                # Silently ignore malformed packets or non-app traffic on the same port
                pass

        """