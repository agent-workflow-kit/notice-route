"""The retry schedule (#25): docs/interface.md, Retries and outcomes.

A retry row is due 1 minute, 5 minutes, 30 minutes and 2 hours after the end of its previous
call, at most 5 attempts in total (3 for ``wsl-notice``; the first call counts). After the last
the row ends ``failed``, code ``retries_exhausted``. There is no jitter: the schedule is a pure
function of the destination kind, the end of the call (UTC epoch seconds from the wall clock the
caller already read for the transaction) and the attempts made, so a digest can ask it for any
attempt count and the claim of an escaped row can ask whether a step is left.

``retry_schedule(kind, now)`` returns ``retry_at(attempts) -> int | None`` where ``attempts`` is
the count after the call just made (>= 1): the ``deliver_after`` of the next try, or None when
the attempts are used up, that is for every ``attempts >= attempt_cap(kind)``. It never returns
a time before ``now``. ``lock_busy`` and ``env_missing`` count no attempt and never ask it.
Callers inject another schedule through ``send.Seams.retry_schedule``.
"""

from __future__ import annotations

from collections.abc import Callable

__all__ = ["ATTEMPT_CAP", "DEFAULT_ATTEMPT_CAP", "RETRY_DELAYS_S", "attempt_cap", "retry_schedule"]

RETRY_DELAYS_S = (60, 300, 1800, 7200)  # after the end of attempt 1, 2, 3 and 4
DEFAULT_ATTEMPT_CAP = 5
ATTEMPT_CAP = {"wsl-notice": 3}  # the kinds with a cap of their own


def _int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an integer")
    return value


def attempt_cap(kind: str) -> int:
    """The attempts a row of a ``kind`` destination gets in total (an unknown kind: 5)."""
    if not isinstance(kind, str):
        raise TypeError("kind must be a string")
    return ATTEMPT_CAP.get(kind, DEFAULT_ATTEMPT_CAP)


def retry_schedule(kind: str, now: int) -> Callable[[int], int | None]:
    """The schedule of a ``kind`` destination for a call that ended at ``now``."""
    cap = attempt_cap(kind)
    ended = _int(now, "now")
    if ended < 0:
        raise ValueError("now must not be negative")

    def retry_at(attempts: int) -> int | None:
        n = _int(attempts, "attempts")
        if n < 1:
            raise ValueError("attempts counts the calls made, at least 1")
        if n >= cap:
            return None
        return ended + RETRY_DELAYS_S[n - 1]

    return retry_at
