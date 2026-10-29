"""SQL schema definitions for the SQLite database.

Kept deliberately small (per the project brief: "do not over-engineer
the database"). Ten tables:

- users            : one row per Telegram user
- search_history   : past parsed search requests
- favorites        : products explicitly saved by a user for quick access
- saved_products   : product snapshots the user asked the bot to keep
- tracked_products : products with price tracking enabled (read by a
                     future scheduler - see the ``services`` package)
- price_history    : observed prices per tracked product, which is what
                     makes "notify me when the price drops" possible
- analytics_events : one row per user action/outcome the admin panel
                     aggregates (see ``services/analytics.py``)
- popular_shown    : per-user history of products already shown, SCOPED per
                     flow (``search`` for the search-like flows, ``feed`` for
                     the «✨ Находки» AliExpress feed), so one flow's transient
                     state can never decide another flow's screen
                     (see ``handlers/discovery.py`` and ``database.models``);
- popular_state    : per-user cursor of the «✨ Находки» feed (the next
                     documented AliExpress ``page_no`` to ask), so the feed
                     keeps moving forward after a restart instead of
                     replaying the same first pages
- user_view_history : products a user has ACTUALLY been shown, as read by the
                     curation layer (``curation.pipeline.HistoryFilter``) to
                     keep a feed novel - one row per (user, product), no
                     snapshot, because the only question it answers is
                     "has this user met this product?"

``favorites`` and ``tracked_products`` are separate because they express
different intents: bookmarking a product for later viewing vs. actively
monitoring its price. ``saved_products`` stores a full snapshot
(including fields a listing may later change) so a saved product can be
compared against a freshly fetched one. ``popular_shown`` is a pure
history log (INSERT OR IGNORE, no snapshot): it only has to answer
"has this user already seen this product?" forever.
"""

from __future__ import annotations

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT,
    country TEXT,
    currency TEXT,
    -- Onboarding (first /start shows the multi-screen tutorial; 1 = done).
    onboarded INTEGER NOT NULL DEFAULT 0,
    -- Price-notification preference (spec: notifications screen + scheduler).
    notify_price_changes INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_SEARCH_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS search_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    original_query TEXT NOT NULL,
    search_request_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_FAVORITES_TABLE = """
CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    product_id TEXT NOT NULL,
    product_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, marketplace, product_id)
);
"""

CREATE_SAVED_PRODUCTS_TABLE = """
CREATE TABLE IF NOT EXISTS saved_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    product_id TEXT NOT NULL,
    product_json TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, marketplace, product_id)
);
"""

CREATE_TRACKED_PRODUCTS_TABLE = """
CREATE TABLE IF NOT EXISTS tracked_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    product_id TEXT NOT NULL,
    title TEXT,
    product_url TEXT NOT NULL,
    initial_price REAL,
    last_checked_price REAL,
    currency TEXT,
    last_checked_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, marketplace, product_id)
);
"""

CREATE_PRICE_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracked_product_id INTEGER NOT NULL
        REFERENCES tracked_products (id) ON DELETE CASCADE,
    price REAL,
    shipping_cost REAL,
    currency TEXT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_ANALYTICS_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users (id) ON DELETE SET NULL,
    telegram_id INTEGER,
    event_type TEXT NOT NULL,
    marketplace TEXT,
    product_id TEXT,
    title TEXT,
    promotion_name TEXT,
    is_affiliate INTEGER,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_POPULAR_SHOWN_TABLE = """
CREATE TABLE IF NOT EXISTS popular_shown (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    product_id TEXT NOT NULL,
    flow TEXT NOT NULL DEFAULT 'search',
    shown_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, flow, marketplace, product_id)
);
"""

#: The scopes of the shown-product history (``popular_shown.flow``).
#:
#: ONE table stores what a user has already been shown, but the question it
#: answers is not the same in every screen:
#:
#: * :data:`FLOW_SEARCH` - the novelty scope of the search-like flows (text
#:   search, discovery, category, photo). A new search skips products already
#:   shown by a PREVIOUS search and knows nothing about the «✨ Находки» feed;
#: * :data:`FLOW_FEED` - the state of the «✨ Находки» feed itself (which
#:   cards this feed has already shown). A normal search must not be emptied
#:   by it, and the feed must not be declared exhausted because a user
#:   searched for something.
#:
#: The split is what stops one flow's transient state from deciding another
#: flow's screen. The value ``"feed"`` replaced the legacy ``"popular"``
#: scope of the removed «🔥 Популярное» rotation; ``Database.initialize``
#: migrates old rows in place so their shown-history is not lost.
FLOW_SEARCH = "search"

#: See :data:`FLOW_SEARCH`. The «✨ Находки» feed scope (G24).
FLOW_FEED = "feed"

#: Retention of ONE user's feed history (``popular_shown`` rows of
#: :data:`FLOW_FEED`): the newest rows are kept, older ones expire, so the
#: no-repeat set cannot grow forever (spec §10: 500..1000 per user).
FEED_HISTORY_LIMIT = 1000

#: Per-user cursor of the «✨ Находки» feed: the next documented
#: AliExpress ``page_no`` the feed continues from. The table keeps its
#: original name (and the column keeps ``step``): renaming either would
#: cost a table rebuild for no gain - the behaviour lives in
#: ``Database.get_feed_page``/``Database.set_feed_page``.
CREATE_POPULAR_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS popular_state (
    user_id INTEGER PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    step INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_USER_VIEW_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS user_view_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    product_id TEXT NOT NULL,
    viewed_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- One row per (user, product): re-showing a card must not duplicate its
    -- history, and the only question this table answers is this very pair.
    UNIQUE (user_id, product_id)
);
"""

#: Persistent key/value configuration the bot reads at runtime.
#:
#: This is the operational half of the reusable sales section (see
#: ``config.sales``): whether an event is enabled and its affiliate
#: destination URL live here as rows, so an administrator can flip them with a
#: command-line tool (``manage_sales.py``) WITHOUT editing source code,
#: redeploying or restarting the bot. It is a general-purpose store kept in the
#: SAME database the bot already uses - no second database, no new dependency.
#:
#: ``INSERT ... ON CONFLICT(key) DO UPDATE`` (see ``Database.set_config``)
#: makes every write an idempotent upsert. A missing row simply means "not
#: configured", which every reader treats as the safe default (disabled / no
#: URL) - so a wiped or brand-new database never exposes the hidden section.
CREATE_BOT_CONFIG_TABLE = """
CREATE TABLE IF NOT EXISTS bot_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

ALL_TABLES = (
    CREATE_USERS_TABLE,
    CREATE_SEARCH_HISTORY_TABLE,
    CREATE_FAVORITES_TABLE,
    CREATE_SAVED_PRODUCTS_TABLE,
    CREATE_TRACKED_PRODUCTS_TABLE,
    CREATE_PRICE_HISTORY_TABLE,
    CREATE_ANALYTICS_EVENTS_TABLE,
    CREATE_POPULAR_SHOWN_TABLE,
    CREATE_POPULAR_STATE_TABLE,
    CREATE_USER_VIEW_HISTORY_TABLE,
    CREATE_BOT_CONFIG_TABLE,
)

#: Indexes added together with the tables. Created after the tables in
#: ``Database.initialize`` so the schema stays one logical unit.
ALL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_search_history_user "
    "ON search_history (user_id, id DESC);",
    "CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites (user_id, id DESC);",
    "CREATE INDEX IF NOT EXISTS idx_saved_products_user "
    "ON saved_products (user_id, id DESC);",
    "CREATE INDEX IF NOT EXISTS idx_tracked_products_active "
    "ON tracked_products (is_active, user_id);",
    "CREATE INDEX IF NOT EXISTS idx_price_history_tracked "
    "ON price_history (tracked_product_id, id DESC);",
    # Analytics is read by period (created_at) and always grouped by the
    # event type, which is exactly what the admin panel asks for.
    "CREATE INDEX IF NOT EXISTS idx_analytics_events_created "
    "ON analytics_events (created_at);",
    "CREATE INDEX IF NOT EXISTS idx_analytics_events_type "
    "ON analytics_events (event_type, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_analytics_events_user "
    "ON analytics_events (user_id, id DESC);",
    # The «✨ Находки» feed asks one question per open: "what has THIS
    # user already seen?" - always scoped to a single user.
    "CREATE INDEX IF NOT EXISTS idx_popular_shown_user "
    "ON popular_shown (user_id, id DESC);",
    # The curation layer asks the same question on every feed open ("has this
    # user already met this product?"), always for ONE user and always about
    # the newest rows, which is exactly this index.
    "CREATE INDEX IF NOT EXISTS idx_user_view_history_user "
    "ON user_view_history (user_id, id DESC);",
)
