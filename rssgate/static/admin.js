/* RSSgate admin panel logic. */
const $ = id => document.getElementById(id);
const esc = s => (s || '').replace(/[&<>"]/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'content-type': 'application/json' }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// ------------------------------------------------------------------ feeds
let ALLCATS = [];
const catChip = v => `<span class="chip user cat" data-name="${esc(v)}">${esc(v)} <b>×</b></span>`;

async function renderFeeds() {
  const feeds = await api('/api/feeds');
  ALLCATS = (await api('/api/categories')).map(c => c.name);
  $('all-cats').innerHTML = ALLCATS.map(c => `<option>${esc(c)}</option>`).join('');
  const tbody = $('feed-table').querySelector('tbody');
  tbody.innerHTML = feeds.map(f => `
    <tr data-id="${f.id}">
      <td><a href="${esc(f.url)}" target="_blank">${esc(f.title || f.url)}</a>
          <div class="hint">${esc(f.last_status || '')}</div></td>
      <td><span class="type-tag">${esc(f.type)}</span></td>
      <td class="cats">
        <div class="catchips" data-role="cats">${(f.categories || []).map(catChip).join('')}</div>
        <select data-role="catadd"><option value="">+ category…</option></select>
        ${(f.auto_categories || []).length
          ? `<span class="auto-cat">feed says: ${esc(f.auto_categories.join(', '))}</span>`
          : ''}
      </td>
      <td style="text-align:center; white-space:nowrap">
        <label style="display:inline; margin:0"><input type="checkbox" data-role="llm" style="width:auto"
          ${f.summarize === 0 ? '' : 'checked'} title="Use LLM digest (unchecked = show raw extracted text, zero tokens)"> LLM</label>
        <label style="display:inline; margin:0 0 0 8px"><input type="checkbox" data-role="spons" style="width:auto"
          ${f.hide_sponsored ? 'checked' : ''} title="Hide sponsored posts before they reach the LLM"> Ads</label>
        ${f.hidden_count ? `<div class="auto-cat">${f.hidden_count} hidden</div>` : ''}
      </td>
      <td>${f.article_count}</td>
      <td class="hint">${esc((f.last_fetched_at || '').replace('T', ' ').replace('Z', '')) || 'never'}</td>
      <td style="white-space:nowrap">
        <button class="btn ghost" data-act="save">Save</button>
        <button class="btn ghost" data-act="refresh" title="refresh">&#x21bb;</button>
        <button class="btn ghost" data-act="del">&#10005;</button>
      </td>
    </tr>`).join('');

  tbody.querySelectorAll('tr').forEach(tr => {
    const id = tr.dataset.id;
    const chips = tr.querySelector('[data-role=cats]');
    const dd = tr.querySelector('[data-role=catadd]');
    dd.innerHTML = '<option value="">+ category…</option>'
      + ALLCATS.map(c => `<option>${esc(c)}</option>`).join('')
      + '<option value="__new">✚ create new…</option>';
    dd.addEventListener('change', () => {
      let v = dd.value;
      if (!v) return;
      if (v === '__new') { v = (prompt('New category name:') || '').trim(); dd.value = ''; }
      if (!v) return;
      if ([...chips.querySelectorAll('.chip')].some(c =>
            c.dataset.name.toLowerCase() === v.toLowerCase())) return;
      chips.insertAdjacentHTML('beforeend', catChip(v));
      if (!ALLCATS.some(c => c.toLowerCase() === v.toLowerCase())) ALLCATS.push(v);
    });
    chips.addEventListener('click', e => {
      if (e.target.closest('b')) e.target.closest('.chip').remove();
    });

    tr.querySelectorAll('button').forEach(btn => btn.addEventListener('click', async () => {
      if (btn.dataset.act === 'save')
        await api(`/api/feeds/${id}`, { method: 'PUT', body: JSON.stringify({
          categories: [...chips.querySelectorAll('.chip')].map(c => c.dataset.name),
          summarize: tr.querySelector('[data-role=llm]').checked,
          hide_sponsored: tr.querySelector('[data-role=spons]').checked }) });
      if (btn.dataset.act === 'del' && confirm('Delete this feed and its articles?'))
        await api(`/api/feeds/${id}`, { method: 'DELETE' });
      if (btn.dataset.act === 'refresh') {
        btn.textContent = '…';
        await api(`/api/feeds/${id}/refresh`, { method: 'POST' });
      }
      renderFeeds(); renderCategories();
    }));
  });
}

// ---- add feed: probe the URL, find a real feed if we can, ask before page mode
async function addFeed(url, type, cats) {
  await api('/api/feeds', { method: 'POST',
    body: JSON.stringify({ url, type, categories: cats, refresh: true }) });
  $('new-url').value = ''; $('new-cats').value = '';
  $('add-suggest').hidden = true;
  renderFeeds();
}

function suggestBox(html) {
  const box = $('add-suggest');
  box.innerHTML = html; box.hidden = false;
  return box;
}

$('add-feed-btn').addEventListener('click', async () => {
  const url = $('new-url').value.trim();
  if (!url) return;
  const cats = $('new-cats').value.split(',').map(s => s.trim()).filter(Boolean);
  const btn = $('add-feed-btn');
  btn.disabled = true; btn.textContent = 'probing\u2026';
  try {
    const p = await api('/api/feeds/probe', { method: 'POST', body: JSON.stringify({ url }) });
    btn.disabled = false; btn.textContent = 'Add feed';
    if (p.type === 'feed') {
      suggestBox('<span>That URL is itself a feed \u2713 adding it.</span>');
      await addFeed(url, 'auto', cats); return;
    }
    if (p.type === 'error') throw new Error(p.error || 'could not fetch URL');
    if (p.candidates.length) {
      const box = suggestBox(`<span>No feed at that URL \u2014 but I found one nearby:</span>
        <select id="sugg">${p.candidates.map((c, i) =>
          `<option value="${i}">${esc(c.title || c.url)}</option>`).join('')}</select>
        <button class="btn" id="sugg-add">Add this feed</button>
        <button class="btn ghost" id="sugg-page">Add original URL as bare page</button>
        <button class="btn ghost" id="sugg-no">Cancel</button>`);
      box.querySelector('#sugg-add').onclick = () =>
        addFeed(p.candidates[+box.querySelector('#sugg').value].url, 'feed', cats);
      box.querySelector('#sugg-page').onclick = () => addFeed(url, 'page', cats);
      box.querySelector('#sugg-no').onclick = () => box.hidden = true;
      return;
    }
    const box = suggestBox(`<span>No feed found at <b>${esc(p.page_title || url)}</b>. We could add it as a
      <b>bare page</b> \u2014 the LLM discovers its articles each poll (slower, costs tokens).</span>
      <button class="btn" id="pg-add">Add as bare page</button>
      <button class="btn ghost" id="pg-no">Cancel</button>`);
    box.querySelector('#pg-add').onclick = () => addFeed(url, 'page', cats);
    box.querySelector('#pg-no').onclick = () => box.hidden = true;
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Add feed';
    const box = suggestBox(`<span class="hint">Probe failed: ${esc(e.message)}</span>
      <button class="btn ghost" id="pg-add">Add as bare page anyway</button>
      <button class="btn ghost" id="pg-no">Cancel</button>`);
    box.querySelector('#pg-add').onclick = () => addFeed(url, 'page', cats);
    box.querySelector('#pg-no').onclick = () => box.hidden = true;
  }
});

// ------------------------------------------------------------------ categories
async function renderCategories() {
  const cats = await api('/api/categories');
  $('category-list').innerHTML = cats.length
    ? cats.map(c => `<li>${esc(c.name)} <b class="hint">×${c.count}</b>
        <button title="remove" data-name="${esc(c.name)}">×</button></li>`).join('')
    : '<li class="hint">no categories yet — assign some to feeds above</li>';
  $('category-list').querySelectorAll('button[data-name]').forEach(b =>
    b.addEventListener('click', async () => {
      await api('/api/categories/' + encodeURIComponent(b.dataset.name), { method: 'DELETE' });
      renderFeeds(); renderCategories();
    }));
}

$('rename-btn').addEventListener('click', async () => {
  const from = $('rename-from').value.trim(), to = $('rename-to').value.trim();
  if (!from) return;
  if (to) await api('/api/categories/rename', { method: 'POST',
    body: JSON.stringify({ from, to }) });
  else await api('/api/categories/' + encodeURIComponent(from), { method: 'DELETE' });
  $('rename-from').value = $('rename-to').value = '';
  renderFeeds(); renderCategories();
});

// ------------------------------------------------------------------ config
async function loadConfig() {
  const cfg = await api('/api/config');
  $('cfg-provider').value = cfg.llm.provider;
  $('cfg-base-url').value = cfg.llm.base_url || '';
  $('cfg-api-key').placeholder = cfg.llm.api_key_set ? '(unchanged)' : 'not set';
  $('cfg-model').value = cfg.llm.model || '';
  $('cfg-model-discover').value = cfg.llm.model_discover || '';
  $('cfg-extra-body').value = JSON.stringify(cfg.llm.extra_body ?? {}, null, 1);
  $('cfg-feed-min').value = cfg.polling.feed_interval_minutes;
  $('cfg-page-min').value = cfg.polling.page_interval_minutes;
  $('cfg-order').value = (cfg.ui && cfg.ui.order) || 'newest';
  $('cfg-length').value = cfg.summarizer.length;
  $('cfg-max-chars').value = cfg.summarizer.max_input_chars;
  $('cfg-concurrency').value = cfg.summarizer.concurrency ?? 2;
  $('cfg-prompt').value = cfg.summarizer.system_prompt;
  try {
    const m = await api('/api/models');
    const sel = $('cfg-model'); const cur = sel.value;
    sel.innerHTML = '<option value="">auto</option>' +
      m.models.map(x => `<option${x === cur ? ' selected' : ''}>${esc(x)}</option>`).join('');
    if (m.error) sel.title = m.error;
  } catch { /* endpoint down */ }
}

$('save-btn').addEventListener('click', async () => {
  const patch = {
    llm: { provider: $('cfg-provider').value, base_url: $('cfg-base-url').value.trim(),
           model: $('cfg-model').value,
           model_discover: $('cfg-model-discover').value.trim() },
    polling: { feed_interval_minutes: +$('cfg-feed-min').value,
               page_interval_minutes: +$('cfg-page-min').value },
    ui: { order: $('cfg-order').value },
    summarizer: { length: $('cfg-length').value,
                  max_input_chars: +$('cfg-max-chars').value,
                  concurrency: +$('cfg-concurrency').value,
                  system_prompt: $('cfg-prompt').value },
  };
  const key = $('cfg-api-key').value.trim();
  if (key) patch.llm.api_key = key;
  const ebRaw = $('cfg-extra-body').value.trim();
  if (ebRaw) {
    try { patch.llm.extra_body = JSON.parse(ebRaw); }
    catch { alert('Extra request body must be valid JSON'); return; }
  }
  await api('/api/config', { method: 'PUT', body: JSON.stringify(patch) });
  $('save-result').textContent = 'saved ✓';
  setTimeout(() => $('save-result').textContent = '', 3000);
});

$('llm-test-btn').addEventListener('click', async () => {
  $('llm-test-result').textContent = 'testing…';
  try {
    const r = await api('/api/llm/test', { method: 'POST' });
    $('llm-test-result').textContent = `✓ ${r.model || '(model)'} replied: ${r.reply}`;
  } catch (e) { $('llm-test-result').textContent = '✗ ' + e.message; }
});

$('poll-now-btn').addEventListener('click', async () => {
  $('save-result').textContent = 'polling…';
  await api('/api/poll', { method: 'POST' });
  setTimeout(() => { renderFeeds(); }, 5000);
});

// ------------------------------------------------------------------ work queue
const WQ_ICON = { ready: '\u2713', error: '\u2717', processing: '\u25f3' };
function fmtSec(ms) { return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : ms + 'ms'; }
function ago(ts) {
  if (!ts) return '\u2013';
  const s = (Date.now() - new Date(ts).getTime()) / 1000;
  if (s < 90) return 'just now';
  if (s < 5400) return (s / 60 | 0) + ' min ago';
  return (s / 3600 | 0) + ' h ago';
}

async function renderWorkqueue() {
  const wq = await api('/api/workqueue');
  $('wq-summary').textContent =
    `${wq.working} summarizing \u00b7 ${wq.queue_ahead} queued ahead`;
  $('wq-current').innerHTML = wq.current.length
    ? wq.current.map(c => `<div class="wq-item">\u23f3 <b>${esc(c.title)}</b>
        <span class="hint">${esc(c.feed_title)} \u00b7 running ${ago(c.started_at).replace(' ago', '')}</span></div>`).join('')
    : '<div class="hint">idle \u2014 nothing being summarized right now</div>';
  $('wq-table').querySelector('tbody').innerHTML = wq.recent.map(r => `<tr>
      <td title="${r.status}">${WQ_ICON[r.status] || '?'}</td>
      <td>${esc(r.title)}</td><td class="hint">${esc(r.feed_title)}</td>
      <td class="hint">${ago(r.summarized_at)}</td>
      <td>${r.llm_ms ? fmtSec(r.llm_ms) : '\u2013'}</td>
      <td>${r.tokens_in + r.tokens_out ? (r.tokens_in + r.tokens_out).toLocaleString() : '\u2013'}</td>
    </tr>`).join('') ||
    '<tr><td colspan="6" class="hint">nothing processed yet</td></tr>';
}

// ------------------------------------------------------------------ usage
async function renderUsage() {
  const u = await api('/api/usage');
  const n = x => x.toLocaleString();
  $('usage-today').textContent = n(u.today);
  $('usage-month').textContent = n(u.month);
  $('usage-all').textContent = n(u.all_time);
}

async function renderLlmStats() {
  const s = await api('/api/llm/stats');
  const n = x => (x ?? 0).toLocaleString();
  $('st-queue').textContent = n(s.queue);
  $('st-peak').textContent = n(s.queue_peak);
  $('st-avg').textContent = s.avg_seconds ? s.avg_seconds + 's' : '–';
  $('st-range').textContent = s.avg_seconds ? `${s.min_seconds}s–${s.max_seconds}s` : '–';
  $('st-drain').textContent = s.queue ? (s.est_drain_minutes < 60
      ? s.est_drain_minutes.toFixed(0) + ' min' : (s.est_drain_minutes / 60).toFixed(1) + ' h')
    : 'clear';
  $('st-cache').textContent = n(s.cache_hits);
  $('st-calls').textContent = n(s.calls_today);
  $('st-errors').textContent = n(s.errors);
  $('st-last').textContent = 'last LLM call: ' + (s.last_call_ts || 'never');
}

renderFeeds(); renderCategories(); loadConfig(); renderUsage(); renderLlmStats(); renderWorkqueue();
setInterval(() => { renderUsage(); renderLlmStats(); }, 15000);
setInterval(renderWorkqueue, 5000);
