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
async function renderFeeds() {
  const feeds = await api('/api/feeds');
  const tbody = $('feed-table').querySelector('tbody');
  tbody.innerHTML = feeds.map(f => `
    <tr data-id="${f.id}">
      <td><a href="${esc(f.url)}" target="_blank">${esc(f.title || f.url)}</a>
          <div class="hint">${esc(f.last_status || '')}</div></td>
      <td><span class="type-tag">${esc(f.type)}</span></td>
      <td class="cats">
        <input value="${esc((f.categories || []).join(', '))}" placeholder="e.g. tech, science"
               data-role="cats">
        ${(f.auto_categories || []).length
          ? `<span class="auto-cat">feed says: ${esc(f.auto_categories.join(', '))}</span>`
          : ''}
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
    tr.querySelectorAll('button').forEach(btn => btn.addEventListener('click', async () => {
      if (btn.dataset.act === 'save')
        await api(`/api/feeds/${id}`, { method: 'PUT', body: JSON.stringify(
          { categories: tr.querySelector('[data-role=cats]').value.split(',') }) });
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

$('add-feed-btn').addEventListener('click', async () => {
  const url = $('new-url').value.trim();
  if (!url) return;
  const body = { url, type: $('new-type').value,
                 categories: $('new-cats').value.split(',').map(s => s.trim()).filter(Boolean) };
  try { await api('/api/feeds', { method: 'POST', body: JSON.stringify(body) });
    $('new-url').value = ''; $('new-cats').value = '';
    renderFeeds();
  } catch (e) { alert(e.message); }
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
  $('cfg-feed-min').value = cfg.polling.feed_interval_minutes;
  $('cfg-page-min').value = cfg.polling.page_interval_minutes;
  $('cfg-length').value = cfg.summarizer.length;
  $('cfg-max-chars').value = cfg.summarizer.max_input_chars;
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
           model: $('cfg-model').value },
    polling: { feed_interval_minutes: +$('cfg-feed-min').value,
               page_interval_minutes: +$('cfg-page-min').value },
    summarizer: { length: $('cfg-length').value,
                  max_input_chars: +$('cfg-max-chars').value,
                  system_prompt: $('cfg-prompt').value },
  };
  const key = $('cfg-api-key').value.trim();
  if (key) patch.llm.api_key = key;
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

// ------------------------------------------------------------------ usage
async function renderUsage() {
  const u = await api('/api/usage');
  const n = x => x.toLocaleString();
  $('usage-today').textContent = n(u.today);
  $('usage-month').textContent = n(u.month);
  $('usage-all').textContent = n(u.all_time);
}

renderFeeds(); renderCategories(); loadConfig(); renderUsage();
