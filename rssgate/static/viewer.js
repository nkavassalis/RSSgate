/* RSSgate reader: endless reverse-chronological scroller with resume position. */
(() => {
  const PAGE = window.RSSGATE_PAGE || 20;
  const stream = document.getElementById('stream');
  const sentinel = document.getElementById('sentinel');
  const endBanner = document.getElementById('end-banner');
  const emptyHint = document.getElementById('empty-hint');

  let cursor = null;          // {ts, id} of oldest item currently loaded
  let loading = false;
  let exhausted = false;
  let started = false;
  let saveTimer = null;

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
        ? `<div class="unsummarized">⚠ could not summarize — <a href="${esc(a.link)}">read original</a></div>`
        : `<div class="unsummarized">⏳ waiting for AI transcription…</div>`;
    return `<article class="card" data-ts="${esc(a.ts)}" data-id="${a.id}">
      <div class="card-meta"><span class="feed-title">${esc(a.feed_title || '—')}</span>${raw}
        ${cats}<time datetime="${esc(a.ts)}">${fmt(a.ts)}</time></div>
      ${sub}
      <h2><a href="${esc(a.link)}" target="_blank" rel="noopener">${esc(a.title)}</a></h2>
      ${body}</article>`;
  }

  async function loadNext() {
    if (loading || exhausted) return;
    loading = true;
    const params = new URLSearchParams({ limit: PAGE });
    if (cursor) { params.set('before_ts', cursor.ts); params.set('before_id', cursor.id); }
    const res = await fetch('/api/articles?' + params);
    const data = await res.json();
    loading = false;
    if (!started) {
      started = true;
      if (!data.items.length) emptyHint.hidden = false;
    }
    if (data.items.length) {
      emptyHint.hidden = true;
      endBanner.hidden = true;
      stream.insertAdjacentHTML('beforeend', data.items.map(cardHtml).join(''));
      cursor = { ts: data.items[data.items.length - 1].ts,
                 id: data.items[data.items.length - 1].id };
    }
    if (!data.has_more) {
      exhausted = true;
      if (started || data.items.length) showEndBanner();
      return;
    }
    // fill the viewport if the page is still short
    if (document.body.scrollHeight <= window.innerHeight + 200) loadNext();
  }

  // ---- resume position tracking: remember the oldest article in view ----
  function savePosition() {
    const cards = stream.querySelectorAll('.card');
    if (!cards.length) return;
    let best = null;
    const cutoff = window.scrollY + window.innerHeight * 0.6;
    cards.forEach(c => {
      if (c.offsetTop <= cutoff) best = c;   // oldest card we've scrolled past
    });
    if (!best) best = cards[cards.length - 1];
    const body = JSON.stringify({ ts: best.dataset.ts, id: +best.dataset.id });
    navigator.sendBeacon && navigator.sendBeacon('/api/position',
      new Blob([body], { type: 'application/json' }))
      || fetch('/api/position', { method: 'POST', body,
          headers: { 'content-type': 'application/json' } });
  }
  function queueSave() { clearTimeout(saveTimer); saveTimer = setTimeout(savePosition, 1200); }
  window.addEventListener('scroll', queueSave, { passive: true });
  document.addEventListener('visibilitychange', () => { if (document.hidden) savePosition(); });
  window.addEventListener('pagehide', savePosition);

  // ---- date jump (defaults to yesterday) ----
  function yesterdayStr() {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
      + '-' + String(d.getDate()).padStart(2, '0');
  }
  function showEndBanner() {
    document.getElementById('jump-date').value = yesterdayStr();
    endBanner.hidden = false;
  }
  document.getElementById('jump-date').value = yesterdayStr();

  document.getElementById('jump-btn').addEventListener('click', async () => {
    const v = document.getElementById('jump-date').value || yesterdayStr();
    if (!v) return;
    const d = new Date(v + 'T23:59:59');
    stream.innerHTML = ''; cursor = null; exhausted = false;
    const res = await fetch('/api/articles?limit=' + PAGE +
      '&before_ts=' + encodeURIComponent(d.toISOString()));
    const data = await res.json();
    exhausted = !data.has_more;
    endBanner.hidden = true;
    if (data.items.length) {
      stream.insertAdjacentHTML('beforeend', data.items.map(cardHtml).join(''));
      cursor = { ts: data.items.at(-1).ts, id: data.items.at(-1).id };
    }
    window.scrollTo(0, 0);
  });
  document.getElementById('back-btn').addEventListener('click', () => {
    stream.innerHTML = ''; exhausted = false; endBanner.hidden = true;
    fetch('/api/resume').then(r => r.json()).then(s => {
      cursor = s.resume_ts ? { ts: s.resume_ts, id: +s.resume_id } : null;
      exhausted = false; loadNext();
    });
  });
  document.getElementById('refresh-btn').addEventListener('click', async e => {
    e.target.textContent = '…';
    await fetch('/api/poll', { method: 'POST' });
    setTimeout(() => location.reload(), 4000);
  });

  new IntersectionObserver(entries => {
    if (entries[0].isIntersecting) loadNext();
  }, { rootMargin: '1200px' }).observe(sentinel);

  // ---- boot: resume where we left off ----
  fetch('/api/resume').then(r => r.json()).then(s => {
    if (s.resume_ts && s.newest_ts) {
      cursor = { ts: s.resume_ts, id: +s.resume_id };
    }
    loadNext();
  });
})();
