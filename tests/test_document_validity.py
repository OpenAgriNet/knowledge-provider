"""Unit tests for the period a document's chunks are searchable in.

The clock is injected everywhere it matters, so these tests pin a day rather
than expressing expectations relative to whatever day they run on.
"""

from datetime import datetime

import pytest

from pipeline.document_validity import (
    DocumentValidityError,
    ValidityPeriod,
    day_of,
    default_period,
    parse_day,
    parse_period,
    period_from_row,
    period_from_upload_date,
    period_from_upload_timestamp,
    system_clock,
    today,
)


def clock_at(stamp: str):
    """A clock frozen at `stamp` (`YYYY-MM-DD`)."""
    return lambda: datetime.strptime(stamp, "%Y-%m-%d")


FROZEN = clock_at("2026-09-22")


class TestToday:
    @pytest.mark.unit
    def test_reads_the_injected_clock(self):
        assert today(FROZEN) == "2026-09-22"

    @pytest.mark.unit
    def test_falls_back_to_the_system_clock(self):
        assert today() == system_clock().date().isoformat()


class TestDefaultPeriod:
    @pytest.mark.unit
    def test_starts_today_and_never_ends(self):
        # What an uploader's document gets before anyone edits anything. The
        # business asks that content stay answerable until someone decides
        # otherwise, rather than expiring on a date nobody chose.
        assert default_period(FROZEN) == ValidityPeriod(
            start_date="2026-09-22", end_date=None
        )

    @pytest.mark.unit
    def test_does_not_expire(self):
        assert default_period(FROZEN).expires is False

    @pytest.mark.unit
    def test_is_active_on_its_own_first_day(self):
        # The whole point of defaulting the start to the upload day: a document
        # uploaded this morning has to be searchable this afternoon.
        assert default_period(FROZEN).is_active_on("2026-09-22")

    @pytest.mark.unit
    @pytest.mark.parametrize("day", ["2027-09-23", "2099-01-01", "9999-12-31"])
    def test_is_still_active_however_far_out_you_look(self, day):
        assert default_period(FROZEN).is_active_on(day)

    @pytest.mark.unit
    def test_is_not_active_the_day_before_it_starts(self):
        # An open end does not mean an open start.
        assert not default_period(FROZEN).is_active_on("2026-09-21")


class TestPeriodFromUploadDate:
    @pytest.mark.unit
    def test_anchors_on_the_upload_day_not_today(self):
        # A document uploaded two years ago must not be dated as though it
        # arrived this morning just because it is being reingested today.
        assert period_from_upload_date("2024-05-01") == ValidityPeriod(
            start_date="2024-05-01", end_date=None
        )

    @pytest.mark.unit
    def test_rejects_a_malformed_upload_date(self):
        with pytest.raises(DocumentValidityError, match="upload date"):
            period_from_upload_date("")


class TestParsePeriod:
    @pytest.mark.unit
    def test_defaults_to_today_with_no_end(self):
        assert parse_period(clock=FROZEN) == ValidityPeriod(
            start_date="2026-09-22", end_date=None
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_a_blank_end_means_never_expires(self, blank):
        # Blank is a real answer here, not a missing one: a reviewer who
        # clears the end date is asking for a document that never expires.
        assert parse_period("2026-01-15", blank, clock=FROZEN).end_date is None

    @pytest.mark.unit
    def test_an_explicit_end_is_still_honoured(self):
        assert parse_period("2026-01-15", "2026-06-30", clock=FROZEN).end_date == "2026-06-30"

    @pytest.mark.unit
    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_defaults_a_blank_start_to_today(self, blank):
        assert parse_period(blank, "2030-01-01", clock=FROZEN).start_date == "2026-09-22"

    @pytest.mark.unit
    def test_keeps_an_approver_edited_period(self):
        assert parse_period("2026-10-01", "2026-12-31", clock=FROZEN) == ValidityPeriod(
            start_date="2026-10-01", end_date="2026-12-31"
        )

    @pytest.mark.unit
    def test_a_single_day_period_is_legal(self):
        assert parse_period("2026-09-22", "2026-09-22", clock=FROZEN).end_date == "2026-09-22"

    @pytest.mark.unit
    def test_a_backdated_start_is_legal(self):
        # Digitising an order issued last year is a real case; the period
        # describes the content, not when someone got around to uploading it.
        assert parse_period("2025-01-01", "2026-12-31", clock=FROZEN).start_date == "2025-01-01"

    @pytest.mark.unit
    def test_trims_surrounding_whitespace(self):
        period = parse_period("  2026-10-01 ", " 2026-12-31  ", clock=FROZEN)

        assert period == ValidityPeriod(start_date="2026-10-01", end_date="2026-12-31")

    @pytest.mark.unit
    def test_rejects_an_end_before_the_start(self):
        with pytest.raises(DocumentValidityError, match="cannot be before"):
            parse_period("2026-12-31", "2026-01-01", clock=FROZEN)

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", ["22-09-2026", "2026/09/22", "next year", "2026-13-01"])
    def test_rejects_a_malformed_date(self, bad):
        with pytest.raises(DocumentValidityError, match="YYYY-MM-DD"):
            parse_period(bad, "2027-01-01", clock=FROZEN)

    @pytest.mark.unit
    def test_names_the_end_it_rejected(self):
        with pytest.raises(DocumentValidityError, match="end date"):
            parse_period("2026-01-01", "not-a-date", clock=FROZEN)


class TestPeriodFromRow:
    @pytest.mark.unit
    def test_rebuilds_a_stored_period(self):
        assert period_from_row("2026-01-01", "2026-12-31") == ValidityPeriod(
            start_date="2026-01-01", end_date="2026-12-31"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "start,end",
        [(None, None), (None, "2026-12-31"), ("", ""), ("  ", "2026-12-31")],
    )
    def test_returns_none_when_the_start_is_missing(self, start, end):
        # A document uploaded before validity existed. None means "no stated
        # period", which search reads as always current. The start is what
        # decides whether a period exists at all.
        assert period_from_row(start, end) is None

    @pytest.mark.unit
    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_a_start_with_no_end_is_an_open_ended_period(self, blank):
        # The normal shape: this is exactly what an upload stores. It must
        # rebuild as a real period, not as "no period" — the document is
        # searchable from its start day and never expires.
        assert period_from_row("2026-01-01", blank) == ValidityPeriod(
            start_date="2026-01-01", end_date=None
        )

    @pytest.mark.unit
    def test_returns_none_for_a_row_this_module_never_wrote(self):
        assert period_from_row("01/01/2026", "31/12/2026") is None

    @pytest.mark.unit
    def test_returns_none_for_an_inverted_stored_period(self):
        assert period_from_row("2026-12-31", "2026-01-01") is None


class TestDayOf:
    """Reading the calendar day out of a stored timestamp."""

    @pytest.mark.unit
    def test_reads_what_the_db_layer_writes(self):
        # db.py stores datetime.utcnow().isoformat().
        assert day_of("2026-09-23T09:30:00.123456") == "2026-09-23"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stamp",
        [
            "2026-09-23T09:30:00",
            "2026-09-23 09:30:00",
            "2026-09-23T09:30:00+05:30",
            "2026-09-23",
        ],
    )
    def test_reads_the_other_shapes_a_row_can_carry(self, stamp):
        assert day_of(stamp) == "2026-09-23"

    @pytest.mark.unit
    def test_reads_a_trailing_z(self):
        # Python 3.10's fromisoformat rejects "Z"; a row can still hold one.
        assert day_of("2026-09-23T09:30:00Z") == "2026-09-23"

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", [None, "", "   ", "garbage", "2026-13-45", "23/09/2026"])
    def test_unreadable_timestamps_report_none(self, bad):
        # The point of parsing rather than slicing: an unexpected shape comes
        # back as "I cannot tell" instead of its first ten characters.
        assert day_of(bad) is None

    @pytest.mark.unit
    def test_zero_pads_a_year_below_1000(self):
        # Must round-trip back through this module's own parser; see
        # TestAddYears.test_zero_pads_a_year_below_1000 for why it can't.
        assert day_of("0001-01-01T00:00:00") == "0001-01-01"

    @pytest.mark.unit
    def test_does_not_slice_a_misleading_prefix(self):
        # "23/09/2026" sliced to ten characters looks like a date and is not.
        assert day_of("23/09/2026 09:30:00") is None


class TestPeriodFromUploadTimestamp:
    @pytest.mark.unit
    def test_anchors_on_the_day_the_timestamp_falls_on(self):
        assert period_from_upload_timestamp("2024-05-01T09:30:00.123456") == ValidityPeriod(
            start_date="2024-05-01", end_date=None
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", [None, "", "garbage"])
    def test_falls_back_to_todays_default_when_unreadable(self, bad):
        # On the ingest path: refusing to ingest over an unparseable audit
        # column would be a worse answer than a period starting now.
        assert period_from_upload_timestamp(bad, clock=FROZEN) == default_period(FROZEN)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            "garbage",
            "2026-13-45",
            "0001-01-01T00:00:00",  # below the year strftime zero-pads
            "9999-12-31T23:59:59",  # cannot move a year forward
        ],
    )
    def test_never_raises(self, value):
        # This runs on the ingest path: whatever an audit column holds, it must
        # come back as a period rather than stopping the document.
        assert period_from_upload_timestamp(value, clock=FROZEN) is not None

    @pytest.mark.unit
    def test_a_timestamp_at_the_end_of_the_range_is_anchored_not_rejected(self):
        # Used to fall back to today: deriving an end a year out overflowed
        # past date.max. With no end to derive there is nothing to overflow.
        assert period_from_upload_timestamp(
            "9999-12-31T23:59:59", clock=FROZEN
        ) == ValidityPeriod(start_date="9999-12-31", end_date=None)


class TestParseDay:
    """Validating a single calendar date, and getting it back canonical."""

    @pytest.mark.unit
    def test_returns_the_date_it_validated(self):
        assert parse_day("2026-09-03") == "2026-09-03"

    @pytest.mark.unit
    def test_zero_pads_a_date_strptime_accepts_unpadded(self):
        # The reason this returns a value instead of just raising: strptime
        # takes "2026-9-3", and a caller forwarding that raw string fails in
        # whatever consumer wants the padded form.
        assert parse_day("2026-9-3") == "2026-09-03"

    @pytest.mark.unit
    def test_trims_surrounding_whitespace(self):
        assert parse_day("  2026-09-03 ") == "2026-09-03"

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", [None, "", "   ", "01-01-2027", "2026/09/03", "garbage", "2026-13-45"])
    def test_rejects_anything_that_is_not_a_calendar_date(self, bad):
        with pytest.raises(DocumentValidityError, match="YYYY-MM-DD"):
            parse_day(bad)

    @pytest.mark.unit
    def test_names_the_field_in_the_error(self):
        with pytest.raises(DocumentValidityError, match="valid_on"):
            parse_day("nope", "valid_on")
