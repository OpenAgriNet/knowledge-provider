"""
The validity period a document's chunks are searchable in.

A document is only worth answering from for as long as its content still
holds. This module owns that rule: what a legal period is, the period an
uploader's document starts life with, and whether a period is live on a given
day. Pure - no env, no I/O, no db - so the API can validate an approver's edit,
the ingest activity can stamp the same period onto every chunk, and the vector
store can build a search filter from it without any of them re-deriving it.

`pipeline/activities.py` stamps the period onto chunk payloads as
`start_date` / `end_date`; `pipeline/vector_store/qdrant_store.py` filters on
those fields at search time; `pipeline/api.py` validates approver input here.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Optional

# Operators think in calendar dates, not instants - a period is stored,
# exchanged and filtered as a plain `YYYY-MM-DD` day.
#
# Parsing only. Dates are rendered with `date.isoformat()`, which is the same
# format but zero-pads the year on every platform - glibc's strftime does not
# ("%Y" of year 1 is "1-01-01" on Linux and "0001-01-01" on macOS), and a date
# this module emitted would then fail the parse this module does.
DATE_FORMAT = "%Y-%m-%d"

# How long a freshly uploaded document is presumed to stay current. A year is
# the business's default review cadence, not a technical limit: the approver is
# shown it prefilled and is free to move either end.
DEFAULT_VALIDITY_YEARS = 1

# Returns "now". Injected wherever the answer depends on the current day, so a
# test can pin the day instead of building fixtures relative to the real one.
Clock = Callable[[], datetime]


class DocumentValidityError(ValueError):
    """An operator-supplied period that cannot be stored."""


def system_clock() -> datetime:
    """The real clock. Local rather than UTC: a period is a calendar date an
    operator picked, so "today" should mean their today."""
    return datetime.now()


@dataclass(frozen=True)
class ValidityPeriod:
    """Inclusive calendar span, both ends `YYYY-MM-DD`.

    Inclusive on purpose: a document uploaded today defaults to a period
    starting today and must be searchable the same day, and an end date names
    the last day it answers rather than the first day it does not.
    """

    start_date: str
    end_date: str

    def is_active_on(self, on_date: str) -> bool:
        """Whether this period covers `on_date` (`YYYY-MM-DD`)."""
        return self.start_date <= on_date <= self.end_date


def today(clock: Optional[Clock] = None) -> str:
    """Today's date in the stored form."""
    return (clock or system_clock)().date().isoformat()


def add_years(stamp: str, years: int = DEFAULT_VALIDITY_YEARS) -> str:
    """`stamp` moved forward by whole calendar years.

    29 February lands on 28 February in a non-leap year - the alternative
    (1 March) would push the period into the wrong month for no gain.

    A stamp near `date.max` cannot move forward at all; that comes back as a
    `DocumentValidityError` rather than the raw `ValueError` the stdlib
    raises, so every failure out of this module is the one kind callers
    already handle.
    """
    anchor = _parse_date(stamp, "date")
    target_year = anchor.year + years
    try:
        moved = anchor.replace(year=target_year)
    except ValueError:
        try:
            moved = anchor.replace(year=target_year, day=28)
        except ValueError:
            raise DocumentValidityError(
                f"{stamp} cannot move {years} year(s) forward: "
                f"year {target_year} is outside the supported range."
            ) from None
    return moved.isoformat()


def default_period(clock: Optional[Clock] = None) -> ValidityPeriod:
    """The period a document gets on upload, before anyone edits it.

    Starts the day it is uploaded and runs a year out, so a document is live
    the moment it is ingested and expires without anyone having to remember to
    expire it.
    """
    return period_from_upload_date(today(clock))


def period_from_upload_date(upload_date: str) -> ValidityPeriod:
    """The default period anchored on the day a document was uploaded.

    Used when stamping a document that predates the stored columns: its own
    upload day is a truer start than today, which would silently extend a
    two-year-old document's life by another year.
    """
    stamp = _parse_date(upload_date, "upload date").isoformat()
    return ValidityPeriod(start_date=stamp, end_date=add_years(stamp))


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Read a stored ISO-8601 timestamp, or None when it cannot be read.

    `db.py` writes `datetime.utcnow().isoformat()`, which round-trips through
    `fromisoformat` exactly. The trailing-Z form is accepted too because rows
    can come from other writers, and Python 3.10's `fromisoformat` rejects it.
    """
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def day_of(timestamp: Optional[str]) -> Optional[str]:
    """The calendar day an ISO-8601 timestamp falls on, or None when unreadable.

    Parsed rather than sliced: a timestamp in an unexpected shape should come
    back as "I cannot tell", not as whatever its first ten characters happen
    to spell.
    """
    parsed = _parse_timestamp(timestamp)
    return None if parsed is None else parsed.date().isoformat()


def period_from_upload_timestamp(
    timestamp: Optional[str],
    clock: Optional[Clock] = None,
) -> ValidityPeriod:
    """The default period for a document that has none stored, anchored on the
    day it was uploaded.

    Its own upload day is a truer start than today, which would silently extend
    an old document's life by another year. A timestamp that cannot be read
    falls back to today's default rather than raising: this runs on the ingest
    path, where refusing to ingest over an unparseable audit column would be a
    worse answer than giving the document a period starting now.
    """
    upload_day = day_of(timestamp)
    if upload_day is None:
        return default_period(clock)
    try:
        return period_from_upload_date(upload_day)
    except DocumentValidityError:
        # Belt and braces: `day_of` emits a form `period_from_upload_date`
        # accepts, so this is unreachable today. It stays because the promise
        # this function makes is "never raises", and a caller on the ingest
        # path should not be the one to discover that drifted.
        return default_period(clock)


def _parse_date(value: Optional[str], field: str) -> date:
    try:
        return datetime.strptime((value or "").strip(), DATE_FORMAT).date()
    except (AttributeError, ValueError):
        raise DocumentValidityError(
            f"{field} must be a calendar date formatted as YYYY-MM-DD, got {value!r}."
        ) from None


def parse_period(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    clock: Optional[Clock] = None,
) -> ValidityPeriod:
    """Validate an operator-supplied period, defaulting either end.

    An omitted start defaults to today and an omitted end to a year past the
    resolved start, so a caller that sends only one end still gets a complete
    period. Raises `DocumentValidityError` - never returns a half-valid
    period, so a caller holding one can store it unchecked.
    """
    raw_start = start_date if (start_date or "").strip() else today(clock)
    start = _parse_date(raw_start, "start date")
    stamp = start.isoformat()

    raw_end = end_date if (end_date or "").strip() else add_years(stamp)
    end = _parse_date(raw_end, "end date")

    if end < start:
        raise DocumentValidityError(
            f"end date ({end.isoformat()}) cannot be before "
            f"start date ({stamp})."
        )

    return ValidityPeriod(start_date=stamp, end_date=end.isoformat())


def period_from_row(
    start_date: Optional[str],
    end_date: Optional[str],
) -> Optional[ValidityPeriod]:
    """Rebuild a stored period, or None when the document has none.

    None is the normal case for a document uploaded before validity existed.
    It means "this document has no stated period", which search reads as
    always current - not an error, and not a silent fallback to today, which
    would retire every legacy document at once.
    """
    if not (start_date or "").strip() or not (end_date or "").strip():
        return None
    try:
        return parse_period(start_date, end_date)
    except DocumentValidityError:
        # A row this module never wrote. Treating it as no period beats
        # filtering on a date the store cannot compare.
        return None
