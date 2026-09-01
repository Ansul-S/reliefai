// ── State ──────────────────────────────────────────────────────────
let currentDisasterId = null;
let currentSection = 'command';

// ── Supply severity ────────────────────────────────────────────────
// One function decides the bar colour, the card's left edge and the badge, so
// the three can never disagree on screen. The 0.3 cut matches the server's
// definition of critical (database/crud.py get_critical_supplies), and the API
// still sends is_critical — honoured here so an item the backend flags is
// never shown as healthy, even if the ratio alone would not trip the cut.
const CRITICAL_RATIO = 0.30;
const LOW_RATIO = 0.60;

function supplySeverity(s) {
  const pct = s.quantity_needed > 0
    ? Math.min(100, (s.quantity_available / s.quantity_needed) * 100)
    : 0;
  const ratio = pct / 100;
  if (s.is_critical || ratio < CRITICAL_RATIO) return { pct, key: 'crit', label: 'Critical' };
  if (ratio < LOW_RATIO) return { pct, key: 'warn', label: 'Low' };
  return { pct, key: 'ok', label: 'Adequate' };
}
let autoRefreshTimer = null;

const API_BASE = window.location.origin;

const AGENT_COLORS = {
  volunteer_agent: 'safe',
  supply_agent:    'warn',
  comms_agent:     'purple',
  shift_agent:     'info',
  report_agent:    'gray',
  orchestrator:    'accent',
};

const AGENT_PULSE_NAMES = [
  'volunteer_agent', 'supply_agent', 'comms_agent',
  'shift_agent', 'report_agent'
];

// ── Core API ────────────────────────────────────────────────────────
function getApiKey() {
  return document.getElementById('api-key-input').value.trim() || 'reliefai-demo-key';
}

async function apiCall(method, path, body = null) {
  const opts = {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-API-Key': getApiKey(),
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(API_BASE + path, opts);
  const data = await res.json();
  if (!res.ok || data.success === false) {
    throw new Error(data.error || data.detail || `HTTP ${res.status}`);
  }
  return data;
}

// ── UI Helpers ──────────────────────────────────────────────────────
function showSection(name) {
  currentSection = name;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const sec = document.getElementById('section-' + name);
  if (sec) sec.classList.add('active');
  document.querySelectorAll(`.nav-item[data-section="${name}"]`).forEach(n => n.classList.add('active'));
  loadSection(name);
}

function loadSection(name) {
  if (!currentDisasterId) return;
  switch(name) {
    case 'command':    loadOverview();    break;
    case 'volunteers': loadVolunteers(); break;
    case 'supplies':   loadSupplies();   break;
    case 'broadcasts': loadBroadcasts(); break;
    case 'shifts':     loadShifts();     break;
  }
}

function refreshCurrentSection() {
  updateTimestamp();
  loadSection(currentSection);
}

function updateTimestamp() {
  document.getElementById('last-updated').textContent =
    'Updated ' + new Date().toLocaleTimeString();
}

function showToast(message, type = 'info') {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = message;
  c.appendChild(t);
  setTimeout(() => {
    t.style.animation = 'none';
    t.style.opacity = '0';
    t.style.transition = 'opacity 0.3s';
    setTimeout(() => t.remove(), 300);
  }, 3200);
}

function showLoading(title, sub = '') {
  document.getElementById('overlay-title').textContent = title;
  document.getElementById('overlay-sub').textContent = sub;
  document.getElementById('overlay').classList.add('active');
}

function hideLoading() {
  document.getElementById('overlay').classList.remove('active');
}

function setHtml(id, html) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = html;
}

function renderMarkdown(text) {
  if (!text) return '';
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>')
    .replace(/\n{2,}/g, '</p><p>')
    .replace(/^(?!<[hul])(.+)$/gm, (m, p) => p.trim() ? `<p>${p}</p>` : '')
    .replace(/<\/li>\n<li>/g, '</li><li>')
    .replace(/(<li>[\s\S]+<\/li>)/g, '<ul>$1</ul>');
}

// ── Disasters ──────────────────────────────────────────────────────
async function loadDisasters() {
  try {
    const data = await apiCall('GET', '/disasters');
    const disasters = data.data || [];
    const sel = document.getElementById('disaster-select');
    sel.innerHTML = disasters.length
      ? disasters.map(d =>
          `<option value="${d.id}">[S${d.severity}] ${d.name}</option>`
        ).join('')
      : '<option value="">No disasters found</option>';

    if (disasters.length > 0) {
      currentDisasterId = disasters[0].id;
      sel.value = currentDisasterId;
      document.getElementById('disaster-name').textContent = disasters[0].name;
    }
  } catch (e) {
    showToast('Failed to load disasters: ' + e.message, 'error');
  }
}

document.getElementById('disaster-select').addEventListener('change', function() {
  currentDisasterId = this.value ? parseInt(this.value) : null;
  const opt = this.options[this.selectedIndex];
  const rawName = opt.text;
  const cleanName = rawName.replace(/^\[S\d+\]\s*/, '');
  document.getElementById('disaster-name').textContent = cleanName;
  loadSection(currentSection);
});

// ── OPERATION OVERVIEW ─────────────────────────────────────────────
// Every figure below is read from the API. GET /disasters/{id} already returns
// the counts the tiles need, so the overview costs one request; the attention
// lists reuse the supplies and tasks endpoints.

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

async function loadOverview() {
  try {
    const data = await apiCall('GET', `/disasters/${currentDisasterId}`);
    const d = data.data || {};
    const critical = d.critical_supplies ?? 0;

    setText('kpi-critical', critical);
    setText('kpi-volunteers', d.volunteers_assigned ?? 0);
    setText('kpi-tasks', d.tasks_open ?? 0);
    setText('kpi-shifts', d.shifts_today ?? 0);
    setText('kpi-supplies', d.supplies_total ?? 0);

    // Status rides in a dot plus a word, never in the colour of the number.
    const key = critical === 0 ? 'ok' : critical <= 2 ? 'warn' : 'crit';
    const word = critical === 0
      ? 'All supplies above threshold'
      : critical === 1
        ? '1 item below 30% of need'
        : `${critical} items below 30% of need`;
    const status = document.getElementById('kpi-critical-status');
    if (status) status.innerHTML =
      `<span class="kpi-dot dot-${key}"></span><span>${escHtml(word)}</span>`;
  } catch (e) {
    showToast('Failed to load overview: ' + e.message, 'error');
  }
  loadAttention();
}

async function loadAttention() {
  const panel = document.getElementById('attention');
  try {
    const [supplyRes, taskRes] = await Promise.all([
      apiCall('GET', `/disasters/${currentDisasterId}/supplies`),
      apiCall('GET', `/disasters/${currentDisasterId}/tasks`),
    ]);

    const short = (supplyRes.data || [])
      .map(s => ({ s, sev: supplySeverity(s) }))
      .filter(x => x.sev.key !== 'ok')
      .sort((a, b) => a.sev.pct - b.sev.pct);

    setHtml('attention-supplies', short.length
      ? short.map(({ s, sev }) => `<li class="attention-item">
          <span class="kpi-dot dot-${sev.key}"></span>
          <span class="attention-name">${escHtml(s.item_name)}</span>
          <span class="attention-meta">${Math.round(sev.pct)}%</span>
        </li>`).join('')
      : `<li class="attention-empty"><span class="kpi-dot dot-ok"></span>Every tracked supply is above threshold</li>`);

    const open = (taskRes.data || [])
      .filter(t => t.status !== 'done')
      .sort((a, b) => (a.priority === 'high' ? -1 : 1) - (b.priority === 'high' ? -1 : 1));

    setHtml('attention-tasks', open.length
      ? open.slice(0, 6).map(t => `<li class="attention-item">
          <span class="kpi-dot dot-${t.priority === 'high' ? 'crit' : t.priority === 'medium' ? 'warn' : 'ok'}"></span>
          <span class="attention-name">${escHtml(t.title)}</span>
          <span class="attention-meta">${escHtml(t.priority || '')}</span>
        </li>`).join('')
      : `<li class="attention-empty"><span class="kpi-dot dot-ok"></span>No open tasks</li>`);

    panel.hidden = false;
  } catch (e) {
    panel.hidden = true;
  }
}

// ── COMMAND CENTER ─────────────────────────────────────────────────
document.querySelectorAll('.quick-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.getElementById('cmd-textarea').value = btn.dataset.cmd;
  });
});

let agentPulseInterval = null;

function startAgentPulse() {
  let i = 0;
  const el = document.getElementById('agent-pulse-name');
  agentPulseInterval = setInterval(() => {
    if (el) el.textContent = AGENT_PULSE_NAMES[i % AGENT_PULSE_NAMES.length] + '...';
    i++;
  }, 800);
}

function stopAgentPulse() {
  if (agentPulseInterval) clearInterval(agentPulseInterval);
  agentPulseInterval = null;
}

async function deployAgents() {
  const cmd = document.getElementById('cmd-textarea').value.trim();
  if (!cmd) { showToast('Enter a command first', 'warn'); return; }
  if (!currentDisasterId) { showToast('Select a disaster first', 'warn'); return; }

  const btn = document.getElementById('deploy-btn');
  btn.disabled = true;
  document.getElementById('agent-loading').style.display = 'block';
  document.getElementById('cmd-results').innerHTML = '';
  startAgentPulse();

  try {
    const data = await apiCall('POST', `/disasters/${currentDisasterId}/command`, { command: cmd });
    renderOrchestratorResult(data.data);
    showToast('Agents completed successfully', 'success');
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    document.getElementById('cmd-results').innerHTML =
      `<div class="alert-card"><span class="alert-icon">⚠</span>${escHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    document.getElementById('agent-loading').style.display = 'none';
    stopAgentPulse();
    updateTimestamp();
  }
}

function renderOrchestratorResult(result) {
  if (!result) return;
  const container = document.getElementById('cmd-results');
  let html = '';

  // Summary
  if (result.orchestrator_summary) {
    html += `<div class="result-summary-card">
      <div class="card-title">Orchestrator Summary</div>
      <div class="result-summary-text">${escHtml(result.orchestrator_summary)}</div>
    </div>`;
  }

  // Agents called
  const agents = result.agents_called || [];
  if (agents.length) {
    html += `<div class="agents-called-row mb-8">
      <span class="agents-called-label">Agents deployed:</span>
      ${agents.map(a => `<span class="badge badge-${AGENT_COLORS[a] || 'gray'}">${a}</span>`).join('')}
      <span class="mono text-muted" style="font-size:11px;">${result.execution_time_ms || 0}ms</span>
    </div>`;
  }

  // Alerts
  const alerts = result.alerts || [];
  if (alerts.length) {
    html += alerts.map(a =>
      `<div class="alert-card"><span class="alert-icon">⚠</span>${escHtml(a)}</div>`
    ).join('');
  }

  // Per-agent results
  const results = result.results || {};
  Object.entries(results).forEach(([agentName, agentResult]) => {
    const color = AGENT_COLORS[agentName] || 'gray';
    const hasError = agentResult && agentResult.error;
    html += `<div class="agent-result-card">
      <div class="agent-result-header" onclick="toggleAgentResult(this)">
        <div class="agent-result-name">
          <span class="badge badge-${color}">${agentName}</span>
          ${hasError ? '<span class="text-crit" style="font-size:11px;">⚠ error</span>' : ''}
        </div>
        <span class="chevron">▼</span>
      </div>
      <div class="agent-result-body">
${escHtml(JSON.stringify(agentResult, null, 2))}
      </div>
    </div>`;
  });

  // Next actions
  const actions = result.next_recommended_actions || [];
  if (actions.length) {
    html += `<div class="next-actions-card">
      <div class="next-actions-title">▶ Recommended Next Actions</div>
      ${actions.map(a => `<div class="next-action-item"><span class="next-action-bullet">→</span>${escHtml(a)}</div>`).join('')}
    </div>`;
  }

  container.innerHTML = html;
}

function toggleAgentResult(header) {
  const body = header.nextElementSibling;
  const chevron = header.querySelector('.chevron');
  body.classList.toggle('open');
  chevron.classList.toggle('open');
}

// ── VOLUNTEERS ─────────────────────────────────────────────────────
async function loadVolunteers() {
  if (!currentDisasterId) return;
  setHtml('volunteers-tbody', '<tr><td colspan="5" style="text-align:center;padding:20px;"><div class="spinner-inline"></div></td></tr>');
  try {
    const data = await apiCall('GET', `/disasters/${currentDisasterId}/volunteers`);
    const vols = data.data || [];
    if (!vols.length) {
      setHtml('volunteers-tbody',
        '<tr><td colspan="5"><div class="empty-state"><div class="empty-icon">👥</div><div class="empty-text">No volunteers assigned</div></div></td></tr>');
      return;
    }
    const rows = vols.map(v => {
      const skills = (v.skills || []).map(s => `<span class="skill-tag">${escHtml(s)}</span>`).join('');
      const langs  = (v.languages || []).map(l => `<span class="lang-tag">${escHtml(l)}</span>`).join('');
      const hours  = v.hours_today || 0;
      const shiftText = hours > 0 ? `${hours}h today` : 'Not scheduled';
      const statusColor = v.is_active ? (hours > 0 ? 'green' : 'amber') : 'gray';
      const statusText = v.is_active ? (hours > 0 ? 'On duty' : 'Available') : 'Inactive';
      return `<tr>
        <td><strong>${escHtml(v.name)}</strong><br><span class="text-muted mono" style="font-size:10px;">${escHtml(v.phone)}</span></td>
        <td>${skills || '<span class="text-muted">—</span>'}</td>
        <td>${langs || '<span class="text-muted">—</span>'}</td>
        <td><span class="mono" style="font-size:12px;">${shiftText}</span></td>
        <td><span class="status-indicator"><span class="dot dot-${statusColor}"></span>${statusText}</span></td>
      </tr>`;
    }).join('');
    setHtml('volunteers-tbody', rows);
  } catch (e) {
    showToast('Failed to load volunteers: ' + e.message, 'error');
  }
}

async function triggerGapDetection() {
  if (!currentDisasterId) { showToast('Select a disaster first', 'warn'); return; }
  setHtml('gap-result-vol', '<div class="gap-result-card"><div class="spinner-inline"></div> Detecting gaps...</div>');
  try {
    const data = await apiCall('POST', `/disasters/${currentDisasterId}/shifts/gaps`);
    const r = data.data || {};
    const gaps = r.gaps_found || [];
    const filled = r.gaps_filled || [];
    const escalations = r.escalations_needed || [];
    setHtml('gap-result-vol', `<div class="gap-result-card">
      <div class="card-title">Gap Detection Result</div>
      <div class="row mb-8">
        <span class="badge badge-${gaps.length > 0 ? 'accent' : 'safe'}">${gaps.length} gaps found</span>
        <span class="badge badge-safe">${filled.length} filled</span>
        <span class="badge badge-${escalations.length > 0 ? 'accent' : 'gray'}">${escalations.length} escalations</span>
        <span class="badge badge-gray">${r.coverage_status || 'analyzed'}</span>
      </div>
      ${escalations.length ? escalations.map(e =>
        `<div class="alert-card"><span class="alert-icon">⚠</span>${escHtml(JSON.stringify(e))}</div>`
      ).join('') : ''}
    </div>`);
    showToast(`Gap analysis complete: ${gaps.length} gaps found`, gaps.length > 0 ? 'warn' : 'success');
  } catch (e) {
    setHtml('gap-result-vol', `<div class="alert-card"><span class="alert-icon">⚠</span>${escHtml(e.message)}</div>`);
    showToast('Gap detection failed: ' + e.message, 'error');
  }
}

// ── SUPPLIES ───────────────────────────────────────────────────────
async function loadSupplies() {
  if (!currentDisasterId) return;
  setHtml('supply-grid', '<div style="grid-column:1/-1;text-align:center;padding:40px;"><div class="spinner-inline"></div></div>');
  try {
    const data = await apiCall('GET', `/disasters/${currentDisasterId}/supplies`);
    const supplies = data.data || [];
    if (!supplies.length) {
      setHtml('supply-grid', '<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">📦</div><div class="empty-text">No supplies tracked</div></div>');
      return;
    }
    const cards = supplies.map(s => {
      const { pct, key, label } = supplySeverity(s);
      return `<div class="supply-card sev-${key}">
        <div class="supply-body">
          <div class="supply-name">${escHtml(s.item_name)}</div>
          <div class="supply-bar-wrap">
            <div class="supply-bar bar-${key}" style="width:${pct.toFixed(1)}%"></div>
          </div>
          <div class="supply-qty">${s.quantity_available}<span class="supply-qty-need"> / ${s.quantity_needed} ${escHtml(s.unit)}</span></div>
          <div class="supply-foot">
            <span class="badge badge-${key === 'crit' ? 'accent' : key === 'warn' ? 'warn' : 'safe'}">${label}</span>
            <span class="badge cat-${escHtml(s.category)}">${escHtml(s.category)}</span>
          </div>
        </div>
      </div>`;
    }).join('');
    setHtml('supply-grid', cards);
  } catch (e) {
    showToast('Failed to load supplies: ' + e.message, 'error');
  }
}

async function runSupplyAssessment() {
  if (!currentDisasterId) { showToast('Select a disaster first', 'warn'); return; }
  setHtml('supply-assessment-result', '<div class="card"><div class="spinner-inline"></div> Running supply assessment...</div>');
  try {
    const data = await apiCall('POST', `/disasters/${currentDisasterId}/supplies/assess`);
    const r = data.data || {};
    const crit = r.critical_shortages || [];
    const tasks = r.tasks_created_in_db || r.tasks_created || [];
    const suggestions = r.reallocation_suggestions || [];
    setHtml('supply-assessment-result', `<div class="card">
      <div class="card-title">Supply Assessment Result</div>
      <div class="row mb-8">
        <span class="badge badge-${crit.length > 0 ? 'accent' : 'safe'}">${crit.length} critical shortages</span>
        <span class="badge badge-info">${tasks.length} tasks created</span>
      </div>
      <div style="font-size:13px;color:var(--text-muted);margin-bottom:8px;">${escHtml(r.overall_supply_status || '')}</div>
      ${suggestions.length ? `<div class="card-title">Reallocation Suggestions</div>
        ${suggestions.map(s => `<div class="next-action-item"><span class="next-action-bullet">→</span>${escHtml(s)}</div>`).join('')}` : ''}
    </div>`);
    showToast('Supply assessment complete', 'success');
    loadSupplies();
  } catch (e) {
    setHtml('supply-assessment-result', `<div class="alert-card"><span class="alert-icon">⚠</span>${escHtml(e.message)}</div>`);
    showToast('Assessment failed: ' + e.message, 'error');
  }
}

// ── BROADCASTS ─────────────────────────────────────────────────────
document.querySelectorAll('.lang-check-label').forEach(label => {
  label.addEventListener('click', () => {
    const cb = label.querySelector('input');
    cb.checked = !cb.checked;
    label.classList.toggle('checked', cb.checked);
  });
});

function getCheckedLanguages() {
  return [...document.querySelectorAll('.lang-check-label')]
    .filter(l => l.querySelector('input').checked)
    .map(l => l.dataset.lang);
}

async function sendBroadcast() {
  const msg = document.getElementById('broadcast-msg').value.trim();
  if (!msg) { showToast('Enter a message first', 'warn'); return; }
  if (!currentDisasterId) { showToast('Select a disaster first', 'warn'); return; }
  const langs = getCheckedLanguages();
  const channel = document.getElementById('broadcast-channel').value;

  setHtml('translations-result', '<div class="card"><div class="spinner-inline"></div> Translating &amp; sending...</div>');
  try {
    const data = await apiCall('POST', `/disasters/${currentDisasterId}/broadcasts`, {
      message: msg, languages: langs, channel
    });
    const r = data.data || {};
    const trans = r.translations || {};
    const langMeta = {
      en: ['🇬🇧','English'], hi: ['🇮🇳','Hindi'], ta: ['🇮🇳','Tamil'],
      id: ['🇮🇩','Bahasa'], tl: ['🇵🇭','Filipino'], ja: ['🇯🇵','Japanese']
    };
    const cards = Object.entries(trans)
      .filter(([k]) => k !== 'sms')
      .map(([lang, text]) => {
        const meta = langMeta[lang] || ['🌐', lang.toUpperCase()];
        return `<div class="translation-card">
          <div class="translation-lang">${meta[0]} <span>${meta[1]}</span></div>
          <div class="translation-text">${escHtml(text)}</div>
        </div>`;
      }).join('');
    setHtml('translations-result', `<div style="margin-bottom:14px;">
      <div class="row mb-8">
        <span class="badge badge-safe">Broadcast #${r.broadcast_id || '?'} saved</span>
        <span class="badge badge-info">channel: ${channel}</span>
      </div>
      <div class="translation-grid">${cards}</div>
    </div>`);
    showToast('Broadcast sent and translated!', 'success');
    loadBroadcasts();
  } catch (e) {
    setHtml('translations-result', `<div class="alert-card"><span class="alert-icon">⚠</span>${escHtml(e.message)}</div>`);
    showToast('Broadcast failed: ' + e.message, 'error');
  }
}

async function loadBroadcasts() {
  if (!currentDisasterId) return;
  setHtml('broadcast-history', '<div style="text-align:center;padding:20px;"><div class="spinner-inline"></div></div>');
  try {
    const data = await apiCall('GET', `/disasters/${currentDisasterId}/broadcasts`);
    const broadcasts = data.data || [];
    if (!broadcasts.length) {
      setHtml('broadcast-history', '<div class="empty-state"><div class="empty-icon">📡</div><div class="empty-text">No broadcasts yet</div></div>');
      return;
    }
    const langFlags = { en:'🇬🇧', hi:'🇮🇳', ta:'🇮🇳', id:'🇮🇩', tl:'🇵🇭', ja:'🇯🇵' };
    const items = broadcasts.map(b => {
      const trans = b.message_translations || {};
      const langPills = Object.keys(trans)
        .filter(k => k !== 'sms')
        .map(k => `<span class="lang-tag">${langFlags[k] || '🌐'} ${k.toUpperCase()}</span>`)
        .join('');
      const transRows = Object.entries(trans)
        .filter(([k]) => k !== 'sms')
        .map(([k,v]) => `<div class="broadcast-trans-row"><span class="broadcast-trans-lang">${(langFlags[k]||'🌐')} ${k.toUpperCase()}:</span> ${escHtml(String(v).substring(0,120))}</div>`)
        .join('');
      const time = b.sent_at ? new Date(b.sent_at).toLocaleString() : (b.status === 'draft' ? 'Draft' : '—');
      return `<div class="broadcast-history-item" onclick="toggleBroadcastExpand(this)">
        <div class="broadcast-history-header">
          <span class="broadcast-time mono">${time}</span>
          <div class="row" style="gap:5px;">
            <span class="badge badge-${b.status === 'sent' ? 'safe' : b.status === 'draft' ? 'gray' : 'accent'}">${b.status}</span>
            <span class="badge badge-info">${b.channel}</span>
          </div>
        </div>
        <div class="broadcast-preview">${escHtml((b.message_en||'').substring(0,80))}${(b.message_en||'').length>80?'…':''}</div>
        <div style="margin-top:5px;">${langPills}</div>
        <div class="broadcast-translations">
          ${transRows || '<span class="text-muted">No translations</span>'}
        </div>
      </div>`;
    }).join('');
    setHtml('broadcast-history', items);
  } catch (e) {
    showToast('Failed to load broadcasts: ' + e.message, 'error');
  }
}

function toggleBroadcastExpand(el) {
  el.querySelector('.broadcast-translations').classList.toggle('open');
}

// ── SHIFTS ─────────────────────────────────────────────────────────
function todayStr() {
  return new Date().toISOString().slice(0, 10);
}

async function loadShifts() {
  if (!currentDisasterId) return;
  const dateInput = document.getElementById('shift-date');
  const dateVal = dateInput.value || todayStr();
  if (!dateInput.value) dateInput.value = dateVal;

  try {
    const data = await apiCall('GET', `/disasters/${currentDisasterId}/shifts?date=${dateVal}`);
    const shifts = data.data || [];
    renderShiftGrid(shifts, dateVal);
  } catch (e) {
    showToast('Failed to load shifts: ' + e.message, 'error');
  }
}

function renderShiftGrid(shifts, dateStr) {
  const header = document.getElementById('shift-grid-header');
  const body = document.getElementById('shift-grid-body');

  // Build header: empty name cell + 24 hour cells
  let hHtml = '<th class="shift-name-cell" style="background:var(--card2);">Volunteer</th>';
  for (let h = 0; h < 24; h++) {
    hHtml += `<th>${String(h).padStart(2,'0')}</th>`;
  }
  header.innerHTML = hHtml;

  if (!shifts.length) {
    body.innerHTML = `<tr><td colspan="25"><div class="empty-state"><div class="empty-icon">🗓</div><div class="empty-text">No shifts scheduled for this date</div></div></td></tr>`;
    return;
  }

  // Group by volunteer
  const byVol = {};
  shifts.forEach(s => {
    const key = `${s.volunteer_id}__${s.volunteer_name || ('V'+s.volunteer_id)}`;
    if (!byVol[key]) byVol[key] = [];
    byVol[key].push(s);
  });

  // Coverage count per hour
  const coverage = new Array(24).fill(0);

  let rows = '';
  Object.entries(byVol).forEach(([key, volShifts]) => {
    const [, volName] = key.split('__');
    let cells = `<td class="shift-name-cell">${escHtml(volName)}</td>`;
    for (let h = 0; h < 24; h++) {
      const activeShift = volShifts.find(s => {
        if (!s.start_time || !s.end_time) return false;
        const start = new Date(s.start_time);
        const end   = new Date(s.end_time);
        return start.getUTCHours() <= h && end.getUTCHours() > h &&
               s.status !== 'cancelled';
      });
      const cancelShift = volShifts.find(s => {
        if (!s.start_time || !s.end_time) return false;
        const start = new Date(s.start_time);
        const end   = new Date(s.end_time);
        return start.getUTCHours() <= h && end.getUTCHours() > h &&
               s.status === 'cancelled';
      });
      if (activeShift) {
        coverage[h]++;
        cells += `<td class="shift-cell-active" title="${escHtml(activeShift.role)}: ${activeShift.status}"></td>`;
      } else if (cancelShift) {
        cells += `<td class="shift-cell-cancelled" title="Cancelled: ${escHtml(cancelShift.role)}"></td>`;
      } else {
        cells += `<td class="shift-cell-empty"></td>`;
      }
    }
    rows += `<tr>${cells}</tr>`;
  });

  // Coverage row
  let covRow = '<td class="coverage-label">Coverage</td>';
  coverage.forEach(c => {
    covRow += `<td class="${c < 2 ? 'coverage-gap' : 'coverage-ok'}">${c}</td>`;
  });
  rows += `<tr class="coverage-row">${covRow}</tr>`;
  body.innerHTML = rows;
}

async function checkShiftGaps() {
  if (!currentDisasterId) { showToast('Select a disaster first', 'warn'); return; }
  setHtml('shift-gap-result', '<div class="gap-result-card"><div class="spinner-inline"></div> Analyzing coverage...</div>');
  try {
    const data = await apiCall('POST', `/disasters/${currentDisasterId}/shifts/gaps`);
    const r = data.data || {};
    setHtml('shift-gap-result', `<div class="gap-result-card">
      <div class="card-title">Coverage Analysis</div>
      <div class="row mb-8">
        <span class="badge badge-${(r.gaps_found||[]).length > 0 ? 'accent':'safe'}">${(r.gaps_found||[]).length} gaps</span>
        <span class="badge badge-safe">${(r.gaps_filled||[]).length} auto-filled</span>
        <span class="badge badge-gray">${r.coverage_status || 'analyzed'}</span>
      </div>
      ${(r.escalations_needed||[]).length ? `<div class="card-title text-crit">Escalations Required</div>
        ${(r.escalations_needed||[]).map(e =>
          `<div class="alert-card"><span>⚠</span>${escHtml(typeof e === 'object' ? JSON.stringify(e) : e)}</div>`
        ).join('')}` : ''}
    </div>`);
  } catch (e) {
    setHtml('shift-gap-result', `<div class="alert-card"><span>⚠</span>${escHtml(e.message)}</div>`);
    showToast('Gap check failed: ' + e.message, 'error');
  }
}

// ── REPORTS ────────────────────────────────────────────────────────
async function generateReport(type) {
  if (!currentDisasterId) { showToast('Select a disaster first', 'warn'); return; }
  const container = document.getElementById('report-result');
  container.style.display = 'block';
  container.innerHTML = `<div style="padding:32px;text-align:center;">
    <div class="spinner-inline"></div>
    <div style="margin-top:12px;color:var(--text-muted);font-family:var(--mono);font-size:13px;">Report agent generating ${type} report...</div>
  </div>`;

  try {
    const data = await apiCall('GET', `/disasters/${currentDisasterId}/report?type=${type}`);
    const r = data.data || {};
    const typeColors = { donor: 'safe', authority: 'accent', internal: 'gray' };
    const color = typeColors[type] || 'gray';
    const flags = r.critical_flags || [];
    const sections = r.sections || {};
    const md = r.markdown_report || Object.values(sections).join('\n\n');

    container.innerHTML = `
      <div class="report-header">
        <div class="row">
          <span class="badge badge-${color}">${(type||'').toUpperCase()} REPORT</span>
          <span class="text-muted mono" style="font-size:11px;">${r.generated_at ? new Date(r.generated_at).toLocaleString() : 'now'}</span>
        </div>
        <button class="btn btn-ghost btn-sm" onclick="copyReport(${JSON.stringify(md).replace(/"/g,'&quot;')})">
          📋 Copy
        </button>
      </div>
      <div class="report-body">
        ${flags.length ? `<div class="report-flags">
          <div class="card-title text-crit">🚨 Critical Flags</div>
          ${flags.map(f => `<div class="report-flag-item">⚠ ${escHtml(f)}</div>`).join('')}
        </div><div class="divider"></div>` : ''}
        <div class="report-markdown">${renderMarkdown(md)}</div>
      </div>`;
    showToast('Report generated', 'success');
  } catch (e) {
    container.innerHTML = `<div style="padding:16px;"><div class="alert-card"><span>⚠</span>${escHtml(e.message)}</div></div>`;
    showToast('Report failed: ' + e.message, 'error');
  }
}

function copyReport(text) {
  navigator.clipboard.writeText(text).then(() => showToast('Copied to clipboard', 'success'));
}

// ── FULL DEMO ──────────────────────────────────────────────────────
async function runFullDemo() {
  showLoading('Seeding Typhoon Carina scenario...', 'Creating 12 volunteers, 8 supplies, shifts and notes');

  try {
    const seedData = await apiCall('POST', '/demo/seed');
    const did = seedData.data.disaster_id;
    currentDisasterId = did;

    // Update selector
    await loadDisasters();
    document.getElementById('disaster-select').value = did;
    const opt = document.getElementById('disaster-select').options;
    for (let i = 0; i < opt.length; i++) {
      if (parseInt(opt[i].value) === did) {
        document.getElementById('disaster-name').textContent = opt[i].text.replace(/^\[S\d+\]\s*/,'');
        break;
      }
    }

    document.getElementById('overlay-title').textContent = 'Running 5 agent commands...';
    document.getElementById('overlay-sub').textContent = 'Assessing situation → finding replacements → drafting broadcasts → generating reports';

    const simData = await apiCall('POST', '/demo/simulate');
    hideLoading();

    // Show results in command center
    showSection('command');
    const allResults = simData.data.results || [];
    const container = document.getElementById('cmd-results');

    let html = `<div class="result-summary-card" style="border-left-color:var(--accent);">
      <div class="card-title">Demo Complete — 5 Commands Executed</div>
      <div class="result-summary-text">Full Typhoon Carina scenario simulated with ${allResults.length} orchestrator commands.</div>
    </div>`;

    allResults.forEach((item, i) => {
      const r = item.result || {};
      const agents = r.agents_called || [];
      html += `<div class="agent-result-card">
        <div class="agent-result-header" onclick="toggleAgentResult(this)">
          <div class="agent-result-name">
            <span class="badge badge-info">Command ${i+1}</span>
            <span style="font-size:12px;color:var(--text-muted);font-weight:400;">${escHtml((item.command||'').substring(0,70))}</span>
          </div>
          <div class="row" style="gap:5px;">
            ${agents.map(a => `<span class="badge badge-${AGENT_COLORS[a]||'gray'}" style="font-size:10px;">${a}</span>`).join('')}
            <span class="chevron">▼</span>
          </div>
        </div>
        <div class="agent-result-body">${escHtml(JSON.stringify(r, null, 2))}</div>
      </div>`;
    });

    container.innerHTML = html;
    showToast('Demo complete! 5 commands executed.', 'success');

    // Refresh all sections
    setTimeout(() => { loadVolunteers(); loadSupplies(); loadBroadcasts(); }, 500);

  } catch(e) {
    hideLoading();
    showToast('Demo failed: ' + e.message, 'error');
  }
}

// ── UTILITIES ──────────────────────────────────────────────────────
function escHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}

// ── NAV ────────────────────────────────────────────────────────────
document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => showSection(item.dataset.section));
});

// ── INIT ───────────────────────────────────────────────────────────
async function init() {
  document.getElementById('shift-date').value = todayStr();
  await loadDisasters();
  updateTimestamp();
  // loadDisasters only picks the active disaster; the command view's overview
  // needs an explicit first load or it sits on em dashes until a section change.
  loadSection(currentSection);

  // Auto refresh every 30s
  autoRefreshTimer = setInterval(refreshCurrentSection, 30000);
}


// ── Off-canvas navigation (<= 768px) ───────────────────────────────
// Desktop and the icon rail need none of this: the CSS keeps the sidebar in
// flow there, and the .open class is inert outside the drawer breakpoint.
const sidebarEl = document.getElementById('sidebar');
const backdropEl = document.getElementById('sidebar-backdrop');
const menuBtn = document.getElementById('menu-btn');

function setDrawer(open) {
  sidebarEl.classList.toggle('open', open);
  menuBtn.setAttribute('aria-expanded', String(open));
  if (open) {
    backdropEl.hidden = false;
    requestAnimationFrame(() => backdropEl.classList.add('show'));
  } else {
    backdropEl.classList.remove('show');
    setTimeout(() => { if (!sidebarEl.classList.contains('open')) backdropEl.hidden = true; }, 240);
  }
}

menuBtn.addEventListener('click', () => setDrawer(!sidebarEl.classList.contains('open')));
backdropEl.addEventListener('click', () => setDrawer(false));
document.addEventListener('keydown', e => { if (e.key === 'Escape') setDrawer(false); });

// Picking a destination should close the drawer behind you.
document.querySelectorAll('#sidebar .nav-item').forEach(item =>
  item.addEventListener('click', () => setDrawer(false))
);

// Leaving the drawer breakpoint must not strand a stale backdrop on screen.
window.matchMedia('(max-width: 768px)').addEventListener('change', e => {
  if (!e.matches) setDrawer(false);
});

init();
