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

  card.append(title, desc, actions);
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
  cancelBtn.classList.remove("hidden");
  form.scrollIntoView({ behavior: "smooth", block: "center" });
  $("#f-name").focus();
}

function resetForm() {
  editingId = null;
  form.reset();
  submitBtn.textContent = "Créer le dossier";
  cancelBtn.classList.add("hidden");
}

cancelBtn.addEventListener("click", resetForm);

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
      resetForm();
      await loadFolders();
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
const chatPanel = $("#chat-panel");

$("#chat-close").addEventListener("click", () => chatPanel.classList.add("hidden"));

chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = chatInput.value.trim();
  if (!question) return;
  withBusy(chatSend, async () => {
    chatInput.disabled = true;
    chatPanel.classList.remove("hidden");
    $("#chat-loading").classList.remove("hidden");
    $("#chat-answer").textContent = "";
    $("#chat-sources").replaceChildren();
    $("#chat-query").textContent = "";
    try {
      const data = await api("POST", "/api/chat", { question });
      $("#chat-answer").textContent = data.answer;
      $("#chat-sources").replaceChildren(
        ...data.sources.map((s) => {
          const li = document.createElement("li");
          li.textContent = s.from + " — " + s.date + " — " + s.subject;
          return li;
        })
      );
      $("#chat-query").textContent = "Recherche Gmail : " + data.query;
    } catch (e) {
      chatPanel.classList.add("hidden");
      toast(e.message, "error");
    } finally {
      $("#chat-loading").classList.add("hidden");
      chatInput.disabled = false;
    }
  });
});

loadFolders();
