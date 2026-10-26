import asyncio
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

import db
import llm
from gmail_mcp import GmailMCP, QuotaExceeded, group_senders, label_query, sender_address

load_dotenv()

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Le client MCP lance npx en sous-processus : il faut la boucle Proactor sous Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await db.connect()
    await db.init_db(app.state.db)
    app.state.mcp = GmailMCP()
    try:
        await app.state.mcp.start()
        print(f"[MCP] connecté ({len(await app.state.mcp.list_tools())} outils)")
    except Exception as exc:  # l'app reste en vie : les routes Gmail renverront une erreur claire
        print(f"[MCP] échec au démarrage : {type(exc).__name__}: {exc}")
    app.state.auto = {"questions": {}, "labeled": 0, "last_check": None}
    auto_task = asyncio.create_task(auto_sort_loop(app))
    try:
        yield
    finally:
        auto_task.cancel()
        await asyncio.gather(auto_task, return_exceptions=True)
        await app.state.mcp.stop()
        await app.state.db.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=500)

    @field_validator("name", "description")
    @classmethod
    def clean(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Champ vide")
        return value

    @field_validator("name")
    @classmethod
    def no_slash(cls, value: str) -> str:
        if "/" in value or '"' in value or not value.isprintable():
            raise ValueError("Le nom ne peut pas contenir « / », de guillemets ni de caractères spéciaux")
        return value


# Mails de l'onglet Principal reçus (ni envoyés ni brouillons) : les promos et newsletters
# n'ont rien à faire dans un dossier « perso » et coûteraient un appel au modèle chacun.
SORT_BASE_QUERY = "category:primary -in:sent -in:drafts"
AUTO_INTERVAL = 120  # secondes entre deux relevés des nouveaux mails
AUTO_BATCH = 15
AUTO_PERIOD = "1d"
SORT_BATCH = 8  # mails examinés par appel (petit : Groq limite le débit de tokens, et l'écran se met à jour à chaque lot)
SORT_PARALLEL = 2
SORT_EXCERPT_CHARS = 600
AUTO_THRESHOLD = 0.8  # au-dessus : libellé posé sans demander
ASK_THRESHOLD = 0.4  # entre les deux : on pose la question ; en dessous : on laisse le mail
SUGGEST_SCAN = 60  # mails récents examinés pour proposer de nouveaux dossiers
SUGGEST_SHOWN = 4  # exemples d'objets montrés par suggestion
PARALLEL = 5
CHAT_RESULTS = 5  # mails affichés au maximum
CHAT_READ = 6  # mails lus puis classés par le modèle
CHAT_PER_QUERY = 4
CHAT_BODY_CHARS = 1500
CHAT_FALLBACK_SCAN = 200  # mails récents dont on compare les expéditeurs (2e passage, fautes de frappe)
CHAT_FALLBACK_SENDERS = 3
SENDERS_CACHE_SECONDS = 1800
CHAT_MIN_SCORE = 0.25
CHAT_SURE_SCORE = 0.8
CHAT_SURE_GAP = 0.15  # écart minimal avec le 2e pour annoncer « le bon mail »
# Une seule analyse/application à la fois (anti double-clic côté serveur).
busy_lock = asyncio.Lock()


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)

    @field_validator("question")
    @classmethod
    def clean(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Question vide")
        return value


class ImproveIn(BaseModel):
    name: str = Field(default="", max_length=50)
    description: str = Field(min_length=1, max_length=500)

    @field_validator("name", "description")
    @classmethod
    def clean(cls, value: str) -> str:
        return value.strip()


class SortIn(BaseModel):
    before: int | None = Field(default=None, ge=0)  # curseur : ne regarder que les mails plus anciens
    period: Literal["1d", "7d", "1m", "3m"] = "3m"  # valeur de newer_than: choisie dans l'interface


class AutoIn(BaseModel):
    enabled: bool


class AnswerIn(BaseModel):
    message_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9]+$")
    folder_id: int
    yes: bool


def error_text(exc: Exception) -> str:
    if isinstance(exc, llm.LLMError):
        return str(exc)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "Délai dépassé."
    return "Erreur lors du traitement de ce mail."


def gmail_error(exc: Exception) -> HTTPException:
    print(f"[gmail] {type(exc).__name__}: {exc}")  # jamais de secret dans ces messages
    if isinstance(exc, QuotaExceeded):
        return HTTPException(429, "Quota Gmail dépassé : patiente une minute puis réessaie.")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return HTTPException(504, "Gmail met trop de temps à répondre (timeout).")
    return HTTPException(502, "Erreur Gmail : impossible d'effectuer l'opération.")


def mcp_or_503(request: Request) -> GmailMCP:
    mcp = request.app.state.mcp
    if mcp.session is None:
        raise HTTPException(503, "Connexion à Gmail indisponible. Vérifie le serveur MCP puis relance l'app.")
    return mcp


@app.get("/health")
async def health():
    return {"status": "ok", "mcp": app.state.mcp.session is not None}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/folders")
async def get_folders(request: Request):
    return await db.list_folders(request.app.state.db)


@app.post("/api/folders", status_code=201)
async def post_folder(request: Request, body: FolderIn):
    conn = request.app.state.db
    mcp = mcp_or_503(request)
    if await db.folder_name_taken(conn, body.name):
        raise HTTPException(409, "Un dossier porte déjà ce nom.")
    try:
        label_id = await mcp.ensure_label(body.name)
    except Exception as exc:
        raise gmail_error(exc) from exc
    try:
        folder_id = await db.create_folder(conn, body.name, body.description, label_id)
    except db.FolderExists:
        raise HTTPException(409, "Un dossier porte déjà ce nom.")
    return await db.get_folder(conn, folder_id)


@app.put("/api/folders/{folder_id}")
async def put_folder(request: Request, folder_id: int, body: FolderIn):
    conn = request.app.state.db
    folder = await db.get_folder(conn, folder_id)
    if folder is None:
        raise HTTPException(404, "Dossier introuvable.")
    if body.name != folder["name"]:
        if await db.folder_name_taken(conn, body.name, exclude_id=folder_id):
            raise HTTPException(409, "Un dossier porte déjà ce nom.")
        mcp = mcp_or_503(request)
        try:
            await mcp.rename_label(folder["gmail_label_id"], body.name)
        except Exception as exc:
            raise gmail_error(exc) from exc
    try:
        await db.update_folder(conn, folder_id, body.name, body.description)
    except db.FolderExists:
        raise HTTPException(409, "Un dossier porte déjà ce nom.")
    if body.description != folder["description"]:
        await db.forget_unmatched(conn)  # les mails laissés de côté sont à revoir avec la nouvelle description
    return await db.get_folder(conn, folder_id)


@app.delete("/api/folders/{folder_id}", status_code=204)
async def delete_folder(request: Request, folder_id: int):
    if not await db.delete_folder(request.app.state.db, folder_id):
        raise HTTPException(404, "Dossier introuvable.")


@app.post("/api/folders/improve-description")
async def improve_description(request: Request, body: ImproveIn):
    """Propose une description plus claire (rien n'est enregistré)."""
    if not body.description:
        raise HTTPException(422, "Écris d'abord une description à améliorer.")
    folders = await db.list_folders(request.app.state.db)
    try:
        improved = await llm.improve_description(body.name or "(sans nom)", body.description, folders)
    except Exception as exc:
        raise HTTPException(502, error_text(exc)) from exc
    return {"description": improved}


@app.post("/api/folders/suggest")
async def suggest_folders(request: Request):
    """Propose de nouveaux dossiers d'après les mails récents qui n'ont aucun dossier TriMail."""
    conn = request.app.state.db
    mcp = mcp_or_503(request)
    folders = await db.list_folders(conn)
    if busy_lock.locked():
        raise HTTPException(409, "Une opération est déjà en cours.")
    async with busy_lock:
        try:
            mails = await mcp.search_mails("in:inbox OR in:sent", SUGGEST_SCAN)
            labeled: set[str] = set()
            for folder in folders:
                if folder["gmail_label_id"]:
                    labeled |= await mcp.label_message_ids(folder["name"])
        except Exception as exc:
            raise gmail_error(exc) from exc
        unsorted = [m for m in mails if m["id"] not in labeled]
        if len(unsorted) < 3:
            return {"suggestions": [], "scanned": len(unsorted)}
        try:
            found = await llm.suggest_folders(unsorted, folders)
        except Exception as exc:
            raise HTTPException(502, error_text(exc)) from exc
    suggestions = [
        {
            "name": s["name"],
            "description": s["description"],
            "count": len(s["examples"]),
            "subjects": [unsorted[i]["subject"].strip() or "(sans objet)" for i in s["examples"][:SUGGEST_SHOWN]],
        }
        for s in found
    ]
    return {"suggestions": suggestions, "scanned": len(unsorted)}


def sort_query(folders: list[dict], period: str, before: int | None = None) -> str:
    """Requête Gmail des mails à trier. Ceux déjà rangés par TriMail sont exclus par Gmail lui-même."""
    query = f"{SORT_BASE_QUERY} newer_than:{period}"
    query += "".join(f" -label:{label_query(f['name'])}" for f in folders)
    if before is not None:
        query += f" before:{before}"
    return query


def mail_timestamp(raw: str) -> int | None:
    try:
        return int(parsedate_to_datetime(raw).timestamp())
    except (TypeError, ValueError, IndexError):
        return None


async def sort_one(state, sem, mail: dict, folders: list[dict], rules: dict) -> dict:
    """Trie un mail : libellé posé si on est sûr, question si on hésite. Une erreur ici ne bloque pas les autres."""
    address = sender_address(mail.get("from", ""))
    result = {
        "message_id": mail["id"],
        "from": mail.get("from", ""),
        "subject": mail.get("subject", ""),
        "address": address,
        "status": "skipped",
        "folder_id": None,
        "folder_name": None,
        "confidence": None,
        "error": None,
    }
    conn, mcp = state.db, state.mcp
    known = rules.get(address, {})
    try:
        sure = next((f for f in folders if known.get(f["id"]) is True), None)  # déjà répondu « oui »
        if sure is None:
            candidates = [f for f in folders if known.get(f["id"]) is not False]
            if not candidates:  # déjà répondu « non » partout
                await db.save_classification(conn, mail["id"], None, None, False)
                return result
            async with sem:
                full = await mcp.read_mail(mail["id"], SORT_EXCERPT_CHARS)
                full["from"] = full["from"] or result["from"]
                full["subject"] = full["subject"] or result["subject"]
                verdict = await llm.classify_mail(full, candidates)
            folder = next((f for f in candidates if f["id"] == verdict["folder_id"]), None)
            confidence = verdict["confidence"]
            if folder is None or confidence < ASK_THRESHOLD:
                await db.save_classification(conn, mail["id"], None, confidence, False)
                return result
            result.update(folder_id=folder["id"], folder_name=folder["name"], confidence=confidence)
            if confidence < AUTO_THRESHOLD:
                await db.save_classification(conn, mail["id"], folder["id"], confidence, False)
                result["status"] = "ask"
                return result
        else:
            folder = sure
            result.update(folder_id=folder["id"], folder_name=folder["name"], confidence=1.0)
        async with sem:
            await mcp.add_label(mail["id"], folder["gmail_label_id"])
        await db.save_classification(conn, mail["id"], folder["id"], result["confidence"], True)
        result["status"] = "labeled"
    except Exception as exc:
        result.update(status="error", error=error_text(exc))
    return result


@app.post("/api/sort")
async def sort_mailbox(request: Request, body: SortIn):
    """Trie un lot de mails (du plus récent au plus ancien) ; l'interface rappelle avec `next_before`."""
    conn = request.app.state.db
    mcp = mcp_or_503(request)
    folders = [f for f in await db.list_folders(conn) if f["gmail_label_id"]]
    if not folders:
        raise HTTPException(400, "Crée au moins un dossier avant de trier tes mails.")
    if busy_lock.locked():
        raise HTTPException(409, "Une opération est déjà en cours.")
    async with busy_lock:
        # Les mails déjà rangés par TriMail sont exclus par Gmail lui-même.
        query = sort_query(folders, body.period, body.before)
        try:
            mails = await mcp.search_mails(query, SORT_BATCH)
        except Exception as exc:
            raise gmail_error(exc) from exc
        done = await db.get_done(conn, [m["id"] for m in mails])
        rules = await db.get_rules(conn)
        sem = asyncio.Semaphore(SORT_PARALLEL)
        work = asyncio.gather(
            *(sort_one(request.app.state, sem, m, folders, rules) for m in mails if m["id"] not in done)
        )
        # « Arrêter » coupe la connexion : on abandonne aussitôt le lot en cours.
        while True:
            try:
                results = await asyncio.wait_for(asyncio.shield(work), 0.5)
                break
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    raise HTTPException(499, "Tri arrêté.")
    stamps = [t for t in (mail_timestamp(m["date"]) for m in mails) if t is not None]
    more = len(mails) >= SORT_BATCH and bool(stamps)
    return {
        "scanned": len(mails),
        "results": [r for r in results if r["status"] != "skipped"],
        "next_before": min(stamps) if more else None,
    }


@app.post("/api/answer")
async def answer_question(request: Request, body: AnswerIn):
    """Réponse à « ce mail va-t-il dans ce dossier ? » : mémorisée pour cet expéditeur."""
    conn = request.app.state.db
    mcp = mcp_or_503(request)
    folder = await db.get_folder(conn, body.folder_id)
    if folder is None or not folder["gmail_label_id"]:
        raise HTTPException(404, "Dossier introuvable.")
    try:
        address = sender_address((await mcp.read_mail(body.message_id, 1))["from"])
        if body.yes:
            await mcp.add_label(body.message_id, folder["gmail_label_id"])
    except Exception as exc:
        raise gmail_error(exc) from exc
    if body.yes:
        await db.save_classification(conn, body.message_id, folder["id"], 1.0, True)
    else:
        await db.save_classification(conn, body.message_id, None, None, True)
    if address:
        await db.set_rule(conn, address, folder["id"], body.yes)
    request.app.state.auto["questions"].pop(body.message_id, None)
    return {"address": address}


async def auto_sort_once(state) -> None:
    """Trie les mails du dernier jour pas encore vus (les nouveaux arrivés depuis le dernier relevé)."""
    folders = [f for f in await db.list_folders(state.db) if f["gmail_label_id"]]
    if not folders:
        return
    mails = await state.mcp.search_mails(sort_query(folders, AUTO_PERIOD), AUTO_BATCH)
    done = await db.get_done(state.db, [m["id"] for m in mails])
    todo = [m for m in mails if m["id"] not in done and m["id"] not in state.auto["questions"]]
    rules = await db.get_rules(state.db)
    sem = asyncio.Semaphore(SORT_PARALLEL)
    for r in await asyncio.gather(*(sort_one(state, sem, m, folders, rules) for m in todo)):
        if r["status"] == "labeled":
            state.auto["labeled"] += 1
        elif r["status"] == "ask":
            state.auto["questions"][r["message_id"]] = r
    state.auto["last_check"] = datetime.now().isoformat(timespec="seconds")


async def auto_sort_loop(app: FastAPI) -> None:
    """Relève les nouveaux mails toutes les AUTO_INTERVAL secondes tant que l'app tourne."""
    await asyncio.sleep(20)
    while True:
        try:
            enabled = await db.get_setting(app.state.db, "auto_sort", "1") == "1"
            if enabled and app.state.mcp.session is not None and not busy_lock.locked():
                async with busy_lock:
                    await auto_sort_once(app.state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # quota Gmail, limite Groq... : on réessaie au prochain passage
            print(f"[auto] {type(exc).__name__}: {exc}")
        await asyncio.sleep(AUTO_INTERVAL)


@app.get("/api/auto")
async def get_auto(request: Request):
    auto = request.app.state.auto
    return {
        "enabled": await db.get_setting(request.app.state.db, "auto_sort", "1") == "1",
        "last_check": auto["last_check"],
        "labeled": auto["labeled"],
        "questions": list(auto["questions"].values()),
    }


@app.post("/api/auto")
async def set_auto(request: Request, body: AutoIn):
    await db.set_setting(request.app.state.db, "auto_sort", "1" if body.enabled else "0")
    return {"enabled": body.enabled}


async def recent_senders(request: Request) -> list[dict]:
    """Expéditeurs des derniers mails, gardés en mémoire (le scan consomme du quota Gmail)."""
    cache = getattr(request.app.state, "senders_cache", None)
    if cache and time.monotonic() - cache[0] < SENDERS_CACHE_SECONDS:
        return cache[1]
    recent = await request.app.state.mcp.search_mails("in:inbox OR in:sent", CHAT_FALLBACK_SCAN)
    senders = group_senders(recent)
    request.app.state.senders_cache = (time.monotonic(), senders)
    return senders


@app.post("/api/chat")
async def chat(request: Request, body: ChatIn):
    """Retrouve un mail : question -> requêtes Gmail -> recherche -> classement. Lecture seule."""
    mcp = mcp_or_503(request)
    today = date.today().isoformat()
    sem = asyncio.Semaphore(PARALLEL)

    async def read_one(mail: dict) -> dict:
        if "body" in mail:  # déjà lu au premier passage
            return mail
        try:
            async with sem:
                full = await mcp.read_mail(mail["id"], CHAT_BODY_CHARS)
            mail["body"] = full["body"]
        except Exception:
            mail["body"] = ""
        return mail

    async def read_and_rank(found: list[dict]) -> tuple[list[dict], list[dict]]:
        mails = list(await asyncio.gather(*(read_one(m) for m in found)))
        ranked = await llm.rank_mails(body.question, mails, today, CHAT_RESULTS)
        return mails, [r for r in ranked if r["score"] >= CHAT_MIN_SCORE]

    try:
        queries = await llm.build_search_queries(body.question, today)
        per_query = [await mcp.search_mails(q, CHAT_PER_QUERY) for q in queries]
        # Meilleurs résultats de chaque requête en alternance : une requête large ne prend pas toute la place.
        found: list[dict] = []
        seen: set[str] = set()
        for rank in range(CHAT_PER_QUERY):
            for results in per_query:
                if rank < len(results) and results[rank]["id"] not in seen:
                    seen.add(results[rank]["id"])
                    found.append(results[rank])
        mails, ranked = await read_and_rank(found[:CHAT_READ]) if found else ([], [])

        if not ranked or ranked[0]["score"] < CHAT_SURE_SCORE:
            # Second passage tolérant aux fautes de frappe (ex. « hacco » pour « Hacoo ») :
            # on compare la question aux expéditeurs récents et on ajoute leurs mails.
            senders = await recent_senders(request)
            picked = await llm.pick_senders(body.question, senders, CHAT_FALLBACK_SENDERS)
            known = {mail["id"] for mail in mails}
            extra: list[dict] = []
            for i in picked:
                for mail in await mcp.search_mails(f"from:{senders[i]['address']}", CHAT_PER_QUERY):
                    if mail["id"] not in known:
                        known.add(mail["id"])
                        extra.append(mail)
            if extra:
                mails, ranked = await read_and_rank((mails + extra)[:CHAT_READ + CHAT_PER_QUERY])
    except llm.LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise gmail_error(exc) from exc

    matches = [
        {
            "from": mails[r["index"]]["from"],
            "subject": mails[r["index"]]["subject"].strip(),
            "date": mails[r["index"]]["date"],
            "body": mails[r["index"]].get("body", ""),
            "score": r["score"],
            "reason": r["reason"],
        }
        for r in ranked
    ]
    if not matches:
        mode = "none"
    elif matches[0]["score"] >= CHAT_SURE_SCORE and (
        len(matches) == 1 or matches[0]["score"] - matches[1]["score"] >= CHAT_SURE_GAP
    ):
        mode, matches = "sure", matches[:1]
    else:
        mode = "list"
    return {"mode": mode, "matches": matches, "queries": queries}
