
  // Texts come from locales/<lang>.json (section "web"); main.py puts them here (US6).


  const history = [];
  let busy = false;
  let guideLoaded = false;
  let lang = pickInitialLang();

  const $ = (id) => document.getElementById(id);
  const thread = $("thread"), messages = $("messages"), input = $("input"), sendBtn = $("send");
  // A text in the chosen language, else English, else nothing (the page then
  // keeps the English wording written in the HTML).
  const t = (key) => (I18N[lang] || {})[key] ?? (I18N.en || {})[key] ?? "";

  function pickInitialLang() {
    try {
      const saved = localStorage.getItem("lang");
      if (saved && I18N[saved]) return saved;
    } catch { /* storage may be unavailable */ }
    const nav = (navigator.language || "en").slice(0, 2).toLowerCase();
    return I18N[nav] ? nav : "en";
  }

  function applyLang() {
    document.documentElement.lang = lang;
    document.title = t("title") || document.title;
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const value = t(el.dataset.i18n);
      if (value) el.textContent = value;
    });
    input.placeholder = t("placeholder");
    input.setAttribute("aria-label", t("inputLabel"));
    document.querySelectorAll(".lang").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.lang === lang)));

    const chips = $("chips");
    chips.textContent = "";
    (t("chips") || []).forEach((text) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.textContent = text;
      b.addEventListener("click", () => send(text));
      chips.appendChild(b);
    });

    renderEvents();
    renderWeek();
    if ($('account-dialog').open) renderAccountDialog();
    guideLoaded = false;
    if ($("panel-guide").classList.contains("active")) loadGuide();
  }

  document.querySelectorAll(".lang").forEach((b) =>
    b.addEventListener("click", () => {
      lang = b.dataset.lang;
      try { localStorage.setItem("lang", lang); } catch { /* ignore */ }
      applyLang();
    }));

  /* --- text rendering (DOM only, no innerHTML) --- */
  // **bold**, [text](https://link) and Markdown escapes such as "Add \- Drop".
  function renderInline(parent, raw) {
    const text = raw.replace(/\\([\\`*_{}\[\]()#+\-.!|>~])/g, "$1");
    const pattern = /\[([^\[\]\n]+)\]\((https?:\/\/[^\s()]+)\)|\*\*(.+?)\*\*/g;
    let last = 0, m;
    while ((m = pattern.exec(text))) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1] !== undefined) {
        const a = document.createElement("a");
        a.href = m[2];
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        renderInline(a, m[1]);
        parent.appendChild(a);
      } else {
        const s = document.createElement("strong");
        renderInline(s, m[3]);
        parent.appendChild(s);
      }
      last = pattern.lastIndex;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }
  function renderRich(parent, text) {
    parent.textContent = "";
    renderInline(parent, text.replace(/^(\s*)[*-]\s+/gm, "$1• "));
  }

  /* --- chat --- */
  function addMessage(role, text) {
    const el = document.createElement("div");
    el.className = "msg " + role;
    renderRich(el, text);
    thread.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
    return el;
  }

  async function send(raw) {
    const text = raw.trim();
    if (!text || busy) return;
    busy = true;
    sendBtn.disabled = true;
    $("empty").hidden = true;

    addMessage("user", text);
    const pending = addMessage("bot", t("pending"));
    pending.classList.add("pending");
    input.value = "";
    input.style.height = "auto";

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history: history.slice(-10), lang }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || t("unknownError"));
      pending.classList.remove("pending");
      renderRich(pending, data.reply);
      history.push({ role: "user", text }, { role: "assistant", text: data.reply });
    } catch (err) {
      pending.classList.remove("pending");
      pending.classList.add("error");
      renderRich(pending, err instanceof TypeError ? t("offline") : err.message);
    } finally {
      busy = false;
      sendBtn.disabled = false;
      messages.scrollTop = messages.scrollHeight;
      input.focus();
    }
  }

  $("form").addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 140) + "px";
  });

  /* --- tabs --- */
  function showTab(name) {
    ["home", "chat", "events", "schedule", "guide"].forEach((n) => {
      $("panel-" + n).classList.toggle("active", n === name);
      $("tab-" + n).setAttribute("aria-selected", String(n === name));
    });
    if (name === "guide" && !guideLoaded) loadGuide();
    window.scrollTo({ top: 0, behavior: "instant" });
    if (name === "events" && !eventFeed) loadEvents();
    if (name === "schedule") loadWeek();
  }
  document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => showTab(button.dataset.view)));

  /* --- registration guide (US2) --- */
  const cells = (line) => line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
  const isTableRule = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line) && line.includes("|");

  function renderGuide(container, text) {
    container.textContent = "";
    let list = null, listType = null;
    const closeList = () => { list = null; listType = null; };
    const addItem = (type, content) => {
      if (listType !== type) {
        list = document.createElement(type);
        listType = type;
        container.appendChild(list);
      }
      const li = document.createElement("li");
      renderInline(li, content);
      list.appendChild(li);
    };
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line || line.startsWith("<!--")) { closeList(); continue; }
      let m;
      if (line.startsWith("|") && isTableRule(lines[i + 1] || "")) {
        closeList();
        const table = document.createElement("table");
        const head = table.createTHead().insertRow();
        cells(line).forEach((c) => { const th = document.createElement("th"); renderInline(th, c); head.appendChild(th); });
        const body = table.createTBody();
        for (i += 2; i < lines.length && lines[i].trim().startsWith("|"); i++) {
          const row = body.insertRow();
          cells(lines[i]).forEach((c) => renderInline(row.insertCell(), c));
        }
        i--;  // the for-loop moves on to the first line after the table
        container.appendChild(table);
      } else if ((m = line.match(/^(#{1,3})\s+(.*)/))) {
        closeList();
        const h = document.createElement(m[1].length === 1 ? "h2" : "h3");
        renderInline(h, m[2]);
        container.appendChild(h);
      } else if ((m = line.match(/^\d+\.\s+(.*)/))) {
        addItem("ol", m[1]);
      } else if ((m = line.match(/^[-*]\s+(.*)/))) {
        addItem("ul", m[1]);
      } else {
        closeList();
        const p = document.createElement("p");
        renderInline(p, line);
        container.appendChild(p);
      }
    }
  }
  // "Not published" is final; a temporary failure gets a Retry button.
  function guideMessage(body, text, retry) {
    body.textContent = "";
    const p = document.createElement("p");
    p.textContent = text;
    body.appendChild(p);
    if (retry) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "send";
      b.textContent = t("retry");
      b.addEventListener("click", loadGuide);
      body.appendChild(b);
    }
  }
  async function loadGuide() {
    const body = $("guide-body");
    body.textContent = t("guideLoading");
    const requestedLang = lang;
    try {
      const res = await fetch("/api/guide?lang=" + requestedLang);
      if (requestedLang !== lang) return; // language changed while loading
      if (res.status === 404) {
        const data = await res.json().catch(() => ({}));
        guideMessage(body, data.detail || t("guideError"), false);
        return;
      }
      if (!res.ok) throw new Error();
      renderGuide(body, await res.text());
      guideLoaded = true;
    } catch {
      if (requestedLang === lang) guideMessage(body, t("guideError"), true);
    }
  }



  let eventFeed = null, eventsError = false, eventsBusy = false, showPast = false;
  let telegramURL = 'https://t.me/sdu_hub_bot';
  let weekData = null, weekBusy = false, weekError = false, weekSequence = 0, requestedWeek = null;
  let activeAccount = null, authPhase = 'credentials', authBusy = false;
  const days = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'];
  const dayKeys = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const campusDay = (value) => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Almaty', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(value));
  const eventDate = (event) => {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Asia/Almaty', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).formatToParts(new Date(event.date)).map(part => [part.type, part.value]));
    return `${parts.day}.${parts.month}.${parts.year}` + (event.has_time ? ` · ${parts.hour}:${parts.minute}` : '');
  };

  function botLink(start) {
    const url = new URL(telegramURL);
    if (start) url.searchParams.set('start', start);
    return url.href;
  }
  function updateBotLinks() {
    document.querySelectorAll('[data-bot-link]').forEach(a => { a.href = botLink(a.dataset.botStart); });
  }
  fetch('/api/config').then(r => r.ok ? r.json() : Promise.reject()).then(data => {
    const url = new URL(data.telegram_url);
    if (url.protocol === 'https:' && url.hostname === 't.me') telegramURL = url.href;
    updateBotLinks();
    renderWeek();
  }).catch(() => {});

  function eventCard(event) {
    const card = el('article', 'event-card');
    const art = el('div', 'event-art');
    if (event.image && /^https:\/\/(life\.sdu\.edu\.kz|s3\.twcstorage\.ru)\//.test(event.image)) {
      const img = document.createElement('img');
      img.src = event.image; img.alt = event.title; img.loading = 'lazy';
      img.addEventListener('error', () => { img.remove(); art.append(el('span', 'event-fallback', 'SDU')); }, { once: true });
      art.append(img);
    } else art.append(el('span', 'event-fallback', 'SDU'));
    const content = el('div', 'event-content');
    content.append(el('p', 'event-date', eventDate(event)), el('h3', '', event.title), el('p', 'organizer', event.organizer));
    const button = el('button', 'text-button', t('eventDetails')); button.type = 'button';
    button.addEventListener('click', () => openEvent(event));
    content.append(button); card.append(art, content);
    return card;
  }
  function eventMessage(container, message, retry = false) {
    const box = el('div', 'empty-state'); box.append(el('p', '', message));
    if (retry) {
      const button = el('button', 'text-button', t('retry')); button.style.marginTop = '16px';
      button.addEventListener('click', loadEvents); box.append(button);
    }
    container.append(box);
  }
  function renderEvents() {
    $('events-upcoming').setAttribute('aria-pressed', String(!showPast));
    $('events-all').setAttribute('aria-pressed', String(showPast));
    $('refresh-events').disabled = eventsBusy;
    $('events-status').textContent = eventFeed?.stale ? t('eventStale') : '';
    const today = campusDay(Date.now());
    ['home-events', 'all-events'].forEach(id => {
      const container = $(id); container.replaceChildren();
      if (!eventFeed) { eventMessage(container, t(eventsError ? 'eventsError' : 'loadingEvents'), eventsError); return; }
      const upcoming = eventFeed.events.filter(e => campusDay(e.date) >= today);
      const rows = id === 'home-events' ? upcoming.slice(0, 3) : showPast ? [...eventFeed.events].reverse() : upcoming;
      if (!rows.length) { eventMessage(container, t('eventsEmpty')); return; }
      rows.forEach(event => container.append(eventCard(event)));
    });
  }
  async function loadEvents() {
    if (eventsBusy) return;
    eventsBusy = true; eventsError = false; renderEvents();
    try {
      const res = await fetch('/api/events');
      if (!res.ok) throw new Error();
      const data = await res.json();
      if (!Array.isArray(data.events)) throw new Error();
      eventFeed = data;
    } catch { eventsError = true; if (eventFeed) eventFeed.stale = true; }
    finally { eventsBusy = false; renderEvents(); }
  }
  function openEvent(event) {
    const container = $('event-detail'); container.replaceChildren();
    const content = el('div', 'dialog-content');
    content.append(el('p', 'event-date', eventDate(event)), el('h2', '', event.title), el('p', 'organizer', event.organizer + ' · ' + (event.venue || t('eventVenue'))));
    const description = el('p'); renderRich(description, event.description || ''); content.append(description);
    const link = el('a', 'button', t('eventDetails'));
    if (/^https:\/\/life\.sdu\.edu\.kz\/en\/events\/\d+$/.test(event.url)) link.href = event.url;
    else link.href = 'https://life.sdu.edu.kz/en';
    link.target = '_blank'; link.rel = 'noopener noreferrer'; content.append(link);
    container.append(content); $('close-event').setAttribute('aria-label', t('dismiss')); $('event-dialog').showModal();
  }
  $('close-event').addEventListener('click', () => $('event-dialog').close());
  $('event-dialog').addEventListener('click', event => { if (event.target === $('event-dialog')) $('event-dialog').close(); });
  $('events-upcoming').addEventListener('click', () => { showPast = false; renderEvents(); });
  $('events-all').addEventListener('click', () => { showPast = true; renderEvents(); });
  $('refresh-events').addEventListener('click', loadEvents);

  function formatDate(value) {
    const [year, month, day] = value.split('-');
    return `${day}.${month}.${year}`;
  }
  function accountMessage(status) {
    return t(({ ready: 'accountConnected', disconnected: 'accountDisconnected', expired: 'accountExpired', unavailable: 'accountUnavailable' })[status] || 'accountDisconnected');
  }
  function renderWeek() {
    const connections = $('week-accounts'); connections.replaceChildren();
    ['mysdu', 'moodle'].forEach(account => {
      const status = weekData?.[account]?.status || 'disconnected';
      const card = el('div', 'connection-card');
      const info = el('div'); info.append(el('strong', '', account === 'mysdu' ? 'MySDU' : 'Moodle'), el('p', 'caption', accountMessage(status)));
      const action = el('button', 'text-button', t(status === 'ready' || status === 'unavailable' ? 'accountDisconnect' : 'accountConnect'));
      action.type = 'button'; action.disabled = weekBusy;
      action.addEventListener('click', () => status === 'ready' || status === 'unavailable' ? disconnectAccount(account) : openAccount(account));
      card.append(info, action); connections.append(card);
    });
    if (weekData) $('week-range').textContent = formatDate(weekData.week_start) + ' – ' + formatDate(weekData.week_end);
    $('week-status').textContent = weekBusy ? t('weekLoading') : weekError ? t('weekLoadError') : '';
    ['previous-week', 'current-week', 'next-week', 'refresh-week'].forEach(id => { $(id).disabled = weekBusy; });
    const week = $('my-week'); week.replaceChildren();
    if (!weekData) { if (!weekBusy) week.append(el('p', 'empty-state', t('weekConnect'))); return; }
    const lessons = weekData.mysdu.items || [], deadlines = weekData.moodle.items || [];
    if (!lessons.length && !deadlines.length) {
      const connected = weekData.mysdu.status === 'ready' || weekData.moodle.status === 'ready';
      const failed = ['expired', 'unavailable'].some(status => weekData.mysdu.status === status || weekData.moodle.status === status);
      week.append(el('p', 'empty-state', t(failed ? 'weekSourceError' : connected ? 'weekEmpty' : 'weekConnect')));
      return;
    }
    const monday = new Date(weekData.week_start + 'T00:00:00Z');
    days.forEach((day, index) => {
      const date = new Date(monday); date.setUTCDate(date.getUTCDate() + index);
      const dateKey = date.toISOString().slice(0, 10);
      const entries = [
        ...lessons.filter(item => item.date === dateKey).map(item => ({ ...item, sort: item.start })),
        ...deadlines.filter(item => item.due.slice(0, 10) === dateKey).map(item => ({ ...item, sort: item.due.slice(11, 16) }))
      ].sort((a, b) => a.sort.localeCompare(b.sort));
      if (!entries.length) return;
      const column = el('section', 'day-card' + (dateKey === weekData.today ? ' today' : ''));
      column.append(el('h3', '', t(dayKeys[index])), el('p', 'day-date caption', formatDate(dateKey)));
      entries.forEach(item => {
        const lesson = el('article', 'lesson' + (item.source === 'moodle' ? ' deadline-lesson' : ''));
        const time = item.source === 'moodle' ? t('weekDue') + ' ' + item.sort : item.start + (item.end ? '–' + item.end : '');
        lesson.append(el('span', 'source-label', item.source === 'moodle' ? 'Moodle' : 'MySDU'), el('time', '', time), el('strong', '', item.title || item.course));
        if (item.title !== item.course) lesson.append(el('p', '', item.course));
        if (item.source === 'mysdu') lesson.append(el('p', '', item.room || t('eventVenue')));
        column.append(lesson);
      });
      week.append(column);
    });
  }
  async function loadWeek() {
    const sequence = ++weekSequence;
    weekBusy = true; weekError = false; renderWeek();
    try {
      const response = await fetch('/api/me/week' + (requestedWeek ? '?week=' + encodeURIComponent(requestedWeek) : ''), { cache: 'no-store', credentials: 'same-origin' });
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (sequence !== weekSequence) return;
      weekData = data;
    } catch { if (sequence === weekSequence) { weekError = true; weekData = null; } }
    finally { if (sequence === weekSequence) { weekBusy = false; renderWeek(); } }
  }
  function moveWeek(direction) {
    if (!weekData) return;
    const date = new Date(weekData.week_start + 'T00:00:00Z'); date.setUTCDate(date.getUTCDate() + direction * 7);
    requestedWeek = date.toISOString().slice(0, 10); loadWeek();
  }
  $('previous-week').addEventListener('click', () => moveWeek(-1));
  $('next-week').addEventListener('click', () => moveWeek(1));
  $('current-week').addEventListener('click', () => { requestedWeek = null; loadWeek(); });
  $('refresh-week').addEventListener('click', loadWeek);

  function renderAccountDialog() {
    $('account-brand').textContent = activeAccount === 'mysdu' ? 'MySDU' : 'Moodle';
    $('account-dialog-title').textContent = t(authPhase === 'code' ? 'accountVerifyTitle' : 'accountSignIn');
    $('account-description').textContent = t(authPhase === 'code' ? 'accountCodeHint' : activeAccount === 'mysdu' ? 'accountPortalHint' : 'accountMoodleHint');
    $('account-username-label').textContent = t(activeAccount === 'mysdu' ? 'accountStudentId' : 'accountUsername');
    $('credentials-fields').hidden = authPhase === 'code'; $('code-fields').hidden = authPhase !== 'code';
    $('account-username').required = authPhase !== 'code'; $('account-password').required = authPhase !== 'code'; $('account-code').required = authPhase === 'code';
    $('account-submit').textContent = t(authBusy ? 'accountSigningIn' : authPhase === 'code' ? 'accountVerify' : 'accountSignIn');
    $('account-submit').disabled = authBusy;
    $('close-account').setAttribute('aria-label', t('dismiss'));
  }
  function openAccount(account) {
    activeAccount = account; authPhase = 'credentials'; $('account-form').reset(); $('account-error').textContent = '';
    renderAccountDialog(); $('account-dialog').showModal(); $('account-username').focus();
  }
  $('close-account').addEventListener('click', () => $('account-dialog').close());
  $('account-dialog').addEventListener('close', () => { $('account-form').reset(); $('account-error').textContent = ''; });
  $('account-form').addEventListener('submit', async event => {
    event.preventDefault(); if (authBusy) return;
    authBusy = true; $('account-error').textContent = ''; renderAccountDialog();
    const code = authPhase === 'code';
    const payload = code ? { code: $('account-code').value.trim() } : { username: $('account-username').value.trim(), password: $('account-password').value };
    // Remove secrets from the visible form immediately; they are sent only to the same-origin account endpoint.
    $('account-password').value = ''; $('account-code').value = '';
    try {
      const response = await fetch('/api/me/' + activeAccount + (code ? '/code' : '/login'), {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-SDU-Hub': '1' }, body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) {
        const key = ({ bad_credentials: 'accountBadCredentials', bad_code: 'accountBadCode', session_expired: 'accountExpired', invalid_student_id: 'accountInvalidId', accounts_not_configured: 'accountNotConfigured' })[data.detail] || 'accountUnavailable';
        throw new Error(t(key));
      }
      if (data.status === 'code_required') { authPhase = 'code'; renderAccountDialog(); $('account-code').focus(); }
      else { $('account-dialog').close(); await loadWeek(); }
    } catch (error) { $('account-error').textContent = error instanceof TypeError ? t('weekLoadError') : error.message; }
    finally { payload.password = ''; payload.code = ''; authBusy = false; renderAccountDialog(); }
  });
  async function disconnectAccount(account) {
    try {
      const response = await fetch('/api/me/disconnect', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-SDU-Hub': '1' }, body: JSON.stringify({ account }) });
      if (!response.ok) throw new Error();
      await loadWeek();
    } catch { $('week-status').textContent = t('weekLoadError'); }
  }
  $('new-chat').addEventListener('click', () => {
    if (busy) return;
    history.length = 0; thread.replaceChildren(); $('empty').hidden = false; input.focus();
  });
  $('navigation').addEventListener('keydown', event => {
    const tabs = [...document.querySelectorAll('.tab')];
    const index = tabs.indexOf(document.activeElement);
    if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus(); showTab(tabs[next].dataset.view);
  });
  applyLang();
  updateBotLinks();
  loadEvents();
