CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    gmail_label_id TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS classifications (
    message_id TEXT PRIMARY KEY,
    folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    confidence REAL,
    applied INTEGER DEFAULT 0,
    created_at TEXT
);
