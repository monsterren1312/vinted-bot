import sqlite3
import time
from typing import List, Optional, Tuple

FEED_LIMIT = 200  # found listings kept per chat


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS filters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                brand_id INTEGER NOT NULL,
                brand_title TEXT NOT NULL,
                price_from REAL,
                price_to REAL,
                primed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS seen (
                filter_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                PRIMARY KEY (filter_id, item_id)
            );
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                paused INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS found (
                chat_id INTEGER NOT NULL,
                filter_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                title TEXT, price REAL, currency TEXT, brand TEXT, details TEXT,
                url TEXT, photo TEXT,
                seller_login TEXT, seller_feedback INTEGER, seller_reputation REAL,
                found_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, item_id)
            );
            CREATE TABLE IF NOT EXISTS favorites (
                chat_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                title TEXT, price REAL, currency TEXT, brand TEXT, details TEXT,
                url TEXT, photo TEXT,
                seller_login TEXT, seller_feedback INTEGER, seller_reputation REAL,
                found_at INTEGER NOT NULL,
                saved_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, item_id)
            );
            CREATE TABLE IF NOT EXISTS id_clock (
                ts INTEGER NOT NULL,
                max_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocked (
                chat_id INTEGER NOT NULL,
                seller_id INTEGER NOT NULL,
                seller_login TEXT,
                PRIMARY KEY (chat_id, seller_id)
            );
            """
        )
        self._add_column("filters", "enabled", "INTEGER NOT NULL DEFAULT 1")
        # Highest listing id seen; ids grow with creation time, so anything lower is an old,
        # bumped listing resurfacing at the top of "newest first".
        self._add_column("filters", "last_id", "INTEGER")
        self._add_column("chats", "min_feedback", "INTEGER")
        self._add_column("filters", "domain", "TEXT NOT NULL DEFAULT 'www.vinted.de'")
        self._add_column("chats", "domain", "TEXT")
        self._add_column("filters", "keyword", "TEXT")
        self._add_column("filters", "category", "INTEGER")
        self._add_column("filters", "conditions", "TEXT")  # comma-separated status ids
        self._add_column("filters", "sizes", "TEXT")  # comma-separated size ids
        self._add_column("chats", "quiet", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("found", "seller_id", "INTEGER")
        self.db.commit()

    def _add_column(self, table: str, column: str, decl: str):
        cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # --- chats ---

    def chat(self, chat_id: int) -> sqlite3.Row:
        self.db.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
        self.db.commit()
        return self.db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()

    def set_paused(self, chat_id: int, paused: bool):
        self.chat(chat_id)
        self.db.execute("UPDATE chats SET paused = ? WHERE chat_id = ?", (int(paused), chat_id))
        self.db.commit()

    def set_min_feedback(self, chat_id: int, value: int):
        self.chat(chat_id)
        self.db.execute("UPDATE chats SET min_feedback = ? WHERE chat_id = ?", (value, chat_id))
        self.db.commit()

    def set_quiet(self, chat_id: int, quiet: bool):
        self.chat(chat_id)
        self.db.execute("UPDATE chats SET quiet = ? WHERE chat_id = ?", (int(quiet), chat_id))
        self.db.commit()

    def set_chat_domain(self, chat_id: int, domain: str):
        self.chat(chat_id)
        self.db.execute("UPDATE chats SET domain = ? WHERE chat_id = ?", (domain, chat_id))
        self.db.commit()

    # --- filters ---

    def add_filter(
        self, chat_id: int, brand_id: int, brand_title: str,
        price_from: Optional[float], price_to: Optional[float], domain: str,
    ) -> int:
        self.chat(chat_id)
        cur = self.db.execute(
            "INSERT INTO filters (chat_id, brand_id, brand_title, price_from, price_to, domain)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, brand_id, brand_title, price_from, price_to, domain),
        )
        self.db.commit()
        return cur.lastrowid

    def get_filter(self, chat_id: int, filter_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM filters WHERE id = ? AND chat_id = ?", (filter_id, chat_id)
        ).fetchone()

    def filters_for(self, chat_id: int) -> List[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM filters WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()

    def active_filters(self, chat_id: Optional[int] = None) -> List[sqlite3.Row]:
        sql = (
            "SELECT f.*, c.min_feedback, c.quiet FROM filters f JOIN chats c ON c.chat_id = f.chat_id"
            " WHERE c.paused = 0 AND f.enabled = 1"
        )
        args = ()
        if chat_id is not None:
            sql += " AND f.chat_id = ?"
            args = (chat_id,)
        return self.db.execute(sql + " ORDER BY f.id", args).fetchall()

    def set_price(self, chat_id: int, filter_id: int, pf: Optional[float], pt: Optional[float]):
        # New range = new result set: re-prime so old listings aren't sent as "new".
        self.db.execute(
            "UPDATE filters SET price_from = ?, price_to = ?, primed = 0 WHERE id = ? AND chat_id = ?",
            (pf, pt, filter_id, chat_id),
        )
        self.db.execute("DELETE FROM seen WHERE filter_id = ?", (filter_id,))
        self.db.commit()

    def set_filter_domain(self, chat_id: int, filter_id: int, domain: str):
        # Another site = another result set: re-prime like a price change.
        self.db.execute(
            "UPDATE filters SET domain = ?, primed = 0 WHERE id = ? AND chat_id = ?",
            (domain, filter_id, chat_id),
        )
        self.db.execute("DELETE FROM seen WHERE filter_id = ?", (filter_id,))
        self.db.commit()

    def set_option(self, chat_id: int, filter_id: int, column: str, value):
        # Search options change the result set: re-prime like a price change.
        assert column in ("keyword", "category", "conditions", "sizes")
        self.db.execute(
            f"UPDATE filters SET {column} = ?, primed = 0 WHERE id = ? AND chat_id = ?",
            (value, filter_id, chat_id),
        )
        self.db.execute("DELETE FROM seen WHERE filter_id = ?", (filter_id,))
        self.db.commit()

    def set_all_enabled(self, chat_id: int, enabled: bool):
        self.db.execute("UPDATE filters SET enabled = ? WHERE chat_id = ?", (int(enabled), chat_id))
        self.db.commit()

    def delete_all_filters(self, chat_id: int):
        self.db.execute(
            "DELETE FROM seen WHERE filter_id IN (SELECT id FROM filters WHERE chat_id = ?)", (chat_id,)
        )
        self.db.execute("DELETE FROM filters WHERE chat_id = ?", (chat_id,))
        self.db.commit()

    def toggle_filter(self, chat_id: int, filter_id: int):
        self.db.execute(
            "UPDATE filters SET enabled = 1 - enabled WHERE id = ? AND chat_id = ?",
            (filter_id, chat_id),
        )
        self.db.commit()

    def delete_filter(self, chat_id: int, filter_id: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM filters WHERE id = ? AND chat_id = ?", (filter_id, chat_id)
        )
        self.db.execute("DELETE FROM seen WHERE filter_id = ?", (filter_id,))
        self.db.commit()
        return cur.rowcount > 0

    def mark_primed(self, filter_id: int, last_id: int):
        self.db.execute("UPDATE filters SET primed = 1, last_id = ? WHERE id = ?", (last_id, filter_id))
        self.db.commit()

    def set_last_id(self, filter_id: int, last_id: int):
        self.db.execute(
            "UPDATE filters SET last_id = MAX(COALESCE(last_id, 0), ?) WHERE id = ?", (last_id, filter_id)
        )
        self.db.commit()

    # --- seen / found ---

    def unseen(self, filter_id: int, item_ids: List[int]) -> List[int]:
        if not item_ids:
            return []
        marks = ",".join("?" * len(item_ids))
        seen = {
            r[0] for r in self.db.execute(
                f"SELECT item_id FROM seen WHERE filter_id = ? AND item_id IN ({marks})",
                (filter_id, *item_ids),
            )
        }
        return [i for i in item_ids if i not in seen]

    def mark_seen(self, filter_id: int, item_ids: List[int]):
        self.db.executemany(
            "INSERT OR IGNORE INTO seen (filter_id, item_id) VALUES (?, ?)",
            [(filter_id, i) for i in item_ids],
        )
        self.db.commit()

    def add_found(self, chat_id: int, filter_id: int, item, seller) -> bool:
        """Returns False if this listing was already found by another filter."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO found (chat_id, filter_id, item_id, title, price, currency, brand,"
            " details, url, photo, seller_login, seller_feedback, seller_reputation, found_at, seller_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                chat_id, filter_id, item.id, item.title, item.price, item.currency,
                item.brand, item.details, item.url, item.photo,
                seller.login, seller.feedback_count, seller.reputation, int(time.time()), item.user_id,
            ),
        )
        if cur.rowcount == 0:
            return False
        self.db.execute(
            "DELETE FROM found WHERE chat_id = ? AND item_id NOT IN"
            " (SELECT item_id FROM found WHERE chat_id = ? ORDER BY found_at DESC, item_id DESC LIMIT ?)",
            (chat_id, chat_id, FEED_LIMIT),
        )
        self.db.commit()
        return True

    def clear_found(self, chat_id: int):
        self.db.execute("DELETE FROM found WHERE chat_id = ?", (chat_id,))
        self.db.commit()

    def get_found(self, chat_id: int, item_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM found WHERE chat_id = ? AND item_id = ?", (chat_id, item_id)
        ).fetchone()

    # --- id clock: (time, newest listing id) samples used to estimate when a listing was posted ---

    def add_clock(self, ts: int, max_id: int):
        last = self.db.execute("SELECT ts, max_id FROM id_clock ORDER BY ts DESC LIMIT 1").fetchone()
        if last and (max_id <= last["max_id"] or ts - last["ts"] < 60):
            return
        self.db.execute("INSERT INTO id_clock VALUES (?, ?)", (ts, max_id))
        self.db.execute("DELETE FROM id_clock WHERE ts < ?", (ts - 8 * 86400,))
        self.db.commit()

    def clock(self) -> List[Tuple[int, int]]:
        return [(r[0], r[1]) for r in self.db.execute("SELECT ts, max_id FROM id_clock ORDER BY ts")]

    # --- favorites ---

    def save_favorite(self, chat_id: int, d: dict):
        """Save a listing that isn't in the feed (e.g. from a period search)."""
        self.db.execute(
            "INSERT OR IGNORE INTO favorites VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, d["item_id"], d["title"], d["price"], d["currency"], d["brand"], d["details"],
             d["url"], d["photo"], d["seller_login"], d["seller_feedback"], d["seller_reputation"],
             int(d["found_at"]), int(time.time())),
        )
        self.db.commit()

    def toggle_favorite(self, chat_id: int, item_id: int) -> bool:
        """Returns True if the listing is now saved."""
        if self.is_favorite(chat_id, item_id):
            self.db.execute("DELETE FROM favorites WHERE chat_id = ? AND item_id = ?", (chat_id, item_id))
            self.db.commit()
            return False
        self.db.execute(
            "INSERT OR IGNORE INTO favorites SELECT chat_id, item_id, title, price, currency, brand,"
            " details, url, photo, seller_login, seller_feedback, seller_reputation, found_at, ?"
            " FROM found WHERE chat_id = ? AND item_id = ?",
            (int(time.time()), chat_id, item_id),
        )
        self.db.commit()
        return True

    def is_favorite(self, chat_id: int, item_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM favorites WHERE chat_id = ? AND item_id = ?", (chat_id, item_id)
        ).fetchone() is not None

    def favorites_for(self, chat_id: int) -> List[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM favorites WHERE chat_id = ? ORDER BY saved_at DESC", (chat_id,)
        ).fetchall()

    # --- blocked sellers ---

    def block_seller(self, chat_id: int, seller_id: int, login: str):
        self.db.execute("INSERT OR IGNORE INTO blocked VALUES (?, ?, ?)", (chat_id, seller_id, login))
        self.db.execute("DELETE FROM found WHERE chat_id = ? AND seller_id = ?", (chat_id, seller_id))
        self.db.commit()

    def unblock_seller(self, chat_id: int, seller_id: int):
        self.db.execute("DELETE FROM blocked WHERE chat_id = ? AND seller_id = ?", (chat_id, seller_id))
        self.db.commit()

    def blocked_for(self, chat_id: int) -> List[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM blocked WHERE chat_id = ? ORDER BY seller_login", (chat_id,)
        ).fetchall()

    def is_blocked(self, chat_id: int, seller_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM blocked WHERE chat_id = ? AND seller_id = ?", (chat_id, seller_id)
        ).fetchone() is not None

    def found_for(self, chat_id: int, filter_id: Optional[int] = None) -> List[sqlite3.Row]:
        sql, args = "SELECT * FROM found WHERE chat_id = ?", [chat_id]
        if filter_id is not None:
            sql += " AND filter_id = ?"
            args.append(filter_id)
        return self.db.execute(sql + " ORDER BY found_at DESC, item_id DESC", args).fetchall()

    def found_count(self, chat_id: int, filter_id: Optional[int] = None, since: int = 0) -> int:
        sql, args = "SELECT COUNT(*) FROM found WHERE chat_id = ? AND found_at >= ?", [chat_id, since]
        if filter_id is not None:
            sql += " AND filter_id = ?"
            args.append(filter_id)
        return self.db.execute(sql, args).fetchone()[0]
