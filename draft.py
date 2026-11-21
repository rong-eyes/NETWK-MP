import socket
import threading
import hashlib #for SHA 256
import secrets #for generating nonce

#import game_session file
#import resource_trading file
import constants as const

"""
A,B,C,D each: broadcast CARD_COMMIT ------------------------>
A,B,C,D each: (barrier satisfied once own log has 4 commits)
A,B,C,D each: broadcast CARD_REVEAL ------------------------>
A          : needs 1 ORE for its build; A lacks ORE, B (its
             right neighbor) produces it
A -> B     : RESOURCE_QUERY { resource: ORE }
B -> A     : RESOURCE_OFFER { resource: ORE, qty: 1, price: 2 }
A -> B     : RESOURCE_BUY   { resource: ORE, qty: 1, coins: 2 }
A,B,C,D each: broadcast ACTION_ANNOUNCE ---------------------->
A -> D (left neighbor in Age I): HAND_PASS { cards: [...] }
B -> A                        : HAND_PASS { cards: [...] }
C -> B                        : HAND_PASS { cards: [...] }
D -> C                        : HAND_PASS { cards: [...] }
-- next Draft Turn begins once every peer has both sent and
   received its HAND_PASS --
"""

class Draft:
    def __init__(self, peer, public_log, resource_trading, game_session):
        self.peer = peer
        self.public_log = public_log
        self.resource_trading = resource_trading
        self.game_session = game_session

        #CURRENT DRAFT STATE
        self.age = None
        self.phase = None
        self.current_turn = 1 #increments every draft turn
        self.current_hand = []

        #temporary forthe current turn
        self.commits = []
        self.reveals = []
        self.incoming_hand = None

        self.missed_commit_barriers = {}
        self.condition = threading.Condition()  #keeps different parts of the program synced

        #THE FOLLOWINg maps UDP message to a function that processes them 
        self.peer.register_handler("CARD_COMMIT", self.handle_card_commit)
        self.peer.register_handler("CARD_REVEAL", self.handle_card_reveal)
        self.peer.register_handler("ACTION_ANNOUNCE", self.handle_action_announce)
        self.peer.register_handler("HAND_PASS", self.handle_hand_pass)

    #progreession
    def run_current_age(self, age, current_hand):
        self.age = age
        self.hand = current_hand.copy()

        for turn in range(1,6): #6 turns only
            self.current_turn = turn
            self.run_turn() #simulate every turn

    def run_turn(self):
        self.reset_turn_state() 

        self.phase = "COMMIT" #an internal tracker type thing

        selected_card, action = self.choose_action()
        nonce = secrets.token_bytes(64)
        commitment = self.calculate_commit(selected_card, action, nonce)

        self.commits[self.peer.peer_id] = commitment

        self.peer.broadcast_msg("CARD_COMMIT",
                                {
                                    "age": self.age,
                                    "turn": self.turn,
                                    "commitment": commitment
                                    #"card_id": <card_id>
                                })

        #NOT SURE ABOUT THIS, TO BE IMPLMEENT
        expected_peers = self.public_log.get_active_peers() - {self.peer_id}
        completed, missing_peers = self.wait_for_commits(expected_peers)

        if not completed:
            pass #handles peers thaat missed the marriers

        self.phase = "REVEAL"

        reveal = {
            "age": self.age,
            "turn": self.turn,
            "action": action,
            "nonce": nonce.hex() #hex() turns an object into string of hexadecimal
        }

        if action == "BUILD":
            reveal["card_id"] = selected_card #assigns it/ just adds it if it doesnt exist

        self.peer.broadcast_msg("CARD_REVEAL",reveal)
        if not self.wait_for_reveal(): #im not sure if this is needed
            return

        self.phase = "RESOLVE"
        self.resolve_action(selected_card, action)

        self.phase = "ANNOUNCE"
        announcement = self.create_announcement(selected_card, action)
        self.peer.broadcast_msg("ACTION_ANNOUNCE", announcement)#DIFFERENT MAY NEED TO REIMPLEMENT

        #records locally if the network layer doesnt include the self
        self.handle_action_announce(self.game_session.own_peer_id, announcement)

        self.phase = "PASS"
        self.pass_hand()

    #message handlers --> not the actual process, they just verify the paylod
    def handle_card_commit(self, sender, payload):
        #this is a security check
        if payload["age"] != self.age:
            return
        if payload["turn"] != self.turn:
            return
    
        with self.condition:
            if sender not in self.commits: #prevents a retransmission or a duplicate cqrd_commit
                self.commits[sender] = payload["commitment"]
                self.condition.notify_all()
    
    def handle_card_reveal(self, sender_id, payload):
        if payload["age"] != self.age:
            return
        if payload["turn"] != self.turn:
            return
    
            #essentially ignores if its a retransmit
        with self.condition:
            if sender_id in self.reveals:
                return
            if sender_id not in self.commits:
               return
            self.reveals[sender_id]=payload
            if not self.verify_commitment(sender_id,payload["commitment"]):
                return
            self.condition.notify_all()
    
    def handle_action_announce(self, sender_id, payload):
        if payload["age"] != self.age:
            return
        if payload["turn"] != self.turn:
            return
    
        with self.condition:
            self.public_log.append_action(sender_id, payload)
            self.condition.notify_all()
    
    def handle_hand_pass(self, sender_id, payload): 
        if payload["age"] != self.age:
            return
        if payload["turn"] != self.turn:
            return
        if sender_id != self.get_expected_incoming_neighbour():
            return
    
        with self.condition:
            self.incoming_hand = payload["cards"]
            self.condition.notify_all()
    
    

    #inner functions 
    def reset_turn_state(self):
        with self.condition:    #this is needed to prevent overlap between information on
                                #different player turn resets. because the game itself  is a shared instance
                                #and a thread is like each side of the table that the player is sitting on
            self.phase = None
            self.commits.clear()
            self.reveals.clear()
            self.incoming_hand = None

    def calculate_commit(self, card_id, action, nonce):
        data = self.encode_commitment_data(card_id, action,nonce)
        return hashlib.sha256(data).hexdigest()

    def encode_commitment_data(self, card_id, action, nonce):
        nonce_bytes = bytes.fromhex(nonce) if isinstance(nonce, str) else nonce
        #bascially converts a hex string (if it is one) back into bits
        return (card_id.encode("utf-8")+action.encode("utf-8")+ nonce_bytes)

    def verify_commitment(self, sender_id, reveal):
        committed_hash = self.commits[sender_id]#for every peer in the turn to broadcast a ommit
        card_id = reveal.get("card_id")
        action = reveal["action"]
        nonce = bytes.fromhex(reveal["nonce"])

        calculated_hash = self.calculate_commit(card_id,action,nonce)
        return calculated_hash == committed_hash

    def choose_card(self):
        #INSERT CARD CHOOSING LOGIC
        raise NotImplementedError

    def resolve_action(self, card_id, action):
        #IMPLEMENT RESOURCE COMPUTATION IF IT IS POSSIBLE FOR THE PLAYER
        if action == "BUILD":
             #insert resrouce trading
            self.resource_trading.run() #TO BE CHANGED
        elif action == "WONDER_STAGE":
            #insert game logic function
            self.current_hand.remove(card_id)
            #insert gain coins
        elif action == "DISCARD": 
            #insert gain 3 coins logic
            raise NotImplementedError

    def create_announcement(self, card_id, action):
        #return the correct ACTION_ANNOUNCE paylaod for each action
        raise NotImplementedError

    def get_neightbour(self, direction):
        i = self.seat_order.index(self.peer_id) #SUBJECT TO CHANGE AFTER SEATING IS IMPLEMENTED

        if direction == "LEFT":
            step = -1
        elif direction == "RIGHT":
            step = 1
        else:
            raise ValueError("Invalid Direction")

        total_peers = len(self.seat_order)

        for offset in range(1, total_peers):
            idx = (i + (step * offset)) % total_peers #module makes it circular
            candiate = self.seat_order[idx]

            if candiate in self.active_peers:
                return candiate

        return None

    #SUBJECT TO CHANGE WHEN SEATING ORDER GETS IMPLEMENTED
    def get_expected_incoming_neighbour(self):
        if self.age in(1,3):
            return self.get_neightbour("RIGHT")
        if self.age == 2:
            return self.get_neighbour("LEFT")

    def get_outgoing_neighbour(self):
        if self.age in(1,3):
            return self.get_neightbour("LEFT")
        if self.age == 2:
            return self.get_neighbour("RIGHT")

    def pass_hand(self):

        outgoing_neighbour = self.get_outgoing_neighbour

        if outgoing_neighbour is None:
            return

        passing_hand = self.current_hand.copy()

        if self.turn == 6:
            passing_hand = []

        neighbour = self.get_outgoing_neighbour()

        self.peer.unicast_msg("HAND_PASS", {
                                            "age": self.age,
                                            "turn": self.turn,
                                            "cards": passing_hand
                                            }, neighbour) #SUBJECT TO CHANGE

        self.wait_incoming_hand()
        self.current_hand = self.incoming_hand.copy()

    #timer elpasing handling thing
    def wait_for_commits(self, expected_peers):
        with self.condition:
            completed=self.condition.wait_for( lambda: expected_peers.issubset(self.commits.keys()),
                                              timeout = const.T_TURN_TIMEOUT )
            missing_peers = expected_peers - self.commits.keys()
            return completed, missing_peers

    def wait_for_reveal(self, expected_peers):
        with self.condition:
            completed = self.condition.wait_for( lambda: expected_peers.issubset(self.reveals.keys()), timeout=const.T_SEED_TIMEOUT)
            missing_peers = expected_peers - self.commits.keys()
            return completed, missing_peers
        
    def wait_for_incoming_hand(self):
        with self.condition:
            self.condition.wait_for( lambda: self.incoming_hand is not None )

    def update_missed_barriers(self, missing_peers):
        for peer_id in self.get_active_peers():
            if peer_id in missing_peers:
                self.missed_commit_barriers[peer_id] = (
                    self.missed_commit_barriers.get(peer_id, 0) + 1
                )

                if self.missed_commit_barriers[peer_id] >= 2:
                    self.mark_disconnected(peer_id) #placeholder for disconnecting protocl

            else:
                self.missed_commit_barriers[peer_id] = 0