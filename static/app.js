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

async function api(method, url, body, signal) {
  const options = { method, headers: {}, signal };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    if (e.name === "AbortError") throw e;
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

// --- Assistant IA : description et suggestions de dossiers ---

const improveBtn = $("#f-improve");
const suggestBtn = $("#suggest-btn");
const suggestionsEl = $("#suggestions");

improveBtn.addEventListener("click", () => {
  const description = $("#f-desc").value.trim();
  if (!description) {
    toast("Écris d'abord une description à améliorer.", "error");
    $("#f-desc").focus();
    return;
  }
  withBusy(improveBtn, async () => {
    try {
      const data = await api("POST", "/api/folders/improve-description", {
        name: $("#f-name").value,
        description,
      });
      $("#f-desc").value = data.description;
      toast("Description améliorée : relis-la avant d'enregistrer.", "ok");
    } catch (e) {
      toast(e.message, "error");
    }
  });
});

function suggestionCard(s) {
  const card = el("article", "card folder suggestion");
  const head = el("div", "folder-head");
  head.append(el("h3", "", "✨ " + s.name));
  const actions = el("div", "actions");
  const create = el("button", "btn primary small", "Créer");
  const ignore = el("button", "btn ghost small", "Ignorer");
  ignore.addEventListener("click", () => card.remove());
  create.addEventListener("click", () =>
    withBusy(create, async () => {
      try {
        await api("POST", "/api/folders", { name: s.name, description: s.description });
        card.remove();
        toast("Dossier « " + s.name + " » créé.", "ok");
        await loadFolders();
      } catch (e) {
        toast(e.message, "error");
      }
    })
  );
  actions.append(create, ignore);
  head.append(actions);
  card.append(head, el("p", "", s.description));
  card.append(el("p", "examples", s.count + " mails concernés, par ex. : " + s.subjects.join(" · ")));
  return card;
}

suggestBtn.addEventListener("click", () => {
  withBusy(suggestBtn, async () => {
    suggestionsEl.replaceChildren();
    try {
      const data = await api("POST", "/api/folders/suggest");
      if (data.suggestions.length === 0) {
        toast("Aucun nouveau dossier à suggérer pour l'instant.");
      } else {
        suggestionsEl.replaceChildren(...data.suggestions.map(suggestionCard));
      }
    } catch (e) {
      toast(e.message, "error");
    }
  });
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

// --- Tri de la boîte ---

const sortBtn = $("#sort-btn");
const stopBtn = $("#stop-btn");
const statusEl = $("#sort-status");
const progressEl = $("#sort-progress");
const periodEl = $("#sort-period");
const questionsEl = $("#questions");
const resultsEl = $("#results");
let stopRequested = false;
let sortAbort = null;

function badge(text, kind) {
  const node = document.createElement("span");
  node.className = "badge " + kind;
  node.textContent = text;
  return node;
}

function percent(value) {
  return Math.round(value * 100) + " %";
}

function mailRow(r, badges) {
  const li = document.createElement("li");
  li.className = "result";
  const sender = document.createElement("div");
  sender.className = "sender";
  sender.textContent = r.from || "(expéditeur inconnu)";
  const subject = document.createElement("div");
  subject.className = "subject";
  subject.textContent = r.subject || "(sans objet)";
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.append(...badges);
  li.append(sender, subject, meta);
  return li;
}

async function sendAnswer(r, yes) {
  return api("POST", "/api/answer", { message_id: r.message_id, folder_id: r.folder_id, yes });
}

function questionCard(r) {
  const card = el("article", "card folder suggestion question");
  card.dataset.address = r.address || "";
  card.dataset.id = r.message_id;
  card.append(el("h3", "", "Ce mail va-t-il dans « " + r.folder_name + " » ?"));
  card.append(el("p", "", (r.from || "(expéditeur inconnu)") + " : " + (r.subject || "(sans objet)")));
  const actions = el("div", "actions");
  const yes = el("button", "btn primary small", "Oui");
  const no = el("button", "btn ghost small", "Non");
  for (const [button, answer] of [[yes, true], [no, false]]) {
    button.addEventListener("click", () =>
      withBusy(button, async () => {
        try {
          await sendAnswer(r, answer);
          answeredIds.add(r.message_id);
          card.remove();
          if (answer) resultsEl.prepend(mailRow(r, [badge("→ " + r.folder_name, "proposed")]));
          await answerSameSender(r, answer);
        } catch (e) {
          toast(e.message, "error");
        }
      })
    );
  }
  actions.append(yes, no);
  card.append(actions);
  return card;
}

// Même expéditeur, même réponse : les autres questions en attente sont réglées d'un coup.
async function answerSameSender(r, yes) {
  if (!r.address) return;
  for (const card of [...questionsEl.children]) {
    if (card.dataset.address !== r.address) continue;
    const other = card.questionData;
    if (!other || other.folder_id !== r.folder_id) continue;
    try {
      await sendAnswer(other, yes);
      answeredIds.add(other.message_id);
      card.remove();
      if (yes) resultsEl.prepend(mailRow(other, [badge("→ " + other.folder_name, "proposed")]));
    } catch (e) {
      toast(e.message, "error");
      return;
    }
  }
}

const answeredIds = new Set();

function addQuestion(r) {
  if (answeredIds.has(r.message_id) || questionsEl.querySelector('[data-id="' + r.message_id + '"]')) return;
  const card = questionCard(r);
  card.questionData = r;
  questionsEl.append(card);
}

stopBtn.addEventListener("click", () => {
  stopRequested = true;
  stopBtn.disabled = true;
  if (sortAbort) sortAbort.abort();
});

sortBtn.addEventListener("click", () => {
  withBusy(sortBtn, async () => {
    stopRequested = false;
    sortAbort = new AbortController();
    stopBtn.disabled = false;
    stopBtn.classList.remove("hidden");
    resultsEl.replaceChildren();
    questionsEl.replaceChildren();
    progressEl.classList.remove("hidden", "done");
    let before = null;
    let reached = "";
    let scanned = 0, labeled = 0, asked = 0, failed = 0;
    const summary = (finished) =>
      (reached && !finished ? "Jusqu'au " + reached + " · " : "") +
      scanned + " mails examinés · " + labeled + " classés · " + asked + " question(s)" +
      (failed ? " · " + failed + " en erreur" : "") + (finished ? "" : "…");
    try {
      do {
        statusEl.textContent = summary(false);
        const data = await api("POST", "/api/sort", { before, period: periodEl.value }, sortAbort.signal);
        scanned += data.scanned;
        for (const r of data.results) {
          if (r.status === "labeled") {
            labeled += 1;
            resultsEl.append(mailRow(r, [badge("→ " + r.folder_name + " · " + percent(r.confidence), "proposed")]));
          } else if (r.status === "ask") {
            asked += 1;
            addQuestion(r);
          } else if (r.status === "error") {
            failed += 1;
            resultsEl.append(mailRow(r, [badge("Erreur : " + (r.error || "inconnue"), "error")]));
          }
        }
        before = data.next_before;
        if (before !== null) reached = new Date(before * 1000).toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
      } while (before !== null && !stopRequested);
      statusEl.textContent = summary(true) + (before !== null ? " (arrêté)" : " · terminé");
      if (before === null) progressEl.classList.add("done");
      else progressEl.classList.add("hidden");
    } catch (e) {
      progressEl.classList.add("hidden");
      if (e.name === "AbortError") {
        statusEl.textContent = summary(true) + " (arrêté)";
      } else {
        statusEl.textContent = summary(true) + " (interrompu)";
        toast(e.message, "error");
      }
    } finally {
      stopBtn.classList.add("hidden");
    }
  });
});

// --- Tri automatique des nouveaux mails ---

const autoToggle = $("#auto-toggle");
const autoStatus = $("#auto-status");

async function refreshAuto() {
  try {
    const data = await api("GET", "/api/auto");
    autoToggle.checked = data.enabled;
    autoStatus.textContent = !data.enabled
      ? "Tri automatique désactivé."
      : (data.last_check
          ? "Dernier relevé à " + data.last_check.slice(11, 16) + " · " + data.labeled + " mail(s) classé(s) automatiquement"
          : "Premier relevé dans un instant…") + " (tant que l'appli est ouverte)";
    for (const q of data.questions) addQuestion(q);
  } catch (e) {
    /* le prochain passage réessaiera */
  }
}

autoToggle.addEventListener("change", async () => {
  try {
    await api("POST", "/api/auto", { enabled: autoToggle.checked });
  } catch (e) {
    toast(e.message, "error");
  }
  refreshAuto();
});

refreshAuto();
setInterval(() => {
  if (!document.hidden) refreshAuto();
}, 15000);

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
    bubble.append(mailCard(data.matches[0], 0, false));
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
