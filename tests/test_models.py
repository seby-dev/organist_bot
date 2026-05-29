import datetime

from organist_bot.models import Gig


def _make(**kwargs) -> Gig:
    defaults = dict(
        header="Sunday Service",
        organisation="St. Mary's",
        locality="London",
        date="Sunday, 15 March 2026",
        time="9:30 AM",
        fee="£150",
        link="https://example.com/gig/1",
    )
    defaults.update(kwargs)
    return Gig(**defaults)


class TestGigParsedDate:
    def test_valid_date_sets_parsed_date(self):
        gig = _make(date="Sunday, 15 March 2026")
        assert gig.parsed_date == datetime.date(2026, 3, 15)

    def test_iso_date_sets_parsed_date(self):
        gig = _make(date="2026-03-15")
        assert gig.parsed_date == datetime.date(2026, 3, 15)

    def test_day_month_year_format(self):
        gig = _make(date="15 March 2026")
        assert gig.parsed_date == datetime.date(2026, 3, 15)

    def test_ordinal_date_sets_parsed_date(self):
        gig = _make(date="Sunday 15th March 2026")
        assert gig.parsed_date == datetime.date(2026, 3, 15)

    def test_unparseable_date_gives_none(self):
        gig = _make(date="next Sunday")
        assert gig.parsed_date is None

    def test_empty_date_gives_none(self):
        gig = _make(date="")
        assert gig.parsed_date is None

    def test_none_date_gives_none(self):
        gig = _make(date=None)
        assert gig.parsed_date is None

    def test_weekday_correct_for_monday(self):
        gig = _make(date="Monday, 16 March 2026")
        assert gig.parsed_date is not None
        assert gig.parsed_date.weekday() == 0  # Monday

    def test_weekday_correct_for_sunday(self):
        gig = _make(date="Sunday, 15 March 2026")
        assert gig.parsed_date is not None
        assert gig.parsed_date.weekday() == 6  # Sunday


class TestGigParsedTime:
    def test_valid_time_sets_parsed_time(self):
        gig = _make(time="9:30 AM")
        assert gig.parsed_time == datetime.time(9, 30)

    def test_pm_time_sets_parsed_time(self):
        gig = _make(time="2:00 PM")
        assert gig.parsed_time == datetime.time(14, 0)

    def test_lowercase_am_sets_parsed_time(self):
        gig = _make(time="9am")
        assert gig.parsed_time == datetime.time(9, 0)

    def test_bst_suffix_stripped(self):
        gig = _make(time="10:00 AM BST")
        assert gig.parsed_time == datetime.time(10, 0)

    def test_unparseable_time_gives_none(self):
        gig = _make(time="morning")
        assert gig.parsed_time is None

    def test_empty_time_gives_none(self):
        gig = _make(time="")
        assert gig.parsed_time is None

    def test_none_time_gives_none(self):
        gig = _make(time=None)
        assert gig.parsed_time is None


class TestGigEquality:
    def test_equal_gigs_are_equal(self):
        """parsed fields are compare=False, so equality is purely on raw fields."""
        g1 = _make(date="Sunday, 15 March 2026", time="9:30 AM")
        g2 = _make(date="Sunday, 15 March 2026", time="9:30 AM")
        assert g1 == g2

    def test_parsed_fields_excluded_from_repr(self):
        gig = _make()
        r = repr(gig)
        assert "parsed_date" not in r
        assert "parsed_time" not in r

    def test_parsed_fields_not_settable_via_init(self):
        """parsed_date and parsed_time are init=False — not accepted as constructor args."""
        import pytest

        with pytest.raises(TypeError):
            Gig(
                header="h",
                organisation="o",
                locality="l",
                date="Sunday, 15 March 2026",
                time="9:30 AM",
                fee="£100",
                link="https://example.com",
                parsed_date=datetime.date(2026, 3, 15),
            )
