/*
  The inbox page. No framework and no build step: the page is small, and the
  rule from the reports console holds here too - nothing fetched from the
  internet at runtime.

  It polls: the chat list every few seconds, the open chat a little faster.
  A message arrives at Meta, Meta posts it to the webhook, and the next poll
  shows it - typically within five seconds.
*/
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    chats: [],
    current: null,        // wa_id of the open chat
    chat: null,           // its last fetched contents
    templates: null,
    file: null,
    sending: false,
    lastSeenIds: "",      // what was last drawn, so an unchanged poll redraws nothing
  };

  // ------------------------------------------------------------ helpers

  async function api(path, options = {}) {
    const response = await fetch(path, { credentials: "same-origin", ...options });
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail || detail; } catch (e) { /* not JSON */ }
      const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  function escapeHtml(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  /** WhatsApp's own formatting - *bold*, _italic_, ~strike~, ```mono``` - and links. */
  function format(text) {
    let html = escapeHtml(text);
    html = html.replace(/```([\s\S]+?)```/g, "<code>$1</code>");
    html = html.replace(/(^|[\s(])\*(?!\s)([^*\n]+?)\*(?=$|[\s.,!?)])/g, "$1<strong>$2</strong>");
    html = html.replace(/(^|[\s(])_(?!\s)([^_\n]+?)_(?=$|[\s.,!?)])/g, "$1<em>$2</em>");
    html = html.replace(/(^|[\s(])~(?!\s)([^~\n]+?)~(?=$|[\s.,!?)])/g, "$1<s>$2</s>");
    html = html.replace(/\bhttps?:\/\/[^\s<]+/g, (url) =>
      `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
    return html.replace(/\n/g, "<br>");
  }

  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });

  function dayLabel(ts) {
    const date = new Date(ts * 1000);
    const today = new Date();
    const yesterday = new Date(Date.now() - 86400000);
    if (date.toDateString() === today.toDateString()) return "Today";
    if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
    return date.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
  }

  function listTime(ts) {
    const date = new Date(ts * 1000);
    if (date.toDateString() === new Date().toDateString()) return clock(ts);
    return date.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
  }

  function windowText(closes) {
    if (!closes) return null;
    const minutes = Math.max(0, Math.round((closes * 1000 - Date.now()) / 60000));
    const h = Math.floor(minutes / 60), m = minutes % 60;
    return h ? `${h} h ${m} m` : `${m} m`;
  }

  let toastTimer;
  function toast(message, isError = false) {
    const el = $("toast");
    el.textContent = message;
    el.className = "toast" + (isError ? " error" : "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 8000 : 3000);
  }

  function tick(status, error) {
    switch (status) {
      case "read": case "played": return '<span class="tick read" title="Read">✓✓</span>';
      case "delivered": return '<span class="tick" title="Delivered">✓✓</span>';
      case "sent": return '<span class="tick" title="Sent">✓</span>';
      case "failed": return `<span class="tick failed" title="${escapeHtml(error || "Failed")}">⚠</span>`;
      default: return '<span class="tick" title="Waiting for WhatsApp">🕓</span>';
    }
  }

  // --------------------------------------------------------- the list

  async function loadChats() {
    try {
      state.chats = await api("api/chats");
    } catch (error) {
      if (error.status === 403) toast(error.message, true);
      return;
    }
    drawChats();
    const unread = state.chats.reduce((n, c) => n + (c.wa_id === state.current ? 0 : c.unread), 0);
    document.title = (unread ? `(${unread}) ` : "") + "WhatsApp · Iravi Agro Life";
  }

  function drawChats() {
    const query = $("search").value.trim().toLowerCase();
    const digits = query.replace(/\D/g, "");
    const shown = state.chats.filter((c) => !query
      || (c.name || "").toLowerCase().includes(query)
      || (digits && c.wa_id.includes(digits)));
    $("no-chats").hidden = state.chats.length > 0;
    $("chats").innerHTML = shown.map((c) => {
      const unread = c.wa_id === state.current ? 0 : c.unread;
      const mine = c.last_direction === "out" ? tick(c.last_status) + " " : "";
      return `<li data-id="${c.wa_id}" class="${c.wa_id === state.current ? "active" : ""} ${unread ? "has-unread" : ""}">
        <span class="name">${c.window_closes ? '<span class="open-dot" title="Free reply window open"></span>' : ""}${escapeHtml(c.name || c.number)}</span>
        <span class="time">${listTime(c.last_ts)}</span>
        <span class="preview">${mine}${escapeHtml(c.preview || "")}</span>
        ${unread ? `<span class="badge">${unread}</span>` : "<span></span>"}
      </li>`;
    }).join("");
  }

  // ---------------------------------------------------------- one chat

  async function openChat(waId) {
    state.current = waId;
    state.lastSeenIds = "";
    state.chat = null;
    clearFile();
    $("text").value = "";
    $("app").classList.add("in-chat");
    $("placeholder").hidden = true;
    ["chat-head", "messages"].forEach((id) => { $(id).hidden = false; });
    if (location.hash.slice(1) !== waId) history.replaceState(null, "", "#" + waId);
    drawChats();
    await loadChat(true);
  }

  function closeChat() {
    state.current = null;
    state.chat = null;
    $("app").classList.remove("in-chat");
    $("placeholder").hidden = false;
    ["chat-head", "messages", "composer", "closed"].forEach((id) => { $(id).hidden = true; });
    history.replaceState(null, "", location.pathname);
    drawChats();
  }

  async function loadChat(scrollToEnd = false) {
    const waId = state.current;
    if (!waId) return;
    let chat;
    try {
      chat = await api(`api/chats/${waId}`);
    } catch (error) {
      toast(error.message, true);
      return;
    }
    if (state.current !== waId) return;   // another chat was opened meanwhile
    const before = state.chat;
    state.chat = chat;
    drawHead();
    const signature = chat.messages.map((m) => `${m.id}:${m.status}:${m.reactions.join("")}`).join(",");
    if (signature !== state.lastSeenIds) {
      const box = $("messages");
      const nearEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
      state.lastSeenIds = signature;
      drawMessages();
      if (scrollToEnd || nearEnd) box.scrollTop = box.scrollHeight;
    }
    const newest = chat.messages.filter((m) => m.direction === "in").pop();
    const seenBefore = before && before.messages.filter((m) => m.direction === "in").pop();
    if (newest && (!seenBefore || newest.id !== seenBefore.id || scrollToEnd) && !document.hidden) {
      api(`api/chats/${waId}/read`, { method: "POST" }).then(loadChats).catch(() => {});
    }
  }

  function drawHead() {
    const chat = state.chat;
    $("chat-name").textContent = chat.name || chat.number;
    $("chat-number").textContent = chat.name ? chat.number : "";
    const left = windowText(chat.window_closes);
    const badge = $("window");
    badge.textContent = left ? `Reply window: ${left} left` : "Window closed";
    badge.className = "window " + (left ? "open" : "shut");
    badge.title = left
      ? "Normal replies are free until the window closes, 24 hours after their last message."
      : "Only an approved template can be sent until they write again.";
    $("composer").hidden = !left;
    $("closed").hidden = !!left;
  }

  function drawMessages() {
    const byWamid = new Map(state.chat.messages.map((m) => [m.wamid, m]));
    let lastDay = "";
    const parts = [];
    for (const m of state.chat.messages) {
      const day = dayLabel(m.ts);
      if (day !== lastDay) { parts.push(`<div class="day">${day}</div>`); lastDay = day; }
      parts.push(bubble(m, byWamid));
    }
    if (!state.chat.messages.length) {
      parts.push('<div class="day">No messages yet</div>');
    }
    $("messages").innerHTML = parts.join("");
  }

  function bubble(m, byWamid) {
    const classes = ["bubble", m.direction, m.source === "alert" ? "alert" : "",
      m.source === "script" || m.source === "bot" ? "script" : "",
      m.reactions.length ? "has-reactions" : ""].join(" ");
    let label = "";
    if (m.source === "script") label = "Bulk script";
    else if (m.source === "bot") label = "🤖 Bot";
    else if (m.source === "alert") label = "Inbox alert";
    else if (m.source === "external") label = "Sent outside the inbox";
    else if (m.direction === "out" && m.by && m.by !== "this computer") label = escapeHtml(m.by);

    let quote = "";
    if (m.reply_to && byWamid.has(m.reply_to)) {
      const q = byWamid.get(m.reply_to);
      quote = `<div class="quote">${escapeHtml((q.body || q.filename || `[${q.type}]`).slice(0, 140))}</div>`;
    }

    let content = "";
    const url = m.media_url;
    if (url && (m.type === "image" || m.type === "sticker")) {
      content += `<a href="${url}" target="_blank" rel="noopener"><img class="media" src="${url}" alt="" loading="lazy"></a>`;
    } else if (url && m.type === "video") {
      content += `<video controls preload="metadata" src="${url}"></video>`;
    } else if (url && m.type === "audio") {
      content += `<audio controls preload="none" src="${url}"></audio>`;
    } else if (url) {
      content += `<a class="doc" href="${url}" target="_blank" rel="noopener">📄 <span>${escapeHtml(m.filename || "Document")}</span></a>`;
    }
    if (m.location) {
      const q = `${m.location.latitude},${m.location.longitude}`;
      content += `<a href="https://maps.google.com/?q=${encodeURIComponent(q)}" target="_blank" rel="noopener">📍 Open the location</a><br>`;
    }
    if (m.body) content += `<div class="text">${format(m.body)}</div>`;
    if (!content) content = `<div class="text"><em>[${escapeHtml(m.type)}]</em></div>`;

    const reactions = m.reactions.length ? `<span class="reactions">${m.reactions.map(escapeHtml).join("")}</span>` : "";
    const error = m.status === "failed" && m.error ? `<div class="error">Not delivered: ${escapeHtml(m.error)}</div>` : "";
    const status = m.direction === "out" ? tick(m.status, m.error) : "";
    return `<div class="${classes}">
      ${label ? `<div class="label">${label}</div>` : ""}
      ${quote}${content}${error}
      <div class="meta">${clock(m.ts)} ${status}</div>
      ${reactions}
    </div>`;
  }

  // ------------------------------------------------------------ sending

  function setFile(file) {
    state.file = file;
    $("file-chip").hidden = !file;
    $("file-name").textContent = file ? `${file.name} (${Math.ceil(file.size / 1024)} KB)` : "";
    $("text").placeholder = file ? "Add a caption (optional)" : "Type a message";
  }
  function clearFile() { $("file").value = ""; setFile(null); }

  async function send() {
    if (state.sending || !state.current) return;
    const text = $("text").value.trim();
    if (!text && !state.file) return;
    const form = new FormData();
    form.append("text", text);
    if (state.file) form.append("file", state.file);
    state.sending = true;
    $("send").disabled = true;
    try {
      await api(`api/chats/${state.current}/send`, { method: "POST", body: form });
      $("text").value = "";
      autoGrow();
      clearFile();
      await loadChat(true);
      loadChats();
    } catch (error) {
      toast(error.message, true);
      if (error.status === 409) loadChat();
    } finally {
      state.sending = false;
      $("send").disabled = false;
    }
  }

  function autoGrow() {
    const box = $("text");
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 160) + "px";
  }

  // ---------------------------------------------------------- templates

  async function openTemplates() {
    const select = $("template-select");
    if (!state.templates) {
      try {
        state.templates = await api("api/templates");
      } catch (error) {
        toast(error.message, true);
        return;
      }
    }
    const usable = state.templates.filter((t) => t.sendable);
    if (!usable.length) {
      toast("There are no approved templates yet. Create one in WhatsApp Manager.", true);
      return;
    }
    select.innerHTML = usable.map((t, i) =>
      `<option value="${i}">${escapeHtml(t.name)} · ${escapeHtml(t.language)} · ${escapeHtml(t.category)}</option>`).join("");
    select.onchange = () => drawTemplate(usable[Number(select.value)]);
    drawTemplate(usable[0]);
    $("template-dialog").showModal();
  }

  function drawTemplate(t) {
    state.template = t;
    $("template-preview").textContent = (t.header_text ? t.header_text + "\n" : "") + t.body
      + (t.footer ? "\n" + t.footer : "") + (t.buttons.length ? "\n[" + t.buttons.join("] [") + "]" : "");
    const fields = [];
    if (["IMAGE", "VIDEO", "DOCUMENT"].includes(t.header_format)) {
      const accept = { IMAGE: "image/jpeg,image/png", VIDEO: "video/mp4,video/3gpp", DOCUMENT: "" }[t.header_format];
      fields.push(`<label>Header ${t.header_format.toLowerCase()}<input type="file" id="tpl-file" accept="${accept}" required></label>`);
    }
    t.header_slots.forEach((s) => fields.push(
      `<label>Header {{${escapeHtml(s)}}}<input class="tpl-header" required></label>`));
    t.body_slots.forEach((s) => fields.push(
      `<label>Message {{${escapeHtml(s)}}}<input class="tpl-body" required></label>`));
    $("template-fields").innerHTML = fields.join("");
    $("template-cost").textContent = t.category === "marketing"
      ? "Marketing template - charged when delivered, and Meta may hold it back for people who get many."
      : t.category === "utility"
        ? "Utility template - charged when delivered, unless their reply window is open."
        : "";
  }

  async function sendTemplate(event) {
    if (event.submitter && event.submitter.value === "cancel") return;
    event.preventDefault();
    const t = state.template;
    const form = new FormData();
    form.append("name", t.name);
    form.append("language", t.language);
    form.append("header_values", JSON.stringify([...document.querySelectorAll(".tpl-header")].map((i) => i.value)));
    form.append("body_values", JSON.stringify([...document.querySelectorAll(".tpl-body")].map((i) => i.value)));
    const file = $("tpl-file");
    if (file && file.files[0]) form.append("file", file.files[0]);
    $("template-send").disabled = true;
    try {
      await api(`api/chats/${state.current}/template`, { method: "POST", body: form });
      $("template-dialog").close();
      toast("Template sent.");
      await loadChat(true);
      loadChats();
    } catch (error) {
      toast(error.message, true);
    } finally {
      $("template-send").disabled = false;
    }
  }

  // ------------------------------------------------------------- wiring

  $("chats").addEventListener("click", (event) => {
    const item = event.target.closest("li[data-id]");
    if (item) openChat(item.dataset.id);
  });
  $("search").addEventListener("input", drawChats);
  $("back").addEventListener("click", closeChat);
  $("send").addEventListener("click", send);
  $("text").addEventListener("input", autoGrow);
  $("text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  $("file").addEventListener("change", () => setFile($("file").files[0] || null));
  $("file-clear").addEventListener("click", clearFile);
  $("open-templates").addEventListener("click", openTemplates);
  $("template-form").addEventListener("submit", sendTemplate);

  $("new-chat").addEventListener("click", () => {
    $("number-input").value = "";
    $("number-dialog").showModal();
  });
  $("number-form").addEventListener("submit", async (event) => {
    if (event.submitter && event.submitter.value === "cancel") return;
    event.preventDefault();
    try {
      const found = await api(`api/number?raw=${encodeURIComponent($("number-input").value)}`);
      $("number-dialog").close();
      openChat(found.wa_id);
    } catch (error) {
      toast(error.message, true);
    }
  });

  $("theme").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("wapp-theme", root.dataset.theme); } catch (e) { /* private window */ }
  });

  // Dropping a file anywhere on the open chat attaches it.
  $("chat").addEventListener("dragover", (event) => { if (state.current) event.preventDefault(); });
  $("chat").addEventListener("drop", (event) => {
    if (!state.current || !event.dataTransfer.files.length) return;
    event.preventDefault();
    setFile(event.dataTransfer.files[0]);
  });

  window.addEventListener("hashchange", () => {
    const id = location.hash.slice(1);
    if (id && id !== state.current) openChat(id);
  });

  async function start() {
    try {
      const me = await api("api/me");
      $("business").textContent = me.business;
      $("webhook-warning").hidden = me.webhook_ready;
    } catch (error) {
      toast(error.message, true);
    }
    await loadChats();
    const id = location.hash.slice(1);
    if (id) openChat(id);
    setInterval(loadChats, 5000);
    setInterval(() => { if (state.current) loadChat(); }, 3000);
    // The window countdown moves even when nothing new arrives.
    setInterval(() => { if (state.chat) drawHead(); }, 60000);
  }

  start();
})();
