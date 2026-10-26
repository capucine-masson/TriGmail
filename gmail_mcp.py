import asyncio
import os
import re
from contextlib import AsyncExitStack
from html import unescape

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


class QuotaExceeded(RuntimeError):
    """Quota de l'API Gmail dépassé (limite par minute)."""


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
            if "Quota exceeded" in text:
                raise QuotaExceeded(f"Quota Gmail dépassé sur {name}")
            raise RuntimeError(f"Erreur MCP sur {name} : {text[:200]}")
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
        mails = await self.search_mails(f"label:{label_query(folder_name)}", 100)
        return {m["id"] for m in mails}

    async def read_mail(self, message_id: str, max_chars: int = MAIL_EXCERPT_CHARS) -> dict:
        text = await self.call("read_email", {"messageId": message_id})
        return parse_mail(text, max_chars)


def label_query(folder_name: str) -> str:
    """Libellé TriMail/<nom> prêt à suivre « label: » dans une requête Gmail."""
    return '"' + LABEL_PREFIX + folder_name.replace('"', "") + '"'


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
            mails.append({"subject": "", "from": "", "date": "", **mail})
    return mails


HTML_NOISE_RE = re.compile(r"(?is)<(style|script|head)\b.*?</\1\s*>|<!--.*?-->|<!DOCTYPE[^>]*>?")
TAG_RE = re.compile(r"<[^>]+>")
HTML_NOTE_RE = re.compile(r"^\[Note: This email is HTML-formatted[^\]]*\]\s*")


def html_to_text(body: str) -> str:
    """Garde le texte lisible d'un mail HTML (sans CSS, scripts ni balises)."""
    body = HTML_NOTE_RE.sub("", body.lstrip())
    if "<" not in body:
        return body
    body = HTML_NOISE_RE.sub(" ", body)
    return unescape(TAG_RE.sub(" ", body))


def parse_mail(text: str, max_chars: int = MAIL_EXCERPT_CHARS) -> dict:
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
    body = html_to_text("\n".join(lines[i:]))
    body = URL_RE.sub("[lien]", body)
    mail["body"] = re.sub(r"\s+", " ", body).strip()[:max_chars]
    return mail


ADDRESS_RE = re.compile(r"<([^<>@\s]+@[^<>\s]+)>|([^<>@\s]+@[^<>\s]+)")


def sender_address(raw: str) -> str | None:
    """Adresse e-mail (en minuscules) d'un en-tête From, ou None."""
    found = ADDRESS_RE.search(raw)
    return (found.group(1) or found.group(2)).lower() if found else None


def group_senders(mails: list[dict]) -> list[dict]:
    """Regroupe des mails par adresse d'expéditeur : [{name, address, count, subject}]."""
    groups: dict[str, dict] = {}
    for mail in mails:
        address = sender_address(mail["from"])
        if not address:
            continue
        name = re.sub(r"\s*<.*>\s*$", "", mail["from"]).strip().strip('"') or address
        group = groups.setdefault(
            address, {"name": name, "address": address, "count": 0, "subject": mail["subject"].strip()}
        )
        group["count"] += 1
    return list(groups.values())
