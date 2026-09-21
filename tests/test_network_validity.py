"""Unit tests for the published catalog's validity window."""

from datetime import date, timedelta

import pytest

from pipeline.network_validity import (
    ValidityWindow,
    ValidityWindowError,
    default_window,
    parse_window,
    to_catalog_validity,
    today,
    window_from_row,
)

TODAY = date.today().isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


class TestDefaultWindow:
    @pytest.mark.unit
    def test_both_ends_are_today(self):
        # The approver is offered today/today and edits the end from there.
        assert default_window() == ValidityWindow(start_date=TODAY, end_date=TODAY)

    @pytest.mark.unit
    def test_today_is_the_stored_form(self):
        assert today() == TODAY


class TestParseWindow:
    @pytest.mark.unit
    def test_defaults_both_ends_to_today(self):
        assert parse_window() == ValidityWindow(start_date=TODAY, end_date=TODAY)

    @pytest.mark.unit
    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_defaults_a_blank_end_to_today(self, blank):
        assert parse_window(TODAY, blank).end_date == TODAY

    @pytest.mark.unit
    def test_keeps_an_approver_widened_end(self):
        window = parse_window(TODAY, TOMORROW)

        assert window == ValidityWindow(start_date=TODAY, end_date=TOMORROW)

    @pytest.mark.unit
    def test_same_day_window_is_legal(self):
        # The default itself — an end equal to the start must not be rejected.
        assert parse_window(TODAY, TODAY).end_date == TODAY

    @pytest.mark.unit
    def test_trims_surrounding_whitespace(self):
        assert parse_window(f"  {TODAY} ", f" {TOMORROW}  ").start_date == TODAY

    @pytest.mark.unit
    def test_rejects_an_end_before_the_start(self):
        with pytest.raises(ValidityWindowError, match="cannot be before"):
            parse_window(TODAY, YESTERDAY)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "bad",
        ["15-09-2026", "2026/09/15", "2026-13-01", "2026-02-30", "next tuesday", "2026-09-15T00:00:00Z"],
    )
    def test_rejects_anything_that_is_not_a_calendar_date(self, bad):
        with pytest.raises(ValidityWindowError, match="YYYY-MM-DD"):
            parse_window(TODAY, bad)

    @pytest.mark.unit
    def test_names_which_end_was_wrong(self):
        with pytest.raises(ValidityWindowError, match="start date"):
            parse_window("not-a-date", TODAY)


class TestWindowFromRow:
    @pytest.mark.unit
    def test_rebuilds_a_stored_window(self):
        assert window_from_row(TODAY, TOMORROW) == ValidityWindow(
            start_date=TODAY, end_date=TOMORROW
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "start,end",
        [(None, None), (TODAY, None), (None, TOMORROW), ("", ""), ("  ", TOMORROW)],
    )
    def test_a_document_with_no_window_announces_without_one(self, start, end):
        # Documents promoted before approvers named a window: not an error, and
        # deliberately not defaulted to today, which would expire the catalog.
        assert window_from_row(start, end) is None

    @pytest.mark.unit
    def test_a_malformed_row_announces_without_a_window(self):
        # Better an announcement with no validity than one the Discovery
        # Service rejects the whole catalog over.
        assert window_from_row("15-09-2026", TOMORROW) is None
        assert window_from_row(TOMORROW, TODAY) is None


class TestToCatalogValidity:
    @pytest.mark.unit
    def test_widens_dates_to_the_instants_the_spec_examples_use(self):
        validity = to_catalog_validity(ValidityWindow(start_date="2026-09-16", end_date="2026-12-31"))

        assert validity == {
            "startDate": "2026-09-16T00:00:00Z",
            "endDate": "2026-12-31T23:59:59Z",
        }

    @pytest.mark.unit
    def test_end_date_is_inclusive_to_the_last_second_of_that_day(self):
        # A same-day window must still be a non-empty span.
        validity = to_catalog_validity(ValidityWindow(start_date="2026-09-16", end_date="2026-09-16"))

        assert validity["startDate"] < validity["endDate"]
