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

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Réponses de l'utilisateur : cet expéditeur va (1) ou ne va pas (0) dans ce dossier.
CREATE TABLE IF NOT EXISTS sender_rules (
    address TEXT NOT NULL,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    is_match INTEGER NOT NULL,
    PRIMARY KEY (address, folder_id)
);
