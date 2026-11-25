"""Quiet-hour membership and the next window end (#15).

Pure functions over a validated :class:`~notice_route.validate.QuietWindow` and an instant given
as UTC epoch seconds (the caller reads the wall clock through the gateway). Nothing here reads a
clock, a file or the environment; the zone inside the window was loaded by ``Gateway.load_zone``.

The rule is the one of docs/interface.md (Quiet hours): an instant is judged from its local
weekday and minute of the day, never by building boundary timestamps. So a start or an end that
falls in a spring-forward gap needs no special case (the first real minute after the gap is
already past it), and a start or an end in a fall-back fold applies on both passes through the
repeated minutes.

Choices where the interface is silent (the narrower reading):

- An instant must be an ``int`` or ``float`` (not a ``bool``), finite, and within
  ``MIN_EPOCH..MAX_EPOCH`` (year 1 plus two days to year 9999 minus three days, so the zone
  conversion and the two-day search can never overflow ``datetime``); anything else is a
  ``TypeError`` or ``ValueError``.
- A fractional instant is floored (``-0.5`` is the second before the epoch).
- A malformed window (no days, a day outside 0-6, a repeated day, a minute outside 0-1439,
  ``start == end``) is a ``ValueError``; the validator never builds one.
- The "whole minutes" stepped by :func:`window_end` are multiples of 60 epoch seconds. That equals
  the local whole minute in every zone whose offset is a whole number of minutes (all of them
  since 1972); in an older zone with an offset in seconds the end falls on that offset's seconds.
- When no minute in the next two days is outside the window (not reachable for a valid window in
  a real zone), :func:`window_end` returns ``None`` and the caller decides.

TODO(#18): the decision order consumes :func:`in_window` (quiet drops and defers).
TODO(#16): the queue stores :func:`window_end` as the ``deliver_after`` of a held row and
``flush`` calls it again for a due row while the destination is still quiet.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from .validate import QuietWindow

__all__ = ["MAX_EPOCH", "MIN_EPOCH", "in_window", "window_end"]

# 0001-01-03T00:00:00Z and 9999-12-29T00:00:00Z: two and three days inside the datetime range.
MIN_EPOCH = -62135596800 + 2 * 86400
MAX_EPOCH = 253402300800 - 3 * 86400

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SEARCH_MINUTES = 2 * 24 * 60


def _check_window(window: QuietWindow) -> None:
    days = window.days
    if (
        not isinstance(days, tuple)
        or not days
        or len(set(days)) != len(days)
        or any(type(d) is not int or not 0 <= d <= 6 for d in days)
    ):
        raise ValueError("quiet window: days must be distinct weekday numbers 0 to 6")
    for minute in (window.start, window.end):
        if type(minute) is not int or not 0 <= minute <= 1439:
            raise ValueError("quiet window: start and end must be minutes of the day")
    if window.start == window.end:
        raise ValueError("quiet window: start and end must differ")


def _whole_second(instant: float) -> int:
    if isinstance(instant, bool) or not isinstance(instant, (int, float)):
        raise TypeError("an instant must be an int or float of epoch seconds")
    if isinstance(instant, float) and not math.isfinite(instant):
        raise ValueError("an instant must be finite")
    seconds = math.floor(instant)
    if not MIN_EPOCH <= seconds <= MAX_EPOCH:
        raise ValueError("instant outside the supported range")
    return seconds


def _inside(window: QuietWindow, seconds: int) -> bool:
    local = (_EPOCH + timedelta(seconds=seconds)).astimezone(window.zone)
    weekday = local.weekday()
    minute = local.hour * 60 + local.minute
    if window.start < window.end:
        return weekday in window.days and window.start <= minute < window.end
    return (weekday in window.days and minute >= window.start) or (
        (weekday - 1) % 7 in window.days and minute < window.end
    )


def in_window(window: QuietWindow, instant: float) -> bool:
    """Whether ``instant`` (UTC epoch seconds) is inside ``window``."""
    _check_window(window)
    return _inside(window, _whole_second(instant))


def window_end(window: QuietWindow, instant: float) -> int | None:
    """The first whole minute after ``instant`` that is outside ``window``, as epoch seconds.

    Steps one minute at a time for at most two days; ``None`` when none of them is outside.
    The result is strictly after ``instant`` and at most two days after it. When ``instant`` is
    already outside, it is simply the next whole minute.
    """
    _check_window(window)
    first = (_whole_second(instant) // 60 + 1) * 60
    for step in range(_SEARCH_MINUTES):
        candidate = first + step * 60
        if not _inside(window, candidate):
            return candidate
    return None
