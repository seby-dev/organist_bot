import logging
import time

import requests
from bs4 import BeautifulSoup, Tag
from bs4.element import ResultSet
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from organist_bot.config import settings

logger = logging.getLogger(__name__)


def _log_retry(retry_state: RetryCallState) -> None:
    """Tenacity before-sleep callback — logs each retry attempt."""
    attempt = retry_state.attempt_number
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    wait_sec = retry_state.next_action.sleep if retry_state.next_action else 0
    url = retry_state.args[1] if len(retry_state.args) > 1 else "unknown"
    logger.warning(
        "Fetch failed — retrying",
        extra={
            "url": url,
            "attempt": attempt,
            "wait_sec": round(wait_sec, 1),
            "error": str(exc),
        },
    )


class Scraper:
    """Web scraper for organistsonline.org gig listings with retry logic."""

    def __init__(self, user_agent: str = "OrganistBot/1.0", timeout: int = 10):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        # The session is long-lived (shared across all poll runs in main.py).
        # Mounting an explicit adapter keeps up to 10 connections alive in the
        # pool so that every fetch within a run — listing page + any detail
        # pages — reuses an existing TCP/TLS connection with no handshake cost.
        adapter = HTTPAdapter(pool_connections=1, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.timeout = timeout
        logger.debug(
            "Scraper initialised",
            extra={"user_agent": user_agent, "timeout": timeout},
        )

    @retry(
        retry=retry_if_exception_type(RequestException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        before_sleep=_log_retry,
    )
    def fetch(self, url: str) -> str:
        """Fetch HTML content from a URL with automatic retries."""
        logger.debug("Fetching URL", extra={"url": url})
        t0 = time.perf_counter()

        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.debug(
            "Fetch successful",
            extra={
                "url": url,
                "status": response.status_code,
                "size_bytes": len(response.text),
                "elapsed_ms": elapsed_ms,
            },
        )
        return response.text

    def parse_gig_listings(self, html_content: str, cls: str) -> ResultSet:
        """Parse and return all gig listing elements from HTML."""
        soup = BeautifulSoup(html_content, "html.parser")
        results = soup.find_all("div", class_=cls)
        if not results:
            logger.debug("parse_gig_listings: no elements found", extra={"class": cls})
        return results

    def extract_basic_details(self, booking: Tag) -> dict:
        """Extract basic gig details from a listing element."""
        details = {
            "header": self._get_text(booking, "h2", "type"),
            "organisation": self._get_text(booking, "h3", "organisation"),
            "locality": self._get_text(booking, "h4", "locality"),
            "date": self._get_text(booking, "p", "date"),
            "time": self._get_text(booking, "p", "time"),
            "fee": self._get_text(booking, "p", "fee"),
            "link": self._extract_link(booking),
        }
        logger.debug(
            "Extracted basic details",
            extra={
                "header": details.get("header"),
                "date": details.get("date"),
                "fee": details.get("fee"),
                "has_link": details.get("link") is not None,
            },
        )
        return details

    def extract_basic_from_detail(self, detail_html: str, link: str) -> dict:
        """Extract basic gig fields from a detail page.

        The detail page's bookingDetails div contains the same header/org/date/
        time/fee elements as the listing page, so we can build a complete Gig
        from a single detail URL without needing the listings page.
        """
        detail_elements = self.parse_gig_listings(detail_html, "booking bookdet")
        el = detail_elements[0] if detail_elements else None

        details = {
            "header": self._get_text(el, "h2", "type") if el else None,
            "organisation": self._get_text(el, "h3", "organisation") if el else None,
            "locality": self._get_text(el, "h4", "locality") if el else None,
            "date": self._get_text(el, "p", "date") if el else None,
            "time": self._get_text(el, "p", "time") if el else None,
            "fee": self._get_text(el, "p", "fee") if el else None,
            "link": link,
        }
        logger.debug(
            "Extracted basic details from detail page",
            extra={"link": link, "header": details.get("header")},
        )
        return details

    def extract_full_details(self, detail_html: str) -> dict:
        """Extract additional gig details from a detail page."""
        detail_elements = self.parse_gig_listings(detail_html, "bookingDetails")

        if not detail_elements:
            logger.warning("No bookingDetails element found in detail page")
            return {}

        detail_element = detail_elements[0]
        result = {
            "contact": self._get_sibling_text(detail_element, "Contact:"),
            "email": self._get_sibling_text(detail_element, "Email:"),
            "phone": self._get_sibling_text(detail_element, "Phone:"),
            "address": self._get_sibling_text(detail_element, "Address:"),
            "locality": self._get_sibling_text(detail_element, "Locality:"),
            "postcode": self._get_sibling_text(detail_element, "Postcode/Zip:"),
            "musical_requirements": self._get_sibling_text(detail_element, "Musical Requirements:"),
        }

        populated = [k for k, v in result.items() if v is not None]
        missing = [k for k, v in result.items() if v is None]
        logger.debug(
            "Extracted full details",
            extra={"populated": populated, "missing": missing},
        )
        return result

    def _extract_link(self, booking: Tag) -> str | None:
        """Safely extract the gig detail URL from a listing element."""
        anchor = booking.find("a", class_="noselect")
        if not anchor:
            return None
        try:
            return settings.base_url + anchor["href"]
        except (KeyError, TypeError):
            logger.warning(
                "Listing element has anchor with no href", extra={"booking": str(booking)[:120]}
            )
            return None

    @staticmethod
    def _get_text(element: Tag, tag: str, class_name: str) -> str | None:
        """Safely extract text from an element."""
        found = element.find(tag, class_=class_name)
        return found.text.strip() if found else None

    @staticmethod
    def _get_sibling_text(detail_element: Tag, label: str) -> str | None:
        """Extract text from the sibling paragraph of a labeled heading."""
        element = detail_element.find("h3", string=label)
        if element and element.find_next_sibling("p"):
            return element.find_next_sibling("p").get_text(strip=True)
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.close()
        logger.debug("Scraper session closed")
