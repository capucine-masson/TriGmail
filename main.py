import asyncio
import sys
from datetime import date
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

import db
import llm
from gmail_mcp import GmailMCP

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
    try:
        yield
    finally:
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


ANALYZE_COUNT = 20
PARALLEL = 5
CHAT_RESULTS = 5
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


class ApplyIn(BaseModel):
    message_ids: list[str] = Field(max_length=100)

    @field_validator("message_ids")
    @classmethod
    def ids_ok(cls, values: list[str]) -> list[str]:
        if not all(v.isalnum() and len(v) <= 64 for v in values):
            raise ValueError("Identifiant de mail invalide")
        return values


def error_text(exc: Exception) -> str:
    if isinstance(exc, llm.LLMError):
        return str(exc)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "Délai dépassé."
    return "Erreur lors du traitement de ce mail."


def gmail_error(exc: Exception) -> HTTPException:
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
    return await db.get_folder(conn, folder_id)


@app.delete("/api/folders/{folder_id}", status_code=204)
async def delete_folder(request: Request, folder_id: int):
    if not await db.delete_folder(request.app.state.db, folder_id):
        raise HTTPException(404, "Dossier introuvable.")


async def analyze_one(request: Request, sem, mail: dict, folders: list[dict], labeled: dict) -> dict:
    """Classe un mail (aperçu). Une erreur ici ne doit jamais bloquer les autres mails."""
    result = {
        "message_id": mail["id"],
        "from": mail.get("from", ""),
        "subject": mail.get("subject", ""),
        "date": mail.get("date", ""),
        "status": "error",
        "folder_id": None,
        "folder_name": None,
        "confidence": None,
        "error": None,
    }
    names = {f["id"]: f["name"] for f in folders}
    conn = request.app.state.db
    try:
        if mail["id"] in labeled:  # déjà dans un dossier de l'app : on ne reclasse pas
            folder_id = labeled[mail["id"]]
            await db.save_classification(conn, mail["id"], folder_id, None, True)
            result.update(status="already", folder_id=folder_id, folder_name=names[folder_id])
            return result
        async with sem:
            full = await request.app.state.mcp.read_mail(mail["id"])
            full["from"] = full["from"] or result["from"]
            full["subject"] = full["subject"] or result["subject"]
            verdict = await llm.classify_mail(full, folders)
        folder_id, confidence = verdict["folder_id"], verdict["confidence"]
        await db.save_classification(conn, mail["id"], folder_id, confidence, False)
        if folder_id is None:
            result["status"] = "unclassified"
        else:
            confident = confidence >= llm.CONFIDENCE_THRESHOLD
            result.update(
                status="proposed" if confident else "low",
                folder_id=folder_id,
                folder_name=names[folder_id],
                confidence=confidence,
            )
    except Exception as exc:
        result["error"] = error_text(exc)
    return result


@app.post("/api/analyze")
async def analyze(request: Request):
    """Aperçu du tri des derniers mails (envoyés et reçus) : rien n'est modifié dans Gmail."""
    conn = request.app.state.db
    mcp = mcp_or_503(request)
    folders = await db.list_folders(conn)
    folders = [f for f in folders if f["gmail_label_id"]]
    if not folders:
        raise HTTPException(400, "Crée au moins un dossier avant d'analyser tes mails.")
    if busy_lock.locked():
        raise HTTPException(409, "Une opération est déjà en cours.")
    async with busy_lock:
        try:
            mails = await mcp.search_mails("in:inbox OR in:sent", ANALYZE_COUNT)
            labeled: dict[str, int] = {}
            for folder in folders:
                for message_id in await mcp.label_message_ids(folder["name"]):
                    labeled[message_id] = folder["id"]
        except Exception as exc:
            raise gmail_error(exc) from exc
        sem = asyncio.Semaphore(PARALLEL)
        results = await asyncio.gather(
            *(analyze_one(request, sem, m, folders, labeled) for m in mails)
        )
    return {"results": results, "threshold": llm.CONFIDENCE_THRESHOLD}


@app.post("/api/apply")
async def apply_labels(request: Request, body: ApplyIn):
    """Pose les libellés des classements validés (confiance suffisante) parmi message_ids."""
    conn = request.app.state.db
    mcp = mcp_or_503(request)
    if busy_lock.locked():
        raise HTTPException(409, "Une opération est déjà en cours.")
    async with busy_lock:
        pending = await db.get_pending(conn, body.message_ids, llm.CONFIDENCE_THRESHOLD)
        sem = asyncio.Semaphore(PARALLEL)

        async def apply_one(item: dict) -> dict:
            try:
                async with sem:
                    await mcp.add_label(item["message_id"], item["gmail_label_id"])
                await db.mark_applied(conn, item["message_id"])
                return {"message_id": item["message_id"], "ok": True, "error": None}
            except Exception as exc:
                return {"message_id": item["message_id"], "ok": False, "error": error_text(exc)}

        results = await asyncio.gather(*(apply_one(item) for item in pending))
    return {"results": results}


@app.post("/api/chat")
async def chat(request: Request, body: ChatIn):
    """Retrouve un mail : question -> requête Gmail -> recherche -> réponse. Lecture seule."""
    mcp = mcp_or_503(request)
    try:
        query = await llm.build_search_query(body.question, date.today().isoformat())
        found = await mcp.search_mails(query, CHAT_RESULTS)
        if not found:
            return {
                "answer": "Je n'ai trouvé aucun mail correspondant à ta question.",
                "query": query,
                "sources": [],
            }
        sem = asyncio.Semaphore(PARALLEL)

        async def read_one(mail: dict) -> dict:
            try:
                async with sem:
                    full = await mcp.read_mail(mail["id"])
                mail["body"] = full["body"]
            except Exception:
                mail["body"] = ""
            return mail

        mails = await asyncio.gather(*(read_one(m) for m in found))
        answer = await llm.answer_question(body.question, mails)
    except llm.LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise gmail_error(exc) from exc
    sources = [{"from": m["from"], "date": m["date"], "subject": m["subject"]} for m in mails]
    return {"answer": answer, "query": query, "sources": sources}
