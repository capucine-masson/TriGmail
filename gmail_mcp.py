import asyncio
import os
import re
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MCP_TIMEOUT = 30  # secondes
MAIL_EXCERPT_CHARS = 1000

# Seuls ces outils peuvent être appelés. Jamais d'envoi, de suppression ni d'archivage.
ALLOWED_TOOLS = {
    "search_emails",
    "read_email",
    "list_email_labels",
    "create_label",
    "get_or_create_label",
    "update_label",  # renommage d'un libellé TriMail/<nom>
    "modify_email",  # contraint par FORBIDDEN_ARGS
}
# modify_email peut retirer des libellés (ex. INBOX = archivage) : on n'autorise que addLabelIds.
# labelIds remplace l'ensemble des libellés, donc il est interdit aussi.
FORBIDDEN_ARGS = {"modify_email": {"removeLabelIds", "labelIds"}}
LABEL_PREFIX = "TriMail/"

URL_RE = re.compile(r"https?://\S+")
HEADER_RE = re.compile(r"^(Thread ID|Subject|From|To|Cc|Bcc|Date): ?(.*)$")


class GmailMCP:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def start(self) -> None:
        command = "npx.cmd" if os.name == "nt" else "npx"
        params = StdioServerParameters(
            command=command,
            args=["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(self.session.initialize(), MCP_TIMEOUT)

    async def stop(self) -> None:
        await self._stack.aclose()
        self.session = None

    async def list_tools(self) -> list[tuple[str, str]]:
        result = await asyncio.wait_for(self.session.list_tools(), MCP_TIMEOUT)
        return [(t.name, (t.description or "").strip()) for t in result.tools]

    async def call(self, name: str, arguments: dict | None = None) -> str:
        arguments = arguments or {}
        if name not in ALLOWED_TOOLS:
            raise PermissionError(f"Outil MCP non autorisé : {name}")
        forbidden = FORBIDDEN_ARGS.get(name, set()) & arguments.keys()
        if forbidden:
            raise PermissionError(f"Argument interdit pour {name} : {sorted(forbidden)}")
        result = await asyncio.wait_for(
            self.session.call_tool(name, arguments), MCP_TIMEOUT
        )
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        # Ce serveur signale souvent ses erreurs par un texte « Error: ... » sans is_error.
        if result.is_error or text.startswith("Error"):
            raise RuntimeError(f"Erreur MCP sur {name}")
        return text

    # --- Libellés ---

    async def ensure_label(self, folder_name: str) -> str:
        """Crée (ou récupère) le libellé TriMail/<nom> et renvoie son id Gmail."""
        text = await self.call("get_or_create_label", {"name": LABEL_PREFIX + folder_name})
        match = re.search(r"ID:\s*(\S+)", text)
        if not match:
            raise RuntimeError("Réponse inattendue du serveur Gmail (id du libellé introuvable)")
        return match.group(1)

    async def rename_label(self, label_id: str, folder_name: str) -> None:
        await self.call("update_label", {"id": label_id, "name": LABEL_PREFIX + folder_name})

    async def add_label(self, message_id: str, label_id: str) -> None:
        """Pose un libellé sur un mail (ajout uniquement, jamais de retrait)."""
        await self.call("modify_email", {"messageId": message_id, "addLabelIds": [label_id]})

    # --- Mails ---

    async def search_mails(self, query: str, max_results: int) -> list[dict]:
        text = await self.call("search_emails", {"query": query, "maxResults": max_results})
        return parse_search(text)

    async def label_message_ids(self, folder_name: str) -> set[str]:
        """Ids des mails portant déjà le libellé TriMail/<nom>."""
        safe = folder_name.replace('"', "")
        mails = await self.search_mails(f'label:"{LABEL_PREFIX}{safe}"', 100)
        return {m["id"] for m in mails}

    async def read_mail(self, message_id: str) -> dict:
        text = await self.call("read_email", {"messageId": message_id})
        return parse_mail(text)


def parse_search(text: str) -> list[dict]:
    """Découpe la réponse texte de search_emails en [{id, subject, from, date}]."""
    mails = []
    for block in text.split(chr(10) * 2):
        mail = {}
        for line in block.splitlines():
            key, _, value = line.partition(": ")
            if key in ("ID", "Subject", "From", "Date"):
                mail[key.lower()] = value
        if "id" in mail:
            mails.append(mail)
    return mails


def parse_mail(text: str) -> dict:
    """Extrait objet, expéditeur, date et un extrait nettoyé du corps (non fiable)."""
    mail = {"subject": "", "from": "", "date": ""}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = HEADER_RE.match(lines[i])
        if match:
            key = match.group(1).lower()
            if key in mail:
                mail[key] = match.group(2)
        elif lines[i].strip():
            break
        i += 1
    body = URL_RE.sub("[lien]", "\n".join(lines[i:]))
    mail["body"] = re.sub(r"\s+", " ", body).strip()[:MAIL_EXCERPT_CHARS]
    return mail
