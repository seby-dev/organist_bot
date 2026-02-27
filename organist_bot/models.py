# organist_bot/models.py
from dataclasses import dataclass


@dataclass
class Gig:
    """Represents a single organ gig opportunity."""

    header: str
    organisation: str
    locality: str
    date: str
    time: str
    fee: str | None
    link: str
    # Detail page fields (populated after fetching individual page):
    phone: str | None = None
    contact: str | None = None
    email: str | None = None
    address: str | None = None
    postcode: str | None = None
    musical_requirements: str | None = None
