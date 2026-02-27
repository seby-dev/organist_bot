# organist_bot/models.py
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class Gig:
    """Represents a single organ gig opportunity."""
    header: str
    organisation: str
    locality: str
    date: str
    time: str
    fee: Optional[str]
    link: str
    # Detail page fields (populated after fetching individual page):
    phone: Optional[str] = None
    contact: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    postcode: Optional[str] = None
    musical_requirements: Optional[str] = None

