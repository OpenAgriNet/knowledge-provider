"""
The validity period a document's chunks are searchable in.

A document is only worth answering from for as long as its content still
holds. This module owns that rule: what a legal period is, the period an
uploader's document starts life with, and whether a period is live on a given
day. Pure - no env, no I/O, no db - so the API can validate an approver's edit,
the ingest activity can stamp the same period onto every chunk, and the vector
store can build a search filter from it without any of them re-deriving it.

Deliberately separate from `pipeline/network_validity.py`, which owns the
Announcement Lifetime: a different window, with different defaults (today /
today, not today / a year out), named by a different person at a different
moment, and carried on the wire rather than in a chunk payload. They share a
date format and nothing else; see `CONTEXT.md` on not conflating the two.

`pipeline/activities.py` stamps the period onto chunk payloads as
`start_date` / `end_date`; `pipeline/vector_store/qdrant_store.py` filters on
those fields at search time; `pipeline/api.py` validates approver input here.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Optional

# Operators think in calendar dates, not instants - a period is stored,
# exchanged and filtered as a plain `YYYY-MM-DD` day.
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
    """The real clock. Local, matching `network_validity.today()`'s
    `date.today()`, so both windows agree on what day it is."""
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
    return (clock or system_clock)().date().strftime(DATE_FORMAT)


def add_years(stamp: str, years: int = DEFAULT_VALIDITY_YEARS) -> str:
    """`stamp` moved forward by whole calendar years.

    29 February lands on 28 February in a non-leap year - the alternative
    (1 March) would push the period into the wrong month for no gain.
    """
    anchor = _parse_date(stamp, "date")
    try:
        moved = anchor.replace(year=anchor.year + years)
    except ValueError:
        moved = anchor.replace(year=anchor.year + years, day=28)
    return moved.strftime(DATE_FORMAT)


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
    stamp = _parse_date(upload_date, "upload date").strftime(DATE_FORMAT)
    return ValidityPeriod(start_date=stamp, end_date=add_years(stamp))


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
    stamp = start.strftime(DATE_FORMAT)

    raw_end = end_date if (end_date or "").strip() else add_years(stamp)
    end = _parse_date(raw_end, "end date")

    if end < start:
        raise DocumentValidityError(
            f"end date ({end.strftime(DATE_FORMAT)}) cannot be before "
            f"start date ({stamp})."
        )

    return ValidityPeriod(start_date=stamp, end_date=end.strftime(DATE_FORMAT))


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
