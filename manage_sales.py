"""Command-line control of the reusable sales section (e.g. «Чёрная пятница»).

Run on the server AFTER deployment to enable/disable an event or set its
affiliate destination URL - no code change, no redeploy, no restart needed for
the toggle to take effect (the menu and the gate read the value from the
database on every render). The state persists across restarts and deployments
that keep the bot's existing ``DATABASE_PATH`` database.

Usage (from the project root, same place you run ``main.py``):

    python manage_sales.py enable  black_friday
    python manage_sales.py disable black_friday
    python manage_sales.py url     black_friday https://s.click.aliexpress.com/...
    python manage_sales.py status

``enable``/``disable``/``url``/``status`` read ``DATABASE_PATH`` from the same
``.env`` the bot uses (see ``config.settings``), so they operate on exactly the
database the running bot reads. The destination URL is validated (http/https
with a host) before it is stored - an unsafe or malformed link is rejected
instead of being saved, and the stored URL is never printed back to avoid
leaking any tracking parameters into logs.
"""

from __future__ import annotations

import asyncio
import sys

from config.sales import (
    DEFAULT_EVENT_ID,
    SALE_EVENTS,
    enabled_key,
    get_event,
    is_enabled_value,
    safe_http_url,
    url_key,
)
from config.settings import settings
from database.database import Database

USAGE = (
    "Usage:\n"
    "  python manage_sales.py enable  <event_id>\n"
    "  python manage_sales.py disable <event_id>\n"
    "  python manage_sales.py url     <event_id> <https-url>\n"
    "  python manage_sales.py status\n"
    f"\nKnown events: {', '.join(sorted(SALE_EVENTS))}\n"
)


def _resolve_event(event_id: str):
    """Validate an event id against the registry, or exit with guidance."""

    event = get_event(event_id)
    if event is None:
        print(
            f"Unknown event: {event_id!r}. Known events: "
            f"{', '.join(sorted(SALE_EVENTS))}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return event


async def _run(args: list[str]) -> int:
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    command = args[0]
    database = Database(settings.database_full_path)
    await database.initialize()
    try:
        if command == "status":
            for event in SALE_EVENTS.values():
                raw = await database.get_config(enabled_key(event.event_id))
                state = "ENABLED" if is_enabled_value(raw) else "disabled"
                has_url = await _has_url(database, event.event_id)
                print(
                    f"{event.event_id} «{event.title}»: {state}, "
                    f"destination URL: {'configured' if has_url else 'not set'}"
                )
            return 0

        if command in ("enable", "disable") and len(args) >= 2:
            event = _resolve_event(args[1])
            value = "1" if command == "enable" else "0"
            await database.set_config(enabled_key(event.event_id), value)
            print(
                f"Event «{event.title}» ({event.event_id}) is now "
                f"{'ENABLED' if command == 'enable' else 'disabled'}. "
                "The menu updates on the next render; no restart is required."
            )
            return 0

        if command == "url" and len(args) >= 3:
            event = _resolve_event(args[1])
            url = args[2]
            if safe_http_url(url) is None:
                print(
                    "Rejected: the destination must be a valid http(s) URL "
                    "with a host. Nothing was stored.",
                    file=sys.stderr,
                )
                return 2
            await database.set_config(url_key(event.event_id), url.strip())
            # The URL itself is never echoed back (it may carry tracking params).
            print(
                f"Destination URL for «{event.title}» ({event.event_id}) "
                "stored (validated, not printed)."
            )
            return 0

        print(USAGE, file=sys.stderr)
        return 2
    finally:
        await database.close()


async def _has_url(database: Database, event_id: str) -> bool:
    return await database.get_config(url_key(event_id)) is not None


def main() -> int:
    return asyncio.run(_run(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
