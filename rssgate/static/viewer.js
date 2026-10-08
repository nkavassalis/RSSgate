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
    const raw = a.digest_source === 'excerpt'
      ? ' <span class="chip raw excerpt" title="the full article could not be fetched (the site blocks automated readers), so this is the excerpt the feed itself provides">\u26a0 feed excerpt</span>'
      : a.digest_source === 'feed'
        ? ' <span class="chip raw" title="this feed is set to use the text the feed provides, without fetching the article page">from feed</span>'
      : a.feed_summarize === false
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
  let READ_DELAY_MS = 5000;                 // ui.read_delay via /api/resume
  const visCards = new Set();
  let curCard = null, curTimer = 0;
  function armRead() {
    let best = null, bestTop = Infinity;
    for (const c of visCards) {
      if (!c.classList.contains('unread')) continue;
      const t = c.getBoundingClientRect().top;
      if (t < bestTop) { best = c; bestTop = t; }
    }
    if (best === curCard) return;          // sticky: no timer churn
    if (curTimer) { clearTimeout(curTimer); curTimer = 0; }
    curCard = best;
    if (!curCard) return;
    if (READ_DELAY_MS <= 0) { markSeen(curCard); armRead(); return; }
    curTimer = setTimeout(() => {
      curTimer = 0; markSeen(curCard); armRead();
    }, READ_DELAY_MS);
  }
  // "being read" = most of the card is on screen, or (cards taller than
  // the screen) it fills most of the screen - a partly-entered card never is
  const dwellObs = new IntersectionObserver(entries => {
    for (const e of entries) {
      const fills = e.intersectionRect.height >= window.innerHeight * 0.6;
      const vis = e.isIntersecting && (e.intersectionRatio >= 0.55 || fills);
      if (vis) visCards.add(e.target); else visCards.delete(e.target);
    }
    armRead();
  }, { threshold: [0, 0.1, 0.25, 0.4, 0.55, 0.7, 0.85, 1] });
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
    sendPosition({ read_ids: [+card.dataset.id] });
  }
  function sendPosition(payload) {
    const body = JSON.stringify(payload);
    navigator.sendBeacon && navigator.sendBeacon('/api/position',
      new Blob([body], { type: 'application/json' }))
      || fetch('/api/position', { method: 'POST', body,
          headers: { 'content-type': 'application/json' } });
  }
  // clicking anywhere on an unread card (link, image, share, text) reads it
  stream.addEventListener('click', e => {
    const card = e.target.closest && e.target.closest('.card.unread');
    if (card) markSeen(card);
  });
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
  let SHARE_STYLE = 'banner';   // banner | float (ui.share_style)
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
    // resume point (position, not read state): cards reaching 60% down
    const cutoff = window.scrollY + window.innerHeight * 0.6;
    const passed = cards.filter(c => c.offsetTop <= cutoff);
    // READ = scrolled ENTIRELY past (bottom edge above the screen top); a
    // card that has merely started to appear is not read
    const gone = cards.filter(c => c.classList.contains('unread')
                 && c.offsetTop + c.offsetHeight <= window.scrollY);
    const readIds = gone.map(c => +c.dataset.id);
    for (const c of gone) {
      c.classList.remove('unread');
      const dot = c.querySelector('.newdot');
      if (dot) dot.remove();
      bumpPill(c.dataset.feed);
    }
    if (gone.length) setTimeout(renderFeedFilter, 500);   // after beacon
    const global = store.mode === 'new' && !store.feed;
    if (!global && !readIds.length) return;
    const payload = { read_ids: readIds };
    if (global && passed.length) {
      // newest mode: resume = deepest (oldest) card passed;
      // oldest mode: resume = frontier (newest) card passed
      let pos = passed[passed.length - 1];
      if (order === 'oldest')
        for (const c of passed) if (c.dataset.ts > pos.dataset.ts) pos = c;
      Object.assign(payload, { ts: pos.dataset.ts, id: +pos.dataset.id,
                               global: true });
    }
    sendPosition(payload);
  }
  function queueSave() { clearTimeout(saveTimer); saveTimer = setTimeout(savePosition, 1200); }
  window.addEventListener('scroll', queueSave, { passive: true });
  document.addEventListener('visibilitychange', () => { if (document.hidden) savePosition(); });
  window.addEventListener('pagehide', savePosition);

  // ---- card snapshot (copy-as-image for sharing) --------------------------
  const snapData = {};
  function roundRect(x, X, Y, w, h, r) {
    x.beginPath(); x.moveTo(X + r, Y);
    x.arcTo(X + w, Y, X + w, Y + h, r); x.arcTo(X + w, Y + h, X, Y + h, r);
    x.arcTo(X, Y + h, X, Y, r); x.arcTo(X, Y, X + w, Y, r); x.closePath();
  }
  async function loadBitmap(u) {
    const b = await (await fetch(u)).blob();
    return await createImageBitmap(b);
  }
  // ---- share card ---------------------------------------------------------
  // Architecture: shareLayout() is PURE geometry - it measures text and
  // returns { W, H, ops, geo }: a display list of draw ops plus geo.rects /
  // geo.lines (logical px) that tests assert on. paintShare() is a dumb
  // interpreter of ops. Add a style = add a layout function; never draw in
  // layout, never measure in paint.
  function wordsWrap(m, words, tw) {    // longest prefix of words fitting tw
    let line = '', i = 0;
    for (; i < words.length; i++) {
      const t = line ? line + ' ' + words[i] : words[i];
      if (m.measureText(t).width > tw && line) break;
      line = t;
    }
    return [line || words[0] || '', Math.max(i, 1)];
  }
  function wrapMax(m, text, tw, max) {
    const out = []; let words = (text || '').split(/\s+/).filter(Boolean);
    while (words.length && out.length < max) {
      const [line, n] = wordsWrap(m, words, tw);
      out.push(line); words = words.slice(n);
    }
    if (words.length && out.length)
      out[out.length - 1] = out[out.length - 1].replace(/[,.;:\s]+$/, '') + '\u2026';
    return out;
  }
  const paragraphs = s => (s || '').split(/\n\s*\n/).map(p => p.trim())
                                   .filter(Boolean);
  const textW = (m, font, t) => { m.font = font; return m.measureText(t).width; };

  function shareLayout(a, env) {
    return env.style === 'float' ? layoutFloat(a, env) : layoutBanner(a, env);
  }

  // Banner: hero full-bleed on top, title, meta, one comfortable digest
  // column, hairline, footer row (QR + "Read the full article" + domain |
  // via RSSgate). No floats -> nothing can collide, any title/image works.
  function layoutBanner(a, env) {
    const { W, m, fam, hero, qr } = env;
    const PAD = Math.round(W * 0.045), ops = [], R = {}, lines = [];
    let y = 0;
    if (hero) {
      const ar = Math.min(2.4, Math.max(16 / 9, hero.w / hero.h));
      const bh = Math.round(W / ar), T = W / bh;
      let sx = 0, sy = 0, sw = hero.w, sh = hero.h;
      if (hero.w / hero.h > T) { sw = hero.h * T; sx = (hero.w - sw) / 2; }
      else { sh = hero.w / T; sy = (hero.h - sh) / 2; }
      ops.push({ k: 'img', src: 'hero', sx, sy, sw, sh, x: 0, y: 0, w: W, h: bh });
      R.hero = [0, 0, W, bh];
      y = bh;
    }
    const meas = W - PAD * 2;
    y += PAD;
    const tf = `700 ${Math.round(W * 0.046)}px ${fam}`, tlh = Math.round(W * 0.056);
    m.font = tf;
    const tl = wrapMax(m, a.title || '(untitled)', meas, 4);
    tl.forEach((t, i) => ops.push({ k: 'text', t, font: tf, color: 'text',
                                    x: PAD, y: y + tlh * i + tlh * 0.78 }));
    R.title = [PAD, y, Math.max(...tl.map(t => textW(m, tf, t))), tlh * tl.length];
    y += tlh * tl.length + 8;
    const mf = `${Math.round(W * 0.02)}px ${fam}`;
    ops.push({ k: 'text', t: env.meta, font: mf, color: 'muted', x: PAD, y: y + 15 });
    R.meta = [PAD, y, textW(m, mf, env.meta), 20];
    y += 20 + Math.round(W * 0.03);
    const df = `${Math.round(W * 0.0275)}px ${fam}`, dlh = Math.round(W * 0.042);
    m.font = df;
    const dTop = y, MAXL = 28;
    let n = 0, trunc = false;
    paragraphs(a.summary).forEach((p, pi) => {
      if (trunc) return;
      if (pi) y += Math.round(dlh * 0.45);
      let words = p.split(/\s+/);
      while (words.length) {
        if (n >= MAXL) { trunc = true; break; }
        const [t, k] = wordsWrap(m, words, meas);
        words = words.slice(k);
        ops.push({ k: 'text', t, font: df, color: 'text', x: PAD, y: y + dlh * 0.75 });
        lines.push([PAD, y, textW(m, df, t), dlh]); y += dlh; n++;
      }
    });
    if (trunc) { const o = ops[ops.length - 1];
                 o.t = o.t.replace(/[,.;:\s]+$/, '') + '\u2026'; }
    R.digest = [PAD, dTop, meas, y - dTop];
    y += Math.round(W * 0.03);
    ops.push({ k: 'fill', x: PAD, y, w: meas, h: 1, color: 'line' });
    R.rule = [PAD, y, meas, 1];
    y += Math.round(W * 0.025);
    const QS = qr ? Math.round(W * 0.12) : 0, FH = Math.max(QS, 24);
    const vf = `700 ${Math.round(W * 0.019)}px ${fam}`;
    if (qr) {
      ops.push({ k: 'fill', x: PAD, y, w: QS, h: QS, color: '#ffffff' });
      ops.push({ k: 'img', src: 'qr', x: PAD, y, w: QS, h: QS });
      R.qr = [PAD, y, QS, QS];
      const lf = `600 ${Math.round(W * 0.021)}px ${fam}`;
      const sf = `${Math.round(W * 0.018)}px ${fam}`;
      const lx = PAD + QS + 16, cy = y + QS / 2;
      ops.push({ k: 'text', t: 'Read the full article', font: lf, color: 'text',
                 x: lx, y: cy - 4 });
      ops.push({ k: 'text', t: env.domain, font: sf, color: 'muted', x: lx, y: cy + 18 });
      R.caption = [lx, cy - 22, Math.max(textW(m, lf, 'Read the full article'),
                                         textW(m, sf, env.domain)), 46];
    } else if (env.domain) {
      const sf = `${Math.round(W * 0.018)}px ${fam}`;
      ops.push({ k: 'text', t: env.domain, font: sf, color: 'muted', x: PAD, y: y + 17 });
      R.caption = [PAD, y, textW(m, sf, env.domain), 22];
    }
    const vw = textW(m, vf, 'via RSSgate');
    ops.push({ k: 'text', t: 'via RSSgate', font: vf, color: 'accent',
               x: W - PAD - vw, y: y + FH / 2 + 5 });
    R.via = [W - PAD - vw, y + FH / 2 - 10, vw, 18];
    const H = Math.round(y + FH + PAD);
    return { W, H, ops, geo: { style: 'banner', rects: R, lines } };
  }

  // Float ("magazine"): full-width title + meta, hero floats top-right of
  // the digest, QR (+caption) sinks to the digest's bottom-left corner via a
  // fixed-point loop, via RSSgate bottom-right.
  function layoutFloat(a, env) {
    const { W, m, fam, hero, qr } = env;
    const PAD = 28, GAP = 16, CAP = 22, ops = [], R = {};
    let iw = 0, ih = 0;
    if (hero) {
      const s = Math.min(W * 0.30 / hero.w, 175 / hero.h);
      iw = Math.round(hero.w * s); ih = Math.round(hero.h * s);
    }
    const imgX = W - PAD - iw;
    const QS = qr ? Math.round(Math.max(72, Math.min((ih || 100) * 0.8, 120))) : 0;
    const tf = `700 26px ${fam}`;
    m.font = tf;
    const tl = wrapMax(m, a.title || '(untitled)', W - PAD * 2, 4);
    tl.forEach((t, i) => ops.push({ k: 'text', t, font: tf, color: 'text',
                                    x: PAD, y: PAD + 26 + 33 * i }));
    R.title = [PAD, PAD, Math.max(...tl.map(t => textW(m, tf, t))), tl.length * 33 + 6];
    const yMeta = PAD + tl.length * 33 + 12;
    const mf = `14px ${fam}`;
    ops.push({ k: 'text', t: env.meta, font: mf, color: 'muted', x: PAD, y: yMeta + 12 });
    R.meta = [PAD, yMeta, textW(m, mf, env.meta), 16];
    const top = yMeta + 26, heroB = top + ih, df = `16px ${fam}`;
    function flow(qzT) {
      m.font = df;
      const out = []; let dY = top, trunc = false;
      outer: for (const [p, para] of paragraphs(a.summary).entries()) {
        if (p) dY += 8;
        let words = para.split(/\s+/);
        while (words.length) {
          const inHero = dY < heroB - 6;
          const inQr = qzT !== null && dY + 24 > qzT - 24 && dY < qzT + QS + CAP;
          let x0 = PAD, tw = inHero ? imgX - GAP - PAD : W - PAD * 2;
          if (inQr) { x0 = PAD + QS + GAP; tw = (inHero ? imgX - GAP : W - PAD) - x0; }
          if (out.length >= 40) { trunc = true; break outer; }
          const [t, k] = wordsWrap(m, words, tw);
          words = words.slice(k);
          out.push({ t, x: x0, y: dY + 24 }); dY += 24;
        }
      }
      if (trunc && out.length)
        out[out.length - 1].t = out[out.length - 1].t.replace(/[,.;:\s]+$/, '') + '\u2026';
      return { out, dY };
    }
    let qzT = null, fin = flow(null);
    if (qr) for (let it = 0; it < 6; it++) {
      const t = Math.max(top, Math.max(fin.dY, heroB) - QS - CAP);
      if (qzT !== null && Math.abs(t - qzT) < 2) { qzT = t; break; }
      qzT = t; fin = flow(qzT);
    }
    if (hero) {
      ops.push({ k: 'img', src: 'hero', x: imgX, y: top, w: iw, h: ih, r: 10 });
      R.hero = [imgX, top, iw, ih];
    }
    if (qr) {
      ops.push({ k: 'fill', x: PAD, y: qzT, w: QS, h: QS, color: '#ffffff' });
      ops.push({ k: 'img', src: 'qr', x: PAD, y: qzT, w: QS, h: QS });
      R.qr = [PAD, qzT, QS, QS];
      const cf = `12px ${fam}`;
      ops.push({ k: 'text', t: 'Link to full article', font: cf, color: 'muted',
                 x: PAD, y: qzT + QS + 15 });
      R.caption = [PAD, qzT + QS + 3, textW(m, cf, 'Link to full article'), 14];
    }
    const lines = fin.out.map(l => [l.x, l.y - 17, textW(m, df, l.t), 22]);
    fin.out.forEach(l => ops.push({ k: 'text', t: l.t, font: df, color: 'text',
                                    x: l.x, y: l.y }));
    const cb = Math.max(fin.dY, heroB, qr ? qzT + QS + CAP : 0) + 12;
    const vf = `700 13px ${fam}`, vw = textW(m, vf, 'via RSSgate');
    ops.push({ k: 'text', t: 'via RSSgate', font: vf, color: 'accent',
               x: W - PAD - vw, y: cb + 20 });
    R.via = [W - PAD - vw, cb + 9, vw, 14];
    return { W, H: Math.round(cb + 20 + PAD), ops,
             geo: { style: 'float', rects: R, lines, qzT, qs: QS, dY: fin.dY } };
  }

  function paintShare(x, L, assets, pal) {
    x.fillStyle = pal.card; x.fillRect(0, 0, L.W, L.H);
    for (const o of L.ops) {
      if (o.k === 'fill') {
        x.fillStyle = pal[o.color] || o.color; x.fillRect(o.x, o.y, o.w, o.h);
      } else if (o.k === 'img') {
        const im = assets[o.src]; if (!im) continue;
        x.save();
        if (o.r) { roundRect(x, o.x, o.y, o.w, o.h, o.r); x.clip(); }
        if (o.sw) x.drawImage(im, o.sx, o.sy, o.sw, o.sh, o.x, o.y, o.w, o.h);
        else x.drawImage(im, o.x, o.y, o.w, o.h);
        x.restore();
      } else if (o.k === 'text') {
        x.font = o.font; x.fillStyle = pal[o.color] || o.color;
        x.fillText(o.t, o.x, o.y);
      }
    }
  }

  async function renderCardPng(a, opts = {}) {
    const cs = getComputedStyle(document.documentElement);
    const col = (n, fb) => (cs.getPropertyValue(n) || '').trim() || fb;
    const dark = matchMedia('(prefers-color-scheme: dark)').matches;
    const pal = { card: col('--card', dark ? '#1b1c1e' : '#ffffff'),
                  text: col('--text', dark ? '#e8e8ea' : '#17181a'),
                  muted: col('--muted', '#71717a'),
                  accent: col('--accent', '#7c5cff'),
                  line: col('--line', dark ? '#2c2d31' : '#e4e4e7') };
    const hero = a.image ? await loadBitmap('/image/' + a.image).catch(() => null) : null;
    const qr = a.link ? await loadBitmap('/api/qr.png?u=' +
                          encodeURIComponent(a.link)).catch(() => null) : null;
    const when = a.ts ? new Date(a.ts).toLocaleString(undefined, {
      month: 'short', day: 'numeric', year: 'numeric',
      hour: 'numeric', minute: '2-digit' }) : '';       // sharer's timezone
    let domain = '';
    try { domain = a.link ? new URL(a.link).hostname.replace(/^www\./, '') : ''; }
    catch (e) { /* bad link: no domain */ }
    const DPR = 2;
    const L = shareLayout(a, {
      W: opts.width || SHARE_W, style: opts.style || SHARE_STYLE,
      m: document.createElement('canvas').getContext('2d'),
      fam: getComputedStyle(document.body).fontFamily,
      hero: hero ? { w: hero.width, h: hero.height } : null, qr: !!qr,
      meta: [(a.feed_title || '').trim(), when].filter(Boolean).join('  \u00b7  '),
      domain });
    window.__lastShareGeo = { ...L.geo, W: L.W, H: L.H, dpr: DPR };  // test seam
    const cv = document.createElement('canvas');
    cv.width = L.W * DPR; cv.height = L.H * DPR;
    const x = cv.getContext('2d'); x.scale(DPR, DPR);
    paintShare(x, L, { hero, qr }, pal);
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
      const file = new File([png], `rssgate-${a.id}.png`,
                            { type: 'image/png' });
      // native share sheet first (iOS home-screen app & https browsers):
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        try {
          await navigator.share({ files: [file], title: a.title });
          btn.textContent = '\u2713';
          setTimeout(() => { btn.textContent = glyph; }, 1400);
          return;
        } catch (e) {
          if (e && e.name === 'AbortError') {      // user backed out
            btn.textContent = glyph;
            return;
          }                                        // else fall through
        }
      }
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
  async function refreshPips() {
    try {
      const s = await (await fetch('/api/status')).json();
      const pend = (s.pending || 0) + (s.processing || 0);
      const fail = s.errors || 0;
      const down = !!s.llm_down_since;     // AI backend unreachable: held
      $('pip-queue-n').textContent = pend;
      $('pip-queue-label').textContent = down ? 'waiting \u00b7 AI offline' : 'queued';
      $('pip-queue').classList.toggle('pip-warn', down);
      $('pip-queue').href = down ? '/admin#sec-status' : '/admin#sec-queue';
      $('pip-queue').title = down
        ? 'the AI backend is unreachable; these posts are held and resume automatically'
        : 'posts waiting to be digested';
      $('pip-fail-n').textContent = fail;
      $('pip-queue').hidden = pend === 0;
      $('pip-fail').hidden = fail === 0;
    } catch (e) { /* decorative */ }
  }
  window.refreshPips = refreshPips;

  // poll settle wait; tests shrink it via window.__SETTLE_MS (seam)
  const SETTLE_MS = typeof window.__SETTLE_MS === 'number'
                  ? window.__SETTLE_MS : 2500;
  async function doRefreshWork() {       // shared by button + pull-to-refresh
    try {
      const r = await fetch('/api/poll', { method: 'POST' });
      const j = await r.json().catch(() => ({}));
      if (!j.throttled)                  // real poll: let the fetchers land
        await new Promise(rz => setTimeout(rz, SETTLE_MS));
    } catch { /* offline: restart anyway */ }
    restart();
    refreshPips();
  }
  async function doPullRefresh() {
    ptrBusy = true;
    setBusy(true);                       // top rail is the work indicator now;
    ptrLabel.textContent = 'Pull to refresh';   // pill retreats during work
    await doRefreshWork();
    setBusy(false);
    ptrBusy = false;
  }

  window.__renderCardPng = renderCardPng;   // test seam
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
    if (s.share_style === 'float' || s.share_style === 'banner')
      SHARE_STYLE = s.share_style;
    if (typeof s.read_delay === 'number' && s.read_delay >= 0 && s.read_delay <= 60)
      READ_DELAY_MS = s.read_delay * 1000;
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
    refreshPips();
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
