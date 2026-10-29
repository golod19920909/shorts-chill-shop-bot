"""Size budget for Telegram inline keyboards that carry URL buttons.

Telegram refuses a WHOLE message when its reply markup is oversized
(``Bad Request: Reply markup is too long``), so an oversized keyboard hides
the screen entirely instead of merely dropping a button. The real limit was
measured live against the Bot API for this project:

* button URLs of at most :data:`LONG_URL_FREE` characters are not counted
  at all - a keyboard with 218 such URLs (20 056 chars of URLs, 27 KB of
  JSON) was accepted;
* every LONGER URL contributes its FULL length to ONE shared budget per
  keyboard: a sum of 11 011 was accepted while 11 894 was rejected. The
  real favorites screen (6 affiliate links ~1011 + 6 share links ~3000 =
  ~23 900) was rejected exactly this way and the user saw nothing.

:data:`LONG_URL_BUDGET` keeps a ~14% margin under the lowest rejected sum,
so keyboards built with it are accepted. Callback buttons (remove, stop,
navigation, home) carry no URL, cost nothing and are NEVER dropped.
"""

from __future__ import annotations

#: URLs up to this length cost nothing (verified: 20 056 such chars passed).
LONG_URL_FREE = 1000

#: Shared budget for the summed length of all LONGER urls in one keyboard.
#: Verified boundary: 11 011 passes, 11 894 fails; 9 500 leaves margin.
LONG_URL_BUDGET = 9500


def url_cost(url: str | None) -> int:
    """What ``url`` costs against :data:`LONG_URL_BUDGET` in one keyboard."""

    if not url or len(url) <= LONG_URL_FREE:
        return 0
    return len(url)


class UrlBudget:
    """Remaining long-URL allowance of a single keyboard under construction."""

    def __init__(self, budget: int = LONG_URL_BUDGET) -> None:
        self.remaining = budget

    def try_take(self, url: str | None) -> bool:
        """Consume the cost of ``url``; ``False`` = the URL button must be dropped."""

        cost = url_cost(url)
        if cost > self.remaining:
            return False
        self.remaining -= cost
        return True
