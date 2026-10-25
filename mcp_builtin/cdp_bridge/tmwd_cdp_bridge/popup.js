const DEFAULT_CONFIG = {
  bridgeUrl: 'ws://127.0.0.1:8001/mcp/cdp-bridge/session',
  clientToken: '',
  bridgeEnabled: true,
};

const STATUS_LABELS = {
  connected: '已连接',
  authenticating: '正在连接…',
  auth_error: '连接失败',
  unconfigured: '尚未配置 Token',
  disabled: '连接已关闭',
  disconnected: '未连接',
};
const AUTH_ERROR_LABELS = {
  invalid: 'Token 无效',
  invalid_token: 'Token 无效或已撤销',
  disabled: '该客户端已停用',
  permission_denied: '该客户端没有连接权限',
  already_connected: '已在另一个浏览器里连接',
  protocol_required: '服务端版本过旧，需升级',
  revoked: '客户端授权已撤销',
  connection_timeout: '连接超时，请检查服务器地址、端口和网络',
  auth_timeout: '服务端响应超时，请检查服务器地址和服务端版本',
};

const WS_RE = /^wss?:\/\/.+/i;

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('saveBridge').addEventListener('click', saveBridgeConfig);
  document.getElementById('bridgeUrl').addEventListener('input', updatePreview);
  document.getElementById('clientToken').addEventListener('input', updatePreview);
  document.getElementById('bridgeEnabled').addEventListener('change', updatePreview);
  initViews();
  loadBridgeConfig();
  const timer = setInterval(refreshStatus, 750);
  window.addEventListener('unload', () => clearInterval(timer), { once: true });
  initPasswordManager();
  initAuthenticator();
  initProxySection();
});

// ── 菜单式视图路由 ──────────────────────────────────────────────────────────
// 点扩展图标 → 菜单（view-menu，默认）；菜单项/返回按钮在各 view 间切换。
// 网络切换直接做在菜单上（qs-row），不进二级页。
const VIEW_TITLES = {
  conn: '连接设置',
  proxy: '出网代理',
  pwd: '密码管理',
  totp: '身份验证器',
};

function showView(name) {
  for (const v of document.querySelectorAll('.view')) {
    v.classList.toggle('on', v.id === 'view-' + name);
  }
  const isMenu = name === 'menu';
  // 宽度随视图切换：菜单窄（网络切换是短行），子页面宽（表单/密码列表/编辑器需要横向空间）。
  // Chrome popup 会随 documentElement 宽度重新测量弹框尺寸。
  document.documentElement.classList.toggle('wide', !isMenu);
  // 菜单页：header 隐藏（无返回、无标题，连接情况由顶部状态栏承载）。
  // 子页面：header 显示返回 + 页标题。
  document.getElementById('header').hidden = isMenu;
  if (!isMenu) document.getElementById('headerTitle').textContent = VIEW_TITLES[name] || '';
  // 在连接设置页时「查看详情」无意义：隐藏；其它页都显示，便于回到连接详情。
  document.getElementById('connDetailBtn').hidden = name === 'conn';
  if (isMenu) void refreshMenu();
}

function initViews() {
  // 功能入口（配置代理 / 密码列表，都在状态栏 icon 按钮上）：进子页面。
  for (const item of document.querySelectorAll('[data-view]')) {
    item.addEventListener('click', () => showView(item.dataset.view));
  }
  document.getElementById('btnBack').addEventListener('click', () => showView('menu'));
  // 状态栏「查看连接详情」：进入连接设置页。
  document.getElementById('connDetailBtn').addEventListener('click', () => showView('conn'));
  // 内建项（直连 / 系统代理）：ZeroOmega 风格，置顶且不依赖 Profile 库。
  for (const item of document.querySelectorAll('.om-nav-item[data-builtin]')) {
    item.addEventListener('click', async () => {
      await chrome.runtime.sendMessage({ cmd: 'proxy_apply_builtin', builtinType: item.dataset.builtin, appliedBy: 'user' });
      await refreshMenu();
    });
  }
  void refreshMenu();
  const menuTimer = setInterval(() => {
    if (document.getElementById('view-menu').classList.contains('on')) void refreshMenu();
  }, 1500);
  window.addEventListener('unload', () => clearInterval(menuTimer), { once: true });
}

// Profile 类型 → 图标 + 颜色（对齐 ZeroOmega 的 iconForProfileType / profile.color）。
// 我们模型不存 color，故按类型给稳定的默认色。
const PROFILE_ICON = {
  direct: { ic: '⊘', color: '#6b7280' },
  system: { ic: '🖥', color: '#0ea5e9' },
  fixed: { ic: '🌐', color: '#337ab7' },
  pac: { ic: '📄', color: '#a855f7' },
  auto: { ic: '🔀', color: '#f59e0b' },
};

// 菜单渲染：ZeroOmega 结构 + icon-only。
// - 内建直连/系统代理在 HTML 里（data-builtin），这里只翻转 active 态；
// - 各 Profile 渲染成 icon-only 行（只显示类型图标，title 显示名字，hover 出名字）；
// - 当前生效项高亮（内建与 Profile 二选一）。
async function refreshMenu() {
  const resp = await chrome.runtime.sendMessage({ cmd: 'proxy_list' }).catch(() => null);
  if (!resp || !resp.ok) return;
  const { profiles = [], status = {} } = resp.data || {};
  _proxyProfiles = profiles;
  const activeId = status.applied ? String(status.activeProfileId || '') : '';

  // 内建项 active 态
  for (const item of document.querySelectorAll('.om-nav-item[data-builtin]')) {
    item.classList.toggle('on', activeId === item.dataset.builtin);
  }

  const qs = document.getElementById('quickSwitch');
  if (!qs) return;
  qs.innerHTML = '';
  for (const p of profiles) {
    const meta = PROFILE_ICON[p.type] || PROFILE_ICON.direct;
    const row = document.createElement('div');
    row.className = 'om-nav-item' + (activeId === String(p.id) ? ' on' : '');
    row.title = proxyProfileSummary(p);
    const ic = document.createElement('span');
    ic.className = 'ic';
    ic.textContent = meta.ic;
    ic.style.color = meta.color;
    const name = document.createElement('span');
    name.className = 'om-name';
    name.textContent = p.name || p.id;
    row.appendChild(ic);
    row.appendChild(name);
    row.addEventListener('click', async () => {
      await chrome.runtime.sendMessage({ cmd: 'proxy_apply', profileId: p.id, appliedBy: 'user' });
      await refreshMenu();
    });
    qs.appendChild(row);
  }
}

// ── 出网代理（Profile 切换 + 一键还原）──────────────────────────────────────
// 只读状态 750ms 轮询同款节奏；操作经 runtime message 到 SW 的 handleProxyCommand。
function initProxySection() {
  document.getElementById('proxyRestore').addEventListener('click', async () => {
    await chrome.runtime.sendMessage({ cmd: 'proxy_restore' });
    await refreshProxySection();
  });
  document.getElementById('proxyNewBtn').addEventListener('click', () => openProxyEditor(null));
  const fileInput = document.getElementById('proxyImportFile');
  // 两步导入入口：弹来源选择（本地文件 / 另一个浏览器），不直接开文件框。
  document.getElementById('proxyImportBtn').addEventListener('click', () => openImportWizard(null));
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      // SwitchyOmega 备份是 .bak 扩展名、内容是 JSON（OmegaOptions.bak）；这里按 JSON 解析。
      let data;
      try {
        data = JSON.parse(text);
      } catch (_) {
        alert('不是有效的配置备份：请选择 SwitchyOmega 的 OmegaOptions.bak，或本插件导出的 JSON。');
        return;
      }
      // 第一步：解析为候选（不落地）
      const parseResp = await chrome.runtime.sendMessage({ cmd: 'proxy_import_parse', kind: 'file', data });
      if (!parseResp || !parseResp.ok) {
        alert('解析失败：' + ((parseResp && parseResp.error) || '未知错误'));
        return;
      }
      // 第二步：勾选要导入的数据
      await openImportWizard(parseResp.data.candidates);
    } catch (e) {
      alert('导入失败：' + (e.message || e));
    } finally {
      fileInput.value = '';
    }
  });
  refreshProxySection();
  const proxyTimer = setInterval(refreshProxySection, 1500);
  window.addEventListener('unload', () => clearInterval(proxyTimer), { once: true });
}

// 当前 Profile 列表缓存（供编辑器下拉选引用目标）。
let _proxyProfiles = [];

async function refreshProxySection() {
  const resp = await chrome.runtime.sendMessage({ cmd: 'proxy_list' }).catch(() => null);
  if (!resp || !resp.ok) return;
  _proxyProfiles = (resp.data && resp.data.profiles) || [];
  renderProxySection(resp.data || {});
}

function renderProxySection({ profiles = [], status = {} }) {
  const stateEl = document.getElementById('proxyState');
  const textEl = document.getElementById('proxyStateText');
  const whoEl = document.getElementById('proxyStateWho');
  if (status.applied) {
    stateEl.classList.add('on');
    const lease = status.leaseRemainingMs > 0
      ? ` · ${Math.ceil(status.leaseRemainingMs / 60000)} 分钟后还原`
      : '';
    textEl.textContent = `生效：${status.activeName || status.activeProfileId}（${status.activeType}）${lease}`;
    whoEl.textContent = status.appliedBy === 'admin' ? '管理端下发' : '手动设置';
  } else {
    stateEl.classList.remove('on');
    textEl.textContent = '未启用（跟随浏览器默认）';
    whoEl.textContent = '';
  }
  const list = document.getElementById('proxyList');
  list.innerHTML = '';
  if (!profiles.length) {
    const empty = document.createElement('div');
    empty.className = 'proxy-empty';
    empty.textContent = '暂无代理配置。点「新增」创建，或「导入」SwitchyOmega 备份。';
    list.appendChild(empty);
    return;
  }
  for (const p of profiles) {
    const row = document.createElement('div');
    row.className = 'proxy-row' + (status.applied && status.activeProfileId === p.id ? ' active' : '');
    const meta = document.createElement('div');
    meta.className = 'meta';
    const name = document.createElement('div');
    name.className = 'u';
    name.textContent = p.name || p.id;
    const desc = document.createElement('div');
    desc.className = 'd';
    desc.textContent = proxyProfileSummary(p);
    meta.appendChild(name);
    meta.appendChild(desc);
    row.appendChild(meta);

    const editBtn = document.createElement('button');
    editBtn.textContent = '编辑';
    editBtn.addEventListener('click', () => openProxyEditor(p));
    row.appendChild(editBtn);

    const applyBtn = document.createElement('button');
    applyBtn.textContent = status.applied && status.activeProfileId === p.id ? '使用中' : '应用';
    applyBtn.disabled = status.applied && status.activeProfileId === p.id;
    applyBtn.addEventListener('click', async () => {
      applyBtn.disabled = true;
      await chrome.runtime.sendMessage({ cmd: 'proxy_apply', profileId: p.id, appliedBy: 'user' });
      await refreshProxySection();
    });
    row.appendChild(applyBtn);
    list.appendChild(row);
  }
}

function proxyProfileSummary(p) {
  if (p.type === 'fixed') return `${p.fixed?.scheme || 'http'}://${p.fixed?.host || ''}:${p.fixed?.port || ''}`;
  if (p.type === 'auto') return `自动切换（${(p.auto?.rules || []).length} 条规则）`;
  if (p.type === 'pac') return p.pac?.url ? `PAC：${p.pac.url}` : 'PAC 脚本';
  return { direct: '直连', system: '系统代理' }[p.type] || p.type;
}

// ── Profile 编辑器 ──────────────────────────────────────────────────────────
function openProxyEditor(profile) {
  const box = document.getElementById('proxyEditor');
  const isNew = !profile;
  const p = profile ? JSON.parse(JSON.stringify(profile)) : {
    id: '', name: '', type: 'fixed', scope: 'regular',
    fixed: { scheme: 'http', host: '127.0.0.1', port: 7890, bypass: [] },
    pac: { url: '', data: '' }, auto: { rules: [], defaultProfileId: '' },
  };
  box.hidden = false;
  box.innerHTML = '';

  const mk = (tag, cls) => { const el = document.createElement(tag); if (cls) el.className = cls; return el; };
  const field = (labelText, node) => { const f = mk('div', 'f'); const l = mk('label'); l.textContent = labelText; f.appendChild(l); f.appendChild(node); return f; };

  // 名称
  const nameIn = mk('input'); nameIn.value = p.name || ''; nameIn.placeholder = '例如 Clash 7890';
  box.appendChild(field('名称', nameIn));

  // 类型
  const typeSel = mk('select');
  for (const [v, t] of [['direct', '直连'], ['system', '系统代理'], ['fixed', '固定代理'], ['pac', 'PAC 脚本'], ['auto', '自动切换']]) {
    const o = mk('option'); o.value = v; o.textContent = t; typeSel.appendChild(o);
  }
  typeSel.value = p.type;
  box.appendChild(field('类型', typeSel));

  // 各类型的字段容器
  const body = mk('div');
  box.appendChild(body);

  function renderBody() {
    body.innerHTML = '';
    const t = typeSel.value;
    if (t === 'fixed') {
      const scheme = mk('select');
      for (const s of ['http', 'https', 'socks4', 'socks5']) { const o = mk('option'); o.value = s; o.textContent = s; scheme.appendChild(o); }
      scheme.value = p.fixed?.scheme || 'http';
      const host = mk('input'); host.value = p.fixed?.host || ''; host.placeholder = '127.0.0.1';
      const port = mk('input'); port.value = p.fixed?.port || ''; port.placeholder = '7890';
      const r2 = mk('div', 'row2');
      r2.appendChild(field('协议', scheme)); r2.appendChild(field('端口', port));
      body.appendChild(field('主机', host));
      body.appendChild(r2);
      const bypass = mk('input'); bypass.value = (p.fixed?.bypass || []).join(','); bypass.placeholder = '逗号分隔，如 *.internal,10.0.0.0/8';
      body.appendChild(field('绕过列表（bypass）', bypass));
      body._collect = () => ({
        ...p, type: 'fixed', name: nameIn.value.trim(),
        fixed: { scheme: scheme.value, host: host.value.trim(), port: Number(port.value) || 0,
                 bypass: bypass.value.split(',').map(s => s.trim()).filter(Boolean) },
      });
    } else if (t === 'pac') {
      const url = mk('input'); url.value = p.pac?.url || ''; url.placeholder = 'https://example.com/proxy.pac';
      const data = mk('textarea'); data.value = p.pac?.data || ''; data.placeholder = 'function FindProxyForURL(url, host) { ... }';
      body.appendChild(field('PAC URL', url));
      body.appendChild(field('或 PAC 脚本内容', data));
      body._collect = () => ({ ...p, type: 'pac', name: nameIn.value.trim(), pac: { url: url.value.trim(), data: data.value } });
    } else if (t === 'auto') {
      const rulesBox = mk('div');
      const rules = (p.auto?.rules || []).map(r => ({ ...r }));
      function renderRules() {
        rulesBox.innerHTML = '';
        rules.forEach((r, idx) => {
          const row = mk('div', 'row3');
          const ctype = mk('select');
          for (const [v, tx] of [['hostWildcard', '域通配'], ['hostRegex', '域正则'], ['urlWildcard', 'URL通配'], ['urlRegex', 'URL正则'], ['keyword', '关键字']]) {
            const o = mk('option'); o.value = v; o.textContent = tx; ctype.appendChild(o);
          }
          ctype.value = r.condition.type;
          const pat = mk('input'); pat.value = r.condition.pattern; pat.placeholder = '*.example.com';
          const tgt = mk('select');
          for (const q of _proxyProfiles) { const o = mk('option'); o.value = q.id; o.textContent = q.name || q.id; tgt.appendChild(o); }
          tgt.value = r.profileId || (p.auto?.defaultProfileId || '');
          const del = mk('button'); del.className = 'rule-del'; del.textContent = '×';
          del.addEventListener('click', () => { rules.splice(idx, 1); renderRules(); });
          ctype.addEventListener('change', () => { r.condition.type = ctype.value; });
          pat.addEventListener('input', () => { r.condition.pattern = pat.value; });
          tgt.addEventListener('change', () => { r.profileId = tgt.value; });
          row.appendChild(ctype); row.appendChild(pat); row.appendChild(tgt); row.appendChild(del);
          rulesBox.appendChild(row);
        });
      }
      renderRules();
      const addRule = mk('button'); addRule.textContent = '+ 添加规则'; addRule.style.cssText = 'height:24px;font-size:11px;background:#eef1f5;color:#182033;';
      addRule.addEventListener('click', () => { rules.push({ condition: { type: 'hostWildcard', pattern: '' }, profileId: '' }); renderRules(); });
      const defSel = mk('select');
      for (const q of _proxyProfiles) { const o = mk('option'); o.value = q.id; o.textContent = q.name || q.id; defSel.appendChild(o); }
      defSel.value = p.auto?.defaultProfileId || '';
      const directOpt = mk('option'); directOpt.value = ''; directOpt.textContent = '直连'; defSel.insertBefore(directOpt, defSel.firstChild);
      defSel.value = p.auto?.defaultProfileId || '';
      body.appendChild(field('规则（命中即用对应代理）', rulesBox));
      body.appendChild(addRule);
      body.appendChild(field('默认（无命中）', defSel));
      const note = mk('div', 'note'); note.textContent = '规则引用的是其它 Profile；本面板只能选已存在的 Profile 作为目标。';
      body.appendChild(note);
      body._collect = () => ({
        ...p, type: 'auto', name: nameIn.value.trim(),
        auto: { rules: rules.filter(r => r.condition.pattern), defaultProfileId: defSel.value },
      });
    } else {
      const note = mk('div', 'note');
      note.textContent = t === 'direct' ? '直连：不使用任何代理。' : '系统代理：跟随操作系统的代理设置。';
      body.appendChild(note);
      body._collect = () => ({ ...p, type: t, name: nameIn.value.trim() });
    }
  }
  typeSel.addEventListener('change', renderBody);
  renderBody();

  // 操作
  const acts = mk('div', 'acts');
  const save = mk('button'); save.className = 'primary'; save.textContent = '保存';
  save.addEventListener('click', async () => {
    const prof = body._collect();
    if (!prof.name) { alert('请填写名称'); return; }
    if (!prof.id) prof.id = 'pp_' + Math.random().toString(36).slice(2, 10);
    const resp = await chrome.runtime.sendMessage({ cmd: 'proxy_save', profile: prof });
    if (resp && resp.ok) { closeProxyEditor(); await refreshProxySection(); }
    else alert('保存失败：' + ((resp && resp.error) || '未知错误'));
  });
  const cancel = mk('button'); cancel.textContent = '取消';
  cancel.addEventListener('click', closeProxyEditor);
  acts.appendChild(save); acts.appendChild(cancel);
  if (!isNew) {
    const del = mk('button'); del.className = 'danger'; del.textContent = '删除';
    del.addEventListener('click', async () => {
      if (!confirm(`删除代理「${p.name || p.id}」？`)) return;
      await chrome.runtime.sendMessage({ cmd: 'proxy_delete', profileId: p.id });
      closeProxyEditor(); await refreshProxySection();
    });
    acts.appendChild(del);
  }
  box.appendChild(acts);
}

function closeProxyEditor() {
  const box = document.getElementById('proxyEditor');
  box.hidden = true;
  box.innerHTML = '';
}

// ── 两步导入向导 ────────────────────────────────────────────────────────────
// 第一步（candidates=null）：选来源 —— 本地文件 / 另一个浏览器。
// 第二步（candidates 有值）：勾选要导入的数据 → proxy_import_apply 落地。
// 复用 #proxyEditor 容器（与 Profile 编辑器互斥，一次只开一个）。
async function openImportWizard(candidates) {
  const box = document.getElementById('proxyEditor');
  closeProxyEditor();
  box.hidden = false;
  box.innerHTML = '';
  const mk = (tag, cls) => { const el = document.createElement(tag); if (cls) el.className = cls; return el; };
  const head = mk('div'); head.style.cssText = 'font-weight:650;font-size:12px;margin-bottom:8px;';
  box.appendChild(head);

  if (!candidates) {
    // ── 第一步：选来源 ──
    head.textContent = '导入 · 第 1 步：选择来源';
    const fileInput = document.getElementById('proxyImportFile');

    const rowLocal = mk('div', 'proxy-row');
    rowLocal.innerHTML = '<div class="meta"><div class="u">本地文件</div><div class="d">SwitchyOmega 备份 / 导出的 JSON</div></div>';
    const btnLocal = mk('button'); btnLocal.textContent = '选择文件';
    btnLocal.addEventListener('click', () => { closeImportWizard(); fileInput.click(); });
    rowLocal.appendChild(btnLocal);

    const rowBrowser = mk('div', 'proxy-row');
    rowBrowser.innerHTML = '<div class="meta"><div class="u">另一个浏览器</div><div class="d">从你名下的其它 CDP 浏览器导入</div></div>';
    const btnBrowser = mk('button'); btnBrowser.textContent = '加载…';
    btnBrowser.addEventListener('click', async () => {
      btnBrowser.disabled = true; btnBrowser.textContent = '加载中…';
      const resp = await chrome.runtime.sendMessage({ cmd: 'proxy_fetch_sources' }).catch(() => null);
      const browsers = (resp && resp.ok && resp.data.browsers) || [];
      btnBrowser.disabled = false; btnBrowser.textContent = '加载…';
      const others = browsers.filter(b => !b.self && (b.profiles || []).length);
      if (!others.length) { alert('没有其它浏览器可导入（需先在管理端给本浏览器连上同账号的另一台 CDP 浏览器）。'); return; }
      // 进入第二步：先列浏览器，选中后勾选其 Profile。
      openImportWizardSourcePicker(others);
    });
    rowBrowser.appendChild(btnBrowser);

    box.appendChild(rowLocal);
    box.appendChild(rowBrowser);

    const cancel = mk('button'); cancel.textContent = '取消'; cancel.style.marginTop = '8px';
    cancel.addEventListener('click', closeImportWizard);
    box.appendChild(cancel);
    return;
  }

  // ── 第二步：勾选数据 ──
  head.textContent = '导入 · 第 2 步：选择要导入的数据';
  if (!candidates.length) {
    const empty = mk('div'); empty.className = 'proxy-empty';
    empty.textContent = '没有可导入的配置。';
    box.appendChild(empty);
    const cancel0 = mk('button'); cancel0.textContent = '关闭'; cancel0.style.marginTop = '8px';
    cancel0.addEventListener('click', closeImportWizard);
    box.appendChild(cancel0);
    return;
  }
  const list = mk('div'); list.style.cssText = 'display:flex;flex-direction:column;gap:6px;margin-bottom:8px;';
  const chosen = new Set(candidates.map(c => c.id));
  for (const c of candidates) {
    const label = mk('label');
    label.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:12px;';
    const cb = mk('input'); cb.type = 'checkbox'; cb.checked = true;
    cb.addEventListener('change', () => { cb.checked ? chosen.add(c.id) : chosen.delete(c.id); });
    const txt = mk('span'); txt.textContent = `${c.name || c.id}（${c.type}）`;
    label.appendChild(cb); label.appendChild(txt);
    list.appendChild(label);
  }
  box.appendChild(list);

  const selectAll = mk('button'); selectAll.textContent = '全选'; selectAll.style.marginBottom = '8px';
  selectAll.addEventListener('click', () => {
    chosen.clear(); for (const c of candidates) chosen.add(c.id);
    for (const cb of list.querySelectorAll('input')) cb.checked = true;
  });
  box.appendChild(selectAll);

  const acts = mk('div', 'acts');
  const doImport = mk('button'); doImport.className = 'primary'; doImport.textContent = '导入选中';
  doImport.addEventListener('click', async () => {
    const picked = candidates.filter(c => chosen.has(c.id));
    if (!picked.length) { alert('请至少勾选一条'); return; }
    const resp = await chrome.runtime.sendMessage({ cmd: 'proxy_import_apply', profiles: picked });
    if (resp && resp.ok) {
      const d = resp.data;
      alert(`导入完成：新增 ${d.added} 条，更新 ${d.updated} 条（共 ${d.total} 条）`);
      closeImportWizard();
      await refreshProxySection();
    } else {
      alert('导入失败：' + ((resp && resp.error) || '未知错误'));
    }
  });
  const cancel2 = mk('button'); cancel2.textContent = '取消';
  cancel2.addEventListener('click', closeImportWizard);
  acts.appendChild(doImport); acts.appendChild(cancel2);
  box.appendChild(acts);
}

// 第一步选了「另一个浏览器」→ 列其 Profile → 进第二步勾选。
function openImportWizardSourcePicker(browsers) {
  const box = document.getElementById('proxyEditor');
  closeProxyEditor();
  box.hidden = false;
  box.innerHTML = '';
  const mk = (tag, cls) => { const el = document.createElement(tag); if (cls) el.className = cls; return el; };
  const head = mk('div'); head.style.cssText = 'font-weight:650;font-size:12px;margin-bottom:8px;';
  head.textContent = '导入 · 第 1 步：选择浏览器';
  box.appendChild(head);

  const list = mk('div'); list.style.cssText = 'display:flex;flex-direction:column;gap:6px;';
  for (const b of browsers) {
    const row = mk('div', 'proxy-row');
    row.innerHTML = `<div class="meta"><div class="u">${b.name || b.client_id}</div><div class="d">${(b.profiles || []).length} 条配置</div></div>`;
    const btn = mk('button'); btn.textContent = '导入其配置';
    btn.addEventListener('click', () => openImportWizard(b.profiles || []));
    row.appendChild(btn);
    list.appendChild(row);
  }
  box.appendChild(list);
  const cancel = mk('button'); cancel.textContent = '取消'; cancel.style.marginTop = '8px';
  cancel.addEventListener('click', closeImportWizard);
  box.appendChild(cancel);
}

function closeImportWizard() {
  const box = document.getElementById('proxyEditor');
  box.hidden = true;
  box.innerHTML = '';
}

async function loadBridgeConfig() {
  const resp = await chrome.runtime.sendMessage({ cmd: 'bridge_config_get' });
  const config = resp?.data || DEFAULT_CONFIG;
  document.getElementById('bridgeUrl').value = config.bridgeUrl || DEFAULT_CONFIG.bridgeUrl;
  document.getElementById('clientToken').value = config.clientToken || '';
  document.getElementById('bridgeEnabled').checked = config.bridgeEnabled !== false;
  updatePreview();
  await refreshStatus();
}

async function refreshStatus() {
  const resp = await chrome.runtime.sendMessage({ cmd: 'bridge_status_get' });
  renderStatus(resp?.data);
}

function renderStatus(status) {
  const stateEl = document.getElementById('state');
  const dot = document.querySelector('.dot');
  const state = status?.state || 'disconnected';
  let label = STATUS_LABELS[state] || STATUS_LABELS.disconnected;
  if (status?.reprovisionRequired) {
    label = '检测到旧版 Bridge Token，已移除；请在管理端创建或轮换 CDP 客户端 Token 后重新粘贴';
  } else if (state === 'auth_error' && status?.error) {
    label += '：' + (AUTH_ERROR_LABELS[status.error.code] || status.error.message || status.error.code);
  }
  stateEl.textContent = label;
  stateEl.title = status?.error?.message || '';
  dot.dataset.state = state;
}

function readBridgeConfig() {
  const rawUrl = document.getElementById('bridgeUrl').value.trim();
  const clientToken = document.getElementById('clientToken').value.trim();
  const bridgeEnabled = document.getElementById('bridgeEnabled').checked;
  // 允许省略 ws:// 前缀，自动补齐。
  let bridgeUrl = rawUrl || DEFAULT_CONFIG.bridgeUrl;
  if (!/^wss?:\/\//i.test(bridgeUrl)) bridgeUrl = 'ws://' + bridgeUrl;
  return { bridgeUrl, clientToken, bridgeEnabled };
}

function updatePreview() {
  const state = document.getElementById('state');
  const cfg = readBridgeConfig();
  const urlEl = document.getElementById('bridgeUrl');
  const urlValid = WS_RE.test(cfg.bridgeUrl);
  if (!urlValid) {
    urlEl.style.borderColor = '#ef4444';
    document.getElementById('wsPreview').textContent = cfg.bridgeEnabled
      ? '⚠️ 地址需以 ws:// 或 wss:// 开头'
      : '已关闭连接（开关未开启）';
  } else {
    urlEl.style.borderColor = '';
    document.getElementById('wsPreview').textContent = cfg.bridgeEnabled
      ? cfg.bridgeUrl
      : '已关闭连接（开关未开启）';
  }
  if (state && !state.textContent) state.textContent = STATUS_LABELS.disconnected;
}

async function saveBridgeConfig() {
  const config = readBridgeConfig();
  if (config.bridgeEnabled && !WS_RE.test(config.bridgeUrl)) {
    document.getElementById('wsPreview').textContent = '⚠️ 地址需以 ws:// 或 wss:// 开头，不能保存';
    return;
  }
  if (config.bridgeEnabled && !config.clientToken) {
    document.getElementById('wsPreview').textContent = '请粘贴管理端生成的客户端 Token';
    return;
  }
  const resp = await chrome.runtime.sendMessage({ cmd: 'bridge_config_set', config });
  if (resp?.data?.clientToken !== undefined) {
    document.getElementById('clientToken').value = resp.data.clientToken;
  }
  updatePreview();
  await refreshStatus();
}

// ---------------------------------------------------------------------------
// 密码管理
// ---------------------------------------------------------------------------
// popup 里没有 sender.tab，因此密码帧走一条 name='pwd' 的长期 port（background.js
// 的 onConnect 把消息并进同一张派发表）。port 同时让 Service Worker 在往返期间保活。
// ⚠ 本段不得把密码写进 console / DOM 属性 / 页面标题，只允许出现在受控的
//   textContent 与剪贴板里。
const PWD_PORT_NAME = 'pwd';
const PWD_REQ_TIMEOUT_MS = 12000;
const PWD_REVEAL_MS = 15000; // 查看明文 15s 后自动收回
// 与 content.js 的 PWD_LASTGEN_KEY 同键：页面里生成随机密码时写入，
// popup 读回来展示「本次生成」（用户没复制就走开时的找回入口）。
const PWD_LASTGEN_KEY = 'cdpPwdLastGen';
const PWD_LASTGEN_TTL = 10 * 60 * 1000;

let pwdPort = null;
let pwdReqSeq = 0;
let pwdPending = new Map(); // reqId -> {resolve, timer}
let pwdEntries = [];
let pwdTab = 'site';
let pwdSiteHost = '';
let pwdRevealTimer = null;
let pwdConfirmingDelete = 0;

function ensurePwdPort() {
  if (pwdPort) return pwdPort;
  pwdPort = chrome.runtime.connect({ name: PWD_PORT_NAME });
  pwdPort.onMessage.addListener((msg) => {
    if (!msg || msg.cmd !== 'pwd_frame' || !msg.data) return;
    resolvePwdFrame(msg.data);
  });
  pwdPort.onDisconnect.addListener(() => {
    // SW 被回收或扩展重载：先让在途请求失败，下次发送时自动重连。
    failAllPwdRequests('连接已断开，密码未保存；连上后重试即可');
    pwdPort = null;
  });
  return pwdPort;
}

function resolvePwdFrame(data) {
  const reqId = String(data.reqId || '');
  const pending = pwdPending.get(reqId);
  if (!pending) return;
  pwdPending.delete(reqId);
  clearTimeout(pending.timer);
  if (data.ok === false || data.type === 'pwd_error') {
    pending.resolve({ ok: false, error: data.message || '密码管理操作失败' });
    return;
  }
  pending.resolve({ ok: true, data });
}

function failAllPwdRequests(message) {
  for (const [, pending] of pwdPending) {
    clearTimeout(pending.timer);
    pending.resolve({ ok: false, error: message });
  }
  pwdPending.clear();
}

function sendPwdRequest(cmd, payload) {
  return new Promise((resolve) => {
    let port;
    try {
      port = ensurePwdPort();
    } catch (e) {
      resolve({ ok: false, error: '扩展后台不可用，请重新打开弹窗' });
      return;
    }
    const reqId = 'popup_' + (++pwdReqSeq) + '_' + Date.now();
    const timer = setTimeout(() => {
      pwdPending.delete(reqId);
      resolve({ ok: false, error: '请求超时，请稍后重试' });
    }, PWD_REQ_TIMEOUT_MS);
    pwdPending.set(reqId, { resolve, timer });
    try {
      port.postMessage(Object.assign({ cmd, reqId }, payload || {}));
    } catch (e) {
      clearTimeout(timer);
      pwdPending.delete(reqId);
      resolve({ ok: false, error: '扩展后台不可用，请重新打开弹窗' });
    }
  });
}

async function initPasswordManager() {
  const toggle = document.getElementById('pwdGenToggle');
  const tabSite = document.getElementById('pwdTabSite');
  const tabAll = document.getElementById('pwdTabAll');
  const search = document.getElementById('pwdSearch');
  if (!toggle || !tabSite || !tabAll || !search) return;

  toggle.addEventListener('click', togglePwdGenerator);
  document.getElementById('pwdGenAgain').addEventListener('click', () => renderGeneratedPassword());
  document.getElementById('pwdGenCopy').addEventListener('click', (ev) => {
    copyText(document.getElementById('pwdGenValue').textContent, ev.currentTarget);
  });
  document.getElementById('pwdLastGenCopy').addEventListener('click', (ev) => {
    copyText(document.getElementById('pwdLastGenValue').textContent, ev.currentTarget);
  });
  tabSite.addEventListener('click', () => switchPwdTab('site'));
  tabAll.addEventListener('click', () => switchPwdTab('all'));
  search.addEventListener('input', renderPwdList);
  initPwdManual();

  pwdSiteHost = await resolveActiveHost();
  await reloadPwdList();
  await loadLastGenerated();
  // 页面里又生成了一条时，popup 若正开着也能即时刷新「本次生成」。
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'session' && changes[PWD_LASTGEN_KEY]) loadLastGenerated();
  });
}

async function resolveActiveHost() {
  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    const url = tabs && tabs[0] && tabs[0].url ? tabs[0].url : '';
    if (!/^https?:/i.test(url)) return '';
    const u = new URL(url);
    // 用 host（含非默认端口）：127.0.0.1:7890 与 :8080 不是同一个站点。
    return (u.host || u.hostname).toLowerCase();
  } catch (e) {
    return '';
  }
}

function switchPwdTab(tab) {
  pwdTab = tab === 'all' ? 'all' : 'site';
  document.getElementById('pwdTabSite').classList.toggle('on', pwdTab === 'site');
  document.getElementById('pwdTabAll').classList.toggle('on', pwdTab === 'all');
  reloadPwdList();
}

async function reloadPwdList() {
  const list = document.getElementById('pwdList');
  if (!list) return;
  closePwdReveal();
  // 本站 tab 但拿不到当前站点（chrome:// / 新标签页等）：不发请求、不回退到全部——
  // 否则会把所有密码当「本站」展示，泄露其它站点凭据。
  if (pwdTab === 'site' && !pwdSiteHost) {
    pwdEntries = [];
    renderPwdMessage('当前页面不是普通网页，无法判断站点。请在「全部」里查看已保存的密码。');
    return;
  }
  const payload = pwdTab === 'site' ? { domain: pwdSiteHost } : {};
  const resp = await sendPwdRequest('pwd_list', payload);
  if (!resp.ok) {
    pwdEntries = [];
    renderPwdMessage(resp.error || '读取失败');
    return;
  }
  // 未连服务端时 background 回本地缓存的镜像：明确告知，别让用户以为看到的是最新的。
  renderPwdHint(resp.data && resp.data.offline);
  pwdEntries = Array.isArray(resp.data.entries) ? resp.data.entries : [];
  renderPwdList();
}

function renderPwdHint(offline) {
  const hint = document.getElementById('pwdHint');
  if (!hint) return;
  hint.textContent = offline
    ? '当前未连接服务端，以下为本地缓存（离线期间的改动会在联网后自动同步）。'
    : '密码保存在 Ai Lubricant 服务端并关联你的账号。若同时出现 Chrome 自带的「保存密码」提示，可在浏览器设置里关闭它。';
  hint.style.color = offline ? '#d99a18' : '';
}

function renderPwdMessage(text) {
  const list = document.getElementById('pwdList');
  list.replaceChildren();
  const div = document.createElement('div');
  div.className = 'pwd-empty';
  div.textContent = text;
  list.appendChild(div);
}

// 本站 tab 且拿不到当前站点（新标签页 / chrome:// 页）时的兜底：退回全部并说明。
function pwdEmptyText() {
  if (!pwdEntries.length && pwdTab === 'site' && !pwdSiteHost) {
    return '当前页面不是普通网页，无法判断站点；已保存的密码可在「全部」里查看。';
  }
  if (!pwdEntries.length) return '还没有保存的密码。在登录页登录成功后会提示保存。';
  return '';
}

function renderPwdList() {
  const list = document.getElementById('pwdList');
  if (!list) return;
  closePwdReveal();
  const keyword = (document.getElementById('pwdSearch').value || '').trim().toLowerCase();
  const rows = pwdEntries.filter((entry) => {
    if (!keyword) return true;
    const domain = String(entry.domain || '').toLowerCase();
    const username = String(entry.username || '').toLowerCase();
    return domain.includes(keyword) || username.includes(keyword);
  });
  list.replaceChildren();
  if (!rows.length) {
    renderPwdMessage(keyword ? '没有匹配的记录。' : pwdEmptyText());
    return;
  }
  for (const entry of rows) list.appendChild(buildPwdRow(entry));
}

// 手动新增兜底：自动识别漏了的站点，用户在这里补录一条。走既有 pwd_save 通道。
function initPwdManual() {
  const toggle = document.getElementById('pwdManualToggle');
  const box = document.getElementById('pwdManualBox');
  if (!toggle || !box) return;
  const domainEl = document.getElementById('pwdManualDomain');
  const userEl = document.getElementById('pwdManualUser');
  const passEl = document.getElementById('pwdManualPass');
  const statusEl = document.getElementById('pwdManualStatus');
  const saveBtn = document.getElementById('pwdManualSave');
  toggle.addEventListener('click', () => {
    const show = box.hidden;
    box.hidden = !show;
    if (show) {
      domainEl.value = pwdSiteHost || '';
      statusEl.textContent = '';
      userEl.focus();
    }
  });
  saveBtn.addEventListener('click', async () => {
    const domain = (domainEl.value || '').trim() || pwdSiteHost || '';
    const username = (userEl.value || '').trim();
    const password = passEl.value || '';
    if (!domain) { statusEl.textContent = '请填站点域名'; return; }
    if (!username) { statusEl.textContent = '请填用户名'; return; }
    if (!password) { statusEl.textContent = '请填密码'; return; }
    saveBtn.disabled = true;
    statusEl.textContent = '保存中…';
    const resp = await sendPwdRequest('pwd_save', {
      domain, username, password, url: domain,
    });
    saveBtn.disabled = false;
    if (resp.ok) {
      statusEl.textContent = resp.data && resp.data.offline ? '已存本地，联网后同步' : '已保存';
      userEl.value = ''; passEl.value = '';
      await reloadPwdList();
    } else {
      statusEl.textContent = '失败：' + (resp.error || '未知错误');
    }
  });
}

function buildPwdRow(entry) {
  const row = document.createElement('div');
  row.className = 'pwd-row';

  const meta = document.createElement('div');
  meta.className = 'meta';
  const user = document.createElement('div');
  user.className = 'u';
  user.textContent = entry.username || '(无账号)';
  user.title = user.textContent;
  const dom = document.createElement('div');
  dom.className = 'd';
  dom.textContent = entry.domain || '';
  dom.title = dom.textContent;
  meta.append(user, dom);

  const acts = document.createElement('div');
  acts.className = 'acts';
  const revealBtn = document.createElement('button');
  revealBtn.textContent = '查看';
  revealBtn.addEventListener('click', () => revealPwdEntry(row, entry, revealBtn));
  const copyBtn = document.createElement('button');
  copyBtn.textContent = '复制';
  copyBtn.addEventListener('click', () => copyPwdEntry(entry, copyBtn));
  const delBtn = document.createElement('button');
  delBtn.className = 'danger';
  delBtn.textContent = '删除';
  delBtn.addEventListener('click', () => confirmDeletePwdEntry(row, entry, delBtn));
  acts.append(revealBtn, copyBtn, delBtn);

  row.append(meta, acts);
  return row;
}

async function fetchPwdPlaintext(entry) {
  const resp = await sendPwdRequest('pwd_get', { id: entry.id });
  if (!resp.ok) return { ok: false, error: resp.error || '读取失败' };
  const password = resp.data.entry ? resp.data.entry.password : '';
  if (!password) return { ok: false, error: '记录无法解密（服务端密钥已变更）' };
  return { ok: true, password };
}

function closePwdReveal() {
  if (pwdRevealTimer !== null) {
    clearTimeout(pwdRevealTimer);
    pwdRevealTimer = null;
  }
  document.querySelectorAll('.pwd-reveal').forEach((el) => el.remove());
}

async function revealPwdEntry(row, entry, button) {
  const existing = row.querySelector('.pwd-reveal');
  if (existing) {
    closePwdReveal();
    button.textContent = '查看';
    return;
  }
  closePwdReveal();
  const result = await fetchPwdPlaintext(entry);
  const box = document.createElement('div');
  box.className = 'pwd-reveal';
  box.textContent = result.ok ? result.password : result.error;
  if (!result.ok) box.style.color = '#b3261e';
  row.appendChild(box);
  if (!result.ok) return;
  button.textContent = '隐藏';
  pwdRevealTimer = setTimeout(() => {
    pwdRevealTimer = null;
    const node = row.querySelector('.pwd-reveal');
    if (node) node.remove();
    if (button.isConnected) button.textContent = '查看';
  }, PWD_REVEAL_MS);
}

async function copyPwdEntry(entry, button) {
  const result = await fetchPwdPlaintext(entry);
  if (!result.ok) {
    button.textContent = '失败';
    setTimeout(() => { if (button.isConnected) button.textContent = '复制'; }, 1500);
    return;
  }
  copyText(result.password, button);
}

function copyText(text, button) {
  const value = String(text || '');
  const done = () => {
    if (!button || !button.isConnected) return;
    const original = button.textContent;
    button.textContent = '已复制';
    setTimeout(() => { if (button.isConnected) button.textContent = original; }, 1200);
  };
  // HTTP 页面 / 窗口失焦时 navigator.clipboard 会被拒；失败回退 execCommand。
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(value).then(done).catch(() => {
      if (fallbackCopy(value)) done();
    });
    return;
  }
  if (fallbackCopy(value)) done();
}

function fallbackCopy(value) {
  const area = document.createElement('textarea');
  area.value = value;
  area.setAttribute('readonly', 'readonly');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  let ok = false;
  try {
    area.select();
    ok = document.execCommand('copy');
  } catch (e) {
    ok = false;
  }
  area.remove();
  return ok;
}

// 删除走两步确认（按钮原地变成「确认删除」），避免误删。
function confirmDeletePwdEntry(row, entry, button) {
  if (pwdConfirmingDelete !== entry.id) {
    pwdConfirmingDelete = entry.id;
    button.textContent = '确认删除';
    setTimeout(() => {
      if (pwdConfirmingDelete === entry.id && button.isConnected) {
        pwdConfirmingDelete = 0;
        button.textContent = '删除';
      }
    }, 4000);
    return;
  }
  pwdConfirmingDelete = 0;
  button.disabled = true;
  button.textContent = '删除中…';
  sendPwdRequest('pwd_delete', { id: entry.id }).then((resp) => {
    if (!resp.ok) {
      button.disabled = false;
      button.textContent = '删除';
      renderPwdMessage(resp.error || '删除失败');
      return;
    }
    pwdEntries = pwdEntries.filter((item) => item.id !== entry.id);
    renderPwdList();
  });
}

function togglePwdGenerator() {
  const box = document.getElementById('pwdGenBox');
  if (box.hidden) {
    renderGeneratedPassword();
    box.hidden = false;
  } else {
    box.hidden = true;
  }
}

function renderGeneratedPassword() {
  const el = document.getElementById('pwdGenValue');
  el.textContent = tmwdGeneratePassword(16);
}

function loadLastGenerated() {
  chrome.storage.session.get([PWD_LASTGEN_KEY], (r) => {
    const map = r && r[PWD_LASTGEN_KEY];
    if (!map || typeof map !== 'object') {
      showLastGenBox(false);
      return;
    }
    const now = Date.now();
    const fresh = (entry) => entry && entry.password && now - (entry.at || 0) <= PWD_LASTGEN_TTL;
    // 优先「当前页面」生成的（用户问的就是这个），没有才退回最近一次。
    let entry = fresh(map[pwdSiteHost]) ? map[pwdSiteHost] : null;
    if (!entry) {
      for (const host of Object.keys(map)) {
        if (!fresh(map[host])) continue;
        if (!entry || (map[host].at || 0) > (entry.at || 0)) entry = map[host];
      }
    }
    if (!entry) {
      showLastGenBox(false);
      return;
    }
    const codeEl = document.getElementById('pwdLastGenValue');
    if (codeEl) {
      codeEl.textContent = entry.password;
      showLastGenBox(true);
    }
  });
}

function showLastGenBox(show) {
  const box = document.getElementById('pwdLastGen');
  if (box) box.hidden = !show;
}

// ---------------------------------------------------------------------------
// 身份验证器（TOTP / Google Authenticator）
// ---------------------------------------------------------------------------
// popup 经 name='totp' 长期 port 与 background 的 WS 通道通信。密钥永不下发到
// 本机——列表/详情帧只回 meta + 当前码 + **绝对失效时刻 expiresAt**，所以本机算不出
// 码，连不上服务端时读操作直接报错（background 8s 排队兜底）。
//
// **数据入口只有一个：服务端推送（totp_push）**。订阅（totp_subscribe）时服务端
// 立刻把它**内存里**的列表（meta + code + expiresAt）推回来 → popup 收到即画（首屏
// 不必再发一次 totp_list 等回帧，这也是「首次进入要等一段时间」的修法）；此后每次
// 翻转，服务端先刷新内存、再推给该 owner 的全部订阅者。订阅回帧与推送是**同一份**
// 内存 → 同一 expiresAt，客户端不再自己掐表回头拉。
//
// **只订阅一次（全量），「本站/全部」是纯客户端视图**：订阅恒带 domain=""（拿全量），
// 切页签只按当前 URL 主机名在本地过滤，**不再重新订阅**。旧做法切页签就换 domain 重订
// ——既与服务端推送竞争（旧订阅的回帧/推送可能把新页签的列表冲掉 → 数据「没了」），
// 又让服务端刷新泵只覆盖该 domain 的子集（于是「只有第一个在刷新」）。全量订阅后所有
// 条目一起刷新，页签只影响显示。
// ⚠ 当前码是敏感瞬时值，不写 console；otpauth 输入框内容由用户掌控。
const TOTP_PORT_NAME = 'totp';
const TOTP_REQ_TIMEOUT_MS = 12000;

let totpPort = null;
let totpReqSeq = 0;
let totpPending = new Map(); // reqId -> {resolve, timer}
let totpEntries = [];        // **全量** meta + 当前码（服务端推送而来，与页签无关）
let totpTab = 'site';
let totpSiteHost = '';
let totpTicker = null;       // 每秒倒计时；只递减环号，新码由服务端 totp_push 推来
let totpConfirmingDelete = 0;
let totpEditingId = 0;       // 非 0 = 表单处于「编辑」态（保存走 totp_update，不改密钥）

function ensureTotpPort() {
  if (totpPort) return totpPort;
  totpPort = chrome.runtime.connect({ name: TOTP_PORT_NAME });
  totpPort.onMessage.addListener((msg) => {
    if (!msg || msg.cmd !== 'totp_frame' || !msg.data) return;
    // 服务端翻转推送（无 reqId）：原地更新码值/环号，不走 reqId 派发。
    if (msg.data.type === 'totp_push') { applyTotpPush(msg.data.entries); return; }
    resolveTotpFrame(msg.data);
  });
  totpPort.onDisconnect.addListener(() => {
    failAllTotpRequests('连接已断开，操作未完成；连上后重试即可');
    totpPort = null;
  });
  return totpPort;
}

function resolveTotpFrame(data) {
  const reqId = String(data.reqId || '');
  const pending = totpPending.get(reqId);
  if (!pending) return;
  totpPending.delete(reqId);
  clearTimeout(pending.timer);
  if (data.ok === false || data.type === 'totp_error') {
    pending.resolve({ ok: false, error: data.message || '身份验证器操作失败' });
    return;
  }
  pending.resolve({ ok: true, data });
}

function failAllTotpRequests(message) {
  for (const [, pending] of totpPending) {
    clearTimeout(pending.timer);
    pending.resolve({ ok: false, error: message });
  }
  totpPending.clear();
}

function sendTotpRequest(cmd, payload) {
  return new Promise((resolve) => {
    let port;
    try { port = ensureTotpPort(); } catch (e) {
      resolve({ ok: false, error: '扩展后台不可用，请重新打开弹窗' });
      return;
    }
    const reqId = 'totp_' + (++totpReqSeq) + '_' + Date.now();
    const timer = setTimeout(() => {
      totpPending.delete(reqId);
      resolve({ ok: false, error: '请求超时，请稍后重试' });
    }, TOTP_REQ_TIMEOUT_MS);
    totpPending.set(reqId, { resolve, timer });
    try {
      port.postMessage(Object.assign({ cmd, reqId }, payload || {}));
    } catch (e) {
      clearTimeout(timer);
      totpPending.delete(reqId);
      resolve({ ok: false, error: '扩展后台不可用，请重新打开弹窗' });
    }
  });
}

async function initAuthenticator() {
  const addToggle = document.getElementById('totpAddToggle');
  const scanQr = document.getElementById('totpScanQr');
  const saveBtn = document.getElementById('totpSaveBtn');
  const cancelBtn = document.getElementById('totpCancelBtn');
  const search = document.getElementById('totpSearch');
  const tabSite = document.getElementById('totpTabSite');
  const tabAll = document.getElementById('totpTabAll');
  if (!addToggle || !saveBtn || !search) return;

  addToggle.addEventListener('click', async () => {
    const box = document.getElementById('totpAddBox');
    const willShow = box && box.hidden; // 即将展开
    if (box) box.hidden = !box.hidden;
    if (willShow) {
      // 「添加」永远是新增态：清掉可能残留的编辑态与表单。
      if (totpEditingId) clearTotpForm();
      totpSiteHost = await resolveActiveHost();
      // 「关联网站」默认填本站：扫码后手动调也省得用户敲。用户可改。
      const domainsInput = document.getElementById('totpDomainsInput');
      if (!domainsInput.value && totpSiteHost) domainsInput.value = totpSiteHost;
    }
  });
  scanQr && scanQr.addEventListener('click', scanQrOnPage);
  const pickQr = document.getElementById('totpPickQr');
  const qrFile = document.getElementById('totpQrFile');
  pickQr && pickQr.addEventListener('click', () => qrFile && qrFile.click());
  qrFile && qrFile.addEventListener('change', () => {
    const file = qrFile.files && qrFile.files[0];
    void readTotpQrFromImageFile(file);
    qrFile.value = ''; // 允许重复选同一张图
  });
  armTotpPasteDecode();
  saveBtn.addEventListener('click', saveTotpEntry);
  cancelBtn.addEventListener('click', () => {
    document.getElementById('totpAddBox').hidden = true;
    clearTotpForm();
  });
  tabSite && tabSite.addEventListener('click', () => switchTotpTab('site'));
  tabAll && tabAll.addEventListener('click', () => switchTotpTab('all'));
  search.addEventListener('input', renderTotpList);

  // 导入：选 .txt 直接批量入库（一步）。导出：二次鉴权后下载。
  const importBtn = document.getElementById('totpImportBtn');
  const importFile = document.getElementById('totpImportFile');
  const importPick = document.getElementById('totpImportPick');
  const exportBtn = document.getElementById('totpExportBtn');
  const exportGo = document.getElementById('totpExportGo');
  const exportCancel = document.getElementById('totpExportCancel');
  importBtn && importBtn.addEventListener('click', () => {
    hideTotpIoPanels('import');
    const box = document.getElementById('totpImportBox');
    if (box) box.hidden = !box.hidden;
  });
  importPick && importPick.addEventListener('click', () => importFile && importFile.click());
  importFile && importFile.addEventListener('change', () => {
    const file = importFile.files && importFile.files[0];
    void doTotpImportFile(file);
    importFile.value = '';
  });
  exportBtn && exportBtn.addEventListener('click', () => {
    hideTotpIoPanels('export');
    const box = document.getElementById('totpExportBox');
    if (box) { box.hidden = !box.hidden; if (!box.hidden) { const p = document.getElementById('totpExportPwd'); p && p.focus(); } }
  });
  exportGo && exportGo.addEventListener('click', doTotpExport);
  armTotpImportKeys();
  exportCancel && exportCancel.addEventListener('click', () => {
    const box = document.getElementById('totpExportBox');
    if (box) box.hidden = true;
    const p = document.getElementById('totpExportPwd'); if (p) p.value = '';
    setTotpExportStatus('', '');
  });

  totpSiteHost = await resolveActiveHost();
  await reloadTotpList();
  // 每秒倒计时纯**显示**（按 expiresAt 画环），不发请求；到点的新码由服务端推来。
  totpTicker = setInterval(tickTotpCountdown, 1000);
  window.addEventListener('unload', () => {
    if (totpTicker) clearInterval(totpTicker);
    // popup 关闭即退订：停掉服务端到点推送（否则它继续对死连接 fire-and-forget 推）。
    void sendTotpRequest('totp_unsubscribe', {}).catch(() => {});
  }, { once: true });
}

function switchTotpTab(tab) {
  totpTab = tab === 'all' ? 'all' : 'site';
  document.getElementById('totpTabSite').classList.toggle('on', totpTab === 'site');
  document.getElementById('totpTabAll').classList.toggle('on', totpTab === 'all');
  // **纯视图切换**：只重画本地已拿到的全量列表，不重新订阅（订阅恒为全量）。
  renderTotpList();
}

// 「本站」视图的域名匹配：与 server/totp_store.py 的 host_matches_domains 逐条对齐
// （服务端才是权威，这里只为本地过滤显示）。⚠ 改这里必须同步改服务端。
const _TOTP_HOST_CLEAN_RE = /^(?:[a-z][a-z0-9+.-]*:\/\/)?([^/?#]*)/;

function _totpCleanHost(value) {
  const text = String(value || '').trim().toLowerCase();
  const m = _TOTP_HOST_CLEAN_RE.exec(text);
  let host = (m ? m[1] : '').trim();
  host = host.split(':')[0].split('/')[0].split('@').pop();
  return host.replace(/^\.+/, '').trim();
}

function totpHostMatches(host, domains) {
  const clean = _totpCleanHost(host);
  if (!clean) return false;
  const hostParts = clean.split('.');
  for (const raw of (domains || [])) {
    const d = _totpCleanHost(String(raw));
    if (!d) continue;
    if (d === clean || clean.endsWith('.' + d)) return true;
    const hasSpace = String(raw).trim().indexOf(' ') !== -1;
    const looksLikeDomain = d.indexOf('.') !== -1 && d.indexOf(' ') === -1;
    if (looksLikeDomain && clean.endsWith(d)
        && clean.slice(0, clean.length - d.length).endsWith('.')) return true;
    const tokens = hasSpace
      ? d.split(/\s+/).filter(Boolean)
      : (d.indexOf('.') === -1 ? [d] : []);
    for (const tok of tokens) { if (tok && hostParts.indexOf(tok) !== -1) return true; }
  }
  return false;
}

// 当前页签该显示的条目：全部=全量；本站=按 URL 主机名本地过滤（不重订）。
function visibleTotpEntries() {
  if (totpTab !== 'site') return totpEntries;
  if (!totpSiteHost) return [];
  return totpEntries.filter((e) => totpHostMatches(totpSiteHost, e.domains || []));
}

// 订阅恒为**全量**（domain 空）：服务端按 owner 刷新一次、推给所有订阅者；「本站」
// 只是本地视图。这样切页签不重订（不会与推送竞争丢数据），且所有条目一起翻转。
async function subscribeTotpFull() {
  if (totpEntries.length === 0) renderTotpMessage('正在获取验证码…');
  const resp = await sendTotpRequest('totp_subscribe', {});
  if (!resp.ok) {
    totpEntries = [];
    renderTotpMessage(resp.error || '读取失败');
  }
}

// 增删改后重新拉全量：服务端 invalidate 会刷新内存并推来最新快照，这里只需再订阅一次
// （拿内存最新；服务端内存热时不重复读库）。保持当前页签视图不变。
async function reloadTotpList() {
  await subscribeTotpFull();
}

function sameTotpIdSet(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  const ids = new Set(a.map((e) => String(e.id)));
  for (const e of b) { if (!ids.has(String(e.id))) return false; }
  return true;
}

function renderTotpMessage(text) {
  const list = document.getElementById('totpList');
  if (!list) return;
  list.replaceChildren();
  const div = document.createElement('div');
  div.className = 'totp-empty';
  div.textContent = text;
  list.appendChild(div);
}

function totpEmptyText() {
  if (totpTab === 'site' && !totpSiteHost) {
    return '当前页面不是普通网页，无法判断站点；验证器可在「全部」里查看。';
  }
  if (totpTab === 'site' && totpEntries.length && !visibleTotpEntries().length) {
    return '本站没有关联的验证器。切到「全部」可查看全部，或在「添加」里把本站加入关联网站。';
  }
  if (!totpEntries.length) return '还没有验证器。点「添加验证器」扫码或粘贴 otpauth:// 链接新增。';
  return '';
}

function renderTotpList() {
  const list = document.getElementById('totpList');
  if (!list) return;
  const keyword = (document.getElementById('totpSearch').value || '').trim().toLowerCase();
  const rows = visibleTotpEntries().filter((entry) => {
    if (!keyword) return true;
    const issuer = String(entry.issuer || '').toLowerCase();
    const account = String(entry.account || '').toLowerCase();
    const note = String(entry.note || '').toLowerCase();
    const domains = (entry.domains || []).join(' ').toLowerCase();
    return issuer.includes(keyword) || account.includes(keyword) || note.includes(keyword) || domains.includes(keyword);
  });
  list.replaceChildren();
  if (!rows.length) {
    renderTotpMessage(keyword ? '没有匹配的记录。' : totpEmptyText());
    return;
  }
  for (const entry of rows) list.appendChild(buildTotpRow(entry));
}

function formatTotpCode(code, digits) {
  const c = String(code || '');
  // 6 位 → 「123 456」；8 位 → 「1234 5678」。仅展示友好，不改变值。
  const d = digits || c.length;
  if (d === 6 && c.length === 6) return c.slice(0, 3) + ' ' + c.slice(3);
  if (d === 8 && c.length === 8) return c.slice(0, 4) + ' ' + c.slice(4);
  return c;
}

function buildTotpMeta(entry) {
  const meta = document.createElement('div');
  meta.className = 'meta';
  const u = document.createElement('div'); u.className = 'u';
  u.textContent = entry.issuer || entry.label || entry.account || '(未命名)';
  meta.appendChild(u);
  if (entry.account) {
    const a = document.createElement('div'); a.className = 'd';
    a.textContent = entry.account;
    meta.appendChild(a);
  }
  const d = document.createElement('div'); d.className = 'd';
  d.textContent = (entry.domains || []).join(', ') || '(未关联站点)';
  meta.appendChild(d);
  if (entry.note) {
    const n = document.createElement('div'); n.className = 'n';
    n.textContent = entry.note;
    meta.appendChild(n);
  }
  return meta;
}

function buildTotpRow(entry) {
  const row = document.createElement('div');
  row.className = 'totp-row';
  row.dataset.id = entry.id;

  row.appendChild(buildTotpMeta(entry));

  const code = document.createElement('div'); code.className = 'code';
  code.textContent = entry.code ? formatTotpCode(entry.code, entry.digits) : '······';
  row.appendChild(code);

  const ring = document.createElement('div'); ring.className = 'ring';
  const period = Number(entry.period) > 0 ? Number(entry.period) : 30;
  const remaining = entry.expiresAtMs
    ? Math.max(0, Math.min(period, Math.ceil((entry.expiresAtMs - Date.now()) / 1000)))
    : 0;
  ring.textContent = remaining;
  ring.classList.add(remaining <= 5 ? 'low' : remaining <= 10 ? 'warn' : '');
  row.appendChild(ring);

  const acts = document.createElement('div'); acts.className = 'acts';
  const copyBtn = document.createElement('button');
  copyBtn.textContent = '复制';
  copyBtn.addEventListener('click', (ev) => {
    if (entry.code) copyText(formatTotpCode(entry.code, entry.digits).replace(/\s/g, ''), ev.currentTarget);
  });
  acts.appendChild(copyBtn);
  const editBtn = document.createElement('button');
  editBtn.textContent = '编辑';
  editBtn.addEventListener('click', () => openTotpEdit(entry));
  acts.appendChild(editBtn);
  const delBtn = document.createElement('button');
  delBtn.textContent = '删除';
  delBtn.className = 'danger';
  delBtn.addEventListener('click', () => deleteTotpEntry(entry, delBtn));
  acts.appendChild(delBtn);
  row.appendChild(acts);

  return row;
}

// 打开「编辑」：复用「添加」表单，回填展示字段；密钥不可见也不可改（换密钥走重新添加）。
function openTotpEdit(entry) {
  totpEditingId = entry.id;
  hideTotpIoPanels('');
  const box = document.getElementById('totpAddBox');
  box.hidden = false;
  const secretInput = document.getElementById('totpSecretInput');
  secretInput.value = '';
  secretInput.disabled = true;                 // 编辑态不改密钥
  secretInput.placeholder = '编辑时不改密钥（换密钥请重新「添加」扫码）';
  document.getElementById('totpIssuerInput').value = entry.issuer || '';
  document.getElementById('totpAccountInput').value = entry.account || '';
  document.getElementById('totpDomainsInput').value = (entry.domains || []).join(', ');
  document.getElementById('totpNoteInput').value = entry.note || '';
  document.getElementById('totpSaveBtn').textContent = '保存修改';
  setTotpQrStatus('', '');
  document.getElementById('totpIssuerInput').focus();
}

// 环号按**绝对失效时刻**算（服务端下发 expiresAt，epoch 秒），不做「每秒 -1」累加：
// 后者会因 setInterval 被节流/休眠而越走越偏。服务端下发的就是绝对时刻，本地只把
// 环上的数字平滑画下来；到 0 停在那等下一次推送（服务端在翻转点 +0.15s 必然推来新码）。
function setTotpEntryExpiry(entry) {
  // 服务端下发 expiresAt（epoch 秒）；转成毫秒绝对时刻。缺省时按整周期垫一次。
  const expSec = Number(entry.expiresAt);
  const period = Number(entry.period) > 0 ? Number(entry.period) : 30;
  entry.expiresAtMs = Number.isFinite(expSec) && expSec > 0
    ? expSec * 1000
    : Date.now() + period * 1000;
}

function tickTotpCountdown() {
  // 列表常驻：只画环，不发请求、不重建结构。到点的新码由服务端 totp_push 推来。
  const list = document.getElementById('totpList');
  if (!list) return;
  const now = Date.now();
  for (const entry of totpEntries) {
    if (!entry.expiresAtMs) continue;
    const period = Number(entry.period) > 0 ? Number(entry.period) : 30;
    const remaining = Math.max(0, Math.min(period, Math.ceil((entry.expiresAtMs - now) / 1000)));
    const row = list.querySelector(`.totp-row[data-id="${entry.id}"]`);
    if (row) patchTotpRowRing(row, remaining);
  }
}

// 只 patch 一行环号（递减显示），不动结构。
function patchTotpRowRing(row, remaining) {
  const ring = row.querySelector('.ring');
  if (!ring) return;
  ring.textContent = remaining;
  ring.classList.remove('low', 'warn');
  ring.classList.add(remaining <= 5 ? 'low' : remaining <= 10 ? 'warn' : '');
}

// 只 patch 一行码值 + 环号（新码到手后），不动结构、不碰按钮状态。
function patchTotpRowCode(row, entry) {
  const code = row.querySelector('.code');
  if (code) code.textContent = entry.code ? formatTotpCode(entry.code, entry.digits) : '······';
  const period = Number(entry.period) > 0 ? Number(entry.period) : 30;
  const remaining = entry.expiresAtMs
    ? Math.max(0, Math.min(period, Math.ceil((entry.expiresAtMs - Date.now()) / 1000)))
    : 0;
  patchTotpRowRing(row, remaining);
}

// 服务端推送（totp_push.entries）：**这是唯一的数据入口**——订阅时服务端立即推一份
// 内存快照（首帧，恒为**全量**），此后每次翻转推新码（同一批 id）。故：id 集合变化
// （首帧 / 增删）→ 重建结构；不变 → 原地 patch 码值/环号（不闪）。「本站/全部」是
// 本地视图（renderTotpList 里过滤），推送永远推全量。
function applyTotpPush(entries) {
  if (!Array.isArray(entries)) return;
  const next = entries;
  for (const e of next) setTotpEntryExpiry(e); // 校准绝对失效时刻
  if (!next.length || !sameTotpIdSet(totpEntries, next)) {
    // 空表也走重建：否则 sameTotpIdSet([], []) 恒真，占位文案会一直挂着。
    totpEntries = next;
    renderTotpList();
    return;
  }
  // 同一批条目：原地 patch，不重建结构（避免闪烁）。
  const list = document.getElementById('totpList');
  for (const item of next) {
    const entry = totpEntries.find((x) => String(x.id) === String(item.id));
    if (!entry) continue;
    Object.assign(entry, item);   // 码 / expiresAt / meta 一并更新
    if (list) {
      const row = list.querySelector(`.totp-row[data-id="${entry.id}"]`);
      if (row) patchTotpRowCode(row, entry);
    }
  }
}

async function deleteTotpEntry(entry, btn) {
  // 两步确认：第一次按钮变红「确认删除？」，第二次才真删。
  if (totpConfirmingDelete !== entry.id) {
    totpConfirmingDelete = entry.id;
    const original = btn.textContent;
    btn.textContent = '确认删除？';
    setTimeout(() => { if (totpConfirmingDelete === entry.id) { totpConfirmingDelete = 0; if (btn.isConnected) btn.textContent = original; } }, 2500);
    return;
  }
  totpConfirmingDelete = 0;
  btn.textContent = '删除';
  const resp = await sendTotpRequest('totp_delete', { id: entry.id });
  if (!resp.ok) { renderTotpMessage(resp.error || '删除失败'); return; }
  totpEntries = totpEntries.filter((e) => e.id !== entry.id);
  renderTotpList();
}

function clearTotpForm() {
  totpEditingId = 0;
  const secretInput = document.getElementById('totpSecretInput');
  secretInput.value = '';
  secretInput.disabled = false;
  secretInput.placeholder = '粘贴 otpauth:// 链接 / base32 密钥';
  document.getElementById('totpSaveBtn').textContent = '保存';
  document.getElementById('totpIssuerInput').value = '';
  document.getElementById('totpAccountInput').value = '';
  document.getElementById('totpDomainsInput').value = '';
  document.getElementById('totpNoteInput').value = '';
  const status = document.getElementById('totpQrStatus');
  if (status) { status.hidden = true; status.textContent = ''; status.className = 'totp-qr-status'; }
}

function setTotpQrStatus(kind, text) {
  const status = document.getElementById('totpQrStatus');
  if (!status) return;
  status.hidden = !text;
  status.textContent = text || '';
  status.className = 'totp-qr-status ' + (kind === 'ok' ? 'ok' : kind === 'err' ? 'err' : '');
}

// 把一条 otpauth 解析结果回填到表单（扫码/选图/粘贴共用一条路径）。
function fillTotpFormFromQr(parsed, rawText) {
  const secretInput = document.getElementById('totpSecretInput');
  const issuerInput = document.getElementById('totpIssuerInput');
  const accountInput = document.getElementById('totpAccountInput');
  const domainsInput = document.getElementById('totpDomainsInput');
  if (parsed && parsed.secret) {
    // 直接回填规范化后的字段，用户可微调站点/账号/关联域名再保存。
    secretInput.value = rawText && String(rawText).trim().toLowerCase().startsWith('otpauth://')
      ? String(rawText).trim() : parsed.secret;
    if (parsed.issuer) issuerInput.value = parsed.issuer;
    if (parsed.account) accountInput.value = parsed.account;
    if (!domainsInput.value) {
      domainsInput.value = totpSiteHost || (parsed.issuer ? parsed.issuer.toLowerCase() : '');
    }
    setTotpQrStatus('ok', '已识别：' + (parsed.issuer || parsed.account || '验证器') + '，确认关联网站与备注后保存。');
    return true;
  }
  if (rawText) {
    // 非 otpauth：可能是裸 base32 密钥，或站点显示的其它文本。能当密钥就填密钥。
    const b32 = String(rawText).replace(/\s+/g, '').toUpperCase();
    if (/^[A-Z2-7]{16,}$/.test(b32)) {
      secretInput.value = b32;
      setTotpQrStatus('ok', '识别到 base32 密钥，请补站点 / 账号 / 关联网站。');
      return true;
    }
    secretInput.value = String(rawText).trim();
    setTotpQrStatus('err', '二维码内容不是验证器链接（otpauth）。请把文本粘到密钥框，或手动填写。');
    return false;
  }
  setTotpQrStatus('err', '没识别到二维码。请把二维码滚到页面可见区，或改用「粘贴 / 选图识别」。');
  return false;
}

// 「框选页面二维码」：截当前可见区存 session → 打开独立框选页（snip.html）。
// 截图后立刻开新窗口（活动页仍是原页面，snip 页是扩展页不会截到自己——截图在开页
// 之前完成）。snip 页全屏显示截图、拖框、原分辨率裁块识别、就地保存。
async function scanQrOnPage() {
  setTotpQrStatus('', '');
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs && tabs[0];
  if (!tab || !/^https?:/i.test(tab.url || '')) {
    setTotpQrStatus('err', '当前页面不是普通网页，无法截图识别。');
    return;
  }
  let dataUrl;
  try {
    dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
  } catch (e) {
    setTotpQrStatus('err', '截图失败：' + (e && e.message ? e.message : '请重试'));
    return;
  }
  try {
    await chrome.storage.session.set({ totpSnipShot: dataUrl, totpSnipOrigin: totpSiteHost });
    await chrome.windows.create({ url: chrome.runtime.getURL('snip.html'), type: 'popup', width: 980, height: 760 });
    window.close(); // popup 完成使命：框选在 snip 页完成，保存后自动关
  } catch (e) {
    setTotpQrStatus('err', '打开框选窗口失败：' + (e && e.message ? e.message : '请重试'));
  }
}

// 「输入二维码」：粘贴剪贴板里的二维码图片，或选一张本地图片文件 → jsQR 解码。
// 与截屏识别共用解码/回填管线，覆盖「截图在别处、页面上没有二维码」的场景。
async function readTotpQrFromImageFile(file) {
  setTotpQrStatus('', '');
  if (!file || !/^image\//i.test(file.type || '')) {
    setTotpQrStatus('err', '请选择图片文件，或直接 Ctrl+V 粘贴剪贴板里的二维码图片。');
    return;
  }
  const reader = new FileReader();
  let dataUrl;
  try {
    dataUrl = await new Promise((res, rej) => { reader.onload = () => res(reader.result); reader.onerror = rej; reader.readAsDataURL(file); });
  } catch (e) {
    setTotpQrStatus('err', '图片读取失败：' + (e && e.message ? e.message : '请重试'));
    return;
  }
  const text = await decodeQrFromDataUrl(dataUrl);
  fillTotpFormFromQr(text ? parseOtpauth(text) : null, text);
}

// 监听密钥输入框的 paste：粘的是图片就解码，是文本就按原样进输入框（otpauth/base32）。
function armTotpPasteDecode() {
  const input = document.getElementById('totpSecretInput');
  if (!input) return;
  input.addEventListener('paste', (ev) => {
    const items = ev.clipboardData && ev.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (item.kind === 'file' && /^image\//i.test(item.type || '')) {
        const file = item.getAsFile();
        if (file) {
          ev.preventDefault(); // 图片走解码，不让它变成乱码文本塞进输入框
          void readTotpQrFromImageFile(file);
          return;
        }
      }
    }
  });
}

async function decodeQrFromDataUrl(dataUrl) {
  const jsqr = typeof window !== 'undefined' && window.jsQR;
  if (!jsqr) return null;
  const img = new Image();
  await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = dataUrl; });
  // 大图先缩到 ≤1000px 提速；jsQR 对中尺寸二维码最稳。
  const maxSide = 1000;
  let w = img.naturalWidth || img.width;
  let h = img.naturalHeight || img.height;
  const scale = Math.min(1, maxSide / Math.max(w, h));
  w = Math.max(1, Math.round(w * scale));
  h = Math.max(1, Math.round(h * scale));
  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, w, h);
  const imgData = ctx.getImageData(0, 0, w, h);
  const found = jsqr(imgData.data, w, h);
  if (found && found.data) return found.data;
  // 原尺寸失败 → 2 倍放大重试（小二维码放大后识别率显著提升）。
  const cw = Math.min(2000, w * 2), ch = Math.min(2000, h * 2);
  canvas.width = cw; canvas.height = ch;
  ctx.drawImage(img, 0, 0, cw, ch);
  const big = ctx.getImageData(0, 0, cw, ch);
  const found2 = jsqr(big.data, cw, ch);
  return found2 && found2.data ? found2.data : null;
}

function parseOtpauth(text) {
  const uri = String(text || '').trim();
  if (!/^otpauth:\/\/totp\//i.test(uri)) return null;
  let u;
  try { u = new URL(uri); } catch (_) { return null; }
  const label = decodeURIComponent(u.pathname.replace(/^\/+/, ''));
  const params = u.searchParams;
  const issuer = params.get('issuer') || '';
  let account = '';
  if (label.includes(':')) {
    const parts = label.split(':');
    account = (parts.slice(1).join(':')).trim();
  } else {
    account = label.trim();
  }
  return {
    issuer: issuer || (label.split(':')[0] || '').trim(),
    account,
    secret: params.get('secret') || '',
    digits: Number(params.get('digits') || 6),
    period: Number(params.get('period') || 30),
    algorithm: (params.get('algorithm') || 'SHA1').toUpperCase(),
  };
}

async function saveTotpEntry() {
  // 编辑态：只改展示字段（站点/账号/关联网站/备注），走 totp_update，**不动密钥**。
  if (totpEditingId) { await saveTotpEdit(); return; }
  const secret = document.getElementById('totpSecretInput').value.trim();
  const issuer = document.getElementById('totpIssuerInput').value.trim();
  const account = document.getElementById('totpAccountInput').value.trim();
  const domainsRaw = document.getElementById('totpDomainsInput').value.trim();
  const note = document.getElementById('totpNoteInput').value.trim();
  setTotpQrStatus('', '');
  if (!secret) { setTotpQrStatus('err', '请粘贴 otpauth:// 链接或密钥，或用「框选页面二维码」。'); return; }
  const isUri = /^otpauth:\/\//i.test(secret);
  if (!isUri && (!account || !issuer)) {
    setTotpQrStatus('err', '手输密钥时，站点和账号都要填。');
    return;
  }
  const domains = domainsRaw ? domainsRaw.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean) : [];
  if (!domains.length) { setTotpQrStatus('err', '至少关联一个网站域名。'); return; }
  const payload = { secret, issuer, account, domains, note };
  const saveBtn = document.getElementById('totpSaveBtn');
  const original = saveBtn.textContent;
  saveBtn.textContent = '保存中…'; saveBtn.disabled = true;
  const resp = await sendTotpRequest('totp_save', payload);
  saveBtn.disabled = false; saveBtn.textContent = original;
  if (!resp.ok) { setTotpQrStatus('err', resp.error || '保存失败'); return; }
  document.getElementById('totpAddBox').hidden = true;
  clearTotpForm();
  await reloadTotpList();
}

// 编辑保存：只提交展示字段（无 secret），服务端 totp_update 按 id 定位、不新增行。
async function saveTotpEdit() {
  const issuer = document.getElementById('totpIssuerInput').value.trim();
  const account = document.getElementById('totpAccountInput').value.trim();
  const domainsRaw = document.getElementById('totpDomainsInput').value.trim();
  const note = document.getElementById('totpNoteInput').value.trim();
  setTotpQrStatus('', '');
  if (!account || !issuer) { setTotpQrStatus('err', '站点和账号都要填。'); return; }
  const domains = domainsRaw ? domainsRaw.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean) : [];
  if (!domains.length) { setTotpQrStatus('err', '至少关联一个网站域名。'); return; }
  const saveBtn = document.getElementById('totpSaveBtn');
  const original = saveBtn.textContent;
  saveBtn.textContent = '保存中…'; saveBtn.disabled = true;
  const resp = await sendTotpRequest('totp_update', { id: totpEditingId, issuer, account, domains, note });
  saveBtn.disabled = false; saveBtn.textContent = original;
  if (!resp.ok) { setTotpQrStatus('err', resp.error || '保存失败'); return; }
  document.getElementById('totpAddBox').hidden = true;
  clearTotpForm();
  await reloadTotpList();
}

// ── 导入 / 备份导出（经 background 的 totp WS 通道） ────────────────────────
// 导入：选 .txt 或粘贴文本 → totp_import 整包发服务端逐行解析入库（一步批量）。
// 导出：输账号密码 → totp_export（服务端二次鉴权）→ 回明文文本 → 落盘下载。
// ⚠ 导出回帧含全部明文密钥：只在内存与下载文件里存在，不写 console/storage。

function hideTotpIoPanels(keep) {
  if (keep !== 'import') { const b = document.getElementById('totpImportBox'); if (b) b.hidden = true; }
  if (keep !== 'export') { const b = document.getElementById('totpExportBox'); if (b) b.hidden = true; }
}

function setTotpImportStatus(kind, text) {
  const el = document.getElementById('totpImportStatus');
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || '';
  el.className = 'totp-qr-status ' + (kind === 'ok' ? 'ok' : kind === 'err' ? 'err' : '');
}

function setTotpExportStatus(kind, text) {
  const el = document.getElementById('totpExportStatus');
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || '';
  el.className = 'totp-qr-status ' + (kind === 'ok' ? 'ok' : kind === 'err' ? 'err' : '');
}

// 选了 .txt：读为文本 → 立即整包导入（不搞两步确认，导入幂等且可重复）。
async function doTotpImportFile(file) {
  setTotpImportStatus('', '');
  if (!file) return;
  let text = '';
  try { text = await file.text(); } catch (e) {
    setTotpImportStatus('err', '读取文件失败：' + (e && e.message ? e.message : '请重试'));
    return;
  }
  await runTotpImport(text);
}

// 文本框粘贴后没有自动提交入口，这里顺手监听 Ctrl+Enter 提交。
async function runTotpImport(text) {
  const trimmed = String(text || '').trim();
  if (!trimmed) { setTotpImportStatus('err', '内容为空：每行一条 otpauth:// 链接。'); return; }
  setTotpImportStatus('', '导入中…');
  const resp = await sendTotpRequest('totp_import', { content: trimmed });
  if (!resp.ok) { setTotpImportStatus('err', resp.error || '导入失败'); return; }
  const d = resp.data || {};
  const saved = (d.imported || 0) + (d.overwritten || 0);
  const skipN = d.skipped_count || 0;
  const errN = (d.error_count || 0) - skipN;
  if (saved === 0) {
    const first = (d.errors && d.errors[0]) || null;
    setTotpImportStatus('err', first
      ? `没导入成功。第 ${first.line} 行：${first.reason}`
      : '没有可导入的内容：每行需一条 otpauth:// 链接。');
    return;
  }
  // 关键：导入后自动切「全部」——数据按 issuer 站点名关联，「本站」页签按当前页
  // 域名精确匹配，往往匹配不上，留在这页会让用户以为「导了个寂寞」。
  totpTab = 'all';
  document.getElementById('totpTabSite').classList.remove('on');
  document.getElementById('totpTabAll').classList.add('on');
  await reloadTotpList();
  let msg = `已保存 ${saved} 条`;
  if (d.overwritten) msg += `（其中 ${d.overwritten} 条是同站点同账号，已覆盖）`;
  if (skipN) msg += `；${skipN} 行没带账号已跳过`;
  if (errN > 0) msg += `；${errN} 行格式有误未导入`;
  msg += '。下方「全部」里可见。';
  setTotpImportStatus('ok', msg);
}

// 文本框 Ctrl+Enter 直接提交导入；选文件是主路径，粘贴是兜底。
function armTotpImportKeys() {
  const area = document.getElementById('totpImportText');
  if (!area) return;
  area.addEventListener('keydown', (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') {
      ev.preventDefault();
      void runTotpImport(area.value);
    }
  });
}

async function doTotpExport() {
  const pwdInput = document.getElementById('totpExportPwd');
  const password = (pwdInput && pwdInput.value || '').trim();
  if (!password) { setTotpExportStatus('err', '请输入你的账号密码。'); return; }
  setTotpExportStatus('', '导出中…');
  const resp = await sendTotpRequest('totp_export', { password });
  if (!resp.ok) { setTotpExportStatus('err', resp.error || '导出失败'); return; }
  const d = resp.data || {};
  if (!d.content) { setTotpExportStatus('err', '还没有可导出的验证器。'); return; }
  // 落盘下载：明文密钥只存在于这个文件里（建议离线安全位置）。
  const blob = new Blob([d.content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const ts = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  a.download = 'ai-lubricant-totp-' + ts.getFullYear() + pad(ts.getMonth() + 1) + pad(ts.getDate()) + '-' + pad(ts.getHours()) + pad(ts.getMinutes()) + pad(ts.getSeconds()) + '.txt';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  setTotpExportStatus('ok', `已导出 ${d.count} 条验证器备份。`);
  const p = document.getElementById('totpExportPwd'); if (p) p.value = '';
}
