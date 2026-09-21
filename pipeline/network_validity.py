"""
The validity window a published catalog is announced with.

An operator names how long this provider's announcement should stand; this
module owns what a legal window is, what the default one is, and how it renders
on the wire. Pure: no env, no I/O, no db - so the API can validate an operator's
input and the activity can rebuild the same window from a stored row without
either of them re-deriving the rules.

`pipeline/catalog_builder.py` places the rendered window on the catalog;
`pipeline/api.py` validates operator input against `parse_window`.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

# Operators think in dates, not instants - the window is stored and exchanged
# as a plain calendar date and only widened to an instant on the wire.
DATE_FORMAT = "%Y-%m-%d"


class ValidityWindowError(ValueError):
    """An operator-supplied window that cannot be published."""


@dataclass(frozen=True)
class ValidityWindow:
    """Inclusive calendar span, both ends `YYYY-MM-DD`."""

    start_date: str
    end_date: str


def today() -> str:
    """Today's date in the stored form."""
    return date.today().strftime(DATE_FORMAT)


def default_window() -> ValidityWindow:
    """The window an approver is offered before editing anything.

    Both ends are today: the announcement starts the day it is approved, and
    the end is a deliberate floor rather than a guess - an approver who wants
    the catalog to stand longer has to say so.
    """
    stamp = today()
    return ValidityWindow(start_date=stamp, end_date=stamp)


def _parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value.strip(), DATE_FORMAT).date()
    except (AttributeError, ValueError):
        raise ValidityWindowError(
            f"{field} must be a calendar date formatted as YYYY-MM-DD, got {value!r}."
        ) from None


def parse_window(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> ValidityWindow:
    """Validate an operator-supplied window, defaulting either end to today.

    Raises `ValidityWindowError` - never returns a half-valid window, so a
    caller that gets a window back can publish it unchecked.
    """
    default = default_window()
    raw_start = start_date if (start_date or "").strip() else default.start_date
    raw_end = end_date if (end_date or "").strip() else default.end_date

    start = _parse_date(raw_start, "start date")
    end = _parse_date(raw_end, "end date")

    if end < start:
        raise ValidityWindowError(
            f"end date ({end.strftime(DATE_FORMAT)}) cannot be before "
            f"start date ({start.strftime(DATE_FORMAT)})."
        )

    return ValidityWindow(
        start_date=start.strftime(DATE_FORMAT),
        end_date=end.strftime(DATE_FORMAT),
    )


def window_from_row(
    start_date: Optional[str],
    end_date: Optional[str],
) -> Optional[ValidityWindow]:
    """Rebuild a stored window, or None when the document has none.

    None is the normal case for a document promoted before windows existed, and
    means "announce without a validity" - not an error, and not a silent
    fallback to today, which would expire an established catalog.
    """
    if not (start_date or "").strip() or not (end_date or "").strip():
        return None
    try:
        return parse_window(start_date, end_date)
    except ValidityWindowError:
        # A row this module never wrote. Announcing no window beats announcing
        # a malformed one the Discovery Service would reject the catalog for.
        return None


def to_catalog_validity(window: ValidityWindow) -> dict:
    """The `validity` object a catalog carries on the wire.

    Widened from dates to instants because the spec's catalog examples are
    date-times; the end date is inclusive, so it runs to the last second of
    that day rather than its midnight boundary.
    """
    return {
        "startDate": f"{window.start_date}T00:00:00Z",
        "endDate": f"{window.end_date}T23:59:59Z",
    }
