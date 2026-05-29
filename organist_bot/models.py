# organist_bot/models.py
import datetime
from dataclasses import dataclass, field

from organist_bot.parsing import normalize_to_yyyymmdd, parse_start_time


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
    # Parsed once at construction; None when raw string is unparseable.
    # Excluded from repr and equality — they are derived from `date`/`time`.
    parsed_date: datetime.date | None = field(default=None, init=False, repr=False, compare=False)
    parsed_time: datetime.time | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        yyyymmdd = normalize_to_yyyymmdd(self.date)
        if yyyymmdd:
            try:
                self.parsed_date = datetime.datetime.strptime(yyyymmdd, "%Y%m%d").date()
            except ValueError:
                pass
        self.parsed_time = parse_start_time(self.time)
