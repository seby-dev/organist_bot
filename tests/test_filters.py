# -*- coding: utf-8 -*-
import datetime
import pytest
from organist_bot.models import Gig
from unittest.mock import MagicMock
from organist_bot.filters import (
    parse_min_fee,
    parse_start_time,
    parse_weekday,
    normalize_to_yyyymmdd,
    FeeFilter,
    SundayTimeFilter,
    BlacklistFilter,
    BookedDateFilter,
    PostcodeFilter,
    SeenFilter,
    GigFilterChain,
)


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def make_gig(**kwargs) -> Gig:
    """Build a Gig with sensible defaults; override with kwargs."""
    defaults = dict(
        header="Sunday Service",
        organisation="St. Mary's Church",
        locality="London",
        date="Sunday, 15 March 2026",
        time="9:30 AM",
        fee="£150",
        link="https://example.com/gig/1",
        email=None,
    )
    defaults.update(kwargs)
    return Gig(**defaults)


# ─────────────────────────────────────────────────────────
# parse_min_fee
# ─────────────────────────────────────────────────────────

class TestParseMinFee:

    def test_range_returns_lower_bound(self):
        assert parse_min_fee("£80 - £120") == 80.0

    def test_plus_suffix(self):
        assert parse_min_fee("£100+") == 100.0

    def test_from_prefix(self):
        assert parse_min_fee("From £90") == 90.0

    def test_single_amount(self):
        assert parse_min_fee("£120") == 120.0

    def test_no_pound_sign(self):
        assert parse_min_fee("150") == 150.0

    def test_decimal_amount(self):
        assert parse_min_fee("£99.99") == 99.99

    def test_negotiable_returns_none(self):
        assert parse_min_fee("Negotiable") is None

    def test_neg_abbreviation_returns_none(self):
        assert parse_min_fee("neg") is None

    def test_expenses_returns_none(self):
        assert parse_min_fee("Expenses only") is None

    def test_empty_string_returns_none(self):
        assert parse_min_fee("") is None

    def test_none_input_returns_none(self):
        assert parse_min_fee(None) is None

    def test_no_numeric_content_returns_none(self):
        assert parse_min_fee("TBA") is None

    def test_whitespace_only_returns_none(self):
        assert parse_min_fee("   ") is None

    def test_multiple_amounts_returns_minimum(self):
        assert parse_min_fee("£50 - £100 - £200") == 50.0

    def test_case_insensitive_negotiable(self):
        assert parse_min_fee("NEGOTIABLE") is None

    def test_mixed_case_expenses(self):
        assert parse_min_fee("Expenses Only") is None


# ─────────────────────────────────────────────────────────
# parse_start_time
# ─────────────────────────────────────────────────────────

class TestParseStartTime:

    def test_am_with_colon(self):
        assert parse_start_time("9:30 AM") == datetime.time(9, 30)

    def test_am_no_colon(self):
        assert parse_start_time("9am") == datetime.time(9, 0)

    def test_pm_with_colon(self):
        assert parse_start_time("2:00 PM") == datetime.time(14, 0)

    def test_pm_no_colon(self):
        assert parse_start_time("3pm") == datetime.time(15, 0)

    def test_midnight_12am(self):
        assert parse_start_time("12:00 AM") == datetime.time(0, 0)

    def test_noon_12pm(self):
        assert parse_start_time("12:00 PM") == datetime.time(12, 0)

    def test_leading_zero_hour(self):
        assert parse_start_time("09:30 am") == datetime.time(9, 30)

    def test_strips_gmt_suffix(self):
        assert parse_start_time("10:00 AM GMT") == datetime.time(10, 0)

    def test_strips_bst_suffix(self):
        assert parse_start_time("10:00 AM BST") == datetime.time(10, 0)

    def test_empty_string_returns_none(self):
        assert parse_start_time("") is None

    def test_none_returns_none(self):
        assert parse_start_time(None) is None

    def test_unparseable_returns_none(self):
        assert parse_start_time("morning") is None

    def test_case_insensitive_am(self):
        assert parse_start_time("9:00 Am") == datetime.time(9, 0)

    def test_case_insensitive_pm(self):
        assert parse_start_time("2:00 Pm") == datetime.time(14, 0)


# ─────────────────────────────────────────────────────────
# parse_weekday
# ─────────────────────────────────────────────────────────

class TestParseWeekday:
    """Monday=0 ... Sunday=6"""

    def test_full_weekday_name_monday(self):
        assert parse_weekday("Monday, 2 March 2026") == 0

    def test_full_weekday_name_sunday(self):
        assert parse_weekday("Sunday, 1 March 2026") == 6

    def test_abbreviated_weekday(self):
        assert parse_weekday("Sun, 1 Mar 2026") == 6

    def test_weekday_name_in_string(self):
        assert parse_weekday("Saturday evening gig") == 5

    def test_all_weekdays_by_name(self):
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for i, day in enumerate(days):
            assert parse_weekday(day.capitalize()) == i

    def test_ordinal_suffix_stripped(self):
        assert parse_weekday("Sunday 1st March 2026") == 6

    def test_second_ordinal_suffix(self):
        assert parse_weekday("Monday 2nd March 2026") == 0

    def test_third_ordinal_suffix(self):
        assert parse_weekday("Tuesday 3rd March 2026") == 1

    def test_fourth_ordinal_suffix(self):
        assert parse_weekday("Wednesday 4th March 2026") == 2

    def test_empty_string_returns_none(self):
        assert parse_weekday("") is None

    def test_none_returns_none(self):
        assert parse_weekday(None) is None

    def test_date_without_weekday_returns_none(self):
        # A plain date with no weekday word cannot be parsed
        assert parse_weekday("15 March 2026") is None

    def test_case_insensitive_weekday_name(self):
        assert parse_weekday("SUNDAY") == 6


# ─────────────────────────────────────────────────────────
# normalize_to_yyyymmdd
# ─────────────────────────────────────────────────────────

class TestNormalizeToYYYYMMDD:

    def test_full_date_with_weekday(self):
        assert normalize_to_yyyymmdd("Sunday, 15 March 2026") == "20260315"

    def test_day_month_year(self):
        assert normalize_to_yyyymmdd("15 March 2026") == "20260315"

    def test_abbreviated_month(self):
        assert normalize_to_yyyymmdd("15 Mar 2026") == "20260315"

    def test_iso_format(self):
        assert normalize_to_yyyymmdd("2026-03-15") == "20260315"

    def test_month_day_year(self):
        assert normalize_to_yyyymmdd("March 15, 2026") == "20260315"

    def test_ordinal_suffix_stripped(self):
        assert normalize_to_yyyymmdd("15th March 2026") == "20260315"

    def test_abbreviated_weekday_format(self):
        assert normalize_to_yyyymmdd("Sun, 15 Mar 2026") == "20260315"

    def test_empty_string_returns_none(self):
        assert normalize_to_yyyymmdd("") is None

    def test_none_returns_none(self):
        assert normalize_to_yyyymmdd(None) is None

    def test_unparseable_returns_none(self):
        assert normalize_to_yyyymmdd("next Sunday") is None

    def test_partial_date_no_year_future(self):
        today = datetime.date.today()
        future = today + datetime.timedelta(days=30)
        date_str = future.strftime("%d %B")
        result = normalize_to_yyyymmdd(date_str)
        assert result == future.strftime("%Y%m%d")

    def test_partial_date_no_year_past_rolls_to_next_year(self):
        today = datetime.date.today()
        past = today - datetime.timedelta(days=5)
        date_str = past.strftime("%d %B")
        result = normalize_to_yyyymmdd(date_str)
        expected_year = today.year + 1
        assert result == f"{expected_year}{past.strftime('%m%d')}"


# ─────────────────────────────────────────────────────────
# FeeFilter
# ─────────────────────────────────────────────────────────

class TestFeeFilter:

    # --- weekend gigs ---

    def test_weekend_gig_above_threshold_passes(self):
        f = FeeFilter(min_fee=100)
        gig = make_gig(date="Sunday, 15 March 2026", fee="£150")
        assert f(gig) is True

    def test_weekend_gig_at_threshold_passes(self):
        f = FeeFilter(min_fee=100)
        gig = make_gig(date="Sunday, 15 March 2026", fee="£100")
        assert f(gig) is True

    def test_weekend_gig_below_threshold_fails(self):
        f = FeeFilter(min_fee=100)
        gig = make_gig(date="Sunday, 15 March 2026", fee="£50")
        assert f(gig) is False

    def test_saturday_uses_weekend_threshold(self):
        f = FeeFilter(min_fee=50, weekday_min_fee=120)
        gig = make_gig(date="Saturday, 14 March 2026", fee="£60")
        assert f(gig) is True

    # --- weekday gigs ---

    def test_weekday_gig_uses_weekday_threshold(self):
        f = FeeFilter(min_fee=50, weekday_min_fee=120)
        gig = make_gig(date="Monday, 16 March 2026", fee="£100")
        assert f(gig) is False

    def test_weekday_gig_above_weekday_threshold_passes(self):
        f = FeeFilter(min_fee=50, weekday_min_fee=120)
        gig = make_gig(date="Friday, 13 March 2026", fee="£130")
        assert f(gig) is True

    def test_weekday_gig_at_weekday_threshold_passes(self):
        f = FeeFilter(min_fee=50, weekday_min_fee=120)
        gig = make_gig(date="Wednesday, 11 March 2026", fee="£120")
        assert f(gig) is True

    # --- negotiable / unknown fees ---

    def test_negotiable_fee_fails(self):
        f = FeeFilter(min_fee=50)
        gig = make_gig(fee="Negotiable")
        assert f(gig) is False

    def test_empty_fee_fails(self):
        f = FeeFilter(min_fee=50)
        gig = make_gig(fee="")
        assert f(gig) is False

    def test_none_fee_fails(self):
        f = FeeFilter(min_fee=50)
        gig = make_gig(fee=None)
        assert f(gig) is False

    # --- unknown day defaults to weekend threshold ---

    def test_unknown_date_uses_weekend_threshold(self):
        f = FeeFilter(min_fee=50, weekday_min_fee=120)
        gig = make_gig(date="", fee="£60")
        assert f(gig) is True  # 60 >= 50 (weekend threshold)

    def test_unknown_date_fee_below_weekend_threshold_fails(self):
        f = FeeFilter(min_fee=100, weekday_min_fee=120)
        gig = make_gig(date="", fee="£60")
        assert f(gig) is False

    # --- fee ranges ---

    def test_fee_range_uses_minimum(self):
        f = FeeFilter(min_fee=80)
        gig = make_gig(date="Sunday, 15 March 2026", fee="£80 - £120")
        assert f(gig) is True

    def test_fee_range_minimum_below_threshold_fails(self):
        f = FeeFilter(min_fee=100)
        gig = make_gig(date="Sunday, 15 March 2026", fee="£80 - £120")
        assert f(gig) is False

    # --- defaults ---

    def test_default_weekday_min_fee_is_120(self):
        f = FeeFilter(min_fee=50)
        assert f.weekday_min_fee == 120

    def test_repr(self):
        f = FeeFilter(min_fee=50, weekday_min_fee=120)
        assert "FeeFilter" in repr(f)
        assert "50" in repr(f)
        assert "120" in repr(f)


# ─────────────────────────────────────────────────────────
# SundayTimeFilter
# ─────────────────────────────────────────────────────────

class TestSundayTimeFilter:

    # --- non-Sunday gigs always pass ---

    def test_monday_gig_always_passes(self):
        f = SundayTimeFilter()
        gig = make_gig(date="Monday, 16 March 2026", time="6:00 AM")
        assert f(gig) is True

    def test_saturday_gig_always_passes(self):
        f = SundayTimeFilter()
        gig = make_gig(date="Saturday, 14 March 2026", time="6:00 AM")
        assert f(gig) is True

    def test_weekday_with_no_time_passes(self):
        f = SundayTimeFilter()
        gig = make_gig(date="Friday, 13 March 2026", time="")
        assert f(gig) is True

    # --- Sunday gigs within window ---

    def test_sunday_within_window_passes(self):
        f = SundayTimeFilter(earliest=datetime.time(9, 0), latest=datetime.time(10, 0))
        gig = make_gig(date="Sunday, 15 March 2026", time="9:30 AM")
        assert f(gig) is True

    def test_sunday_at_earliest_passes(self):
        f = SundayTimeFilter(earliest=datetime.time(9, 0), latest=datetime.time(10, 0))
        gig = make_gig(date="Sunday, 15 March 2026", time="9:00 AM")
        assert f(gig) is True

    def test_sunday_at_latest_passes(self):
        f = SundayTimeFilter(earliest=datetime.time(9, 0), latest=datetime.time(10, 0))
        gig = make_gig(date="Sunday, 15 March 2026", time="10:00 AM")
        assert f(gig) is True

    # --- Sunday gigs outside window ---

    def test_sunday_too_early_fails(self):
        f = SundayTimeFilter(earliest=datetime.time(9, 0), latest=datetime.time(10, 0))
        gig = make_gig(date="Sunday, 15 March 2026", time="8:00 AM")
        assert f(gig) is False

    def test_sunday_too_late_fails(self):
        f = SundayTimeFilter(earliest=datetime.time(9, 0), latest=datetime.time(10, 0))
        gig = make_gig(date="Sunday, 15 March 2026", time="11:00 AM")
        assert f(gig) is False

    def test_sunday_evening_fails(self):
        f = SundayTimeFilter(earliest=datetime.time(9, 0), latest=datetime.time(10, 0))
        gig = make_gig(date="Sunday, 15 March 2026", time="6:00 PM")
        assert f(gig) is False

    # --- Sunday with missing/unparseable time ---

    def test_sunday_empty_time_fails(self):
        f = SundayTimeFilter()
        gig = make_gig(date="Sunday, 15 March 2026", time="")
        assert f(gig) is False

    def test_sunday_none_time_fails(self):
        f = SundayTimeFilter()
        gig = make_gig(date="Sunday, 15 March 2026", time=None)
        assert f(gig) is False

    def test_sunday_unparseable_time_fails(self):
        f = SundayTimeFilter()
        gig = make_gig(date="Sunday, 15 March 2026", time="morning")
        assert f(gig) is False

    # --- defaults and repr ---

    def test_default_window_is_9am_to_10am(self):
        f = SundayTimeFilter()
        assert f.earliest == datetime.time(9, 0)
        assert f.latest == datetime.time(10, 0)

    def test_repr(self):
        f = SundayTimeFilter()
        assert "SundayTimeFilter" in repr(f)

    # --- unknown day ---

    def test_unknown_day_passes(self):
        """Can't determine weekday -> not Sunday -> pass."""
        f = SundayTimeFilter()
        gig = make_gig(date="", time="6:00 AM")
        assert f(gig) is True


# ─────────────────────────────────────────────────────────
# BlacklistFilter
# ─────────────────────────────────────────────────────────

class TestBlacklistFilter:

    def test_non_blacklisted_email_passes(self):
        f = BlacklistFilter(["bad@example.com"])
        gig = make_gig(email="good@example.com")
        assert f(gig) is True

    def test_blacklisted_email_fails(self):
        f = BlacklistFilter(["bad@example.com"])
        gig = make_gig(email="bad@example.com")
        assert f(gig) is False

    def test_case_insensitive_blacklist(self):
        f = BlacklistFilter(["Bad@Example.COM"])
        gig = make_gig(email="bad@example.com")
        assert f(gig) is False

    def test_case_insensitive_gig_email(self):
        f = BlacklistFilter(["bad@example.com"])
        gig = make_gig(email="BAD@EXAMPLE.COM")
        assert f(gig) is False

    def test_no_email_passes(self):
        f = BlacklistFilter(["bad@example.com"])
        gig = make_gig(email=None)
        assert f(gig) is True

    def test_empty_email_passes(self):
        f = BlacklistFilter(["bad@example.com"])
        gig = make_gig(email="")
        assert f(gig) is True

    def test_empty_blacklist_passes_all(self):
        f = BlacklistFilter([])
        gig = make_gig(email="anyone@example.com")
        assert f(gig) is True

    def test_multiple_blacklisted_emails(self):
        f = BlacklistFilter(["a@x.com", "b@x.com"])
        assert f(make_gig(email="a@x.com")) is False
        assert f(make_gig(email="b@x.com")) is False
        assert f(make_gig(email="c@x.com")) is True

    def test_whitespace_trimmed_in_blacklist(self):
        f = BlacklistFilter(["  bad@example.com  "])
        gig = make_gig(email="bad@example.com")
        assert f(gig) is False

    def test_repr(self):
        f = BlacklistFilter(["a@x.com", "b@x.com"])
        assert "BlacklistFilter" in repr(f)
        assert "2" in repr(f)


# ─────────────────────────────────────────────────────────
# BookedDateFilter
# ─────────────────────────────────────────────────────────

class TestBookedDateFilter:

    def test_non_booked_date_passes(self):
        f = BookedDateFilter(["20260315"])
        gig = make_gig(date="Sunday, 22 March 2026")
        assert f(gig) is True

    def test_booked_date_fails(self):
        f = BookedDateFilter(["20260315"])
        gig = make_gig(date="Sunday, 15 March 2026")
        assert f(gig) is False

    def test_multiple_booked_dates(self):
        f = BookedDateFilter(["20260315", "20260308"])
        assert f(make_gig(date="Sunday, 15 March 2026")) is False
        assert f(make_gig(date="Sunday, 8 March 2026")) is False
        assert f(make_gig(date="Sunday, 22 March 2026")) is True

    def test_unparseable_date_passes(self):
        """When the date can't be normalised we can't confirm a conflict -> allow through."""
        f = BookedDateFilter(["20260315"])
        gig = make_gig(date="next Sunday")
        assert f(gig) is True

    def test_empty_date_passes(self):
        f = BookedDateFilter(["20260315"])
        gig = make_gig(date="")
        assert f(gig) is True

    def test_none_date_passes(self):
        f = BookedDateFilter(["20260315"])
        gig = make_gig(date=None)
        assert f(gig) is True

    def test_empty_booked_dates_passes_all(self):
        f = BookedDateFilter([])
        gig = make_gig(date="Sunday, 15 March 2026")
        assert f(gig) is True

    def test_iso_date_format(self):
        f = BookedDateFilter(["20260315"])
        gig = make_gig(date="2026-03-15")
        assert f(gig) is False

    def test_repr(self):
        f = BookedDateFilter(["20260315", "20260308"])
        assert "BookedDateFilter" in repr(f)
        assert "2" in repr(f)


# ─────────────────────────────────────────────────────────
# SeenFilter
# ─────────────────────────────────────────────────────────

class TestSeenFilter:

    def test_unseen_gig_passes(self):
        """A gig whose link is not in the seen set is allowed through."""
        f = SeenFilter({"https://example.com/other"})
        assert f(make_gig()) is True

    def test_seen_gig_fails(self):
        """A gig whose link is in the seen set is rejected."""
        f = SeenFilter({"https://example.com/gig/1"})   # matches make_gig() default
        assert f(make_gig()) is False

    def test_empty_seen_set_passes_all(self):
        """An empty seen set never rejects anything."""
        f = SeenFilter(set())
        assert f(make_gig()) is True

    def test_no_link_gig_passes(self):
        """A gig with no link can never be in the seen set, so it passes."""
        f = SeenFilter({"https://example.com/gig/1"})
        assert f(make_gig(link=None)) is True

    def test_multiple_seen_links(self):
        """All links in the seen set are rejected; unlisted links pass."""
        f = SeenFilter({"https://example.com/gig/1", "https://example.com/gig/2"})
        assert f(make_gig(link="https://example.com/gig/1")) is False
        assert f(make_gig(link="https://example.com/gig/2")) is False
        assert f(make_gig(link="https://example.com/gig/3")) is True


# ─────────────────────────────────────────────────────────
# PostcodeFilter
# ─────────────────────────────────────────────────────────

def _mock_client(times: dict[str, int | None]) -> MagicMock:
    """
    Build a mock googlemaps Client whose distance_matrix() returns travel
    times driven by the supplied dict: {mode: minutes_or_None}.

    Passing None for a mode makes the mock return a ZERO_RESULTS status
    for that mode, simulating an unavailable route.
    """
    mock = MagicMock()

    def distance_matrix(origins, destinations, mode, units="metric"):
        minutes = times.get(mode)
        if minutes is None:
            return {"rows": [{"elements": [{"status": "ZERO_RESULTS"}]}]}
        return {
            "rows": [{
                "elements": [{
                    "status":   "OK",
                    "duration": {"value": minutes * 60, "text": f"{minutes} mins"},
                    "distance": {"value": 0, "text": ""},
                }]
            }]
        }

    mock.distance_matrix.side_effect = distance_matrix
    return mock


class TestPostcodeFilter:

    HOME = "SW1A 1AA"

    def _filter(self, times: dict[str, int | None], max_minutes: int = 45) -> PostcodeFilter:
        """Convenience: build a PostcodeFilter with a mocked client."""
        return PostcodeFilter(
            home_postcode=self.HOME,
            api_key="fake-key",
            max_minutes=max_minutes,
            _client=_mock_client(times),
        )

    # ── pass / reject basics ──────────────────────────────────────────────────

    def test_passes_when_transit_within_limit(self):
        """Gig passes if transit time is within the limit."""
        f = self._filter({"transit": 30, "bicycling": 60, "walking": 90})
        assert f(make_gig(postcode="EC1A 1BB")) is True

    def test_passes_when_cycling_within_limit(self):
        """Gig passes if cycling time is within the limit."""
        f = self._filter({"transit": 60, "bicycling": 30, "walking": 90})
        assert f(make_gig(postcode="EC1A 1BB")) is True

    def test_passes_when_walking_within_limit(self):
        """Gig passes if walking time is within the limit."""
        f = self._filter({"transit": 60, "bicycling": 60, "walking": 30})
        assert f(make_gig(postcode="EC1A 1BB")) is True

    def test_rejects_when_all_modes_over_limit(self):
        """Gig is rejected only when every mode exceeds the limit."""
        f = self._filter({"transit": 60, "bicycling": 90, "walking": 120})
        assert f(make_gig(postcode="EC1A 1BB")) is False

    def test_passes_at_exact_limit(self):
        """A travel time exactly equal to max_minutes is accepted (inclusive)."""
        f = self._filter({"transit": 45, "bicycling": 90, "walking": 90})
        assert f(make_gig(postcode="EC1A 1BB")) is True

    def test_rejects_one_minute_over_limit(self):
        """A travel time of max_minutes + 1 is rejected when all modes match."""
        f = self._filter({"transit": 46, "bicycling": 46, "walking": 46})
        assert f(make_gig(postcode="EC1A 1BB")) is False

    def test_custom_max_minutes_respected(self):
        """max_minutes constructor argument overrides the default of 45."""
        f = self._filter({"transit": 35, "bicycling": 35, "walking": 35}, max_minutes=30)
        assert f(make_gig(postcode="EC1A 1BB")) is False

    # ── fail-open behaviour ───────────────────────────────────────────────────

    def test_no_postcode_passes_through(self):
        """Gig with no postcode is passed through without any API call."""
        client = _mock_client({})
        f = PostcodeFilter(home_postcode=self.HOME, api_key="key", _client=client)
        assert f(make_gig(postcode=None)) is True
        client.distance_matrix.assert_not_called()

    def test_api_exception_fails_open(self):
        """If the API raises an exception the gig is passed through."""
        client = MagicMock()
        client.distance_matrix.side_effect = Exception("network error")
        f = PostcodeFilter(home_postcode=self.HOME, api_key="key", _client=client)
        assert f(make_gig(postcode="EC1A 1BB")) is True

    def test_non_ok_status_treated_as_none(self):
        """A non-OK API status for a mode is treated as no route (None)."""
        # All three modes return ZERO_RESULTS → all None → fail open → pass
        f = self._filter({"transit": None, "bicycling": None, "walking": None})
        assert f(make_gig(postcode="EC1A 1BB")) is True

    def test_partial_none_modes_still_evaluated(self):
        """If some modes return None, the remaining ones are still checked."""
        # transit=None, bicycling=None, walking=30 → walking passes
        f = self._filter({"transit": None, "bicycling": None, "walking": 30})
        assert f(make_gig(postcode="EC1A 1BB")) is True

    def test_all_none_modes_passes_through(self):
        """When every mode returns None the gig is passed through (fail open)."""
        f = self._filter({"transit": None, "bicycling": None, "walking": None})
        assert f(make_gig(postcode="EC1A 1BB")) is True

    # ── caching ───────────────────────────────────────────────────────────────

    def test_same_postcode_queried_once_per_mode(self):
        """The API is called exactly once per mode for a repeated postcode."""
        client = _mock_client({"transit": 20, "bicycling": 20, "walking": 20})
        f = PostcodeFilter(home_postcode=self.HOME, api_key="key", _client=client)

        f(make_gig(postcode="EC1A 1BB"))
        f(make_gig(postcode="EC1A 1BB"))   # second call — should hit cache

        assert client.distance_matrix.call_count == 3  # once per mode, not six

    def test_different_postcodes_queried_separately(self):
        """Two different destination postcodes each trigger their own API calls."""
        client = _mock_client({"transit": 20, "bicycling": 20, "walking": 20})
        f = PostcodeFilter(home_postcode=self.HOME, api_key="key", _client=client)

        f(make_gig(postcode="EC1A 1BB"))
        f(make_gig(postcode="W1A 1AA"))

        assert client.distance_matrix.call_count == 6  # 3 modes × 2 postcodes

    # ── duration conversion ───────────────────────────────────────────────────

    def test_seconds_converted_to_whole_minutes(self):
        """API duration (seconds) is correctly floored to whole minutes."""
        # 2700 seconds = 45 minutes exactly → should pass at default limit
        client = MagicMock()
        client.distance_matrix.return_value = {
            "rows": [{"elements": [{"status": "OK", "duration": {"value": 2700}}]}]
        }
        f = PostcodeFilter(home_postcode=self.HOME, api_key="key", _client=client)
        assert f(make_gig(postcode="EC1A 1BB")) is True

    def test_fractional_seconds_floored(self):
        """2759 seconds = 45.98 min → floors to 45 → passes at limit of 45."""
        client = MagicMock()
        client.distance_matrix.return_value = {
            "rows": [{"elements": [{"status": "OK", "duration": {"value": 2759}}]}]
        }
        f = PostcodeFilter(home_postcode=self.HOME, api_key="key", _client=client)
        assert f(make_gig(postcode="EC1A 1BB")) is True

    # ── sunday-only behaviour ─────────────────────────────────────────────────

    def test_non_sunday_passes_without_api_call(self):
        """Non-Sunday gigs bypass the distance check entirely."""
        client = _mock_client({"transit": 999, "bicycling": 999, "walking": 999})
        f = PostcodeFilter(home_postcode=self.HOME, api_key="key", _client=client)
        assert f(make_gig(date="Saturday, 14 March 2026", postcode="EC1A 1BB")) is True
        client.distance_matrix.assert_not_called()

    def test_weekday_passes_without_api_call(self):
        """Weekday gigs bypass the distance check entirely."""
        client = _mock_client({"transit": 999, "bicycling": 999, "walking": 999})
        f = PostcodeFilter(home_postcode=self.HOME, api_key="key", _client=client)
        assert f(make_gig(date="Monday, 16 March 2026", postcode="EC1A 1BB")) is True
        client.distance_matrix.assert_not_called()

    def test_unknown_day_passes_without_api_call(self):
        """When the day can't be determined, fail open without an API call."""
        client = _mock_client({"transit": 999, "bicycling": 999, "walking": 999})
        f = PostcodeFilter(home_postcode=self.HOME, api_key="key", _client=client)
        assert f(make_gig(date="", postcode="EC1A 1BB")) is True
        client.distance_matrix.assert_not_called()

    def test_sunday_too_far_fails(self):
        """Sunday gigs still go through the distance check and can be rejected."""
        f = self._filter({"transit": 90, "bicycling": 90, "walking": 90})
        assert f(make_gig(date="Sunday, 15 March 2026", postcode="EC1A 1BB")) is False

    # ── repr ──────────────────────────────────────────────────────────────────

    def test_repr(self):
        f = self._filter({"transit": 30, "bicycling": 30, "walking": 30})
        assert repr(f) == f"PostcodeFilter(home={self.HOME!r}, max_minutes=45)"


# ─────────────────────────────────────────────────────────
# GigFilterChain
# ─────────────────────────────────────────────────────────

class TestGigFilterChain:

    # --- basic mechanics ---

    def test_empty_chain_passes_all(self):
        chain = GigFilterChain()
        gigs = [make_gig(), make_gig(fee="£10")]
        assert chain.apply(gigs) == gigs

    def test_add_returns_self_for_chaining(self):
        chain = GigFilterChain()
        result = chain.add(lambda g: True)
        assert result is chain

    def test_single_filter_applied(self):
        chain = GigFilterChain().add(FeeFilter(min_fee=100))
        passing = make_gig(date="Sunday, 15 March 2026", fee="£150")
        failing = make_gig(date="Sunday, 15 March 2026", fee="£50")
        assert chain.apply([passing, failing]) == [passing]

    def test_all_filters_must_pass(self):
        chain = (
            GigFilterChain()
            .add(FeeFilter(min_fee=100))
            .add(BlacklistFilter(["bad@example.com"]))
        )
        gig = make_gig(date="Sunday, 15 March 2026", fee="£150", email="bad@example.com")
        assert chain.apply([gig]) == []

    def test_is_valid_true(self):
        chain = GigFilterChain().add(lambda g: True)
        assert chain.is_valid(make_gig()) is True

    def test_is_valid_false(self):
        chain = GigFilterChain().add(lambda g: False)
        assert chain.is_valid(make_gig()) is False

    def test_apply_empty_list(self):
        chain = GigFilterChain().add(FeeFilter(min_fee=50))
        assert chain.apply([]) == []

    def test_apply_all_pass(self):
        chain = GigFilterChain().add(FeeFilter(min_fee=50))
        gigs = [
            make_gig(date="Sunday, 15 March 2026", fee="£100"),
            make_gig(date="Sunday, 22 March 2026", fee="£200"),
        ]
        assert chain.apply(gigs) == gigs

    def test_apply_all_fail(self):
        chain = GigFilterChain().add(FeeFilter(min_fee=500))
        gigs = [
            make_gig(date="Sunday, 15 March 2026", fee="£50"),
            make_gig(date="Sunday, 22 March 2026", fee="£80"),
        ]
        assert chain.apply(gigs) == []

    # --- full integration ---

    def test_full_chain_integration(self):
        chain = (
            GigFilterChain()
            .add(FeeFilter(min_fee=50, weekday_min_fee=120))
            .add(SundayTimeFilter(earliest=datetime.time(9, 0), latest=datetime.time(10, 0)))
            .add(BlacklistFilter(["blocked@example.com"]))
            .add(BookedDateFilter(["20260308"]))
        )

        good = make_gig(
            date="Sunday, 15 March 2026",
            time="9:30 AM",
            fee="£150",
            email="good@example.com",
        )
        low_fee = make_gig(
            date="Sunday, 15 March 2026",
            time="9:30 AM",
            fee="£30",
            email="good@example.com",
        )
        wrong_time = make_gig(
            date="Sunday, 15 March 2026",
            time="11:00 AM",
            fee="£150",
            email="good@example.com",
        )
        blacklisted = make_gig(
            date="Sunday, 15 March 2026",
            time="9:30 AM",
            fee="£150",
            email="blocked@example.com",
        )
        booked = make_gig(
            date="Sunday, 8 March 2026",
            time="9:30 AM",
            fee="£150",
            email="good@example.com",
        )

        result = chain.apply([good, low_fee, wrong_time, blacklisted, booked])
        assert result == [good]

    def test_repr_contains_filter_names(self):
        chain = (
            GigFilterChain()
            .add(FeeFilter(min_fee=50))
            .add(BlacklistFilter([]))
        )
        r = repr(chain)
        assert "GigFilterChain" in r
        assert "FeeFilter" in r
        assert "BlacklistFilter" in r

    # --- short-circuit behaviour ---

    def test_short_circuit_stops_at_first_failure(self):
        """Filters after the first failure should not be called."""
        calls = []

        def filter_a(gig):
            calls.append("a")
            return False  # always fail

        def filter_b(gig):
            calls.append("b")
            return True

        chain = GigFilterChain().add(filter_a).add(filter_b)
        chain.is_valid(make_gig())

        assert "a" in calls
        assert "b" not in calls
