from pathlib import Path

import aiosqlite

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "trimail.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"


async def connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    return conn


async def init_db(conn: aiosqlite.Connection) -> None:
    """Exécute schema.sql si la base est vide (aucune table)."""
    async with conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
    ) as cur:
        (count,) = await cur.fetchone()
    if count == 0:
        await conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await conn.commit()


class FolderExists(Exception):
    pass


async def list_folders(conn: aiosqlite.Connection) -> list[dict]:
    async with conn.execute(
        "SELECT id, name, description, gmail_label_id, created_at FROM folders ORDER BY name COLLATE NOCASE"
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def get_folder(conn: aiosqlite.Connection, folder_id: int) -> dict | None:
    async with conn.execute(
        "SELECT id, name, description, gmail_label_id, created_at FROM folders WHERE id = ?",
        (folder_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def folder_name_taken(
    conn: aiosqlite.Connection, name: str, exclude_id: int | None = None
) -> bool:
    async with conn.execute(
        "SELECT 1 FROM folders WHERE name = ? COLLATE NOCASE AND id != ?",
        (name, exclude_id if exclude_id is not None else -1),
    ) as cur:
        return await cur.fetchone() is not None


async def create_folder(
    conn: aiosqlite.Connection, name: str, description: str, gmail_label_id: str
) -> int:
    try:
        cur = await conn.execute(
            "INSERT INTO folders (name, description, gmail_label_id, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (name, description, gmail_label_id),
        )
        await conn.commit()
    except aiosqlite.IntegrityError as exc:
        raise FolderExists(name) from exc
    return cur.lastrowid


async def update_folder(
    conn: aiosqlite.Connection, folder_id: int, name: str, description: str
) -> None:
    try:
        await conn.execute(
            "UPDATE folders SET name = ?, description = ? WHERE id = ?",
            (name, description, folder_id),
        )
        await conn.commit()
    except aiosqlite.IntegrityError as exc:
        raise FolderExists(name) from exc


async def delete_folder(conn: aiosqlite.Connection, folder_id: int) -> bool:
    """Supprime le dossier en base uniquement : le libellé Gmail est conservé."""
    cur = await conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    await conn.commit()
    return cur.rowcount > 0


async def save_classification(
    conn: aiosqlite.Connection,
    message_id: str,
    folder_id: int | None,
    confidence: float | None,
    applied: bool,
) -> None:
    """Enregistre le tri d'un mail (jamais son contenu). Une relance remplace la ligne."""
    await conn.execute(
        "INSERT INTO classifications (message_id, folder_id, confidence, applied, created_at) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(message_id) DO UPDATE SET folder_id = excluded.folder_id, "
        "confidence = excluded.confidence, applied = excluded.applied, "
        "created_at = excluded.created_at",
        (message_id, folder_id, confidence, int(applied)),
    )
    await conn.commit()


async def get_pending(
    conn: aiosqlite.Connection, message_ids: list[str], min_confidence: float
) -> list[dict]:
    """Classements à appliquer parmi message_ids : non appliqués, confiance suffisante."""
    pending = []
    for message_id in message_ids:
        async with conn.execute(
            "SELECT c.message_id, f.gmail_label_id FROM classifications c "
            "JOIN folders f ON f.id = c.folder_id "
            "WHERE c.message_id = ? AND c.applied = 0 AND c.confidence >= ? "
            "AND f.gmail_label_id IS NOT NULL",
            (message_id, min_confidence),
        ) as cur:
            row = await cur.fetchone()
        if row:
            pending.append(dict(row))
    return pending


async def mark_applied(conn: aiosqlite.Connection, message_id: str) -> None:
    await conn.execute(
        "UPDATE classifications SET applied = 1 WHERE message_id = ?", (message_id,)
    )
    await conn.commit()
