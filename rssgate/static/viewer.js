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
  };
  function dateStr(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
      + '-' + String(d.getDate()).padStart(2, '0');
  }
  function defaultSince() {
    const d = new Date(); d.setDate(d.getDate() - 7); return dateStr(d);
  }

  let cursor = null, loading = false, exhausted = false, started = false,
      saveTimer = null, bootResume = '';

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
        ? `<div class="unsummarized">\u26a0 could not summarize \u2014 <a href="${esc(a.link)}">read original</a></div>`
        : `<div class="unsummarized">\u23f3 waiting for AI transcription\u2026</div>`;
    return `<article class="card${a.unread ? ' unread' : ''}" data-ts="${esc(a.ts)}"
        data-id="${a.id}" data-feed="${a.feed_id}">
      <div class="card-meta">${a.unread ? '<span class="newdot" title="unread"></span>' : ''}<span class="feed-title">${esc(a.feed_title || '\u2014')}</span>${raw}
        ${cats}<time datetime="${esc(a.ts)}">${fmt(a.ts)}</time></div>
      ${sub}
      <h2><a href="${esc(a.link)}" target="_blank" rel="noopener">${esc(a.title)}</a></h2>
      ${body}</article>`;
  }

  // ---- loading ------------------------------------------------------------
  async function loadNext() {
    if (loading || exhausted) return;
    loading = true;
    const params = new URLSearchParams({ limit: PAGE });
    if (cursor) { params.set('before_ts', cursor.ts); params.set('before_id', cursor.id); }
    if (store.feed) params.set('feed_id', store.feed);
    if (store.mode === 'since') params.set('since_ts', store.since + 'T00:00:00Z');
    const res = await fetch('/api/articles?' + params);
    const data = await res.json();
    loading = false;
    if (!started) {
      started = true;
      if (!data.items.length) $('empty-hint').hidden = false;
    }
    if (data.items.length) {
      $('empty-hint').hidden = true;
      endBanner.hidden = true;
      stream.insertAdjacentHTML('beforeend', data.items.map(cardHtml).join(''));
      cursor = { ts: data.items.at(-1).ts, id: data.items.at(-1).id };
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
    if (store.mode === 'since') {
      $('end-new').hidden = true; $('end-since').hidden = false;
      $('end-since-text').textContent =
        `That's everything since ${store.since}.`;
    } else {
      $('end-new').hidden = false; $('end-since').hidden = true;
      $('jump-date').value = yesterdayStr();
    }
  }
  function yesterdayStr() {
    const d = new Date(); d.setDate(d.getDate() - 1); return dateStr(d);
  }

  function restart() {
    stream.innerHTML = ''; cursor = null; exhausted = false;
    started = false; endBanner.hidden = true;
    $('sidebar').classList.remove('open'); $('sidebar-veil').classList.remove('show');
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
    const oldest = passed[passed.length - 1];
    const body = JSON.stringify({
      ts: oldest.dataset.ts, id: +oldest.dataset.id, reads,
      global: store.mode === 'new' && !store.feed,
    });
    navigator.sendBeacon && navigator.sendBeacon('/api/position',
      new Blob([body], { type: 'application/json' }))
      || fetch('/api/position', { method: 'POST', body,
          headers: { 'content-type': 'application/json' } });
    // once the stream has been scrolled past the old resume point, the
    // "new articles above" hint is no longer relevant on this boot
    if (oldest.dataset.ts > (bootResume || '')) hideNewAbove();
  }
  function queueSave() { clearTimeout(saveTimer); saveTimer = setTimeout(savePosition, 1200); }
  window.addEventListener('scroll', queueSave, { passive: true });
  document.addEventListener('visibilitychange', () => { if (document.hidden) savePosition(); });
  window.addEventListener('pagehide', savePosition);

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
  $('refresh-btn').addEventListener('click', async e => {
    e.target.textContent = '\u2026';
    await fetch('/api/poll', { method: 'POST' });
    setTimeout(() => location.reload(), 4000);
  });

  function setFeedFilter(id) {
    store.feed = id;
    document.querySelectorAll('#feed-filter li').forEach(li =>
      li.classList.toggle('active', li.dataset.feed === id));
    restart();
  }
  async function renderFeedFilter() {
    const feeds = await fetch('/api/feeds').then(r => r.json());
    const ul = $('feed-filter');
    ul.innerHTML = '<li data-feed="" class="' + (store.feed ? '' : 'active') + '">All feeds' + '</li>'
      + feeds.map(f => `<li data-feed="${f.id}" class="${store.feed == f.id ? 'active' : ''}"
           title="${esc(f.url)}"><span class="fname">${esc(f.title || f.url)}</span>
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
  ]).then(([s]) => {
    bootResume = s.resume_ts || '';
    if (store.mode === 'new' && !store.feed && s.resume_ts && s.newest_ts) {
      cursor = { ts: s.resume_ts, id: +s.resume_id };
      if (s.newest_ts > s.resume_ts) $('new-above').hidden = false;
    }
    loadNext();
  });

  // ---- newer-above jump: resume bounds the New stream, so newly arrived
  // articles sit above it; this is the one-tap way to reach them ----------
  function hideNewAbove() { $('new-above').hidden = true; }
  $('new-above-btn').addEventListener('click', () => {
    hideNewAbove();
    savePosition();                 // don't lose the deep-read position
    stream.innerHTML = ''; cursor = null; exhausted = false; started = false;
    endBanner.hidden = true;
    window.scrollTo(0, 0);
    loadNext();
  });
})();
