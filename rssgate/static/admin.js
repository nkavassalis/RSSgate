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
let ALLFEEDS = [];
const chk = (root, sel, fb) => root.querySelector(sel) || { checked: !!fb };
const catChip = v => `<span class="chip user cat" data-name="${esc(v)}">${esc(v)} <b>×</b></span>`;

async function renderFeeds() {
  const feeds = await api('/api/feeds');
  ALLCATS = (await api('/api/categories')).map(c => c.name);
  ALLFEEDS = feeds;
  $('all-cats').innerHTML = ALLCATS.map(c => `<option>${esc(c)}</option>`).join('');
  const tbody = $('feed-table').querySelector('tbody');
  tbody.innerHTML = feeds.map(f => `
    <tr data-id="${f.id}" data-was-llm="${f.summarize === 0 ? 0 : 1}"
        class="${f.enabled ? '' : 'off'}">
      <td><span class="fname-cell" data-role="fname">
          <a href="${esc(f.url)}" target="_blank">${esc(f.custom_title || f.title || f.url)}</a>
          ${f.custom_title ? ' <b class="hint" title="renamed">(you)</b>' : ''}
          <button class="btn ghost sm" data-act="rename" title="rename feed">&#9998;</button>
        </span>
          <div class="hint">${esc(f.last_status || '')}</div></td>
      <td><span class="type-tag">${esc(f.type)}</span></td>
      <td class="cats">
        <div class="catchips" data-role="cats">${(f.categories || []).map(catChip).join('')}</div>
        <select data-role="catadd"><option value="">+ category…</option></select>

      </td>
      <td style="text-align:center; white-space:nowrap">
        <label style="display:inline; margin:0"><input type="checkbox" data-role="enabled" style="width:auto"
          ${f.enabled === 0 ? '' : 'checked'} title="Feed enabled (unchecked = skipped by polling & digests)"> On</label>
        <label style="display:inline; margin:0 0 0 8px"><input type="checkbox" data-role="llm" style="width:auto"
          ${f.summarize === 0 ? '' : 'checked'} title="Use LLM digest (unchecked = show raw extracted text, zero tokens)"> LLM</label>
        <label style="display:inline; margin:0 0 0 8px"><input type="checkbox" data-role="spons" style="width:auto"
          ${f.hide_sponsored ? 'checked' : ''} title="Hide sponsored posts before they reach the LLM"> Ads</label>
        ${f.hidden_count ? `<div class="auto-cat">${f.hidden_count} hidden</div>` : ''}
      </td>
      <td>${f.article_count}</td>
      <td class="hint">${esc((f.last_fetched_at || '').replace('T', ' ').replace('Z', '')) || 'never'}</td>
      <td style="white-space:nowrap">
        <button class="btn ghost" data-act="cfg" title="advanced config">&#9881;</button>
        <button class="btn ghost" data-act="save">Save</button>
        <button class="btn ghost" data-act="refresh" title="refresh">&#x21bb;</button>
        <button class="btn ghost" data-act="del">&#10005;</button>
      </td>
    </tr>
    <tr class="feed-cfg" data-cfg="${f.id}" hidden><td colspan="7">
      <div class="cfg-panel"><span class="hint">loading…</span></div>
    </td></tr>`).join('');
  tbody.querySelectorAll('tr.feed-cfg').forEach(row => {
    const feed = feeds.find(f => String(f.id) === row.dataset.cfg);
    row.querySelector('.cfg-panel').dataset.dlen = feed.digest_length || 'default';
  });

  tbody.querySelectorAll('tr[data-id]').forEach(tr => {
    const id = tr.dataset.id;
    const chips = tr.querySelector('[data-role=cats]');
    const dd = tr.querySelector('[data-role=catadd]');
    dd.innerHTML = '<option value="">+ category…</option>'
      + ALLCATS.map(c => `<option>${esc(c)}</option>`).join('')
      + '<option value="__new">✚ create new…</option>';
    async function saveCats() {          // chips ARE the state: autosave
      const cell = chips.closest('td');
      cell.classList.add('saving');
      try {
        await api(`/api/feeds/${id}`, { method: 'PUT', body: JSON.stringify({
          categories: [...chips.querySelectorAll('.chip')].map(c => c.dataset.name),
        }) });
        cell.classList.remove('saving'); cell.classList.add('saved');
        setTimeout(() => cell.classList.remove('saved'), 1200);
      } catch (e) {
        cell.classList.remove('saving'); cell.classList.add('save-fail');
        setTimeout(() => cell.classList.remove('save-fail'), 2500);
        alert('Category save failed: ' + e.message);
        loadFeeds();
      }
    }
    tr.querySelectorAll('input[type=checkbox][data-role]').forEach(box => {
      box.addEventListener('change', async () => {
        const field = { llm: 'summarize', spons: 'hide_sponsored',
                        enabled: 'enabled' }[box.dataset.role];
        if (!field) return;
        const was = tr.dataset.wasLlm === '1';
        const cell = box.closest('td');
        cell.classList.add('saving');
        try {
          await api(`/api/feeds/${id}`, { method: 'PUT',
            body: JSON.stringify({ [field]: box.checked }) });
          tr.dataset.wasLlm = tr.querySelector('[data-role=llm]')
                                        .checked ? '1' : '0';
          tr.classList.toggle('off',
            !tr.querySelector('[data-role=enabled]').checked);
          cell.classList.remove('saving'); cell.classList.add('saved');
          setTimeout(() => cell.classList.remove('saved'), 1200);
          if (field === 'summarize' && was !== box.checked)
            await maybeRedigest(id);
        } catch (e) {
          cell.classList.remove('saving'); cell.classList.add('save-fail');
          setTimeout(() => cell.classList.remove('save-fail'), 2500);
          alert('Save failed: ' + e.message); loadFeeds();
        }
      });
    });
    dd.addEventListener('change', async () => {
      let v = dd.value;
      if (!v) return;
      if (v === '__new') { v = (prompt('New category name:') || '').trim(); dd.value = ''; }
      if (!v) return;
      if ([...chips.querySelectorAll('.chip')].some(c =>
            c.dataset.name.toLowerCase() === v.toLowerCase())) return;
      chips.insertAdjacentHTML('beforeend', catChip(v));
      if (!ALLCATS.some(c => c.toLowerCase() === v.toLowerCase())) ALLCATS.push(v);
      await saveCats();
    });
    chips.addEventListener('click', async e => {
      if (e.target.closest('b')) { e.target.closest('.chip').remove(); await saveCats(); }
    });

    tr.querySelectorAll('button').forEach(btn => btn.addEventListener('click', async () => {
      if (btn.dataset.act === 'cfg') {
        const row = tbody.querySelector(`tr[data-cfg="${id}"]`);
        row.hidden = !row.hidden;
        if (!row.hidden && !row.dataset.loaded) {
          row.dataset.loaded = '1';
          loadCfg(row, id);
        }
        return;
      }
      if (btn.dataset.act === 'save') {
        const was = tr.dataset.wasLlm === '1';
        const now = chk(tr, '[data-role=llm]', true).checked;
        await api(`/api/feeds/${id}`, { method: 'PUT', body: JSON.stringify({
          categories: [...chips.querySelectorAll('.chip')].map(c => c.dataset.name),
          summarize: now,
          hide_sponsored: chk(tr, '[data-role=spons]', true).checked,
          enabled: chk(tr, '[data-role=enabled]', true).checked }) });
        renderFeeds(); renderCategories();
        if (was !== now) await maybeRedigest(id);
        return;
      }
      if (btn.dataset.act === 'rename') { startFeedRename(tr, id); return; }
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
  const forced = $('new-type').value;   // explicit type skips the probe
  if (forced === 'feed' || forced === 'page') {
    btn.disabled = true; btn.textContent = 'adding\u2026';
    await addFeed(url, forced, cats);
    btn.disabled = false; btn.textContent = 'Add feed';
    return;
  }
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
    ? cats.map(c => `<li><span class="cname">${esc(c.name)}</span>
        <b class="hint">\u00d7${c.count}</b>
        <button class="btn ghost sm" title="rename everywhere"
          data-rename="${esc(c.name)}">&#9998;</button>
        <button title="remove from all feeds"
          data-name="${esc(c.name)}">\u00d7</button></li>`).join('')
    : '<li class="hint">no categories yet \u2014 assign some to feeds above</li>';
  $('category-list').querySelectorAll('button[data-name]').forEach(b =>
    b.addEventListener('click', async () => {
      await api('/api/categories/' + encodeURIComponent(b.dataset.name),
                { method: 'DELETE' });
      renderFeeds(); renderCategories();
    }));
  $('category-list').querySelectorAll('button[data-rename]').forEach(b =>
    b.addEventListener('click', e => startCatRename(e, b.dataset.rename)));
}

function startCatRename(e, old) {
  const li = e.target.closest('li');
  const q = old.replace(/"/g, '&quot;');
  li.innerHTML = `<input class="cat-edit" value="${q}" list="all-cats">
    <button class="btn sm" data-ok title="rename">&#10003;</button>
    <button class="btn ghost sm" data-cancel title="cancel">&#10005;</button>
    <span class="hint">Enter saves, Esc cancels \u2014 pick an existing name to merge</span>`;
  const inp = li.querySelector('input');
  inp.focus(); inp.select();
  const commit = async () => {
    const to = inp.value.trim().toLowerCase();
    if (to && to !== old)
      await api('/api/categories/rename',
                { method: 'POST', body: JSON.stringify({ from: old, to }) });
    renderCategories(); renderFeeds();
  };
  li.querySelector('[data-ok]').onclick = commit;
  li.querySelector('[data-cancel]').onclick = () => renderCategories();
  inp.onkeydown = ev => {
    if (ev.key === 'Enter') commit();
    if (ev.key === 'Escape') renderCategories();
  };
}



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
  const maint = cfg.maintenance || {};
  $('cfg-retention').value = String(maint.retention_months ?? 0);
  $('cfg-imgcap').value = maint.images_max_mb ?? 0;
  $('cfg-imgperpost').value = maint.images_per_post ?? 4;
  $('cfg-logfail').checked = !!((cfg.troubleshooting || {}).log_llm_failures);
  renderFailures();
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
    troubleshooting: { log_llm_failures: $('cfg-logfail').checked },
    maintenance: { retention_months: +$('cfg-retention').value,
                   images_max_mb: +$('cfg-imgcap').value,
                   images_per_post: Math.max(1, Math.min(8, +$('cfg-imgperpost').value || 4)) },
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
  const s = await api('/api/llm/stats').catch(() => null);
  if (!s) return;
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

// ------------------------------------------------------------------ maintenance
function fmtMaint(r) {
  if (!r || !r.ts) return 'never run';
  const bits = [];
  if (r.deleted_articles) bits.push(`${r.deleted_articles} articles deleted`);
  if (r.orphans_removed) bits.push(`${r.orphans_removed} orphan files (${r.orphans_freed_mb} MB)`);
  if (r.cache_trimmed) bits.push(`cache trimmed ${r.cache_trimmed} files (${r.cache_trimmed_mb} MB)`);
  bits.push(`cache now ${r.cache_mb} MB`);
  return `${r.ts.slice(0, 16).replace('T', ' ')} — ` + (r.ok ? bits.join(', ') : 'failed: ' + r.error);
}
(async () => {
  try {
    const r = await api('/api/maintenance');
    $('maint-result').textContent = fmtMaint(r.report);
  } catch { /* endpoint down */ }
})();
$('maint-run-btn').addEventListener('click', async () => {
  $('maint-result').textContent = 'running…';
  try {
    const r = await api('/api/maintenance/run', { method: 'POST' });
    $('maint-result').textContent = fmtMaint(r);
  } catch (e) { $('maint-result').textContent = '✗ ' + e.message; }
});

// ---- per-feed advanced config panel --------------------------------------
const DLEN = { default: 'Feed default', terse: 'Terse (one sentence, <=20 words)',
               normal: 'Normal (~150 words)', detailed: 'Detailed (300-500 words)' };
async function loadCfg(row, id) {
  const cats = await api(`/api/feeds/${id}/categories`);
  const panel = row.querySelector('.cfg-panel');
  const dlen = panel.dataset.dlen || 'default';
  const feed = (ALLFEEDS.find(f => String(f.id) === id) || {});
  const curPrompt = feed.system_prompt || '';
  panel.dataset.wasPrompt = curPrompt; panel.dataset.wasDlen = dlen;
  const micap = feed.max_input_chars || 0;
  panel.innerHTML = `
    <div class="cfg-grid">
      <label>Digest length
        <select data-role="dlen">${Object.entries(DLEN).map(([v, t]) =>
          `<option value="${v}"${v === dlen ? ' selected' : ''}>${t}</option>`).join('')}</select>
      </label>
      <label class="snap-pick"><input type="checkbox" data-role="syncdel"
        ${feed.sync_deletes ? 'checked' : ''} style="width:auto">
        Prune entries that vanish from the source <small>(snapshot feeds:
        trending lists, breaking-news pages; never prunes on an empty/failed fetch)</small></label>
      <label>Max input chars <small>(0 = global cap; lower = faster, e.g. 6000)</small>
        <input type="number" min="0" step="1000" data-role="micap" value="${micap}" style="width:110px;margin-left:8px">
      </label>
      <label class="sp-label">Custom system prompt <small>(optional — replaces the
        global digest prompt for this feed only; <code>{length}</code> available)</small>
        <textarea data-role="sprompt" rows="4" spellcheck="false"
          placeholder="(empty = use the global prompt)">${esc(curPrompt)}</textarea>
      </label>
      <div class="cat-allow">
        <h4>Post categories <small>checked = allowed; unchecked are hidden BEFORE the LLM (zero tokens). New categories arrive checked.</small></h4>
        ${cats.length ? cats.map(c => `<label class="cat-pick">
            <input type="checkbox" data-cat="${esc(c.name)}" ${c.allowed ? 'checked' : ''}>
            ${esc(c.name)} <small>${c.count} article${c.count === 1 ? '' : 's'}</small></label>`).join('')
          : '<span class="hint">no categories seen on this feed yet</span>'}
      </div>
    </div>
    <button class="btn" data-act="apply-cfg">Apply</button>
    <span class="cfg-status hint"></span>`;
  row.querySelector('[data-act=apply-cfg]').addEventListener('click', async () => {
    const blocked = [...row.querySelectorAll('.cat-pick input:not(:checked)')]
      .map(i => i.dataset.cat);
    const patch = { digest_length: row.querySelector('[data-role=dlen]').value,
                    system_prompt: row.querySelector('[data-role=sprompt]').value,
                    max_input_chars: Math.max(0, +row.querySelector('[data-role=micap]').value || 0),
                    sync_deletes: row.querySelector('[data-role=syncdel]').checked,
                    category_block: blocked };
    await api(`/api/feeds/${id}`, { method: 'PUT', body: JSON.stringify(patch) });
    row.querySelector('.cfg-status').textContent = 'saved \u2713';
    const promptChanged = patch.system_prompt !== panel.wasPrompt
      || patch.digest_length !== panel.wasDlen;
    setTimeout(async () => {
      renderFeedsKeepingOpen();
      if (promptChanged) await maybeRedigest(id);
    }, 500);
  });
}
function renderFeedsKeepingOpen() {
  const open = [...document.querySelectorAll('tr.feed-cfg[data-cfg]')]
    .filter(r => !r.hidden).map(r => r.dataset.cfg);
  renderFeeds().then(() => open.forEach(id => {
    const row = document.querySelector(`tr[data-cfg="${id}"]`);
    if (row) { row.hidden = false; row.dataset.loaded = '1';
               loadCfg(row, id); }
  }));
}

// ---- re-process confirmation ----------------------------------------------
function maybeRedigest(id) {
  const f = ALLFEEDS.find(x => String(x.id) === String(id));
  const n = (f && f.ready_count) || 0;
  if (!n) return Promise.resolve(false);          // nothing to redo, stay quiet
  return confirmRedigest(id, f.title || f.url, n);
}
function confirmRedigest(id, title, n) {
  return new Promise(resolve => {
    const veil = $('rd-modal');
    $('rd-text').innerHTML = `Re-process <b>${esc(title)}</b>? Its
      <b>${n}</b> stored digest${n === 1 ? '' : 's'} will be regenerated with the
      new settings (LLM tokens will be used for LLM feeds).`;
    veil.hidden = false;
    const done = async (yes) => {
      veil.hidden = true;
      $('rd-yes').onclick = $('rd-no').onclick = null;
      if (yes) {
        const r = await api(`/api/feeds/${id}/redigest`, { method: 'POST' });
        toast(`re-queued ${r.requeued} article${r.requeued === 1 ? '' : 's'} \u2192 queue`);
      }
      resolve(yes);
    };
    $('rd-yes').onclick = () => done(true);
    $('rd-no').onclick = () => done(false);
  });
}

function toast(msg) {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.remove('show'), 3200);
}

// ---- inline feed rename ---------------------------------------------------
function startFeedRename(tr, id) {
  const cell = tr.querySelector('[data-role=fname]');
  const f = ALLFEEDS.find(x => String(x.id) === String(id)) || {};
  const cur = f.custom_title || f.title || f.url;
  const q = cur.replace(/"/g, '&quot;');
  cell.innerHTML = `<input class="feed-rename" value="${q}" size="28">
    <button class="btn sm" data-ok title="save">&#10003;</button>
    <button class="btn ghost sm" data-cancel title="cancel">&#10005;</button>
    ${f.custom_title ? '<button class="btn ghost sm" data-revert title="back to feed name">revert</button>' : ''}`;
  const inp = cell.querySelector('input');
  inp.focus(); inp.select();
  const commit = async (val) => {
    await api(`/api/feeds/${id}`, { method: 'PUT',
      body: JSON.stringify({ custom_title: val }) });
    renderFeeds();
  };
  cell.querySelector('[data-ok]').onclick = () => commit(inp.value.trim());
  cell.querySelector('[data-cancel]').onclick = () => renderFeeds();
  if (rv) rv.onclick = () => commit('');
  inp.onkeydown = ev => {
    if (ev.key === 'Enter') commit(inp.value.trim());
    if (ev.key === 'Escape') renderFeeds();
  };
}

// ---- transcription failures -----------------------------------------------
async function renderFailures() {
  const rows = await api('/api/feed-errors?limit=30');
  $('failure-list').innerHTML = rows.length
    ? rows.map(r => `<li><b class="hint">${esc(r.feed_title)}</b>
        <a href="${esc(r.link)}" target="_blank">${esc(r.title || r.link)}</a>
        <span class="att" title="transcription attempts">\u00d7${r.attempts}</span>
        ${r.error_msg ? `<code>${esc(r.error_msg)}</code>` : '<span class="hint">(reason not stored - enable troubleshooting)</span>'}
        <button class="btn ghost sm" data-retry="${r.id}" title="try again (resets attempts)">\u21bb retry</button>
        <button class="btn ghost sm" data-drop="${r.id}" title="hide for good">drop</button></li>`).join('')
    : '<li class="hint">no failed articles \u2713</li>';
  $('failure-list').querySelectorAll('[data-retry]').forEach(b =>
    b.addEventListener('click', async () => {
      await api(`/api/articles/${b.dataset.retry}/retry`, { method: 'POST' });
      renderFailures();
    }));
  $('failure-list').querySelectorAll('[data-drop]').forEach(b =>
    b.addEventListener('click', async () => {
      await api(`/api/articles/${b.dataset.drop}/drop`, { method: 'POST' });
      renderFailures();
    }));
}
$('fail-refresh').addEventListener('click', renderFailures);
$('cfg-logfail').addEventListener('change', () => {});   // saved with Save config
