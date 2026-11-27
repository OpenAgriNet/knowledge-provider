"""OAN document validity and the calendar days used by search."""

import os
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    field_validator,
    model_serializer,
    model_validator,
)

# Operators think in calendar dates, not instants - a period is stored,
# exchanged and filtered as a plain `YYYY-MM-DD` day.
#
# Parsing only. Dates are rendered with `date.isoformat()`, which is the same
# format but zero-pads the year on every platform - glibc's strftime does not
# ("%Y" of year 1 is "1-01-01" on Linux and "0001-01-01" on macOS), and a date
# this module emitted would then fail the parse this module does.
DATE_FORMAT = "%Y-%m-%d"

# Returns "now". Injected wherever the answer depends on the current day, so a
# test can pin the day instead of building fixtures relative to the real one.
Clock = Callable[[], datetime]


class DocumentValidityError(ValueError):
    """An operator-supplied period that cannot be stored."""


def system_clock() -> datetime:
    """Evaluate applicability dates in the deployment's configured timezone."""
    return datetime.now(ZoneInfo(os.environ.get("KNOWLEDGE_TIMEZONE", "UTC")))


class TimePeriod(BaseModel):
    """OAN TimePeriod shared by Knowledge Advisory and Knowledge Resource."""

    model_config = ConfigDict(extra="forbid")

    startsAt: AwareDatetime | None = None
    endsAt: AwareDatetime | None = None

    @field_validator("startsAt", "endsAt", mode="before")
    @classmethod
    def parse_boundary(cls, value, info):
        if isinstance(value, str) and len(value) == 10:
            day = date.fromisoformat(value)
            boundary = time.min if info.field_name == "startsAt" else time.max
            return datetime.combine(day, boundary, tzinfo=system_clock().tzinfo)
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("Use an ISO date or timestamp")
        return value

    @model_validator(mode="after")
    def check_bounds(self):
        if self.startsAt is None and self.endsAt is None:
            raise ValueError("Set at least one validity boundary or omit validity")
        if self.startsAt and self.endsAt and self.startsAt > self.endsAt:
            raise ValueError("Validity start cannot follow its end")
        return self

    @model_serializer(mode="wrap")
    def omit_empty_bounds(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}

    def calendar_days(self) -> dict:
        tz = system_clock().tzinfo
        return {
            "valid_from": self.startsAt.astimezone(tz).date().isoformat() if self.startsAt else None,
            "valid_to": self.endsAt.astimezone(tz).date().isoformat() if self.endsAt else None,
        }


def storage_value(period: TimePeriod | None) -> dict | None:
    return period.model_dump(mode="json", exclude_none=True) if period else None


def read_validity(metadata: dict) -> TimePeriod | None:
    """Read the schema field or convert old date bounds at the restore boundary."""
    if "validity" in metadata:
        value = metadata["validity"]
    else:
        value = {key: metadata[legacy] for key, legacy in (("startsAt", "valid_from"), ("endsAt", "valid_to"))
                 if metadata.get(legacy)}
    return TimePeriod.model_validate(value) if value is not None and value != {} else None


@dataclass(frozen=True)
class ValidityPeriod:
    """Calendar span, `YYYY-MM-DD`, with an inclusive start and optional end.

    Inclusive on purpose: a document uploaded today starts today and must be
    searchable the same day, and an end date names the last day it answers
    rather than the first day it does not.

    `end_date is None` means the document never expires. That is the default a
    document is uploaded with - business asks that content stay answerable
    until someone decides otherwise, rather than going dark on a date nobody
    chose.
    """

    start_date: str
    end_date: Optional[str] = None

    @property
    def expires(self) -> bool:
        """Whether this period has an end at all."""
        return self.end_date is not None

    def is_active_on(self, on_date: str) -> bool:
        """Whether this period covers `on_date` (`YYYY-MM-DD`)."""
        if on_date < self.start_date:
            return False
        return self.end_date is None or on_date <= self.end_date


def today(clock: Optional[Clock] = None) -> str:
    """Today's date in the stored form."""
    return (clock or system_clock)().date().isoformat()


def default_period(clock: Optional[Clock] = None) -> ValidityPeriod:
    """The period a document gets on upload, before anyone edits it.

    Starts the day it is uploaded and does not end: a document is live the
    moment it is ingested and stays answerable until a reviewer says otherwise.
    An end nobody chose would retire content on a date nobody meant.
    """
    return period_from_upload_date(today(clock))


def period_from_upload_date(upload_date: str) -> ValidityPeriod:
    """The default period anchored on the day a document was uploaded.

    Used when stamping a document that predates the stored columns: its own
    upload day is a truer start than today, which would date a two-year-old
    document as though it arrived this morning.
    """
    stamp = _parse_date(upload_date, "upload date").isoformat()
    return ValidityPeriod(start_date=stamp, end_date=None)


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


def parse_day(value: Optional[str], field: str = "date") -> str:
    """Validate a single calendar date and return it in canonical form.

    The canonical form matters as much as the validation: `strptime` accepts
    `2026-9-3`, so a caller that validated a date and then forwarded the raw
    string would pass its own check and fail somewhere downstream that wants
    the padded form. Returning the parsed date means a caller cannot hold a
    validated date and a non-canonical one at the same time.
    """
    return _parse_date(value, field).isoformat()


def parse_period(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    clock: Optional[Clock] = None,
) -> ValidityPeriod:
    """Validate an operator-supplied period.

    An omitted start defaults to today. An omitted end means the document
    never expires - blank is a real answer here, not a missing one, so a
    reviewer who clears the end date is asking for exactly that. Raises
    `DocumentValidityError` - never returns a half-valid period, so a caller
    holding one can store it unchecked.
    """
    raw_start = start_date if (start_date or "").strip() else today(clock)
    start = _parse_date(raw_start, "start date")
    stamp = start.isoformat()

    if not (end_date or "").strip():
        return ValidityPeriod(start_date=stamp, end_date=None)

    end = _parse_date(end_date, "end date")

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

    The start is what decides whether a period exists at all. A row with a
    start and a NULL end is the normal shape - that is what an upload stores -
    and it rebuilds as an open-ended period, not as "no period": the document
    is searchable from its start day and never expires.

    None means "this document has no stated period", the case for a document
    uploaded before validity existed. Search reads that as always current -
    not an error, and not a silent fallback to today, which would retire every
    legacy document at once.
    """
    if not (start_date or "").strip():
        return None
    try:
        return parse_period(start_date, end_date)
    except DocumentValidityError:
        # A row this module never wrote. Treating it as no period beats
        # filtering on a date the store cannot compare.
        return None
