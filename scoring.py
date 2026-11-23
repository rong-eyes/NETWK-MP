import json
import time

CATEGORIES = ("military", "treasury", "wonder", "civilian", "scientific", "commercial", "guilds")
VICTORY_TOKEN = {1: 1, 2: 3, 3: 5}     # +VP for a win in Age I / II / III (Section 3)
DEFEAT_TOKEN = -1

@dataclass(frozen=True)
class CardInfo:
    name: str
    color: str                      
    shields: int = 0               
    vp: int = 0                    
    science: Optional[str] = None 

@dataclass(frozen=True)
class BoardStage:
    vp: int = 0
    shields: int = 0
    wild_science: int = 0

@dataclass
class SeatView:
    seat: int
    built: list                     
    stages: list                    
    stage_ages: list               
    coins: int
    military_tokens: list = field(default_factory=list)   

CardDB = dict  

def load_card_db(path: str) -> CardDB:
    with open(path, encoding="utf-8") as fh:
        return {c["name"]: CardInfo(**c) for c in json.load(fh)}

def neighbours(ring: list, seat: int) -> tuple:
    i, k = ring.index(seat), len(ring)
    return ring[(i - 1) % k], ring[(i + 1) % k]

def shield_total(view: SeatView, db: CardDB, upto_age: int = 3) -> int:
    cards = sum(db[c].shields for c, age in view.built if age <= upto_age)
    stages = sum(view.stages[i].shields for i, age in enumerate(view.stage_ages) if age <= upto_age)
    return cards + stages