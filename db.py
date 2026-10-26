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
    """Exécute schema.sql (CREATE TABLE IF NOT EXISTS : les nouvelles tables arrivent seules)."""
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


async def get_done(conn: aiosqlite.Connection, message_ids: list[str]) -> set[str]:
    """Mails déjà traités : libellé posé, ou examinés sans dossier. Une question en attente
    (dossier proposé, rien appliqué) n'en fait pas partie : elle est reposée au tri suivant."""
    if not message_ids:
        return set()
    marks = ",".join("?" * len(message_ids))
    async with conn.execute(
        f"SELECT message_id FROM classifications WHERE message_id IN ({marks}) "
        "AND (applied = 1 OR folder_id IS NULL)",
        message_ids,
    ) as cur:
        return {row["message_id"] for row in await cur.fetchall()}


async def forget_unmatched(conn: aiosqlite.Connection) -> None:
    """Oublie les mails examinés sans dossier (la description d'un dossier a changé : à revoir)."""
    await conn.execute("DELETE FROM classifications WHERE applied = 0 AND folder_id IS NULL")
    await conn.commit()


async def get_setting(conn: aiosqlite.Connection, key: str, default: str) -> str:
    async with conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else default


async def set_setting(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await conn.commit()


async def get_rules(conn: aiosqlite.Connection) -> dict[str, dict[int, bool]]:
    """Réponses mémorisées : {adresse: {folder_id: l'expéditeur va dans ce dossier ?}}."""
    rules: dict[str, dict[int, bool]] = {}
    async with conn.execute("SELECT address, folder_id, is_match FROM sender_rules") as cur:
        for row in await cur.fetchall():
            rules.setdefault(row["address"], {})[row["folder_id"]] = bool(row["is_match"])
    return rules


async def set_rule(
    conn: aiosqlite.Connection, address: str, folder_id: int, is_match: bool
) -> None:
    await conn.execute(
        "INSERT INTO sender_rules (address, folder_id, is_match) VALUES (?, ?, ?) "
        "ON CONFLICT(address, folder_id) DO UPDATE SET is_match = excluded.is_match",
        (address, folder_id, int(is_match)),
    )
    await conn.commit()
