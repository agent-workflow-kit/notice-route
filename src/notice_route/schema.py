"""The ``state.db`` schema: constants and the versioned migrations (no I/O).

``user_version`` is the number of migrations applied. A migration is additive (new tables, new
columns with a default, new indexes) so an older program can read a newer database's tables it
knows; the router still refuses a ``user_version`` above what it was built for (``store.py``).
Every statement is a fixed string: no value from outside ever reaches SQL by formatting.

Row ids are ``AUTOINCREMENT`` (never reused). Which tables hold which rows is
[Files](interface.md#files); their logic is owned by later issues (queue #16, dedup #17,
counters #18).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

# "NRTE": marks the file as ours, so a database of another program is refused, not migrated.
APPLICATION_ID = 0x4E525445

# A step is one SQL statement or a function that backfills data inside the migration's
# transaction. ``executescript`` is never used: it commits.
Step = str | Callable[[sqlite3.Connection], None]
Migration = Sequence[Step]

# Tables ``read_state`` and the store rely on at version 1.
TABLES = ("deliveries", "queue", "dedup", "event_ids", "surfaced", "rate", "meta")

_V1: tuple[str, ...] = (
    """
    CREATE TABLE deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        source TEXT NOT NULL,
        hook TEXT,
        object_id,
        rule TEXT,
        destination TEXT,
        level TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reason TEXT,
        code TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        count INTEGER NOT NULL DEFAULT 1,
        event_id TEXT,
        dedupe TEXT,
        labels TEXT,
        surfaced INTEGER NOT NULL DEFAULT 0 CHECK (surfaced IN (0, 1)),
        deliver_after INTEGER,
        title TEXT,
        title_sha256 TEXT,
        message_sha256 TEXT
    )
    """,
    "CREATE INDEX deliveries_outcome ON deliveries (outcome, id)",
    "CREATE INDEX deliveries_destination ON deliveries (destination, id)",
    """
    CREATE TABLE queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_id INTEGER NOT NULL REFERENCES deliveries (id),
        kind TEXT NOT NULL CHECK (kind IN ('held', 'single')),
        title TEXT,
        message TEXT NOT NULL,
        link TEXT,
        level TEXT NOT NULL,
        source TEXT NOT NULL,
        rule TEXT,
        dedupe TEXT,
        deliver_after INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        claimed_by TEXT,
        claim_expires INTEGER
    )
    """,
    "CREATE INDEX queue_delivery ON queue (delivery_id)",
    "CREATE INDEX queue_due ON queue (deliver_after)",
    # One window per destination and key; the window length is stored and never changed.
    # TODO(#17): the dedup statements (open, replace an ended window, release by delivery_id).
    """
    CREATE TABLE dedup (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        destination TEXT NOT NULL,
        key_digest TEXT NOT NULL,
        window_start INTEGER NOT NULL,
        window_length INTEGER NOT NULL,
        delivery_id INTEGER,
        UNIQUE (destination, key_digest)
    )
    """,
    "CREATE INDEX dedup_delivery ON dedup (delivery_id)",
    # ``hook`` is '' for a channel notice; ``event_id`` is the id's JSON text.
    """
    CREATE TABLE event_ids (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        destination TEXT NOT NULL,
        source TEXT NOT NULL,
        hook TEXT NOT NULL DEFAULT '',
        event_id TEXT NOT NULL,
        repeat_digest TEXT NOT NULL,
        seen_at INTEGER NOT NULL,
        delivery_id INTEGER,
        UNIQUE (destination, source, hook, event_id, repeat_digest)
    )
    """,
    "CREATE INDEX event_ids_delivery ON event_ids (delivery_id)",
    # ``period`` is the UTC day ``YYYY-MM-DD``, or ``YYYY-MM-DDThh`` for the hourly codes.
    # ``delivery_id`` has no foreign key: a deliveries row may be removed after processing.
    """
    CREATE TABLE surfaced (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        destination TEXT NOT NULL,
        code TEXT NOT NULL,
        period TEXT NOT NULL,
        delivery_id INTEGER,
        processed INTEGER NOT NULL DEFAULT 0 CHECK (processed IN (0, 1)),
        UNIQUE (destination, code, period)
    )
    """,
    "CREATE INDEX surfaced_delivery ON surfaced (delivery_id)",
    "CREATE INDEX surfaced_pending ON surfaced (processed, id)",
    # ``hour`` is the UTC epoch second of the hour's start.
    # TODO(#18): the counter statements.
    """
    CREATE TABLE rate (
        destination TEXT NOT NULL,
        hour INTEGER NOT NULL,
        class TEXT NOT NULL CHECK (class IN ('error', 'other')),
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (destination, hour, class)
    ) WITHOUT ROWID
    """,
    # ``value`` has no declared type: epoch seconds or NULL.
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value) WITHOUT ROWID",
    "INSERT INTO meta (key, value) VALUES ('flush_last', NULL), ('flush_env_missing', NULL)",
)

MIGRATIONS: tuple[Migration, ...] = (_V1,)
SCHEMA_VERSION = len(MIGRATIONS)
