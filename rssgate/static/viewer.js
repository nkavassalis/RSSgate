/* RSSgate reader: endless reverse-chronological scroller with resume position,
   feed filter sidebar, and New / Since date-window modes. */
(() => {
  const PAGE = window.RSSGATE_PAGE || 20;
  const $ = id => document.getElementById(id);
  const stream = $('stream');
  const endBanner = $('end-banner');

  // ---- persisted UI state -------------------------------------------------
  const store = {
    get mode() { return localStorage.getItem('rssgate.mode') || 'new'; },
    set mode(v) { localStorage.setItem('rssgate.mode', v); },
    get since() { return localStorage.getItem('rssgate.since') || defaultSince(); },
    set since(v) { localStorage.setItem('rssgate.since', v); },
    get feed() { return localStorage.getItem('rssgate.feed') || ''; },
    set feed(v) { localStorage.setItem('rssgate.feed', v); },
    get pcats() {
      try { return JSON.parse(localStorage.getItem('rssgate.pcats')) || []; }
      catch { return []; }
    },
    set pcats(v) { localStorage.setItem('rssgate.pcats', JSON.stringify(v)); },
    get fcats() {
      try { return JSON.parse(localStorage.getItem('rssgate.fcats')) || []; }
      catch { return []; }
    },
    set fcats(v) { localStorage.setItem('rssgate.fcats', JSON.stringify(v)); },
    set cats(v) { localStorage.setItem('rssgate.cats', JSON.stringify(v)); },
  };
  function dateStr(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
      + '-' + String(d.getDate()).padStart(2, '0');
  }
  function defaultSince() {
    const d = new Date(); d.setDate(d.getDate() - 7); return dateStr(d);
  }

  let cursor = null, loading = false, exhausted = false, started = false,
      saveTimer = null, bootResume = '', order = 'newest';

  const fmt = ts => {
    const d = new Date(ts), now = new Date();
    const secs = (now - d) / 1000;
    if (secs < 3600) return `${Math.max(1, secs / 60 | 0)}m ago`;
    if (secs < 86400) return `${secs / 3600 | 0}h ago`;
    if (secs < 6 * 86400) return `${secs / 86400 | 0}d ago`;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  };
  const esc = s => (s || '').replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function cardHtml(a) {
    const own = (a.post_categories || []).length > 0;
    const topic = own ? a.post_categories : (a.auto_categories || []);
    const cats = topic.map(c =>
        `<span class="chip" title="${own ? 'post categories' : 'feed categories'}">${esc(c)}</span>`).join(' ')
      + (a.categories || []).map(c =>
          `<span class="chip user" title="your category">${esc(c)}</span>`).join(' ');
    const raw = a.feed_summarize === false
      ? ' <span class="chip raw" title="shown as extracted, no LLM used">raw</span>' : '';
    const sub = a.feed_description && a.feed_description !== a.feed_title
      ? `<div class="feed-sub">${esc(a.feed_description)}</div>` : '';
    const body = a.status === 'ready' && a.summary
      ? `<div class="digest">${esc(a.summary)}</div>`
      : a.status === 'error'
        ? `<div class="unsummarized">\u26a0 could not transcribe \u2014 <a href="${esc(a.link)}">read original</a></div>`
        : `<div class="unsummarized">\u23f3 ${a.feed_summarize === false
            ? 'preparing\u2026' : 'waiting for AI transcription\u2026'}</div>`;
    const extra = (a.gallery || []).filter(g => g !== a.image).slice(0, 3);
    const gallery = extra.length
      ? `<div class="gallery">${extra.map(g =>
          `<a href="${esc(a.link)}" target="_blank" rel="noopener"><img src="/image/${esc(g)}" loading="lazy" alt=""></a>`).join('')}</div>`
      : '';
    const readmore = `<p class="readmore"><a href="${esc(a.link)}" target="_blank" rel="noopener">Read the full article at ${esc(a.feed_title || 'the original')} &#8599;</a></p>`;
    const thumb = a.image
      ? `<img class="card-thumb" src="/image/${esc(a.image)}" alt="" loading="lazy">`
      : '';
    return `<article class="card${a.unread ? ' unread' : ''}" data-ts="${esc(a.ts)}"
        data-id="${a.id}" data-feed="${a.feed_id}">
      <div class="card-meta">${a.unread ? '<span class="newdot" title="unread"></span>' : ''}<span class="feed-title">${esc(a.feed_title || '\u2014')}</span>${raw}
        ${cats}<time datetime="${esc(a.ts)}">${fmt(a.ts)}</time><button class="snap-btn" title="Copy snapshot image to clipboard">\u25a3</button></div>
      ${thumb}
      ${sub}
      <h2><a href="${esc(a.link)}" target="_blank" rel="noopener">${esc(a.title)}</a></h2>
      ${body}${gallery}${readmore}</article>`;
  }

  // ---- viewed = read: dwelling on an unread card marks it seen, even with
  // no further scrolling (so re-clicking a view clears what you're looking at)
  const dwellTimers = new WeakMap();
  const dwellObs = new IntersectionObserver(entries => {
    for (const e of entries) {
      const card = e.target;
      if (e.isIntersecting && e.intersectionRatio >= 0.55 &&
          card.classList.contains('unread')) {
        if (!dwellTimers.has(card))
          dwellTimers.set(card, setTimeout(() => markSeen(card), 1100));
      } else if (dwellTimers.has(card)) {
        clearTimeout(dwellTimers.get(card));
        dwellTimers.delete(card);
      }
    }
  }, { threshold: [0, 0.55] });
  function observeCards() {
    stream.querySelectorAll('.card.unread:not([data-obs])').forEach(c => {
      c.dataset.obs = '1';
      dwellObs.observe(c);
    });
  }
  function markSeen(card) {
    dwellTimers.delete(card);
    if (!card.classList.contains('unread')) return;
    card.classList.remove('unread');
    const dot = card.querySelector('.newdot');
    if (dot) dot.remove();
    bumpPill(card.dataset.feed);
    scheduleFeedSync();
    const body = JSON.stringify({
      ts: card.dataset.ts, id: +card.dataset.id,
      reads: { [card.dataset.feed]: card.dataset.ts },
      global: false,
    });
    navigator.sendBeacon && navigator.sendBeacon('/api/position',
      new Blob([body], { type: 'application/json' }))
      || fetch('/api/position', { method: 'POST', body,
          headers: { 'content-type': 'application/json' } });
  }
  let pillSync = null;
  function scheduleFeedSync() {
    clearTimeout(pillSync);
    pillSync = setTimeout(renderFeedFilter, 2500);   // server truth, debounced
  }
  function bumpPill(feedId) {
    const li = document.querySelector(`#feed-filter li[data-feed="${feedId}"]`);
    if (!li) return;
    const pill = li.querySelector('.unread-pill');
    if (!pill) return;
    const n = +pill.textContent || 0;
    if (n > 1) pill.textContent = n - 1;   // count DOWN as articles are read
    else pill.remove();                    // hit zero: fall back to total
    scheduleFeedSync();
  }

  // ---- loading ------------------------------------------------------------
  const seenIds = new Set();
  const progBar = document.getElementById('stream-progress');
  let busySince = 0, busyOff = 0;
  function setBusy(on) {                    // min-dwell so fast LAN fetches
    if (!progBar) return;                   // are actually perceivable
    clearTimeout(busyOff);
    if (on) { busySince = performance.now(); progBar.classList.add('on'); }
    else {
      const left = Math.max(0, 400 - (performance.now() - busySince));
      busyOff = setTimeout(() => progBar.classList.remove('on'), left);
    }
  }
  async function loadNext() {
    if (loading || exhausted) return;
    loading = true;
    setBusy(true);
    const params = new URLSearchParams({ limit: PAGE, order });
    const PRIO = order === 'newest' && !store.feed && store.mode === 'new';
    if (PRIO) params.set('prio', '1');
    if (cursor) {
      params.set('before_ts', cursor.ts); params.set('before_id', cursor.id);
      if (PRIO && cursor.u !== undefined) params.set('before_u', cursor.u);
    }
    else if (store.mode === 'new' && !store.feed && order === 'newest')
      params.set('fresh', '1');   // oldest mode: no fresh => continue at resume
    if (store.feed) params.set('feed_id', store.feed);
    for (const c of store.pcats) params.append('category', c);
    for (const c of store.fcats) params.append('feed_category', c);
    if (store.mode === 'since') params.set('since_ts', store.since + 'T00:00:00Z');
    let data;
    try {
      const res = await fetch('/api/articles?' + params);
      data = await res.json();
    } finally {
      loading = false; setBusy(false);   // failed fetches must not spin
    }
    if (!started) {
      started = true;
      if (!data.items.length) $('empty-hint').hidden = false;
    }
    // priority mode shuffles rows as read-state changes mid-scroll;
    // keyset drift is real, so the client de-dupes by id.
    const rawLast = PRIO ? (data.items || []).at(-1) : null;
    if (PRIO) data.items = data.items.filter(a => !seenIds.has(a.id)
                                             && seenIds.add(a.id));
    if (data.items.length) {
      $('empty-hint').hidden = true;
      endBanner.hidden = true;
      data.items.forEach(a => { snapData[a.id] = a; });
      stream.insertAdjacentHTML('beforeend', data.items.map(cardHtml).join(''));
      observeCards();
      const last = data.items.at(-1) || (PRIO ? rawLast : null);
      if (last) cursor = PRIO ? { ts: last.ts, id: last.id,
                                  u: last.unread | 0 }
                              : { ts: last.ts, id: last.id };
    }
    if (!data.has_more) {
      exhausted = true;
      if (started || data.items.length) showEnd();
      return;
    }
    if (document.body.scrollHeight <= window.innerHeight + 200) loadNext();
  }

  function showEnd() {
    endBanner.hidden = false;
    if (store.mode === 'since' || order === 'oldest') {
      $('end-new').hidden = true; $('end-since').hidden = false;
      $('end-since-text').textContent = store.mode === 'since'
        ? `That's everything since ${store.since}.`
        : "You're all caught up — new arrivals appear at the end of this list.";
    } else {
      $('end-new').hidden = false; $('end-since').hidden = true;
      $('jump-date').value = yesterdayStr();
    }
  }
  function yesterdayStr() {
    const d = new Date(); d.setDate(d.getDate() - 1); return dateStr(d);
  }

  let SHARE_W = 800;
  function restart(keepDrawer) {
    stream.innerHTML = ''; cursor = null; exhausted = false;
    seenIds.clear();
    started = false; endBanner.hidden = true;
    if (!keepDrawer) {
      $('sidebar').classList.remove('open'); $('sidebar-veil').classList.remove('show');
    }
    loadNext();
  }

  // ---- read tracking: feed cursors advance everywhere, resume cursor only
  // in the unfiltered New view (so filters never hijack your resume point) ----
  function savePosition() {
    const cards = [...stream.querySelectorAll('.card')];
    if (!cards.length) return;
    const cutoff = window.scrollY + window.innerHeight * 0.6;
    const passed = cards.filter(c => c.offsetTop <= cutoff);
    if (!passed.length) return;
    // per-feed cursor = NEWEST passed card of that feed (fast scrolling past
    // an unread card must still mark it read; one oldest-position beacon did not)
    const reads = {};
    for (const c of passed) {
      const f = c.dataset.feed, t = c.dataset.ts;
      if (f && (!reads[f] || t > reads[f])) reads[f] = t;
    }
    // newest mode: resume = deepest (oldest) card passed;
    // oldest mode: resume = frontier (newest) card passed
    let pos = passed[passed.length - 1];
    if (order === 'oldest')
      for (const c of passed) if (c.dataset.ts > pos.dataset.ts) pos = c;
    const body = JSON.stringify({
      ts: pos.dataset.ts, id: +pos.dataset.id, reads,
      global: store.mode === 'new' && !store.feed,
    });
    // local reflection: cards you just passed ARE read — flip them now
    // instead of waiting for a re-fetch, and resync the sidebar pills
    let flipped = false;
    for (const c of passed) {
      if (c.classList.contains('unread')) {
        c.classList.remove('unread');
        const dot = c.querySelector('.newdot');
        if (dot) dot.remove();
        bumpPill(c.dataset.feed);
        flipped = true;
      }
    }
    if (flipped) setTimeout(renderFeedFilter, 500);  // after beacon lands
    navigator.sendBeacon && navigator.sendBeacon('/api/position',
      new Blob([body], { type: 'application/json' }))
      || fetch('/api/position', { method: 'POST', body,
          headers: { 'content-type': 'application/json' } });
  }
  function queueSave() { clearTimeout(saveTimer); saveTimer = setTimeout(savePosition, 1200); }
  window.addEventListener('scroll', queueSave, { passive: true });
  document.addEventListener('visibilitychange', () => { if (document.hidden) savePosition(); });
  window.addEventListener('pagehide', savePosition);

  // ---- card snapshot (copy-as-image for sharing) --------------------------
  const snapData = {};
  function wrapLines(x, text, maxW) {
    const words = (text || '').split(/\s+/).filter(Boolean);
    const out = []; let line = '';
    for (const w of words) {
      const t = line ? line + ' ' + w : w;
      if (x.measureText(t).width > maxW && line) { out.push(line); line = w; }
      else line = t;
    }
    if (line) out.push(line);
    return out;
  }
  function roundRect(x, X, Y, w, h, r) {
    x.beginPath(); x.moveTo(X + r, Y);
    x.arcTo(X + w, Y, X + w, Y + h, r); x.arcTo(X + w, Y + h, X, Y + h, r);
    x.arcTo(X, Y + h, X, Y, r); x.arcTo(X, Y, X + w, Y, r); x.closePath();
  }
  async function loadBitmap(u) {
    const b = await (await fetch(u)).blob();
    return await createImageBitmap(b);
  }
  async function renderCardPng(a) {
    const cs = getComputedStyle(document.documentElement);
    const col = (n, fb) => (cs.getPropertyValue(n) || '').trim() || fb;
    const dark = matchMedia('(prefers-color-scheme: dark)').matches;
    const W = SHARE_W, PAD = 28, DPR = 2;
    const fam = getComputedStyle(document.body).fontFamily;
    const m = document.createElement('canvas').getContext('2d');
    const hero = a.image ? await loadBitmap('/image/' + a.image).catch(() => null) : null;
    m.font = `700 26px ${fam}`;
    const tLines = wrapLines(m, a.title || '(untitled)', W - PAD * 2).slice(0, 4);
    m.font = `16px ${fam}`;
    const dAll = wrapLines(m, a.summary || '', W - PAD * 2);
    const dLines = dAll.slice(0, 40);
    if (dAll.length > 40) dLines[39] = dLines[39].replace(/[,.;:\s]+$/, '') + '\u2026';
    const when = new Date(a.ts).toLocaleString(undefined, {
      month: 'short', day: 'numeric', year: 'numeric',
      hour: 'numeric', minute: '2-digit' });      // sharer's timezone
    const meta = [(a.feed_title || '').trim(), when].filter(Boolean).join('  \u00b7  ');
    const url = (a.link || '').replace(/^https?:\/\//, '').slice(0, 72);
    const qr = a.link ? await loadBitmap('/api/qr.png?u=' +
                          encodeURIComponent(a.link)).catch(() => null) : null;
    const heroH = hero ? Math.min(300, Math.round((W - PAD * 2) * hero.height / hero.width)) : 0;
    const titleH = tLines.length * 33 + 6;
    const digestH = dLines.length * 24 + 10;
    const footH = qr ? 78 : (url ? 24 : 0);
    const H = PAD + (heroH ? heroH + 20 : 0) + titleH + digestH + 26
              + 18 + footH + PAD - 8;
    const cv = document.createElement('canvas');
    cv.width = W * DPR; cv.height = H * DPR;
    const x = cv.getContext('2d'); x.scale(DPR, DPR);
    x.fillStyle = col('--card', dark ? '#1b1c1e' : '#ffffff'); x.fillRect(0, 0, W, H);
    let y = PAD;
    if (hero) {
      const hw = W - PAD * 2;
      x.save(); roundRect(x, PAD, y, hw, heroH, 10); x.clip();
      x.drawImage(hero, PAD, y, hw, heroH); x.restore(); y += heroH + 20;
    }
    x.fillStyle = col('--text', dark ? '#e8e8ea' : '#17181a'); x.font = `700 26px ${fam}`;
    tLines.forEach(l => { y += 26; x.fillText(l, PAD, y); });
    y += 7 + 6;
    x.fillStyle = col('--muted', '#71717a'); x.font = `14px ${fam}`;
    x.fillText(meta, PAD, y + 12); y += 26;
    x.fillStyle = col('--text', dark ? '#e8e8ea' : '#17181a'); x.font = `16px ${fam}`;
    dLines.forEach(l => { y += 24; x.fillText(l, PAD, y); });
    y += 10 + 18;
    if (qr) {
      const qs = 72, qy = y + footH - qs;
      x.fillStyle = '#ffffff'; x.fillRect(W - PAD - qs, qy, qs, qs);
      x.drawImage(qr, W - PAD - qs, qy, qs, qs);
      x.textBaseline = 'bottom';               // labels sit on the QR's base
      x.fillStyle = col('--accent', '#7c5cff'); x.font = `700 13px ${fam}`;
      x.fillText('via RSSgate', PAD, qy + qs - 2);
      x.fillStyle = col('--muted', '#71717a'); x.font = `13px ${fam}`;
      x.textAlign = 'right';
      x.fillText('Scan for the full article', W - PAD - qs - 14, qy + qs - 2);
      x.textAlign = 'left'; x.textBaseline = 'alphabetic';
    } else if (url) {
      x.fillStyle = col('--muted', '#71717a'); x.font = `13px ${fam}`;
      x.fillText(url, PAD, y + 10);
      x.fillStyle = col('--accent', '#7c5cff'); x.font = `700 13px ${fam}`;
      x.textAlign = 'right'; x.fillText('via RSSgate', W - PAD, y + 10);
      x.textAlign = 'left';
    }
    return await new Promise(res => cv.toBlob(res, 'image/png'));
  }
  async function doSnapshot(btn) {
    const card = btn.closest('.card');
    const a = card && snapData[card.dataset.id];
    if (!a) return;
    const glyph = btn.textContent;
    btn.textContent = '\u2026';
    try {
      const png = await renderCardPng(a);
      let copied = false;
      try {
        await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
        copied = true;
      } catch {                     // http contexts: download instead (attachable)
        const u = URL.createObjectURL(png);
        const link = document.createElement('a');
        link.href = u; link.download = `rssgate-${a.id}.png`;
        document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(u), 5000);
      }
      btn.textContent = copied ? '\u2713' : '\u2913';
    } catch { btn.textContent = '\u2717'; }
    setTimeout(() => { btn.textContent = glyph; }, 1400);
  }
  $('stream').addEventListener('click', e => {
    const btn = e.target.closest('.snap-btn');
    if (btn) doSnapshot(btn);
  });

  // ---- pull to refresh (mobile) ------------------------------------------
  const ptr = $('ptr'), ptrLabel = ptr.querySelector('.ptr-label');
  let ptrY = 0, ptrX0 = 0, ptrDist = 0, ptrArmed = false, ptrBusy = false;
  const PTR_MIN = 8, PTR_GO = 72;
  window.addEventListener('touchstart', e => {
    if (window.scrollY > 2) return;
    const tgt = e.target instanceof Element ? e.target : null;
    if (tgt && tgt.closest('#sidebar, #lightbox, .modal')) return;
    ptrY = e.touches[0].clientY; ptrX0 = e.touches[0].clientX;
    ptrDist = 0; ptrArmed = true;
    ptr.classList.remove('spin');
  }, { passive: true });
  window.addEventListener('touchmove', e => {
    if (!ptrArmed || ptrBusy) return;
    const d = e.touches[0].clientY - ptrY;
    // Same intent gate as the drawer: if the gesture leads HORIZONTALLY
    // (diagonal thumb-arc drawer swipes), it's a swipe, not a pull -
    // stand down (v0.37.2).
    const dx = Math.abs(e.touches[0].clientX - ptrX0);
    if (dx > 24 && dx > d) { ptrArmed = false; ptrDist = 0; return; }
    if (d < PTR_MIN || window.scrollY > 2) { ptrDist = 0; return; }
    ptrDist = Math.min(120, d * 0.5);           // rubber-band resistance
    ptr.style.transform = `translate(-50%, ${-80 + ptrDist}px)`;
    ptr.style.opacity = Math.min(1, ptrDist / 40);
    ptrLabel.textContent = ptrDist >= PTR_GO ? 'Release to refresh' : 'Pull to refresh';
    ptr.classList.toggle('ready', ptrDist >= PTR_GO);
  }, { passive: true });
  function ptrEnd() {
    if (!ptrArmed) return;
    ptrArmed = false;
    const go = ptrDist >= PTR_GO && !ptrBusy;
    ptrDist = 0;
    ptr.style.transform = 'translate(-50%,-80px)';
    ptr.style.opacity = 0;
    ptr.classList.remove('ready');
    if (go) doPullRefresh();
  }
  window.addEventListener('touchend', ptrEnd, { passive: true });
  window.addEventListener('touchcancel', ptrEnd, { passive: true });
  async function doRefreshWork() {       // shared by button + pull-to-refresh
    try {
      const r = await fetch('/api/poll', { method: 'POST' });
      const j = await r.json().catch(() => ({}));
      if (!j.throttled)                  // real poll: let the fetchers land
        await new Promise(rz => setTimeout(rz, 2500));
    } catch { /* offline: restart anyway */ }
    restart();
  }
  async function doPullRefresh() {
    ptrBusy = true;
    setBusy(true);                       // top rail is the work indicator now;
    ptrLabel.textContent = 'Pull to refresh';   // pill retreats during work
    await doRefreshWork();
    setBusy(false);
    ptrBusy = false;
  }

  // ---- lightbox: click any cached image to see it full-size --------------
  let lb = null;
  function closeLb() { if (lb) { lb.remove(); lb = null; document.removeEventListener('keydown', lbKey); } }
  function lbKey(e) { if (e.key === 'Escape') closeLb(); }
  function openLb(src, alt) {
    closeLb();
    lb = document.createElement('div');
    lb.id = 'lightbox';
    lb.innerHTML = `<img src="${esc(src)}" alt="${esc(alt || '')}">
      <span class="lb-hint">click anywhere or Esc to close</span>`;
    lb.addEventListener('click', closeLb);
    document.body.appendChild(lb);
    document.addEventListener('keydown', lbKey);
  }
  document.addEventListener('click', e => {
    const img = e.target.closest('img.card-thumb, .gallery img');
    if (!img) return;
    e.preventDefault(); e.stopPropagation();
    openLb(img.currentSrc || img.src, img.alt);
  });

  // ---- sidebar: mode toggle, presets, feed filter -------------------------
  function setMode(mode) {
    store.mode = mode;
    $('mode-new').classList.toggle('active', mode === 'new');
    $('mode-since').classList.toggle('active', mode === 'since');
    $('since-ctrl').hidden = mode !== 'since';
    restart();
  }
  $('mode-new').addEventListener('click', () => setMode('new'));
  $('mode-since').addEventListener('click', () => setMode('since'));
  $('since-date').addEventListener('change', e => {
    if (e.target.value) { store.since = e.target.value; restart(); }
  });
  document.querySelectorAll('.presets button').forEach(b =>
    b.addEventListener('click', () => {
      const d = new Date(); d.setDate(d.getDate() - (+b.dataset.days));
      store.since = dateStr(d); $('since-date').value = store.since;
      setMode('since');
    }));
  $('back-now-btn').addEventListener('click', () => setMode('new'));
  $('jump-btn').addEventListener('click', () => {
    const v = $('jump-date').value || yesterdayStr();
    store.since = v; $('since-date').value = v; setMode('since');
  });
  $('back-btn').addEventListener('click', () => setMode('new'));
  $('menu-btn').addEventListener('click', () => {
    $('sidebar').classList.toggle('open');
    $('sidebar-veil').classList.toggle('show');
  });
  $('sidebar-veil').addEventListener('click', () => {
    $('sidebar').classList.remove('open'); $('sidebar-veil').classList.remove('show');
  });

  // ---- edge-swipe drawer gesture (mobile) ---------------------------------
  // Closed: start within 26px of the left edge and drag right to reveal
  // (tracks the finger). Open: drag left anywhere on the veil to close.
  const drawer = $('sidebar'), dwVeil = $('sidebar-veil');
  let dw = { active: false };
  window.addEventListener('touchstart', e => {
    const tgt = e.target instanceof Element ? e.target : null;
    if (tgt && tgt.closest('#lightbox, .modal, #ptr')) return;
    const touch = e.touches[0];
    const onSidebar = tgt && tgt.closest('#sidebar');
    const open = drawer.classList.contains('open');
    if (!open && (touch.clientX > 22 || onSidebar)) return;  // edge-only when closed
    if (open && onSidebar) return;                            // scroll inside drawer
    dw = { active: true, x0: touch.clientX, y0: touch.clientY, axis: null,
           open, w: 250, x: open ? 250 : 0 };
  }, { passive: true });
  window.addEventListener('touchmove', e => {
    if (!dw.active) return;
    const t = e.touches[0];
    const dx = t.clientX - dw.x0, dy = t.clientY - dw.y0;
    if (!dw.axis) {
      // Decisive-intent only: vertical scrolls carry horizontal jitter,
      // so 'x' needs a 14px lead AND clear dominance before anything moves.
      if (Math.abs(dy) >= 10 && Math.abs(dy) > Math.abs(dx) * 1.2) {
        dw.active = false; return;                 // just scrolling: hands off
      }
      if (!(Math.abs(dx) >= 14 && Math.abs(dx) > Math.abs(dy) * 1.2)) return;
      dw.axis = 'x';
      drawer.classList.add('dragging');
      if (!dw.open) dwVeil.classList.add('show');  // veil rides the reveal
    }
    dw.x = Math.max(0, Math.min(dw.w, (dw.open ? dw.w : 0) + dx));
    drawer.style.transform = `translateX(${dw.x - dw.w}px)`;
    dwVeil.style.opacity = dw.x / dw.w;
  }, { passive: true });
  function dwEnd() {
    if (!dw.active) return;
    dw.active = false;
    drawer.classList.remove('dragging');
    drawer.style.transform = '';
    dwVeil.style.opacity = '';
    if (dw.axis === 'x') {
      const open = dw.x > dw.w * 0.5;
      drawer.classList.toggle('open', open);
      dwVeil.classList.toggle('show', open);
    } else if (dw.open) {
      drawer.classList.add('open'); dwVeil.classList.add('show');
    }
  }
  window.addEventListener('touchend', dwEnd, { passive: true });
  window.addEventListener('touchcancel', dwEnd, { passive: true });
  $('refresh-btn').addEventListener('click', async e => {
    const btn = e.currentTarget;
    if (btn.classList.contains('spinning')) return;
    btn.classList.add('spinning');
    setBusy(true);                       // rail covers the whole poll, too
    await doRefreshWork();
    btn.classList.remove('spinning');
    setBusy(false);
  });

  function setFeedFilter(id) {
    store.feed = id;
    document.querySelectorAll('#feed-filter li').forEach(li =>
      li.classList.toggle('active', li.dataset.feed === id));
    renderFeedFilter();   // pills reflect server truth on every switch
    restart();
  }
  // ---- category chips: two independent boxes, multi-select ---------------
  const boxExpanded = {};
  function drawChipBox(boxId, cats, storeKey, param) {
    const sel = store[storeKey];
    cats.sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
    const expanded = boxExpanded[boxId];
    const shown = expanded ? cats
      : cats.filter(c => sel.includes(c.name) || cats.indexOf(c) < 12);
    const hidden = cats.length - shown.length;
    const box = $(boxId);
    box.innerHTML = `<button class="chip${sel.length ? '' : ' active'}"
        data-cat="">All</button>`
      + shown.map(c => `<button class="chip${sel.includes(c.name) ? ' active' : ''}"
          data-cat="${esc(c.name)}" title="${c.count} article${c.count === 1 ? '' : 's'}">${esc(c.name)}<small>${c.count}</small></button>`).join('')
      + (hidden ? `<button class="chip more" data-more="1">more (${hidden}) &#8230;</button>` : '');
    box.querySelectorAll('.chip').forEach(b =>
      b.addEventListener('click', () => {
        if (b.dataset.more) { boxExpanded[boxId] = true; renderChips(); return; }
        const name = b.dataset.cat;
        let sel2 = name ? [...sel] : [];
        if (!name) sel2 = [];                                    // All clears box
        else if (sel2.includes(name)) sel2 = sel2.filter(x => x !== name);
        else sel2.push(name);
        store[storeKey] = sel2;
        renderChips();
        restart(true);   // keep drawer open for multi-select on mobile
      }));
  }
  async function renderChips() {
    const data = await fetch('/api/categories?viewer=1').then(r => r.json());
    drawChipBox('feedcat-filter', data.feed || [], 'fcats', 'feed_category');
    drawChipBox('cat-filter', data.post || [], 'pcats', 'category');
  }

  async function renderFeedFilter() {
    const feeds = await fetch('/api/feeds').then(r => r.json());
    const ul = $('feed-filter');
    ul.innerHTML = '<li data-feed="" class="' + (store.feed ? '' : 'active') + '">All feeds' + '</li>'
      + feeds.map(f => `<li data-feed="${f.id}" class="${store.feed == f.id ? 'active' : ''}"
           title="${esc(f.url)}"><span class="fname">${esc(f.display_title || f.title || f.url)}</span>
           ${f.unread ? `<b class="unread-pill">${f.unread}</b>`
                      : `<span>${f.article_count}</span>`}</li>`)
          .join('');
    ul.querySelectorAll('li').forEach(li =>
      li.addEventListener('click', () => setFeedFilter(li.dataset.feed)));
  }

  // ---- boot ---------------------------------------------------------------
  $('since-date').value = store.since;
  $('mode-new').classList.toggle('active', store.mode === 'new');
  $('mode-since').classList.toggle('active', store.mode === 'since');
  $('since-ctrl').hidden = store.mode !== 'since';

  new IntersectionObserver(entries => {
    if (entries[0].isIntersecting) loadNext();
  }, { rootMargin: '1200px' }).observe($('sentinel'));

  Promise.all([
    fetch('/api/resume').then(r => r.json()),
    renderFeedFilter(),
    renderChips(),
  ]).then(([s]) => {
    bootResume = s.resume_ts || '';
    order = s.order === 'oldest' ? 'oldest' : 'newest';
    if (s.snapshot_width >= 360 && s.snapshot_width <= 1440)
      SHARE_W = Math.round(s.snapshot_width);
    if (s.stream_width >= 480 && s.stream_width <= 1600)
      document.documentElement.style
        .setProperty('--stream-w', Math.round(s.stream_width) + 'px');
    // Stream ALWAYS boots at newest (what "caught up" means).
    // The saved resume position becomes an explicit "continue" option —
    // except in oldest mode, where continuing at resume IS the boot.
    if (order === 'newest' && store.mode === 'new' && !store.feed
        && s.resume_ts && s.newest_ts && s.resume_ts < s.newest_ts) {
      const d = s.resume_ts.slice(0, 10);
      $('new-above-btn').innerHTML =
        `&#8681; Continue reading from ${d}`;
      $('new-above').hidden = false;
      $('new-above-btn').dataset.resume = s.resume_ts;
      $('new-above-btn').dataset.resumeId = s.resume_id || '0';
    }
    loadNext();
  });

  // ---- continue-reading: jump the stream to the saved resume point -------
  function hideNewAbove() { $('new-above').hidden = true; }
  $('new-above-btn').addEventListener('click', function () {
    hideNewAbove();
    stream.innerHTML = ''; exhausted = false; started = false;
    endBanner.hidden = true;
    cursor = { ts: this.dataset.resume, id: +this.dataset.resumeId };
    window.scrollTo(0, 0);
    loadNext();
  });
})();
