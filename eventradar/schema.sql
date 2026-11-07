PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
 timezone TEXT NOT NULL, report_time TEXT NOT NULL DEFAULT '09:00',
 city TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
 routing INTEGER NOT NULL DEFAULT 0,
 enabled INTEGER NOT NULL DEFAULT 1, last_report TEXT, last_manual REAL NOT NULL DEFAULT 0,
 last_report_at TEXT, last_report_attempt TEXT
);
CREATE TABLE IF NOT EXISTS channels (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 title TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS subscriptions (
 user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
 channel_id INTEGER REFERENCES channels(id) ON DELETE CASCADE,
 PRIMARY KEY(user_id, channel_id)
);
CREATE TABLE IF NOT EXISTS posts (
 id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id),
 message_id INTEGER NOT NULL, published_at TEXT NOT NULL, text TEXT NOT NULL,
 content_hash TEXT NOT NULL, processed_hash TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 retry_after REAL NOT NULL DEFAULT 0, UNIQUE(channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS items (
 id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 UNIQUE(post_id, version, ordinal)
);
CREATE TABLE IF NOT EXISTS decisions (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL, relevant INTEGER NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(user_id, item_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL, sent_at TEXT NOT NULL,
 PRIMARY KEY(user_id, item_id)
);
CREATE TABLE IF NOT EXISTS blackouts (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 weekday INTEGER NOT NULL CHECK(weekday BETWEEN 1 AND 7), start_time TEXT NOT NULL,
 end_time TEXT NOT NULL, from_month INTEGER NOT NULL, to_month INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 item_id INTEGER NOT NULL REFERENCES items(id), review TEXT NOT NULL DEFAULT '', PRIMARY KEY(user_id,item_id)
);
CREATE TABLE IF NOT EXISTS ai_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS posts_pending ON posts(retry_after, id);
CREATE INDEX IF NOT EXISTS subscriptions_channel ON subscriptions(channel_id);
CREATE TABLE IF NOT EXISTS travel_estimates (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL, payload TEXT NOT NULL, expires_at REAL NOT NULL,
 PRIMARY KEY(user_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS report_analyses (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(user_id,post_id)
);
CREATE INDEX IF NOT EXISTS posts_period ON posts(published_at,channel_id);
CREATE TABLE IF NOT EXISTS report_snapshots (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 period_end TEXT NOT NULL, timezone TEXT NOT NULL, cards TEXT NOT NULL, summary TEXT NOT NULL,
 UNIQUE(user_id,period_end)
);
CREATE TABLE IF NOT EXISTS report_publications (
 snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id) ON DELETE CASCADE,
 chat_id INTEGER NOT NULL, next_card INTEGER NOT NULL DEFAULT 0, photos INTEGER NOT NULL DEFAULT 0,
 summary_sent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(snapshot_id,chat_id)
);
CREATE TABLE IF NOT EXISTS publication_candidates (
 owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 chat_id INTEGER NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(owner_id,chat_id)
);
PRAGMA user_version=5;
