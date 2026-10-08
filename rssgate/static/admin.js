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
// X always means HOME (never history-back); replace() so /admin never
// lingers in the back stack (edge-swipe ghost, v0.37.1 lineage).
document.getElementById('admin-close').addEventListener('click', e => {
  e.preventDefault();
  location.replace('/');
});

const chk = (root, sel, fb) => root.querySelector(sel) || { checked: !!fb };
const catChip = v => `<span class="chip user cat" data-name="${esc(v)}">${esc(v)} <b>×</b></span>`;

function applyFeedFilter() {
  const q = $('feed-q').value.trim().toLowerCase();
  $('feed-table').querySelectorAll('tbody tr[data-id]').forEach(tr => {
    tr.hidden = !!q && !tr.dataset.search.includes(q);
  });
}
$('feed-q').addEventListener('input', applyFeedFilter);
async function renderFeeds() {
  const feeds = await api('/api/feeds');
  ALLCATS = (await api('/api/categories')).map(c => c.name);
  ALLFEEDS = feeds;
  $('all-cats').innerHTML = ALLCATS.map(c => `<option>${esc(c)}</option>`).join('');
  const tbody = $('feed-table').querySelector('tbody');
  tbody.innerHTML = feeds.map(f => `
    <tr data-id="${f.id}" data-was-llm="${f.summarize === 0 ? 0 : 1}"
        data-search="${esc([f.custom_title, f.title, f.url, f.categories]
                            .filter(Boolean).join(' ').toLowerCase())}"
        class="${f.enabled ? '' : 'off'}">
      <td><span class="fname-cell" data-role="fname">
          <a href="${esc(f.url)}" target="_blank">${esc(f.custom_title || f.title || f.url)}</a>
          ${f.custom_title ? ' <b class="hint" title="renamed">(you)</b>' : ''}
          <button class="btn ghost sm" data-act="rename" title="rename feed">&#9998;</button>
        </span>
          <div class="hint">${esc(f.last_status || '')}</div>
          ${f.content_source && f.content_source !== 'auto'
            ? `<span class="src-pill">${f.content_source === 'feed' ? 'feed text only' : 'article page only'}</span>` : ''}
          ${f.excerpt_count
            ? `<span class="excerpt-pill" title="the article page couldn't be used, so these posts show the feed's own excerpt">${f.excerpt_count} from feed excerpt</span>` : ''}
          ${f.challenge_at
            ? `<span class="challenge-pill" title="the site answers automated requests with a browser check (often Cloudflare 'Just a moment'): article pages can't be fetched">browser-check site</span>` : ''}
          ${f.backoff_until && f.backoff_until > new Date().toISOString().slice(0, 19) + 'Z'
            ? `<span class="paused-pill" title="the site refused our requests (403/429); polling and digests for this feed wait until then">paused by site until ${esc(f.backoff_until.slice(11, 16))} UTC</span>`
            : ''}</td>
      <td class="cats">
        <div class="catchips" data-role="cats">${(f.categories || []).map(catChip).join('')}</div>
        <select data-role="catadd"><option value="">+ category…</option></select>

      </td>
      <td style="text-align:center; white-space:nowrap">
        <label style="display:inline; margin:0"><input type="checkbox" data-role="enabled" style="width:auto"
          ${f.enabled === 0 ? '' : 'checked'} title="Feed enabled (unchecked = skipped by polling & digests)"> On</label>
        ${f.hidden_count ? `<div class="auto-cat">${f.hidden_count} hidden</div>` : ''}
      </td>
      <td class="hint">${esc((f.last_fetched_at || '').replace('T', ' ').replace('Z', '')) || 'never'}</td>
      <td style="white-space:nowrap">
        <button class="btn ghost" data-act="cfg" title="LLM, ads, images, refresh, delete">&#9881;</button>
      </td>
    </tr>
`).join('');
  $('feed-q').hidden = feeds.length <= 8;   // filter earns its space
  applyFeedFilter();
  // (inline cfg rows replaced by the cog modal)

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
        renderFeeds();
      }
    }
    tr.querySelectorAll('input[type=checkbox][data-role]').forEach(box => {
      box.addEventListener('change', async () => {
        if (box.dataset.role !== 'enabled') return;
        const field = 'enabled';
        const was = false;
        const cell = box.closest('td');
        cell.classList.add('saving');
        try {
          await api(`/api/feeds/${id}`, { method: 'PUT',
            body: JSON.stringify({ [field]: box.checked }) });
          tr.classList.toggle('off', !box.checked);
          cell.classList.remove('saving'); cell.classList.add('saved');
          setTimeout(() => cell.classList.remove('saved'), 1200);
          if (field === 'summarize' && was !== box.checked)
            await maybeRedigest(id);
        } catch (e) {
          cell.classList.remove('saving'); cell.classList.add('save-fail');
          setTimeout(() => cell.classList.remove('save-fail'), 2500);
          alert('Save failed: ' + e.message); renderFeeds();
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
      if (btn.dataset.act === 'cfg') { openFeedCfg(id); return; }
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
  $('cfg-hidepend').checked = (cfg.ui || {}).hide_untranscribed !== false;
  $('cfg-readdelay').value = (cfg.ui || {}).read_delay ?? 5;
  $('cfg-sharewidth').value = (cfg.ui || {}).snapshot_width ?? 800;
  const fc = cfg.fetch || {};
  $('cfg-ua').value = fc.user_agent || '';
  $('cfg-hostgap').value = fc.per_host_interval ?? 3;
  $('cfg-backoff').value = fc.block_backoff_minutes ?? 60;
  $('cfg-streamwidth').value = (cfg.ui || {}).stream_width ?? 800;
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

// Display & sharing widths: autosave (single-key ui patch)
function uiWidthSave(input, key, lo, hi) {
  input.addEventListener('change', async () => {
    const label = input.closest('label');
    const v = Math.max(lo, Math.min(hi, +input.value || (lo + hi) / 2));
    input.value = v;
    label.classList.add('cfg-saving');
    try {
      await api('/api/config', { method: 'PUT',
        body: JSON.stringify({ ui: { [key]: v } }) });
      label.classList.remove('cfg-saving');
      label.classList.add('cfg-ok');
      setTimeout(() => label.classList.remove('cfg-ok'), 1200);
    } catch (e) {
      label.classList.remove('cfg-saving');
      label.classList.add('cfg-bad');
      setTimeout(() => label.classList.remove('cfg-bad'), 2500);
      alert('Save failed: ' + e.message);
      loadConfig();
    }
  });
}
uiWidthSave($('cfg-sharewidth'), 'snapshot_width', 360, 1440);
uiWidthSave($('cfg-streamwidth'), 'stream_width', 480, 1600);
function cfgUiFlash(input, key, val) {
  const label = input.closest('label');
  label.classList.add('cfg-saving');
  api('/api/config', { method: 'PUT',
    body: JSON.stringify({ ui: { [key]: val } }) })
    .then(() => { label.classList.remove('cfg-saving');
                  label.classList.add('cfg-ok');
                  setTimeout(() => label.classList.remove('cfg-ok'), 1200); })
    .catch(err => { label.classList.remove('cfg-saving');
                    label.classList.add('cfg-bad');
                    setTimeout(() => label.classList.remove('cfg-bad'), 2500);
                    alert('Save failed: ' + err.message); loadConfig(); });
}
function cfgFetchFlash(input, key, val) {
  const label = input.closest('label');
  label.classList.add('cfg-saving');
  api('/api/config', { method: 'PUT',
    body: JSON.stringify({ fetch: { [key]: val } }) })
    .then(() => { label.classList.remove('cfg-saving');
                  label.classList.add('cfg-ok');
                  setTimeout(() => label.classList.remove('cfg-ok'), 1200); })
    .catch(err => { label.classList.remove('cfg-saving');
                    label.classList.add('cfg-bad');
                    setTimeout(() => label.classList.remove('cfg-bad'), 2500);
                    alert('Save failed: ' + err.message); loadConfig(); });
}
$('cfg-ua').addEventListener('change', e =>
  cfgFetchFlash(e.target, 'user_agent', e.target.value.trim()));
$('ua-mine-btn').addEventListener('click', () => {
  $('cfg-ua').value = navigator.userAgent;
  cfgFetchFlash($('cfg-ua'), 'user_agent', navigator.userAgent);
});
$('ua-default-btn').addEventListener('click', () => {
  $('cfg-ua').value = '';
  cfgFetchFlash($('cfg-ua'), 'user_agent', '');
});
$('cfg-hostgap').addEventListener('change', e => {
  const v = Math.max(0, Math.min(60, +e.target.value || 0));
  e.target.value = v; cfgFetchFlash(e.target, 'per_host_interval', v);
});
$('cfg-backoff').addEventListener('change', e => {
  const v = Math.max(1, Math.min(1440, Math.round(+e.target.value || 60)));
  e.target.value = v; cfgFetchFlash(e.target, 'block_backoff_minutes', v);
});
$('cfg-readdelay').addEventListener('change', e => {
  const v = Math.max(0, Math.min(60, Math.round(+e.target.value || 0)));
  e.target.value = v;
  cfgUiFlash(e.target, 'read_delay', v);
});
$('cfg-hidepend').addEventListener('change', async e => {
  const label = e.target.closest('label');
  label.classList.add('cfg-saving');
  try {
    await api('/api/config', { method: 'PUT', body: JSON.stringify({
      ui: { hide_untranscribed: e.target.checked } }) });
    label.classList.remove('cfg-saving'); label.classList.add('cfg-ok');
    setTimeout(() => label.classList.remove('cfg-ok'), 1200);
  } catch (err) {
    label.classList.remove('cfg-saving'); label.classList.add('cfg-bad');
    setTimeout(() => label.classList.remove('cfg-bad'), 2500);
    alert('Save failed: ' + err.message); loadConfig();
  }
});
(function () {                       // images-per-post autosave (maintenance key)
  const input = $('cfg-imgperpost');
  input.addEventListener('change', async () => {
    const label = input.closest('label');
    const v = Math.max(1, Math.min(8, +input.value || 4));
    input.value = v;
    label.classList.add('cfg-saving');
    try {
      await api('/api/config', { method: 'PUT',
        body: JSON.stringify({ maintenance: { images_per_post: v } }) });
      label.classList.remove('cfg-saving'); label.classList.add('cfg-ok');
      setTimeout(() => label.classList.remove('cfg-ok'), 1200);
    } catch (e) {
      label.classList.remove('cfg-saving'); label.classList.add('cfg-bad');
      setTimeout(() => label.classList.remove('cfg-bad'), 2500);
      alert('Save failed: ' + e.message); loadConfig();
    }
  });
})();

// ---- Status panel ---------------------------------------------------------
async function loadStatus() {
  const box = document.getElementById('status-grid');
  if (!box) return;
  try {
    const s = await api('/api/status');
    const hhmm = t => t ? new Date(t).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) : '';
    const q = s.llm_down_since ? `${s.pending} held`
      : s.processing ? `${s.processing} working` : (s.pending ? `${s.pending} queued` : 'idle');
    const rowA = [
      ['RSSgate', `v${s.version}`],
      ['Uptime', s.uptime_min < 60 ? `${s.uptime_min}m` : `${Math.floor(s.uptime_min/60)}h ${s.uptime_min%60}m`],
      ['Feeds', `${s.feeds_enabled}/${s.feeds} enabled`],
      ['Queue', q],
      ['AI backend', s.llm_down_since ? `offline since ${hhmm(s.llm_down_since)}` : 'online'],
    ];
    const rowB = [
      ['Digest errors', String(s.errors || 0)],
      ['Database', `${s.db_mb} MB`],
      ['Image cache', `${s.cache_mb} MB`],
    ];
    const cell = ([k, v]) =>
      `<div class="status-cell"><b>${esc(v)}</b><span>${k}</span></div>`;
    box.innerHTML = rowA.map(cell).join('')
      + '<div class="grid-break"></div>' + rowB.map(cell).join('');
    $('status-note').textContent = s.llm_down_since
      ? `AI backend unreachable since ${hhmm(s.llm_down_since)}; posts are held, not failed. Next check ${hhmm(s.llm_next_try)}. (${s.llm_down_reason || ''})`
      : 'updated ' + new Date().toLocaleTimeString();
    box.classList.toggle('llm-down', !!s.llm_down_since);
  } catch { /* server busy; keep last */ }
}
$('status-refresh').addEventListener('click', loadStatus);
setInterval(loadStatus, 30000);
loadStatus();
setTimeout(loadStatus, 2000);

// settings section nav: click = authoritative jump + instant active;
// observer only re-highlights on genuine user scrolling, and the bottom
// of the page always activates the last section (short pages can never
// scroll the last panels to the top band - clicked must look clicked).
(function () {
  const nav = document.querySelector('.sec-nav');
  if (!nav) return;
  const links = [...nav.querySelectorAll('a')];
  const map = new Map(links.map(a => [a.getAttribute('href').slice(1), a]));
  let lockUntil = 0;
  function setActive(id) {
    links.forEach(a => a.classList.toggle('active',
      a.getAttribute('href') === '#' + id));
  }
  links.forEach(a => a.addEventListener('click', e => {
    const id = a.getAttribute('href').slice(1);
    const sec = document.getElementById(id);
    if (!sec) return;
    e.preventDefault();
    lockUntil = Date.now() + 900;
    setActive(id);
    history.replaceState(null, '', '#' + id);
    sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
  // geometry spy: the section whose top you have passed lights up; the
  // bottom of the page always lights the last one. Visual only - never
  // moves the page.
  const secs = [...document.querySelectorAll('section.panel[id]')];
  let ticking = false;
  function spy() {
    ticking = false;
    if (Date.now() < lockUntil) return;
    if (innerHeight + scrollY >= document.documentElement.scrollHeight - 4) {
      setActive(secs[secs.length - 1].id);
      return;
    }
    let cur = secs[0];
    for (const s of secs)
      if (s.getBoundingClientRect().top <= 130) cur = s; else break;
    setActive(cur.id);
  }
  window.addEventListener('scroll', () => {
    if (!ticking) { ticking = true; requestAnimationFrame(spy); }
  }, { passive: true });
  window.addEventListener('resize', spy, { passive: true });
  spy();
})();

$('save-btn').addEventListener('click', async () => {
  const patch = {
    llm: { provider: $('cfg-provider').value, base_url: $('cfg-base-url').value.trim(),
           model: $('cfg-model').value,
           model_discover: $('cfg-model-discover').value.trim() },
    polling: { feed_interval_minutes: +$('cfg-feed-min').value,
               page_interval_minutes: +$('cfg-page-min').value },
    ui: { order: $('cfg-order').value,
        hide_untranscribed: $('cfg-hidepend').checked,
        read_delay: Math.max(0, Math.min(60, +$('cfg-readdelay').value || 0)),
        snapshot_width: Math.max(360, Math.min(1440,
                            +$('cfg-sharewidth').value || 800)),
        stream_width: Math.max(480, Math.min(1600,
                            +$('cfg-streamwidth').value || 1280)) },
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

// initial renders are async and change page height: re-apply a deep-link
// (#sec-*) once they land, or the jump targets a stale position
Promise.allSettled([renderFeeds(), renderCategories(), loadConfig(),
                    renderUsage(), renderLlmStats(), renderWorkqueue()])
  .then(() => {
    const t = location.hash && document.getElementById(location.hash.slice(1));
    if (t) t.scrollIntoView({ block: 'start' });
  });
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
async function openFeedCfg(id) {
  const veil = $('cfg-modal'), body = $('cfg-modal-body');
  const feed = ALLFEEDS.find(f => String(f.id) === id) || {};
  $('cfg-modal-title').textContent =
    feed.custom_title || feed.title || feed.url || 'Feed settings';
  const cats = await api(`/api/feeds/${id}/categories`);
  const dlen = feed.digest_length || 'default';
  body.dataset.wasPrompt = feed.system_prompt || '';
  body.dataset.wasDlen = dlen; body.dataset.wasLlm =
    (feed.summarize === 0 ? '0' : '1');
  body.innerHTML = `
    <p class="hint"><span class="type-tag">${esc(feed.type || 'feed')}</span>
       ${feed.article_count || 0} posts \u00b7
       ${feed.ready_count || 0} digested \u00b7
       ${feed.unread || 0} unread${feed.excerpt_count ?
        ` \u00b7 <b class="warn">${feed.excerpt_count} fell back to the feed's excerpt</b>` : ''}${feed.hidden_count ?
        ` \u00b7 <b>${feed.hidden_count} hidden by filters</b>` : ''}
       \u00b7 images:${esc(feed.images_mode || 'auto')}</p>
    <div class="cfg-grid">
      <label>Content source
        <select data-role="csrc">
          ${[['auto', 'Auto - article page, fall back to the feed\'s text'],
             ['feed', 'Feed text only - never fetch article pages'],
             ['page', 'Article page only - never fall back']].map(([v, t]) =>
            `<option value="${v}"${(feed.content_source || 'auto') === v ? ' selected' : ''}>${t}</option>`).join('')}
        </select>
        <small>${feed.challenge_at
          ? '<b class="warn">\u26a0 This site answers with a browser check (often Cloudflare \u201cJust a moment\u201d): its article pages can\'t be fetched. \u201cFeed text only\u201d is usually the right choice here.</b>'
          : '(feed text only suits sites that block readers or feeds that already carry full articles; long feed text still gets an LLM digest)'}</small></label>
      <label class="snap-pick"><input type="checkbox" data-role="llm"
        ${feed.summarize === 0 ? '' : 'checked'} style="width:auto">
        Use LLM digest <small>(unchecked = show raw extracted text, zero tokens)</small></label>
      <label class="snap-pick"><input type="checkbox" data-role="spons"
        ${feed.hide_sponsored ? 'checked' : ''} style="width:auto">
        Hide sponsored posts <small>(checked = filtered before the LLM, zero
        tokens; already-ingested items move to hidden)</small></label>
      <label>Images
        <select data-role="imgmode" class="imgmode">
          ${['auto','hero','off'].map(m =>
            `<option${(feed.images_mode || 'auto') === m ? ' selected' : ''}>${m}</option>`).join('')}
        </select> <small>(auto = hero + gallery, hero = hero only, off = none)</small></label>
      <label>Digest length
        <select data-role="dlen">${Object.entries(DLEN).map(([v, t]) =>
          `<option value="${v}"${v === dlen ? ' selected' : ''}>${t}</option>`).join('')}</select>
      </label>
      <label class="snap-pick"><input type="checkbox" data-role="syncdel"
        ${feed.sync_deletes ? 'checked' : ''} style="width:auto">
        Prune entries that vanish from the source <small>(snapshot feeds:
        trending lists, breaking-news pages; never prunes on an empty/failed fetch)</small></label>
      <label>Max input chars <small>(0 = global cap; lower = faster, e.g. 6000)</small>
        <input type="number" min="0" step="1000" data-role="micap" value="${feed.max_input_chars || 0}" style="width:110px;margin-left:8px">
      </label>
      <label class="sp-label">Custom system prompt <small>(optional — replaces the
        global digest prompt for this feed only; <code>{length}</code> available)</small>
        <textarea data-role="sprompt" rows="4" spellcheck="false"
          placeholder="(empty = use the global prompt)">${esc(feed.system_prompt || '')}</textarea>
      </label>
      <div class="cat-allow">
        <h4>Post categories <small>checked = allowed; unchecked are hidden BEFORE the LLM (zero tokens). New categories arrive checked.</small></h4>
        ${cats.length ? cats.map(c => `<label class="cat-pick">
            <input type="checkbox" data-cat="${esc(c.name)}" ${c.allowed ? 'checked' : ''}>
            ${esc(c.name)} <small>${c.count} article${c.count === 1 ? '' : 's'}</small></label>`).join('')
          : '<span class="hint">no categories seen on this feed yet</span>'}
      </div>
    </div>
    <div class="row">
      <button class="btn" data-act="cfg-done">Done</button>
      <button class="btn ghost" data-act="cfg-refresh" title="fetch this feed now">&#x21bb; Refresh now</button>
      ${feed.backoff_level ? '<button class="btn ghost" data-act="cfg-unpause" title="clear the site-block pause and try again now">Resume now</button>' : ''}
      <button class="btn ghost danger" data-act="cfg-del">&#10005; Delete feed</button>
      <span class="cfg-status hint">changes save as you make them</span>
    </div>`;
  veil.hidden = false;
  const q = s => body.querySelector(s);
  // autosave: each control persists its OWN field on change (textarea on
  // blur), flashing its label like the Display panel. Settings that change
  // what a digest IS (prompt, length, LLM on/off) offer a re-process.
  const FIELD = {
    dlen:    () => ({ digest_length: q('[data-role=dlen]').value }),
    sprompt: () => ({ system_prompt: q('[data-role=sprompt]').value }),
    micap:   () => ({ max_input_chars: Math.max(0, +q('[data-role=micap]').value || 0) }),
    syncdel: () => ({ sync_deletes: q('[data-role=syncdel]').checked }),
    llm:     () => ({ summarize: q('[data-role=llm]').checked }),
    spons:   () => ({ hide_sponsored: q('[data-role=spons]').checked }),
    imgmode: () => ({ images_mode: q('[data-role=imgmode]').value }),
    csrc:    () => ({ content_source: q('[data-role=csrc]').value }),
    cat:     () => ({ category_block: [...body.querySelectorAll(
                 '.cat-pick input:not(:checked)')].map(i => i.dataset.cat) }),
  };
  body.addEventListener('change', async e => {
    const el = e.target;
    const role = el.dataset.role || (el.dataset.cat !== undefined ? 'cat' : '');
    if (!FIELD[role]) return;
    const label = el.closest('label') || el;
    const st = q('.cfg-status');
    label.classList.add('cfg-saving');
    try {
      await api(`/api/feeds/${id}`, { method: 'PUT',
                                      body: JSON.stringify(FIELD[role]()) });
      label.classList.remove('cfg-saving'); label.classList.add('cfg-ok');
      setTimeout(() => label.classList.remove('cfg-ok'), 1200);
      st.textContent = 'saved \u2713';
    } catch (err) {
      label.classList.remove('cfg-saving'); label.classList.add('cfg-bad');
      setTimeout(() => label.classList.remove('cfg-bad'), 2500);
      st.textContent = 'save failed: ' + err.message;
      return;
    }
    let redo = false;
    if (role === 'sprompt' && el.value !== body.dataset.wasPrompt) {
      body.dataset.wasPrompt = el.value; redo = true; }
    if (role === 'dlen' && el.value !== body.dataset.wasDlen) {
      body.dataset.wasDlen = el.value; redo = true; }
    if (role === 'llm' && (el.checked ? '1' : '0') !== body.dataset.wasLlm) {
      body.dataset.wasLlm = el.checked ? '1' : '0'; redo = true; }
    await renderFeeds();
    if (redo) await maybeRedigest(id);
  });
  q('[data-act=cfg-done]').addEventListener('click', () => { veil.hidden = true; });
  const up = q('[data-act=cfg-unpause]');
  if (up) up.addEventListener('click', async () => {
    await api(`/api/feeds/${id}`, { method: 'PUT',
                                    body: JSON.stringify({ unpause: true }) });
    up.remove(); q('.cfg-status').textContent = 'resumed \u2713';
    renderFeeds();
  });
  q('[data-act=cfg-refresh]').addEventListener('click', async () => {
    const st = q('.cfg-status');
    st.textContent = 'refreshing\u2026';
    try { await api(`/api/feeds/${id}/refresh`, { method: 'POST' });
          st.textContent = 'refresh requested \u2713'; renderFeeds(); }
    catch (e) { st.textContent = 'refresh failed: ' + e.message; }
  });
  q('[data-act=cfg-del]').addEventListener('click', async () => {
    if (!confirm('Delete this feed and its articles?')) return;
    await api(`/api/feeds/${id}`, { method: 'DELETE' });
    veil.hidden = true; renderFeeds();
  });
}
document.addEventListener('DOMContentLoaded', () => {
  const veil = $('cfg-modal');
  veil.addEventListener('click', e => {
    if (e.target.id === 'cfg-modal') veil.hidden = true;
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') veil.hidden = true;
  });
});
function renderFeedsKeepingOpen() { return renderFeeds(); }

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
$('fail-retry-btn').addEventListener('click', async () => {
  const r = await (await fetch('/api/articles/retry-failed',
    { method: 'POST' })).json();
  $('fail-retry-result').textContent =
    `${r.requeued} post${r.requeued === 1 ? '' : 's'} back in the queue`;
  renderFailures();
});
$('fail-clear-btn').addEventListener('click', async () => {
  if (!confirm('Delete ALL failed posts? Their cached images go too.'))
    return;
  const r = await (await fetch('/api/articles/clear-failed',
    { method: 'POST' })).json();
  $('fail-clear-result').textContent =
    `cleared ${r.deleted} post` + (r.deleted === 1 ? '' : 's')
    + `, released ${r.images_released} image`
    + (r.images_released === 1 ? '' : 's');
  renderFailures();
  if (window.refreshPips) window.refreshPips();
});
