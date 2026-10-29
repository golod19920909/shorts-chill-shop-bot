"""Application configuration loaded from environment variables (.env).

Never log the values produced by this module directly - use
``utils.logging.mask_secret`` if a value must ever be shown for
diagnostics.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env once, as early as possible, without overriding real
# environment variables that may already be set (e.g. in production).
load_dotenv(override=False)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class MarketplaceCredentials:
    """Credential pair for a marketplace API.

    ``is_configured`` is True only when both key and secret are
    non-empty, which providers use to decide whether they can operate.
    """

    app_key: str | None = None
    app_secret: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.app_key) and bool(self.app_secret)


@dataclass(frozen=True)
class Settings:
    """Immutable application settings, populated once at startup."""

    bot_token: str | None
    gemini_api_key: str | None
    database_path: str
    log_level: str
    aliexpress: MarketplaceCredentials

    #: Telegram user ids allowed to open the admin panel (``ADMIN_IDS``).
    #: Empty means "no administrator is configured": the panel then says so
    #: instead of granting access to anyone.
    admin_ids: tuple[int, ...] = ()

    #: How often the price-tracking scheduler checks tracked products
    #: (``TRACKING_INTERVAL_MINUTES``). ``0`` disables automatic checks
    #: entirely (the tracking list then says so honestly). The default of
    #: 24 hours (1440 minutes) is the agreed production cadence: a price
    #: move is checked once a day, which keeps tracked products far below
    #: the AliExpress affiliate rate limits.
    tracking_interval_minutes: int = 1440

    #: How deep one PRODUCT search may walk the documented result pages of the
    #: marketplace (``SEARCH_MAX_PAGES``). Page 1 is the ordinary answer; the
    #: SAME phrase is asked for its next page only while the relevant pool is
    #: still short of the result cap. Configurable because the number is a
    #: direct trade between breadth and API usage.
    search_max_pages: int = 3

    #: Photo-search breadth. ``PHOTO_SEARCH_PAGES`` is how many documented
    #: result pages ONE grounded image hypothesis may be asked for, and
    #: ``PHOTO_SEARCH_HYPOTHESES`` how many genuinely different grounded
    #: representations one photo may cost in total. Both are limits on REAL
    #: API calls, so ``load_settings`` clamps them: a photo search must never
    #: turn into an unbounded crawl.
    photo_search_pages: int = 2
    photo_search_hypotheses: int = 4

    # «✨ Находки» (G24): bounds of ONE feed refill, measured in REAL work -
    # so ``load_settings`` clamps them: a typo in .env must never turn one
    # feed open into an unbounded crawl.
    #
    # * ``FEED_OPEN_SIZE``          - how many NEW cards one open hands to
    #   the user (the carousel page; 20 per spec);
    # * ``FEED_MAX_PAGES_PER_OPEN`` - how many documented ``product.query``
    #   pages (50 rows each) one refill may cost at most. The local feed
    #   pool ACCUMULATES across refills, so a long session reaches hundreds
    #   of items without ever asking the API for one giant page.
    feed_open_size: int = 20
    feed_max_pages_per_open: int = 2

    #: Gemini models per ROLE. The names below are configuration, not a
    #: verified availability statement: checking which models a key may
    #: really call costs quota, so it is a separate, intentional step.
    #:
    #: * ``fast``    - query parsing, candidate ranking, refinement;
    #: * ``smart``   - product analysis, comparison, free-form questions;
    #: * ``vision``  - looking at a photo;
    #: * ``fallback``- tried once when a primary model answers "unavailable"
    #:   (never for a quota error, which is account-wide).
    #: All three PRIMARY roles run the same generation: one model means one
    #: quality bar (and one set of prompt behaviours) for parsing, selection
    #: and photo reading; the distinct role variables stay so a deployment can
    #: still pin a cheaper model to one role without touching any code.
    gemini_fast_model: str = "gemini-3.8-flash"
    gemini_smart_model: str = "gemini-3.8-flash"
    gemini_vision_model: str = "gemini-3.8-flash"
    gemini_fallback_model: str = "gemini-3.1-flash-lite"

    @property
    def gemini_model(self) -> str:
        """The smart model - legacy alias kept for existing callers."""

        return self.gemini_smart_model

    @property
    def database_full_path(self) -> Path:
        path = Path(self.database_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    @property
    def is_bot_token_configured(self) -> bool:
        return bool(self.bot_token)

    @property
    def is_gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_admins(self) -> bool:
        """Whether at least one administrator is configured."""

        return bool(self.admin_ids)

    def is_admin(self, telegram_id: int | None) -> bool:
        """Whether a Telegram id may use the admin panel.

        An unconfigured ``ADMIN_IDS`` grants access to nobody - failing
        open would expose statistics to every user.
        """

        if telegram_id is None:
            return False
        return telegram_id in self.admin_ids

    @property
    def logging_level(self) -> int:
        """Map ``LOG_LEVEL`` to a ``logging`` level, defaulting to INFO.

        An unknown value is not an error - falling back to INFO keeps a
        typo in .env from preventing the bot from starting.
        """

        return getattr(logging, self.log_level.upper(), logging.INFO)


def _get(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _get_int_list(name: str) -> tuple[int, ...]:
    """Read a comma/semicolon separated list of integers (``ADMIN_IDS``).

    Malformed entries are skipped with a warning instead of failing
    startup: a typo in one id must not stop the bot, and it must never
    turn into a wildcard ("allow everyone") either.
    """

    raw = _get(name)
    if not raw:
        return ()

    values: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            values.append(int(chunk))
        except ValueError:
            logger.warning("%s contains a non-numeric entry; it is ignored", name)

    # Duplicates are dropped while the order is kept, so the tuple is
    # stable no matter how the value is written in .env.
    return tuple(dict.fromkeys(values))


def _get_int(
    name: str, default: int, *, minimum: int = 0, maximum: int | None = None
) -> int:
    """Read a bounded integer (``TRACKING_INTERVAL_MINUTES``, page budgets).

    A malformed, too-small or too-large value falls back to ``default`` with a
    warning instead of failing startup; ``minimum`` guards against configs that
    would hammer the marketplace API, and ``maximum`` against a typo that would
    turn a page budget into an unbounded crawl.
    """

    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%d is below %d; using %s", name, value, minimum, default)
        return default
    if maximum is not None and value > maximum:
        logger.warning("%s=%d is above %d; using %s", name, value, maximum, default)
        return default
    return value


def load_settings() -> Settings:
    """Read all configuration from environment variables.

    Missing marketplace credentials are not an error at this stage:
    providers must degrade gracefully (see services/marketplaces/base.py).
    """

    return Settings(
        bot_token=_get("BOT_TOKEN"),
        gemini_api_key=_get("GEMINI_API_KEY"),
        database_path=_get("DATABASE_PATH") or "database/shop_bot.db",
        log_level=_get("LOG_LEVEL") or "INFO",
        aliexpress=MarketplaceCredentials(
            app_key=_get("ALIEXPRESS_APP_KEY"),
            app_secret=_get("ALIEXPRESS_APP_SECRET"),
        ),
        admin_ids=_get_int_list("ADMIN_IDS"),
        tracking_interval_minutes=_get_int(
            "TRACKING_INTERVAL_MINUTES", default=1440, minimum=0
        ),
        #: Retrieval depth (documented ``page_no`` budgets). Both are limits on
        #: real API calls, so both are clamped to a hard maximum: a mistaken
        #: .env value must not turn one search or one photo into a crawl.
        search_max_pages=_get_int(
            "SEARCH_MAX_PAGES", default=3, minimum=1, maximum=10
        ),
        photo_search_pages=_get_int(
            "PHOTO_SEARCH_PAGES", default=2, minimum=1, maximum=5
        ),
        photo_search_hypotheses=_get_int(
            "PHOTO_SEARCH_HYPOTHESES", default=4, minimum=1, maximum=6
        ),
        feed_open_size=_get_int(
            "FEED_OPEN_SIZE", default=20, minimum=1, maximum=100
        ),
        feed_max_pages_per_open=_get_int(
            "FEED_MAX_PAGES_PER_OPEN", default=2, minimum=1, maximum=10
        ),
        gemini_fast_model=_get("GEMINI_FAST_MODEL") or "gemini-3.8-flash",
        #: ``GEMINI_MODEL`` stays supported as the single-model override for
        #: the "smart" role, so existing .env files keep working unchanged.
        gemini_smart_model=(
            _get("GEMINI_MODEL") or _get("GEMINI_SMART_MODEL") or "gemini-3.8-flash"
        ),
        gemini_vision_model=_get("GEMINI_VISION_MODEL") or "gemini-3.8-flash",
        gemini_fallback_model=(
            _get("GEMINI_FALLBACK_MODEL") or "gemini-3.1-flash-lite"
        ),
    )


# Module-level singleton for convenient imports (`from config.settings import settings`).
settings = load_settings()
