"use strict";

const $ = (sel) => document.querySelector(sel);
const foldersEl = $("#folders");
const emptyEl = $("#empty");
const form = $("#folder-form");
const submitBtn = $("#f-submit");
const cancelBtn = $("#f-cancel");
let editingId = null;

function toast(message, kind) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast show " + (kind || "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 4000);
}

async function api(method, url, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    throw new Error("Le serveur est injoignable.");
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    let msg = "Erreur inattendue.";
    if (data && typeof data.detail === "string") msg = data.detail;
    else if (data && Array.isArray(data.detail) && data.detail[0]) msg = data.detail[0].msg.replace(/^Value error, /, "");
    throw new Error(msg);
  }
  return data;
}

// Désactive le bouton pendant l'appel (anti double-clic) et affiche un spinner.
async function withBusy(button, fn) {
  if (button.disabled) return;
  button.disabled = true;
  const spinner = document.createElement("span");
  spinner.className = "spinner";
  button.prepend(spinner);
  try {
    return await fn();
  } finally {
    spinner.remove();
    button.disabled = false;
  }
}

function folderCard(folder) {
  const card = document.createElement("article");
  card.className = "card folder";

  const head = document.createElement("div");
  head.className = "folder-head";
  const title = document.createElement("h3");
  title.textContent = folder.name;
  const desc = document.createElement("p");
  desc.textContent = folder.description;

  const actions = document.createElement("div");
  actions.className = "actions";
  const edit = document.createElement("button");
  edit.className = "btn ghost small";
  edit.textContent = "Modifier";
  edit.addEventListener("click", () => startEdit(folder));
  const del = document.createElement("button");
  del.className = "btn danger small";
  del.textContent = "Supprimer";
  del.addEventListener("click", () => removeFolder(folder, del));
  actions.append(edit, del);

  head.append(title, actions);
  card.append(head, desc);
  return card;
}

async function loadFolders() {
  try {
    const folders = await api("GET", "/api/folders");
    foldersEl.replaceChildren(...folders.map(folderCard));
    emptyEl.classList.toggle("hidden", folders.length > 0);
  } catch (e) {
    foldersEl.replaceChildren();
    toast(e.message, "error");
  }
}

function startEdit(folder) {
  editingId = folder.id;
  $("#f-name").value = folder.name;
  $("#f-desc").value = folder.description;
  submitBtn.textContent = "Enregistrer";
  $("#form-title").textContent = "Modifier le dossier";
  cancelBtn.classList.remove("hidden");
  location.hash = "#new";
  $("#f-name").focus();
}

function resetForm() {
  editingId = null;
  form.reset();
  submitBtn.textContent = "Créer le dossier";
  $("#form-title").textContent = "Nouveau dossier";
  cancelBtn.classList.add("hidden");
}

cancelBtn.addEventListener("click", () => {
  resetForm();
  location.hash = "#folders";
});

// --- Navigation (vues) ---

const VIEWS = ["new", "folders", "chat"];

function showView() {
  const name = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "new";
  for (const v of VIEWS) $("#view-" + v).classList.toggle("hidden", v !== name);
  for (const a of document.querySelectorAll(".tabs a")) {
    a.classList.toggle("active", a.dataset.view === name);
  }
  if (name === "folders") loadFolders();
  if (name === "chat") $("#chat-input").focus();
}

window.addEventListener("hashchange", showView);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = { name: $("#f-name").value, description: $("#f-desc").value };
  await withBusy(submitBtn, async () => {
    try {
      if (editingId === null) {
        await api("POST", "/api/folders", payload);
        toast("Dossier créé et libellé Gmail prêt.", "ok");
      } else {
        await api("PUT", "/api/folders/" + editingId, payload);
        toast("Dossier mis à jour.", "ok");
      }
      const wasEditing = editingId !== null;
      resetForm();
      if (wasEditing) location.hash = "#folders";
    } catch (e) {
      toast(e.message, "error");
    }
  });
});

async function removeFolder(folder, button) {
  if (!confirm("Supprimer le dossier « " + folder.name + " » ?\nLe libellé Gmail sera conservé.")) return;
  await withBusy(button, async () => {
    try {
      await api("DELETE", "/api/folders/" + folder.id);
      if (editingId === folder.id) resetForm();
      toast("Dossier supprimé (libellé Gmail conservé).", "ok");
      await loadFolders();
    } catch (e) {
      toast(e.message, "error");
    }
  });
}

// --- Tri des mails ---

const analyzeBtn = $("#analyze-btn");
const applyBtn = $("#apply-btn");
const resultsEl = $("#results");
let previewIds = [];

function badge(text, kind) {
  const el = document.createElement("span");
  el.className = "badge " + kind;
  el.textContent = text;
  return el;
}

function percent(value) {
  return Math.round(value * 100) + " %";
}

function resultRow(r) {
  const li = document.createElement("li");
  li.className = "result";
  li.dataset.id = r.message_id;

  const sender = document.createElement("div");
  sender.className = "sender";
  sender.textContent = r.from || "(expéditeur inconnu)";
  const subject = document.createElement("div");
  subject.className = "subject";
  subject.textContent = r.subject || "(sans objet)";
  const meta = document.createElement("div");
  meta.className = "meta";

  if (r.status === "proposed") {
    meta.append(badge("→ " + r.folder_name + " · " + percent(r.confidence), "proposed"));
  } else if (r.status === "low") {
    meta.append(badge("Non classé", ""), badge("plus probable : " + r.folder_name + " · " + percent(r.confidence), "low"));
  } else if (r.status === "already") {
    meta.append(badge("Déjà dans " + r.folder_name, "already"));
  } else if (r.status === "unclassified") {
    meta.append(badge("Non classé", ""));
  } else {
    meta.append(badge("Erreur : " + (r.error || "inconnue"), "error"));
  }
  li.append(sender, subject, meta);
  return li;
}

function showSkeletons() {
  const items = [];
  for (let i = 0; i < 5; i++) {
    const li = document.createElement("li");
    li.className = "skeleton result-skeleton";
    items.push(li);
  }
  resultsEl.replaceChildren(...items);
}

analyzeBtn.addEventListener("click", () => {
  withBusy(analyzeBtn, async () => {
    applyBtn.disabled = true;
    showSkeletons();
    try {
      const data = await api("POST", "/api/analyze");
      resultsEl.replaceChildren(...data.results.map(resultRow));
      previewIds = data.results.filter((r) => r.status === "proposed").map((r) => r.message_id);
      applyBtn.disabled = previewIds.length === 0;
      if (data.results.length === 0) toast("Aucun mail à analyser.");
    } catch (e) {
      resultsEl.replaceChildren();
      toast(e.message, "error");
    }
  });
});

applyBtn.addEventListener("click", () => {
  withBusy(applyBtn, async () => {
    try {
      const data = await api("POST", "/api/apply", { message_ids: previewIds });
      let ok = 0;
      for (const r of data.results) {
        const row = resultsEl.querySelector('[data-id="' + r.message_id + '"] .meta');
        if (!row) continue;
        if (r.ok) {
          ok += 1;
          row.append(badge("Appliqué ✓", "done"));
        } else {
          row.append(badge("Échec : " + r.error, "error"));
        }
      }
      previewIds = data.results.filter((r) => !r.ok).map((r) => r.message_id);
      toast(ok + " libellé(s) posé(s) dans Gmail.", ok ? "ok" : "error");
    } catch (e) {
      toast(e.message, "error");
    }
    applyBtn.disabled = previewIds.length === 0;
  });
});

// --- Chat de recherche (lecture seule) ---

const chatForm = $("#chat-form");
const chatInput = $("#chat-input");
const chatSend = $("#chat-send");
const chatLog = $("#chat-log");

function addBubble(kind) {
  const el = document.createElement("div");
  el.className = "bubble " + kind;
  $("#chat-hint")?.remove();
  chatLog.append(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}

function shortDate(raw) {
  const d = new Date(raw);
  return isNaN(d) ? raw : d.toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function senderName(raw) {
  return raw.replace(/\s*<.*>\s*$/, "").replace(/^"|"$/g, "") || raw;
}

function mailCard(match, rank, expanded) {
  const tier = match.score >= 0.8 ? "high" : match.score >= 0.5 ? "mid" : "low";
  const card = el("article", "mail-card " + tier);

  const head = el("div", "mail-head");
  if (rank) head.append(el("span", "rank", String(rank)));
  head.append(el("h3", "mail-subject", match.subject || "(sans objet)"));
  head.append(el("span", "score", percent(match.score)));

  const meta = el("div", "mail-meta", senderName(match.from) + " · " + shortDate(match.date));
  card.append(head, meta);
  if (match.reason) card.append(el("p", "reason", match.reason));

  const details = el("details", "mail-details");
  details.open = expanded;
  details.append(el("summary", "", "Contenu du mail"));
  details.append(el("p", "mail-body", match.body || "(contenu vide)"));
  card.append(details);
  return card;
}

function fillAnswer(bubble, data) {
  bubble.replaceChildren();
  bubble.classList.add("wide");

  if (data.mode === "none") {
    bubble.append(el("p", "verdict", "Je n'ai trouvé aucun mail qui corresponde clairement à ta question."));
  } else if (data.mode === "sure") {
    bubble.append(el("p", "verdict sure", "Voici le mail :"));
    bubble.append(mailCard(data.matches[0], 0, true));
  } else {
    bubble.append(el("p", "verdict", "Je ne suis pas certain. Voici les plus probables, du plus au moins sûr :"));
    const list = el("div", "mail-list");
    data.matches.forEach((m, i) => list.append(mailCard(m, i + 1, false)));
    bubble.append(list);
  }
  bubble.append(el("div", "queries", "Recherche Gmail : " + data.queries.join("  ·  ")));
}

for (const chip of document.querySelectorAll(".chip")) {
  chip.addEventListener("click", () => {
    chatInput.value = chip.textContent;
    chatInput.focus();
  });
}

chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = chatInput.value.trim();
  if (!question) return;
  withBusy(chatSend, async () => {
    chatInput.value = "";
    chatInput.disabled = true;
    addBubble("user").textContent = question;
    const bot = addBubble("bot");
    const dots = document.createElement("span");
    dots.className = "spinner";
    bot.append(dots);
    try {
      fillAnswer(bot, await api("POST", "/api/chat", { question }));
    } catch (e) {
      bot.classList.add("error");
      bot.textContent = e.message;
    } finally {
      chatInput.disabled = false;
      chatInput.focus();
      chatLog.scrollTop = chatLog.scrollHeight;
    }
  });
});

showView();
