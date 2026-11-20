/* Scout dashboard.
   No framework and no build step: the install stays one command and the page is
   readable by anyone who opens it. Hash routing, three views, one fetch helper. */

const $ = (sel, root = document) => root.querySelector(sel);
const view = $('#view');

const CORE = new Set([
  'legal_name', 'trading_name', 'website', 'registration_number',
  'headquarters_location', 'industry', 'products_services',
  'employee_count', 'revenue',
]);

const CONTACT = new Set(['contact_email', 'contact_phone']);

/* ------------------------------------------------------------------ helpers */

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json();
}

const money = (n) => (n == null ? '—' : '$' + Number(n).toFixed(n < 0.1 ? 4 : 3));
const secs = (n) => (n == null ? '—' : Math.round(n) + 's');
const pct = (n) => (n == null ? '—' : Math.round(n * 100) + '%');

function when(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(undefined, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

function clip(text, limit) {
  const t = String(text ?? '').trim();
  if (t.length <= limit) return t;
  const cut = t.slice(0, limit);
  const space = cut.lastIndexOf(' ');
  return (space > limit * 0.6 ? cut.slice(0, space) : cut).replace(/[,;:.]$/, '') + '…';
}

const statusMark = (s) => `<span class="status status-${esc(s)}">${esc(s)}</span>`;

function host(url) {
  try { return new URL(url).host.replace(/^www\./, ''); } catch { return url; }
}

function sourceList(urls) {
  if (!urls || !urls.length) return '';
  const seen = [...new Set(urls)].slice(0, 4);
  return `<div class="sources">${seen.map((u) =>
    `<a href="${esc(u)}" target="_blank" rel="noreferrer">${esc(host(u))}</a>`
  ).join(' · ')}</div>`;
}

/* -------------------------------------------------------------------- shell */

async function paintMasthead() {
  try {
    const [health, totals] = await Promise.all([
      api('/api/health'), api('/api/totals'),
    ]);
    $('#masthead-meta').innerHTML =
      `${esc(health.model)} · ${esc(health.engine)} · ${esc(health.depth)}<br>` +
      `${totals.runs} runs · ${totals.findings} findings · $${totals.spend_usd.toFixed(2)} spent`;
    $('#colophon-db').textContent = health.database;
    document.body.dataset.key = health.api_key ? 'yes' : 'no';
  } catch {
    $('#masthead-meta').textContent = 'server unreachable';
  }
}

function markNav(name) {
  document.querySelectorAll('[data-nav]').forEach((a) => {
    if (a.dataset.nav === name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
}

/* --------------------------------------------------------------- runs index */

async function runsView() {
  markNav('runs');
  const runs = await api('/api/runs');
  if (!runs.length) {
    view.innerHTML = `
      <div class="page-head"><h1>Runs</h1></div>
      <p class="empty">Nothing researched yet. Start a run and it will appear here,
      with every claim it made and the page that claim came from.</p>
      <p><a href="#/new"><button>Start a run</button></a></p>`;
    return;
  }
  view.innerHTML = `
    <div class="page-head"><h1>Runs</h1></div>
    <table>
      <thead><tr>
        <th>Run</th><th>Objectives</th><th class="num">Companies</th>
        <th class="num">Cost</th><th>Started</th><th class="right">Status</th>
      </tr></thead>
      <tbody>${runs.map((r) => `
        <tr class="row-link" data-href="#/runs/${r.id}">
          <td>${esc(r.label || 'Run ' + r.id)}<div class="tag">#${r.id} · ${esc(r.model || '')}</div></td>
          <td>${r.objectives ? esc(clip(r.objectives, 96)) : '<span class="profile-empty">none</span>'}</td>
          <td class="num">${r.companies_done}/${r.companies}${
            r.companies_failed ? ` <span class="status-failed">(${r.companies_failed} failed)</span>` : ''
          }</td>
          <td class="num">${money(r.cost_usd)}</td>
          <td class="num">${when(r.created_at)}</td>
          <td class="right">${statusMark(r.status)}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
  wireRows();
}

/* --------------------------------------------------------------- run detail */

let liveSource = null;

function closeLive() {
  if (liveSource) { liveSource.close(); liveSource = null; }
}

async function runView(id) {
  markNav('runs');
  const run = await api(`/api/runs/${id}`);
  paintRun(run);
  if (run.status === 'running' || run.status === 'pending') {
    closeLive();
    liveSource = new EventSource(`/api/runs/${id}/events`);
    liveSource.onmessage = (e) => {
      try { paintRun(JSON.parse(e.data)); } catch { /* ignore a partial frame */ }
    };
    liveSource.addEventListener('end', () => { closeLive(); paintMasthead(); });
  }
}

function paintRun(run) {
  const live = run.status === 'running' || run.status === 'pending';
  const done = run.companies.filter((c) => c.status === 'done');
  const spend = run.companies.reduce((a, c) => a + (c.cost_usd || 0), 0);
  view.innerHTML = `
    <div class="breadcrumb"><a href="#/">Runs</a> / ${esc(run.label || 'Run ' + run.id)}</div>
    <div class="page-head">
      <h1>${live ? '<span class="live"></span>' : ''}${esc(run.label || 'Run ' + run.id)}</h1>
      <div class="domain">#${run.id} · ${esc(run.model)} · ${esc(run.engine)} engine</div>
    </div>
    ${run.objectives ? `<p class="lede">${esc(run.objectives)}</p>` : ''}
    <div class="stats">
      <div><div class="stat-label">Companies</div>
        <div class="stat-value">${done.length}<small> / ${run.companies.length}</small></div></div>
      <div><div class="stat-label">Spent</div><div class="stat-value">${money(spend)}</div></div>
      <div><div class="stat-label">Per company</div>
        <div class="stat-value">${done.length ? money(spend / done.length) : '—'}</div></div>
      <div><div class="stat-label">Status</div><div class="stat-value">${statusMark(run.status)}</div></div>
    </div>
    ${run.icp ? `<h2 class="section">Ideal customer</h2>
      <table><tbody>${Object.entries(run.icp).map(([k, v]) =>
        `<tr><td style="width:12rem">${esc(k)}</td><td>${esc(v)}</td></tr>`).join('')}
      </tbody></table>` : ''}
    <h2 class="section">Companies</h2>
    <table>
      <thead><tr>
        <th>Company</th><th class="num">Facts</th><th class="num">People</th>
        <th class="num">Fit</th><th class="num">Time</th><th class="num">Cost</th>
        <th class="right">Status</th>
      </tr></thead>
      <tbody>${run.companies.map((c) => `
        <tr class="row-link" data-href="#/companies/${c.id}">
          <td>${esc(c.name || c.domain)}<div class="tag">${esc(c.domain)}</div></td>
          <td class="num">${c.facts}</td>
          <td class="num">${c.people}</td>
          <td class="num">${c.score != null ? Math.round(c.score) : '—'}</td>
          <td class="num">${secs(c.duration_s)}</td>
          <td class="num">${money(c.cost_usd)}</td>
          <td class="right">${statusMark(c.status)}${
            c.error ? `<div class="tag">${esc(clip(c.error, 48))}</div>` : ''}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
  wireRows();
}

/* ----------------------------------------------------------- company detail */

async function companyView(id) {
  markNav('runs');
  closeLive();
  const c = await api(`/api/company-runs/${id}`);
  const tab = (location.hash.split('?t=')[1]) || 'profile';
  const named = (field) => c.profile.find((f) => f.field === field && f.filled)?.value;
  // A domain is how you reached the company, not what it is called. Beryl's entity
  // is SMIDSY Ltd; showing beryl.cc as the title hides the more useful fact.
  const title = named('trading_name') || named('legal_name') || c.name || c.domain;
  view.innerHTML = `
    <div class="breadcrumb">
      <a href="#/">Runs</a> / <a href="#/runs/${c.run_id}">Run ${c.run_id}</a> /
      ${esc(c.domain)}
    </div>
    <div class="page-head">
      <h1>${esc(title)}</h1>
      <div class="domain">
        <a href="https://${esc(c.domain)}" target="_blank" rel="noreferrer">${esc(c.domain)}</a>
      </div>
    </div>
    ${c.error ? `<div class="notice bad">${esc(c.error)}</div>` : ''}
    ${c.summary ? `<p class="lede">${esc(c.summary)}</p>` : ''}
    <div class="stats">
      <div><div class="stat-label">Core coverage</div><div class="stat-value">${pct(c.coverage)}</div></div>
      <div><div class="stat-label">Corroborated</div>
        <div class="stat-value">${c.assessment.corroborated}<small> of ${c.assessment.claims}</small></div></div>
      <div><div class="stat-label">People</div><div class="stat-value">${c.people.length}</div></div>
      <div><div class="stat-label">Time</div><div class="stat-value">${secs(c.duration_s)}</div></div>
      <div><div class="stat-label">Cost</div><div class="stat-value">${money(c.cost_usd)}</div></div>
    </div>
    <div class="tabs" id="tabs" role="tablist">
      <button role="tab" data-tab="profile">Profile</button>
      <button role="tab" data-tab="people">People</button>
      <button role="tab" data-tab="evidence">Evidence</button>
      <button role="tab" data-tab="trace">Trace</button>
    </div>
    <div id="panel" role="tabpanel"></div>`;

  const tabs = $('#tabs');
  tabs.addEventListener('click', (e) => {
    const name = e.target.dataset?.tab;
    if (name) showTab(c, name);
  });
  showTab(c, tab);
}

function showTab(c, name) {
  document.querySelectorAll('#tabs button').forEach((b) =>
    b.setAttribute('aria-selected', String(b.dataset.tab === name)));
  history.replaceState(null, '', `#/companies/${c.id}?t=${name}`);
  const panel = $('#panel');
  if (name === 'profile') panel.innerHTML = profilePanel(c);
  else if (name === 'people') panel.innerHTML = peoplePanel(c);
  else if (name === 'evidence') panel.innerHTML = evidencePanel(c);
  else { panel.innerHTML = '<p class="empty">Loading the trace…</p>'; tracePanel(c, panel); }
}

function profilePanel(c) {
  const rows = c.profile.map((f) => {
    const core = CORE.has(f.field) || CONTACT.has(f.field);
    if (!f.filled && !core) return '';
    const value = !f.filled
      ? '<span class="profile-empty">not established</span>'
      : (f.values && f.values.length > 1
        ? `<ul>${f.values.map((v) => `<li>${esc(v)}</li>`).join('')}</ul>`
        : esc(f.value));
    const aside = f.filled
      ? `${f.corroborated ? '<span class="mark-corroborated">corroborated</span><br>' : ''}` +
        `${esc(f.tier_label)} · ${f.sources.length} source${f.sources.length === 1 ? '' : 's'}`
      : '';
    return `
      <div class="profile-row ${core ? 'core' : ''}">
        <div class="profile-field">${esc(f.field)}</div>
        <div class="profile-value">${value}${sourceList(f.sources)}</div>
        <div class="profile-aside">${aside}</div>
      </div>`;
  }).join('');

  const q = c.qualification;
  const verdict = q ? `
    <h2 class="section">Fit against the ideal customer</h2>
    <div class="verdict">
      <span class="verdict-tier tier-${esc(q.tier)}">${esc(q.tier)}</span>
      <span class="verdict-score">${Math.round(q.score)}<small>/100</small></span>
    </div>
    <p style="max-width:var(--measure);font-size:0.9rem">${esc(q.rationale || '')}</p>
    <div class="criteria">
      ${q.matched?.length ? `<div><div class="stat-label">Matched</div>
        <ul>${q.matched.map((m) => `<li>${esc(m)}</li>`).join('')}</ul></div>` : ''}
      ${q.missed?.length ? `<div><div class="stat-label">Missed</div>
        <ul>${q.missed.map((m) => `<li>${esc(m)}</li>`).join('')}</ul></div>` : ''}
    </div>` : `
    <h2 class="section">Fit against the ideal customer</h2>
    <p class="empty">No ideal customer was given for this run, so nothing was scored.</p>`;

  const conflicts = c.assessment.conflicts?.length ? `
    <h2 class="section">Sources disagree</h2>
    ${c.assessment.conflicts.map((cf) => `
      <div class="branch">
        <div class="branch-name">${esc(cf.field)}</div>
        ${cf.values.map((v) => `<div class="branch-question">${esc(v.value)}
          <span class="tag">${esc(v.sources.join(', '))}</span></div>`).join('')}
      </div>`).join('')}` : '';

  const extras = c.extras.length ? `
    <h2 class="section">Outside the schema</h2>
    <p class="note">Recorded, but matching no column. Kept so a gap in the
    vocabulary is visible rather than silent.</p>
    <table><tbody>${c.extras.map((e) => `
      <tr><td style="width:14rem"><span class="profile-field">${esc(e.field)}</span></td>
      <td>${esc(clip(e.value, 200))}</td></tr>`).join('')}</tbody></table>` : '';

  return `<div class="profile">${rows}</div>
    ${c.missing.length ? `<p class="sources" style="margin-top:0.9rem">
      Not established: ${c.missing.map(esc).join(', ')}</p>` : ''}
    ${verdict}${conflicts}${extras}
    ${branchPanel(c)}`;
}

function branchPanel(c) {
  if (!c.branches.length) return '';
  return `<h2 class="section">How it was researched</h2>
    ${c.branches.map((b) => `
      <div class="branch">
        <div class="branch-head">
          <span class="branch-name">${esc(b.name)}</span>
          <span class="tag">${money(b.cost_usd)} · ${secs(b.duration_s)} ·
            ${b.web_searches} search${b.web_searches === 1 ? '' : 'es'}</span>
          ${statusMark(b.status)}
        </div>
        ${b.question ? `<div class="branch-question">${esc(b.question)}</div>` : ''}
        ${b.summary ? `<div class="branch-summary">${esc(b.summary)}</div>` : ''}
        ${b.error ? `<div class="branch-question status-failed">${esc(b.error)}</div>` : ''}
      </div>`).join('')}`;
}

function peoplePanel(c) {
  if (!c.people.length) {
    return `<p class="empty">No named people. Scout records a person only where the
    company or a public register publishes them — it never guesses an address or
    infers a name.</p>`;
  }
  return `<table>
    <thead><tr><th>Name</th><th>Role</th><th>Contact</th><th class="right">Source</th></tr></thead>
    <tbody>${c.people.map((p) => `
      <tr>
        <td>${esc(p.name)}</td>
        <td>${esc(p.role || '')}</td>
        <td class="num">${p.contact ? esc(Object.values(p.contact).join(' · ')) : '—'}</td>
        <td class="right">${sourceList(p.sources)}</td>
      </tr>`).join('')}
    </tbody></table>`;
}

function evidencePanel(c) {
  const rows = c.findings.map((f) => {
    const ev = f.evidence[0];
    return `
      <tr>
        <td style="width:13rem"><span class="profile-field">${esc(f.field)}</span>
          <div class="tag">${esc(f.kind)}</div></td>
        <td>${f.value != null ? esc(String(f.value).replace(/^"|"$/g, ''))
          : `<span class="profile-empty">${esc(f.reason || 'unknown')}</span>`}
          ${ev?.quote ? `<div class="sources">“${esc(ev.quote.slice(0, 220))}”</div>` : ''}</td>
        <td class="right" style="width:11rem">${ev ? `
          <a href="${esc(ev.url)}" target="_blank" rel="noreferrer">${esc(host(ev.url))}</a>
          <div class="tag">${esc(ev.tier_label || '')}</div>` : ''}</td>
      </tr>`;
  }).join('');
  return `<p class="note">Every claim, with the sentence
    it came from. <code>scout verify</code> reopens each page and checks the sentence is
    still there.</p>
    <table><thead><tr><th>Field</th><th>Value and quote</th>
    <th class="right">Source</th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function tracePanel(c, panel) {
  const steps = await api(`/api/company-runs/${c.id}/steps`);
  if (!steps.length) { panel.innerHTML = '<p class="empty">No steps recorded.</p>'; return; }
  const reads = steps.filter((s) => s.tool === 'open_page');
  const escalated = reads.filter((s) => s.escalated);
  panel.innerHTML = `
    <p class="note">
      ${reads.length} page${reads.length === 1 ? '' : 's'} read,
      ${escalated.length} of which needed a real browser. The rest answered over plain
      HTTP, which is faster and costs less — the page decides, not a rule about the host.
    </p>
    <div class="trace">${steps.map((s) => `
      <div class="trace-step">
        <div class="trace-seq">${s.seq}</div>
        <div class="trace-tool">${esc(s.tool || s.type || '')}</div>
        <div class="trace-detail">
          ${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noreferrer">${esc(s.url)}</a>`
            : esc(summariseInput(s.input))}
          ${s.escalated ? `<div class="escalated">opened in the browser — ${esc(s.reason || '')}</div>` : ''}
        </div>
        <div class="trace-ms">${s.duration_ms != null ? s.duration_ms + 'ms' : ''}</div>
      </div>`).join('')}</div>`;
}

function summariseInput(input) {
  if (!input) return '';
  if (typeof input === 'string') return input.slice(0, 120);
  if (input.fields) return input.fields.join(', ');
  if (input.name) return input.name;
  if (input.field) return input.field + (input.reason ? ' — ' + input.reason : '');
  if (input.count != null) return input.count + ' finding(s)';
  return JSON.stringify(input).slice(0, 120);
}

/* ----------------------------------------------------------------- new run */

function newRunView() {
  markNav('new');
  closeLive();
  const noKey = document.body.dataset.key === 'no';
  view.innerHTML = `
    <div class="page-head"><h1>New run</h1></div>
    <p class="lede">Scout researches each company in four short conversations, records
    what it finds with the page it came from, and scores the company against what you
    are looking for. About two minutes and four pence each.</p>
    ${noKey ? `<div class="notice bad">No <code>ANTHROPIC_API_KEY</code> found.
      Add it to <code>.env</code> and restart the server.</div>` : ''}
    <form class="form" id="run-form">
      <div class="field">
        <label for="domains">Domains</label>
        <textarea id="domains" rows="4" placeholder="asiagate.co.uk&#10;beryl.cc"></textarea>
        <div class="hint">One per line. Just the domain.</div>
      </div>
      <div class="field">
        <label for="objectives">What do you want to know?</label>
        <textarea id="objectives" rows="3"
          placeholder="who runs it, how big it is, what it sells, what changed recently"></textarea>
        <div class="hint">This decides what gets researched. Without it, Scout has no
          reason to prefer one fact over another.</div>
      </div>
      <div class="field">
        <label for="icp">Who are you looking for?</label>
        <textarea id="icp" rows="3"
          placeholder='{"sector": "UK construction supply", "size": "10+ staff", "buying_signal": "recent contract"}'></textarea>
        <div class="hint">JSON. Each company is scored against these and told you which
          ones it matched and missed. Leave empty for a description instead of a verdict.</div>
      </div>
      <div class="field">
        <label for="depth">Depth</label>
        <select id="depth">
          <option value="shallow">shallow — one pass</option>
          <option value="standard" selected>standard — four areas, researched separately</option>
          <option value="deep">deep — every area, plus follow-ups on what it finds</option>
        </select>
        <div class="hint">Depth is a count of separate conversations, not a bigger budget
          for one. That is why it costs about what you would expect.</div>
      </div>
      <div class="field">
        <label for="label">Label</label>
        <input type="text" id="label" placeholder="november batch">
      </div>
      <div id="form-error"></div>
      <button type="submit" ${noKey ? 'disabled' : ''}>Start</button>
    </form>`;

  $('#run-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const button = $('#run-form button');
    const error = $('#form-error');
    error.innerHTML = '';
    let icp = null;
    const raw = $('#icp').value.trim();
    if (raw) {
      try { icp = JSON.parse(raw); } catch {
        error.innerHTML = '<div class="notice bad">The ideal customer must be valid JSON.</div>';
        return;
      }
    }
    button.disabled = true;
    button.textContent = 'Starting…';
    try {
      const body = {
        domains: $('#domains').value.split('\n').map((s) => s.trim()).filter(Boolean),
        objectives: $('#objectives').value.trim() || null,
        icp,
        depth: $('#depth').value,
        label: $('#label').value.trim() || null,
      };
      const { run_id } = await api('/api/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      location.hash = `#/runs/${run_id}`;
    } catch (err) {
      error.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
      button.disabled = false;
      button.textContent = 'Start';
    }
  });
}

/* ------------------------------------------------------------------ routing */

function wireRows() {
  // A whole row is the target, so it has to behave like a link for a keyboard and
  // for a screen reader, not only for a mouse.
  document.querySelectorAll('.row-link').forEach((row) => {
    row.tabIndex = 0;
    row.setAttribute('role', 'link');
    const go = () => { location.hash = row.dataset.href; };
    row.addEventListener('click', go);
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
  });
}

async function route() {
  const path = location.hash.replace(/^#/, '').split('?')[0] || '/';
  try {
    if (path === '/' || path === '') await runsView();
    else if (path === '/new') newRunView();
    else if (path.startsWith('/runs/')) await runView(path.split('/')[2]);
    else if (path.startsWith('/companies/')) await companyView(path.split('/')[2]);
    else view.innerHTML = '<p class="empty">No such page.</p>';
  } catch (err) {
    view.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
  }
}

window.addEventListener('hashchange', route);
paintMasthead();
route();
