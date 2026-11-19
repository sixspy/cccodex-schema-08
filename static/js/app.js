const msg = document.getElementById('msg');
const view = document.getElementById('view');
let contracts = [];
let lastCheck = null;
let windowMinutes = 30;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (ch) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[ch]));

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function setMsg(t, isErr = false) {
  msg.textContent = t || '';
  msg.className = isErr ? 'err' : 'muted';
}

async function loadContracts() {
  const data = await api('/api/contracts');
  contracts = data.items || [];
}

async function loadMeta() {
  try {
    const j = await api('/api/meta');
    windowMinutes = j.rollback_window_minutes || 30;
    document.getElementById('window-hint').textContent = `回退窗口 ${windowMinutes} 分钟`;
  } catch (e) { /* 顶栏提示失败不挡主流程 */ }
}

function stateClass(s) {
  if (s === '现行') return 'chip live';
  if (s === '草稿') return 'chip draft';
  if (s === '只读历史') return 'chip history';
  return 'chip retired';
}

function renderContracts() {
  view.innerHTML = `<div class="grid">${contracts.map(c => `
    <article class="card">
      <div class="row">
        <h3>${esc(c.table_name)} · v${c.version_no}</h3>
        <span class="${stateClass(c.status)}">${esc(c.status)}</span>
        ${c.readable ? '' : '<span class="chip muted-chip">不可读</span>'}
      </div>
      <p class="muted">${esc(c.note || '')} · 字段 ${c.field_count}</p>
    </article>`).join('')}</div>`;
}

function draftOptions() {
  return contracts.filter(c => c.status === '草稿')
    .map(c => `<option value="${c.id}">${esc(c.table_name)} v${c.version_no}</option>`).join('');
}

function renderCheck() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-check" type="button">运行破坏性检查</button>
      <pre id="out" class="muted"></pre>
    </div>`;
  document.getElementById('btn-check').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/breaking-check`, { method: 'POST', body: '{}' });
      lastCheck = j.check;
      document.getElementById('out').textContent = JSON.stringify(j.check, null, 2);
      setMsg(`检查结论：${j.check.verdict}`);
    } catch (e) { setMsg(e.message, true); }
  };
}

function actorField() {
  return `<label>值班代号
    <input id="actor" value="admin" placeholder="谁在操作，写进回滚卡片与时间线" />
  </label>`;
}

function renderApply() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      ${actorField()}
      <button class="primary" id="btn-apply" type="button">换版应用</button>
      <p class="muted">换版后 ${windowMinutes} 分钟内可整笔退回上一版，超过时限回滚窗口关闭。</p>
    </div>`;
  document.getElementById('btn-apply').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const actor = document.getElementById('actor').value.trim() || 'admin';
      const j = await api(`/api/contracts/${cid}/apply`, {
        method: 'POST', body: JSON.stringify({ actor })
      });
      setMsg(`已应用 ${j.contract.table_name} v${j.contract.version_no}；` +
        `窗口到 ${j.contract.window_deadline || '—'} 关闭`);
      await loadContracts();
    } catch (e) { setMsg(e.message, true); }
  };
}

function fateChip(mode) {
  return mode === '整笔回滚'
    ? '<span class="chip fate-full">整笔回滚 · 误升版封存不可读</span>'
    : '<span class="chip fate-retain">部分留存 · 误升版留档可读</span>';
}

function minutesLeftText(p) {
  if (!p.window_open) return '<span class="err">窗口已关</span>';
  return `<strong>剩 ${p.minutes_left} 分钟</strong>（截止 ${esc(p.window_deadline)}）`;
}

function rollbackCard(p) {
  const options = (p.options || []).map(o => `
    <label class="fate-option">
      <input type="radio" name="mode-${esc(p.table_name)}" value="${esc(o.mode)}" />
      ${fateChip(o.mode)}
      <span class="muted">误升版转「${esc(o.mis_state)}」，${o.mis_readable ? '仍可查' : '不再可读'}</span>
    </label>`).join('');
  return `
    <article class="card rollback-card">
      <div class="row">
        <h3>${esc(p.table_name)}</h3>
        <span class="chip">现行 v${p.live_version ?? '—'}</span>
        <span class="chip ghost-chip">退至 v${p.target_version ?? '—'}</span>
        ${p.lock_holder ? `<span class="chip lock-chip">锁：${esc(p.lock_holder)}</span>` : ''}
      </div>
      <p class="muted">换版于 ${esc(p.applied_at || '—')} · ${minutesLeftText(p)}</p>
      ${p.blocked_reason
        ? `<p class="err">拦住了：${esc(p.blocked_reason)}</p>`
        : `<div class="grid">${options}
             <div class="row">
               <button class="primary" data-rb="${esc(p.table_name)}" type="button">确认回退</button>
               <button class="ghost" data-lock="${esc(p.table_name)}" type="button">占住回退锁</button>
               <button class="ghost" data-unlock="${esc(p.table_name)}" type="button">交还回退锁</button>
             </div>
           </div>`}
    </article>`;
}

function chipRow(label, value, cls = '') {
  return `<div class="kv"><span class="muted">${label}</span><span class="${cls}">${esc(value)}</span></div>`;
}

function timelineBlock(items, table) {
  const rows = items.filter(e => e.table_name === table).map(e => `
    <div class="kv">
      <span>${esc(e.at || '')}</span>
      <span>${esc(e.event)} · v${e.version_no}</span>
      <span class="muted">${esc(e.from_state)} → ${esc(e.to_state)}${e.actor ? ' · ' + esc(e.actor) : ''}</span>
    </div>`).join('');
  return `<section class="card"><h4>版本时间线</h4>${rows || '<p class="muted">无记录</p>'}</section>`;
}

function pageBlock(items, table) {
  const rows = items.filter(p => p.table_name === table).map(p => `
    <div class="kv">
      <span>附页 #${p.page_no}</span>
      <span>v${p.version_no}</span>
      <span class="${p.page_state === '现行' ? 'ok' : 'muted'}">${esc(p.page_state)}</span>
    </div>`).join('');
  return `<section class="card"><h4>目录附页</h4>${rows || '<p class="muted">无附页</p>'}</section>`;
}

function watermarkBlock(reports) {
  const r = reports[0];
  if (!r) return '<section class="card"><h4>对账水线</h4><p class="muted">暂无对账</p></section>';
  const rows = (r.lines || []).map(li => `
    <div class="kv">
      <span>${esc(li.table_name)}</span>
      <span class="${li.aligned ? 'ok' : 'err'}">
        契约v${li.contract_version ?? '—'} / 目录v${li.catalog_version ?? '—'} / 附页v${li.page_version ?? '—'}
      </span>
    </div>`).join('');
  return `<section class="card"><h4>对账水线 v${esc(r.watermark || r.as_of)}</h4>
    <p class="muted">${esc(r.summary)}</p>${rows}</section>`;
}

function rollbackHistory(items) {
  if (!items.length) return '<p class="muted">还没有回退记录</p>';
  return items.map(rb => `
    <article class="card rollback-card">
      <div class="row">
        <h3>回滚卡片 #${rb.id} · ${esc(rb.table_name)}</h3>
        ${fateChip(rb.mode)}
        <span class="chip ${rb.verified ? 'ok-chip' : 'warn-chip'}">${esc(rb.status)}</span>
      </div>
      <p class="muted">误升 v${rb.mis_version} 退至 v${rb.target_version} ·
        误升版转「${esc(rb.mis_state)}」· ${rb.mis_readable ? '仍可查' : '不可读'} ·
        窗口截止 ${esc(rb.window_deadline || '—')}</p>
      <div class="grid">
        ${(rb.verifications || []).map(v => `
          <div class="kv"><span>复验第${v.round_no}轮</span>
            <span class="${v.consistent ? 'ok' : 'err'}">${esc(v.detail)}</span></div>`).join('')}
      </div>
    </article>`).join('');
}

async function renderRollback() {
  const [targets, rollbacks, timeline, pages, reports] = await Promise.all([
    api('/api/rollback/targets'),
    api('/api/rollback/rollbacks'),
    api('/api/timeline'),
    api('/api/catalog-pages'),
    api('/api/reconcile-reports')
  ]);
  const cards = (targets.items || []).map(p => rollbackCard(p)).join('') ||
    '<p class="muted">目录里没有可回退的表</p>';
  const focus = (targets.items || []).find(p => p.target_version)?.table_name
    || (targets.items || [])[0]?.table_name;

  view.innerHTML = `
    <div class="grid two-col">
      <div class="grid">
        <h2 class="section-title">回滚卡片</h2>
        ${cards}
      </div>
      <div class="grid">
        <h2 class="section-title">四处联查 · ${esc(focus || '—')}</h2>
        ${focus ? timelineBlock(timeline.items || [], focus) : ''}
        ${watermarkBlock(reports.items || [])}
        ${focus ? pageBlock(pages.items || [], focus) : ''}
      </div>
    </div>
    <h2 class="section-title spaced">回退留痕</h2>
    <div class="grid">${rollbackHistory(rollbacks.items || [])}</div>`;

  view.querySelectorAll('button[data-rb]').forEach(btn => {
    btn.onclick = async () => {
      const table = btn.dataset.rb;
      const picked = view.querySelector(`input[name="mode-${CSS.escape(table)}"]:checked`);
      if (!picked) { setMsg('先在两条命运里选一条，回退只能贯彻一条', true); return; }
      const actor = prompt('值班代号', 'admin');
      if (actor === null) return;
      try {
        const j = await api(`/api/rollback/${encodeURIComponent(table)}`, {
          method: 'POST',
          body: JSON.stringify({ mode: picked.value, actor: actor.trim() || 'admin' })
        });
        const rb = j.rollback;
        setMsg(`已回退 ${rb.table_name}：${rb.fate}；两轮复验${rb.verified ? '一致' : '不符'}`);
        await loadContracts();
        await renderRollback();
      } catch (e) { setMsg(e.message, true); }
    };
  });
  view.querySelectorAll('button[data-lock]').forEach(btn => {
    btn.onclick = async () => {
      try {
        const actor = prompt('值班代号', 'admin');
        if (actor === null) return;
        await api(`/api/rollback/${encodeURIComponent(btn.dataset.lock)}/lock`, {
          method: 'POST', body: JSON.stringify({ actor: actor.trim() || 'admin' })
        });
        setMsg(`已占住 ${btn.dataset.lock} 的回退锁`);
        await renderRollback();
      } catch (e) { setMsg(e.message, true); }
    };
  });
  view.querySelectorAll('button[data-unlock]').forEach(btn => {
    btn.onclick = async () => {
      try {
        const actor = prompt('值班代号', 'admin');
        if (actor === null) return;
        await api(`/api/rollback/${encodeURIComponent(btn.dataset.unlock)}/lock`, {
          method: 'DELETE', body: JSON.stringify({ actor: actor.trim() || 'admin' })
        });
        setMsg(`已交还 ${btn.dataset.unlock} 的回退锁`);
        await renderRollback();
      } catch (e) { setMsg(e.message, true); }
    };
  });
}

async function renderReconcile() {
  const reports = (await api('/api/reconcile-reports')).items || [];
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <button class="primary" id="btn-rec" type="button">跑一轮对账</button>
        <span class="muted">水线按同一版号对照：契约版 / 目录指针 / 目录附页</span>
      </div>
      <div class="grid" id="reps">${reports.map(r => `
        <article class="card">
          <strong>${esc(r.watermark || r.as_of)}</strong>
          <p class="${r.drifted ? 'err' : 'muted'}">${esc(r.summary)}</p>
          ${(r.lines || []).map(li => `
            <div class="kv"><span>${esc(li.table_name)}</span>
              <span class="${li.aligned ? 'ok' : 'err'}">${esc(li.detail)}</span></div>`).join('')}
        </article>`).join('') || '<p class="muted">暂无对账</p>'}</div>
    </div>`;
  document.getElementById('btn-rec').onclick = async () => {
    try {
      const j = await api('/api/reconcile-reports', { method: 'POST', body: JSON.stringify({}) });
      setMsg(j.report.summary);
      await renderReconcile();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function show(tab) {
  setMsg('');
  await loadContracts();
  if (tab === 'contracts') renderContracts();
  if (tab === 'check') renderCheck();
  if (tab === 'apply') renderApply();
  if (tab === 'rollback') await renderRollback();
  if (tab === 'reconcile') await renderReconcile();
}

document.querySelectorAll('.pills button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.pills button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    show(btn.dataset.tab);
  };
});
document.getElementById('btn-logout').onclick = async () => {
  await api('/api/logout', { method: 'POST', body: '{}' });
  location.href = '/login';
};
loadMeta().then(() => show('contracts'));
