"""organist_bot/travel.py
─────────────────────────
Travel time lookup via Google Maps Distance Matrix API.

get_travel_minutes(postcode)
    Returns drive time in minutes from settings.travel_home_postcode
    (falling back to settings.home_postcode) to the given gig postcode.
    Returns None if postcode is blank, API key is missing, or the API call fails.
"""

import logging

import googlemaps

from organist_bot.config import settings

logger = logging.getLogger(__name__)


def get_travel_minutes(postcode: str) -> int | None:
    """Return drive time in minutes from home to postcode.

    Uses settings.travel_home_postcode as origin; falls back to settings.home_postcode.
    Returns None if postcode is blank, API key is missing, or the API call fails.
    """
    if not postcode or not postcode.strip():
        return None
    api_key = settings.google_maps_api_key
    if not api_key:
        return None
    origin = settings.travel_home_postcode or settings.home_postcode
    if not origin:
        return None
    try:
        client = googlemaps.Client(key=api_key)
        result = client.distance_matrix(
            origins=[origin],
            destinations=[postcode],
            mode="driving",
            units="metric",
        )
        element = result["rows"][0]["elements"][0]
        if element["status"] != "OK":
            logger.debug(
                "travel: Distance Matrix non-OK status %s for postcode %r",
                element["status"],
                postcode,
            )
            return None
        minutes = element["duration"]["value"] // 60
        logger.debug("travel: %r → %r = %d min", origin, postcode, minutes)
        return minutes
    except Exception as exc:
        logger.warning("travel: get_travel_minutes failed for %r: %s", postcode, exc)
        return None
