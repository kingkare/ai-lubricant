// proxy.js — 浏览器出网代理引擎（类 SwitchyOmega 3 的 Profile 模型）。
//
// 独立于 CDP 桥接的业务逻辑：只负责 chrome.proxy 的配置/还原，不碰 WS 通道。
// 由 background.js importScripts 加载；命令经 handleExtMessage 的 proxy_* 分支进入。
//
// 为什么单独成文件：代理的 Profile 模型 + PAC 编译 + 租约/还原是一套自洽状态机，
// 混进 background.js 会让本已 1700+ 行的桥接逻辑更难维护。这里只依赖
// chrome.proxy / chrome.storage / chrome.alarms 三个扩展 API。
//
// 安全边界：本引擎改的是整台浏览器的全局出网路径，必须可归属、可还原。
// appliedBy 只有 'user'（popup 手动）与 'admin'（管理端下发）两种——**没有 agent**：
// Agent 无权改出网路径（见 docs/browser-proxy-design.md §1）。

const PROXY_STORAGE_KEY = 'proxyEngine';
const PROXY_LEASE_DEFAULT_MS = 30 * 60 * 1000;      // 管理端未给 TTL 时的默认租约
const PROXY_LEASE_MAX_MS = 24 * 60 * 60 * 1000;     // 租约上限，防「永久占着」
const PROXY_LEASE_ALARM = 'tmwd-proxy-lease';
// 正则/通配 pattern 上限与字符白名单：PAC 脚本是扩展要执行的 JS，pattern 来自
// 服务端/用户输入，必须限制长度并拒绝可疑字符，绝不做字符串拼接插值。
const PROXY_PATTERN_MAX = 256;

// ── 状态：profiles 库 + 单一活跃态 ─────────────────────────────────────────
// 真相源在服务端（Phase 2 接入）；此处是扩展本地镜像，保证 SW 重启/离线时
// 仍能还原到一致状态。结构：
//   { profiles: [ProxyProfile], active: {activeProfileId, appliedBy, leaseExpiresAt, appliedAt} | null }
let proxyState = { profiles: [], active: null, deleted: [] };
let proxyStateLoaded = false;

function proxyDefaults() {
  return {
    id: '',
    name: '',
    type: 'direct',          // direct | system | fixed | pac | auto
    fixed: { scheme: 'http', host: '', port: 0, bypass: [] },
    pac: { url: '', data: '' },
    auto: { rules: [], defaultProfileId: '' },
    scope: 'regular',        // regular | incognito_session_only
  };
}

async function loadProxyState() {
  try {
    const stored = await chrome.storage.local.get([PROXY_STORAGE_KEY]);
    const s = stored[PROXY_STORAGE_KEY];
    if (s && typeof s === 'object') {
      proxyState = {
        profiles: Array.isArray(s.profiles) ? s.profiles.map(sanitizeProfile) : [],
        active: s.active && typeof s.active === 'object' ? s.active : null,
        // 删除墓碑：本地删了但还没同步到服务端的 id。同步成功后清空。
        deleted: Array.isArray(s.deleted) ? s.deleted.map(String) : [],
      };
    }
  } catch (_) { /* storage 不可用则用默认空态 */ }
  proxyStateLoaded = true;
  return proxyState;
}

async function saveProxyState() {
  try {
    await chrome.storage.local.set({ [PROXY_STORAGE_KEY]: proxyState });
  } catch (_) { /* 写入失败不致命：下次 load 会回落默认态 */ }
}

// ── Profile 规范化（白名单式，拒绝任何越界字段）────────────────────────────
function sanitizePattern(p) {
  const s = String(p || '');
  if (s.length > PROXY_PATTERN_MAX) return s.slice(0, PROXY_PATTERN_MAX);
  return s;
}

function sanitizeProfile(raw) {
  const d = proxyDefaults();
  const p = { ...d, ...(raw && typeof raw === 'object' ? raw : {}) };
  p.id = String(p.id || '').slice(0, 64);
  p.name = String(p.name || '').slice(0, 128);
  p.type = ['direct', 'system', 'fixed', 'pac', 'auto'].includes(p.type) ? p.type : 'direct';
  p.scope = p.scope === 'incognito_session_only' ? 'incognito_session_only' : 'regular';

  const f = p.fixed && typeof p.fixed === 'object' ? p.fixed : {};
  p.fixed = {
    scheme: ['http', 'https', 'socks4', 'socks5'].includes(f.scheme) ? f.scheme : 'http',
    host: String(f.host || '').slice(0, 255),
    port: Math.max(0, Math.min(65535, Number(f.port) || 0)),
    bypass: Array.isArray(f.bypass) ? f.bypass.map(x => String(x).slice(0, 255)) : [],
    // 代理认证（user/pass）：schema 先留着，实现见 §7 坑 5（需 webRequest）。
    auth: {
      username: String((f.auth && f.auth.username) || '').slice(0, 255),
      password: String((f.auth && f.auth.password) || ''),
    },
  };

  const pac = p.pac && typeof p.pac === 'object' ? p.pac : {};
  p.pac = { url: String(pac.url || '').slice(0, 2048), data: String(pac.data || '').slice(0, 65536) };

  const auto = p.auto && typeof p.auto === 'object' ? p.auto : {};
  const RULE_TYPES = ['hostWildcard', 'hostRegex', 'urlWildcard', 'urlRegex', 'keyword'];
  p.auto = {
    rules: Array.isArray(auto.rules) ? auto.rules.slice(0, 500).map((r) => ({
      condition: {
        type: RULE_TYPES.includes(r && r.condition && r.condition.type) ? r.condition.type : 'hostWildcard',
        pattern: sanitizePattern(r && r.condition && r.condition.pattern),
      },
      profileId: String((r && r.profileId) || ''),
    })) : [],
    defaultProfileId: String(auto.defaultProfileId || ''),
  };
  return p;
}

// ── bypass 注入：桥接 host 与本地恒直连，防「代理自己把自己代理了」 ─────────
// 实测：Chrome 对 loopback 目标永不代理，故 <local>/127.0.0.1 是兜底；桥接若是
// 远程域名（ws://host/...）必须显式进 bypassList，否则 Profile 一设就可能把桥接
// WS 也塞进代理，Clash 规则一变就断桥（见 §7 坑 1）。
function bridgeHostFromUrl(url) {
  try {
    const u = new URL(String(url || ''));
    return u.hostname || '';
  } catch (_) { return ''; }
}

function effectiveBypass(profile) {
  const set = new Set(['<local>', '127.0.0.1', 'localhost']);
  for (const b of (profile.fixed && profile.fixed.bypass) || []) if (b) set.add(b);
  const host = bridgeHostFromUrl(typeof bridgeWsUrl !== 'undefined' ? bridgeWsUrl : '');
  if (host) set.add(host);
  return [...set];
}

// ── 编译：ProxyProfile → chrome.proxy 的 ProxyConfig ───────────────────────
function schemeToPac(scheme) {
  if (scheme === 'socks5') return 'SOCKS5';
  if (scheme === 'socks4') return 'SOCKS';
  return 'PROXY'; // http/https 都是 HTTP 代理
}

// 单个 Profile 在 PAC 里的指令（auto 规则命中后 return 什么）。
function profileDirective(profile) {
  if (!profile) return 'DIRECT';
  if (profile.type === 'fixed' && profile.fixed.host && profile.fixed.port) {
    return `${schemeToPac(profile.fixed.scheme)} ${profile.fixed.host}:${profile.fixed.port}`;
  }
  // direct / system / 不完整的 fixed / pac：PAC 里表达为 DIRECT（system 语义在
  // PAC 中不可达——PAC 本身就是要显式决策，system 由 mode:'system' 承载）。
  return 'DIRECT';
}

// 生成 FindProxyForURL 的源码。所有 pattern 一律 JSON.stringify 注入（防注入），
// 规则表先算出「profileId → 指令」再内联为常量。
function compileAutoPac(profile, profilesById, bypassHosts) {
  const direct = profileDirective(profilesById.get(profile.auto.defaultProfileId));
  const lines = [];
  lines.push('function FindProxyForURL(url, host) {');
  // 桥接/本地 host 恒直连（防自环）。
  if (bypassHosts.length) {
    const list = JSON.stringify(bypassHosts);
    lines.push(`  var BYPASS = ${list};`);
    lines.push('  for (var i = 0; i < BYPASS.length; i++) {');
    lines.push('    if (host === BYPASS[i] || host === "www." + BYPASS[i]) return "DIRECT";');
    lines.push('  }');
    lines.push('  if (isPlainHostName(host) || host === "127.0.0.1" || host === "localhost") return "DIRECT";');
  }
  lines.push(`  var DEFAULT = ${JSON.stringify(direct)};`);
  for (const rule of profile.auto.rules) {
    const t = rule.condition.type;
    const pat = JSON.stringify(rule.condition.pattern);
    const directive = JSON.stringify(profileDirective(profilesById.get(rule.profileId)));
    let cond;
    if (t === 'hostWildcard') cond = `shExpMatch(host, ${pat})`;
    else if (t === 'urlWildcard') cond = `shExpMatch(url, ${pat})`;
    else if (t === 'keyword') cond = `url.indexOf(${pat}) >= 0`;
    else if (t === 'hostRegex') cond = `safeRe(${pat}).test(host)`;
    else cond = `safeRe(${pat}).test(url)`; // urlRegex
    lines.push(`  if (${cond}) return ${directive};`);
  }
  lines.push('  return DEFAULT;');
  lines.push('}');
  // safeRe：带 try/catch 的正则构造，非法 pattern 退化为永不匹配，避免整个 PAC
  // 抛异常导致所有请求 DIRECT。
  const prelude = 'function safeRe(p) { try { return new RegExp(p); } catch (e) { return /$^/; } }';
  return prelude + '\n' + lines.join('\n');
}

// 把 Profile 编译成 chrome.proxy 的 value。失败抛 Error（调用方转成错误回帧）。
function compileProfile(profile, allProfiles) {
  const bypass = effectiveBypass(profile);
  if (profile.type === 'direct') return { mode: 'direct' };
  if (profile.type === 'system') return { mode: 'system' };
  if (profile.type === 'fixed') {
    if (!profile.fixed.host || !profile.fixed.port) throw new Error('fixed 代理缺少 host/port');
    return {
      mode: 'fixed_servers',
      rules: {
        singleProxy: { scheme: profile.fixed.scheme, host: profile.fixed.host, port: profile.fixed.port },
        bypassList: bypass,
      },
    };
  }
  if (profile.type === 'pac') {
    if (profile.pac.data) return { mode: 'pac_script', pacScript: { data: profile.pac.data } };
    if (profile.pac.url) return { mode: 'pac_script', pacScript: { url: profile.pac.url } };
    throw new Error('pac 代理缺少 url/data');
  }
  if (profile.type === 'auto') {
    const byId = new Map((allProfiles || []).map(p => [p.id, p]));
    const bridgeHosts = [];
    const bh = bridgeHostFromUrl(typeof bridgeWsUrl !== 'undefined' ? bridgeWsUrl : '');
    if (bh) bridgeHosts.push(bh);
    const data = compileAutoPac(profile, byId, bridgeHosts);
    return { mode: 'pac_script', pacScript: { data } };
  }
  throw new Error('未知 Profile 类型: ' + profile.type);
}

// ── 应用 / 还原 ────────────────────────────────────────────────────────────
function findProfile(id) {
  return proxyState.profiles.find(p => p.id === id) || null;
}

// 应用某 Profile。appliedBy ∈ {'user','admin'}（无 agent）。leaseMs 仅对 admin 有意义。
async function applyProfile(profileId, { appliedBy = 'user', leaseMs, force = false } = {}) {
  if (!proxyStateLoaded) await loadProxyState();
  const profile = findProfile(profileId);
  if (!profile) throw new Error('Profile 不存在: ' + profileId);

  // 用户手动态（user）优先级最高：admin 不得覆盖，除非显式 force。
  const cur = proxyState.active;
  if (cur && cur.appliedBy === 'user' && appliedBy === 'admin' && !force) {
    throw new Error('当前为用户手动设置，管理端不得覆盖（需 force）');
  }

  const value = compileProfile(profile, proxyState.profiles);
  const scope = profile.scope === 'incognito_session_only' ? 'incognito_session_only' : 'regular';
  await chrome.proxy.settings.set({ value, scope });

  let leaseExpiresAt = 0;
  if (appliedBy === 'admin') {
    const ttl = Math.min(Math.max(Number(leaseMs) || PROXY_LEASE_DEFAULT_MS, 1000), PROXY_LEASE_MAX_MS);
    leaseExpiresAt = Date.now() + ttl;
  }
  proxyState.active = {
    activeProfileId: profile.id,
    appliedBy,
    scope,
    leaseExpiresAt,
    appliedAt: Date.now(),
  };
  await saveProxyState();
  await scheduleLease(leaseExpiresAt);
  return proxyState.active;
}

// 内建项应用（ZeroOmega 风格）：direct / system 不是库里的 Profile，是虚拟内建。
// 用一个内部 id（__direct / __system）标记活跃态，状态回读时据此还原显示名。
const BUILTIN_PROFILES = {
  __direct: { id: '__direct', name: '直接连接', type: 'direct', scope: 'regular' },
  __system: { id: '__system', name: '系统代理', type: 'system', scope: 'regular' },
};

async function applyBuiltin(builtinId, { appliedBy = 'user', leaseMs, force = false } = {}) {
  const profile = BUILTIN_PROFILES[builtinId];
  if (!profile) throw new Error('未知内建代理类型: ' + builtinId);
  if (!proxyStateLoaded) await loadProxyState();
  const cur = proxyState.active;
  if (cur && cur.appliedBy === 'user' && appliedBy === 'admin' && !force) {
    throw new Error('当前为用户手动设置，管理端不得覆盖（需 force）');
  }
  const value = compileProfile(profile, proxyState.profiles);
  await chrome.proxy.settings.set({ value, scope: 'regular' });
  let leaseExpiresAt = 0;
  if (appliedBy === 'admin') {
    const ttl = Math.min(Math.max(Number(leaseMs) || PROXY_LEASE_DEFAULT_MS, 1000), PROXY_LEASE_MAX_MS);
    leaseExpiresAt = Date.now() + ttl;
  }
  proxyState.active = {
    activeProfileId: profile.id,
    appliedBy,
    scope: 'regular',
    leaseExpiresAt,
    appliedAt: Date.now(),
  };
  await saveProxyState();
  await scheduleLease(leaseExpiresAt);
  return proxyState.active;
}

async function scheduleLease(leaseExpiresAt) {
  try {
    await chrome.alarms.clear(PROXY_LEASE_ALARM);
    if (leaseExpiresAt > Date.now()) {
      await chrome.alarms.create(PROXY_LEASE_ALARM, { when: leaseExpiresAt });
    }
  } catch (_) { /* alarms 不可用：租约到期的兜底由 SW 唤醒时的 reconcile 处理 */ }
}

// 还原：清掉本扩展设的代理配置，回到浏览器原有状态。
async function restoreProxy(reason = 'manual') {
  if (!proxyStateLoaded) await loadProxyState();
  const scope = (proxyState.active && proxyState.active.scope) || 'regular';
  try {
    await chrome.proxy.settings.clear({ scope });
  } catch (_) { /* 已清/不可用：继续把本地态清干净 */ }
  proxyState.active = null;
  await saveProxyState();
  try { await chrome.alarms.clear(PROXY_LEASE_ALARM); } catch (_) {}
  return { restored: true, reason };
}

async function getProxyStatus() {
  if (!proxyStateLoaded) await loadProxyState();
  let levelOfControl = '';
  try {
    const got = await chrome.proxy.settings.get({ incognito: false });
    levelOfControl = got.levelOfControl || '';
  } catch (_) {}
  const active = proxyState.active;
  // 活跃项可能是内建（__direct/__system），不在 Profile 库里。
  const profile = active ? (findProfile(active.activeProfileId) || BUILTIN_PROFILES[active.activeProfileId] || null) : null;
  return {
    applied: Boolean(active && levelOfControl === 'controlled_by_this_extension'),
    levelOfControl,
    activeProfileId: active ? active.activeProfileId : '',
    activeName: profile ? profile.name : '',
    activeType: profile ? profile.type : '',
    appliedBy: active ? active.appliedBy : '',
    leaseExpiresAt: active ? active.leaseExpiresAt : 0,
    leaseRemainingMs: active && active.leaseExpiresAt ? Math.max(0, active.leaseExpiresAt - Date.now()) : 0,
  };
}

// ── SW 启动对账 ────────────────────────────────────────────────────────────
// MV3 SW 会被随时杀死、内存态全丢；chrome.proxy 的设置在扩展存活期内是浏览器级
// 持久的，但 SW 重启后我们不知道是否还生效。这里读本地镜像：有活跃态就重放一遍
// （幂等），并重排租约 alarm；租约已过期则直接还原。
async function reconcileProxyOnStartup() {
  await loadProxyState();
  const active = proxyState.active;
  if (!active) return;
  if (active.leaseExpiresAt && active.leaseExpiresAt <= Date.now()) {
    await restoreProxy('lease_expired_on_startup');
    return;
  }
  const profile = findProfile(active.activeProfileId) || BUILTIN_PROFILES[active.activeProfileId];
  if (!profile) { await restoreProxy('profile_missing'); return; }
  try {
    const value = compileProfile(profile, proxyState.profiles);
    await chrome.proxy.settings.set({ value, scope: active.scope || 'regular' });
    await scheduleLease(active.leaseExpiresAt);
  } catch (_) { await restoreProxy('reconcile_failed'); }
}

// 租约到期 alarm 回调（background.js 里注册）。
async function onProxyLeaseAlarm(alarm) {
  if (!alarm || alarm.name !== PROXY_LEASE_ALARM) return false;
  if (!proxyStateLoaded) await loadProxyState();
  const active = proxyState.active;
  if (active && active.leaseExpiresAt && active.leaseExpiresAt <= Date.now()) {
    await restoreProxy('lease_expired');
  }
  return true;
}

// 服务端下发的代理控制帧（经控制通道 /session 到达，data.type === 'proxy_ctl'）。
// action ∈ {activate, restore, sync_profiles}。执行后经代理通道上报 proxy_status。
// 真相源在服务端 Profile 库：activate/sync_profiles 会带完整 Profile 定义，
// 扩展本地镜像 upsert 后按 id 应用，保证「服务端存什么、浏览器生效什么」。
async function handleProxyControl(data) {
  const action = String(data.action || '');
  const profiles = Array.isArray(data.profiles) ? data.profiles.map(sanitizeProfile) : null;
  if (action === 'sync_profiles' && profiles) {
    // 全量覆盖本地镜像（服务端是真相源）：删掉本地有、服务端没有的；服务端有的整组替换。
    const serverIds = new Set(profiles.filter(p => p.id).map(p => p.id));
    proxyState.profiles = profiles.filter(p => p.id);
    // 删除墓碑：服务端既然还在（或已删除），同步回来后清掉与本次全量对齐的墓碑，
    // 避免下次上行又把服务端同步下来的配置误删掉。
    if (Array.isArray(proxyState.deleted)) {
      proxyState.deleted = proxyState.deleted.filter(d => !serverIds.has(d));
    }
    // 若当前活跃 Profile 被同步删掉（服务端已删），则还原出网路径，别留悬空引用。
    if (proxyState.active && proxyState.active.activeProfileId && !serverIds.has(proxyState.active.activeProfileId)) {
      await restoreProxy('profile_removed_by_sync');
    }
    await saveProxyState();
    await reportProxyStatus();
    return;
  }
  if (profiles && profiles.length && action !== 'sync_profiles') {
    // upsert 进本地镜像（activate 带单条）
    for (const p of profiles) {
      if (!p.id) continue;
      const idx = proxyState.profiles.findIndex(x => x.id === p.id);
      if (idx >= 0) proxyState.profiles[idx] = p; else proxyState.profiles.push(p);
    }
    await saveProxyState();
  }
  if (action === 'activate') {
    const profileId = String(data.profileId || '');
    const leaseMs = Number(data.leaseMs) || 0;
    try {
      await applyProfile(profileId, { appliedBy: 'admin', leaseMs, force: data.force === true });
    } catch (e) {
      console.warn('[proxy] activate failed:', e.message || e);
    }
  } else if (action === 'restore') {
    await restoreProxy('server');
  } else {
    return;
  }
  await reportProxyStatus();
}

// 把本地 Profile 全量推上服务端（双向同步的上行）。断连时静默：下次连上会补推。
// deleted 是删除墓碑（本地删了、服务端还没同步的 id），ack 后清空。
async function syncLocalToServer() {
  await loadProxyState();
  const profiles = proxyState.profiles.filter(p => p.id);
  const deleted = Array.isArray(proxyState.deleted) ? proxyState.deleted : [];
  const reqId = 'sync_' + Math.random().toString(36).slice(2, 10);
  const frame = { type: 'proxy_sync', reqId, profiles, deleted, protocol: 2 };
  if (typeof proxySendFrame === 'function') {
    proxySendFrame(frame);
  }
  return reqId;
}

// 服务端 ack：确认已接受 → 清空删除墓碑（否则下次同步会重复删）。
async function onProxySyncAck(data) {
  if (!data || data.ok === false) return;
  await loadProxyState();
  if (Array.isArray(proxyState.deleted) && proxyState.deleted.length) {
    proxyState.deleted = [];
    await saveProxyState();
  }
}

// 「从另一个浏览器导入」第一步的等待方：reqId → resolve(browsers)。
const _proxySourcesWaiters = new Map();

// 拉同 owner 域内的浏览器列表 + 各自 Profile。8s 超时 → 返回空（导入可退回本地文件）。
async function requestProxySources(timeoutMs = 8000) {
  const reqId = 'src_' + Math.random().toString(36).slice(2, 10);
  return new Promise((resolve) => {
    if (typeof proxySendFrame !== 'function' || !proxySendFrame({
      type: 'proxy_sources_request', reqId, protocol: 2,
    })) {
      resolve([]);
      return;
    }
    const timer = setTimeout(() => {
      _proxySourcesWaiters.delete(reqId);
      resolve([]);
    }, timeoutMs);
    _proxySourcesWaiters.set(reqId, (browsers) => {
      clearTimeout(timer);
      resolve(browsers);
    });
  });
}

// 把当前生效态经代理通道上报给服务端。无连接则 no-op。
async function reportProxyStatus() {
  const status = await getProxyStatus();
  const frame = { type: 'proxy_status', status };
  if (typeof proxySendFrame === 'function') {
    proxySendFrame(frame);
  }
}

// ── 代理通道 WS（/ws/proxy，数据面）────────────────────────────────────────
// 独立于控制通道 connectWS：自己的 epoch、重连退避、keepalive alarm。
// 只用一条文本帧 socket 承载 proxy_status 上报 + 后续 proxy_chunk 数据帧。
// 关键：不能复用 socketEpoch / connecting / authenticated 这些控制通道状态，
// 否则代理重连会误判控制通道的连接失效。
let proxyWs = null;
let proxyConnecting = false;
let proxyAuthenticated = false;
let proxyEpoch = 0;
let proxyUrl = '';
const PROXY_KEEPALIVE_ALARM = 'tmwd-proxy-keepalive';
const PROXY_KEEPALIVE_MS = 0.4 * 60 * 1000;  // ~24s，与控制通道同频
const PROXY_RECONNECT_MS = 30000;

// 从控制通道 bridgeUrl 派生代理通道 URL：同 host、路径换 /ws/proxy。
function deriveProxyUrl(bridgeUrl) {
  try {
    const u = new URL(String(bridgeUrl || ''));
    const path = u.pathname.replace(/\/session$/, '/ws/proxy');
    u.pathname = path;
    return u.toString();
  } catch (_) {
    return String(bridgeUrl || '').replace(/\/session$/, '/ws/proxy');
  }
}

function proxySendFrame(frame) {
  if (!proxyWs || proxyWs.readyState !== WebSocket.OPEN || !proxyAuthenticated) return false;
  try { proxyWs.send(JSON.stringify(frame)); return true; } catch (_) { return false; }
}

async function connectProxyWS() {
  if (proxyConnecting || (proxyWs && proxyWs.readyState <= 1)) return;
  await loadBridgeConfig();
  if (!bridgeConfig.bridgeEnabled || !bridgeConfig.clientToken) { proxyConnecting = false; return; }
  proxyConnecting = true;
  const epoch = ++proxyEpoch;
  proxyUrl = deriveProxyUrl(bridgeConfig.bridgeUrl);
  let socket;
  try {
    socket = new WebSocket(proxyUrl);
    proxyWs = socket;
  } catch (_) {
    proxyWs = null;
    proxyConnecting = false;
    scheduleProxyKeepalive(PROXY_RECONNECT_MS);
    return;
  }
  const isCurrent = () => proxyWs === socket && proxyEpoch === epoch;
  socket.onopen = () => {
    if (!isCurrent()) { try { socket.close(); } catch (_) {} return; }
    socket.send(JSON.stringify({ type: 'auth', protocol: 2, token: bridgeConfig.clientToken }));
  };
  socket.onmessage = async (event) => {
    if (!isCurrent()) return;
    try {
      const data = JSON.parse(event.data);
      if (data && data.type === 'auth_ok' && data.protocol === 2) {
        proxyAuthenticated = true;
        proxyConnecting = false;
        scheduleProxyKeepalive(PROXY_KEEPALIVE_MS);
        await reportProxyStatus();  // 连上立刻上报一次当前生效态
        void syncLocalToServer();   // 连上补推本地全量（离线期间的编辑在此落库）
        return;
      }
      if (data && data.type === 'auth_error') {
        proxyAuthenticated = false;
        proxyConnecting = false;
        // 代理通道 auth 失败不弹 already_connected 退避那套（控制通道专属）；
        // 简单退避后重试，失败只影响出口不影响对话。
        scheduleProxyKeepalive(PROXY_RECONNECT_MS);
        return;
      }
      // 服务端下发的代理控制帧（activate/restore/sync_profiles）走代理通道。
      if (data && data.type === 'proxy_ctl') {
        await handleProxyControl(data);
        return;
      }
      // 同步 ack：服务端确认接受 → 清空删除墓碑。
      if (data && data.type === 'proxy_sync_ack') {
        await onProxySyncAck(data);
        return;
      }
      // 「从另一个浏览器导入」的 sources 回帧：唤醒等待方。
      if (data && data.type === 'proxy_sources') {
        const waiter = _proxySourcesWaiters.get(String(data.reqId || ''));
        if (waiter) {
          _proxySourcesWaiters.delete(String(data.reqId || ''));
          waiter(data.browsers || []);
        }
        return;
      }
      // 数据面：服务端要求经这台浏览器出网。
      if (data && data.type === 'proxy_request') {
        void handleProxyRequest(data);
        return;
      }
      if (data && data.type === 'proxy_cancel') {
        handleProxyCancel(data);
        return;
      }
      // 服务端下发（如未来 proxy_result 确认）：Phase 1 无数据面，忽略。
    } catch (_) { /* ignore malformed */ }
  };
  socket.onclose = () => {
    if (!isCurrent()) return;
    proxyAuthenticated = false;
    proxyConnecting = false;
    if (proxyWs === socket) proxyWs = null;
    scheduleProxyKeepalive(PROXY_RECONNECT_MS);
  };
  socket.onerror = () => { /* onclose 兜底 */ };
}

function scheduleProxyKeepalive(delayMs) {
  try { chrome.alarms.create(PROXY_KEEPALIVE_ALARM, { delayInMinutes: delayMs / 60000 }); } catch (_) {}
}

// 配置变更（URL/token）后重连：作废旧 socket 的回调（proxyEpoch++），关旧连接，
// 按新配置重连。与控制通道 socketEpoch 互不干扰。
async function resetProxyConn() {
  const old = proxyWs;
  proxyWs = null;
  proxyEpoch++;
  proxyAuthenticated = false;
  proxyConnecting = false;
  try { if (old) old.close(); } catch (_) {}
  try { await chrome.alarms.clear(PROXY_KEEPALIVE_ALARM); } catch (_) {}
  if (bridgeConfig.bridgeEnabled && bridgeConfig.clientToken) await connectProxyWS();
}

// 代理 keepalive：连上时发 ping 续命并触发重连；未连时按退避重试。
async function onProxyKeepalive() {
  if (proxyWs && proxyWs.readyState === WebSocket.OPEN && proxyAuthenticated) {
    try { proxyWs.send(JSON.stringify({ type: 'ping' })); } catch (_) {}
    scheduleProxyKeepalive(PROXY_KEEPALIVE_MS);
    return;
  }
  // 未连/半开：重试连接
  await connectProxyWS();
}

// ── 数据面（出口请求：服务端 → 扩展 fetch → 流式回传）──────────────────────
// Phase 3：浏览器当代理节点。proxy_request 帧到达后扩展 fetch 并流式回传。
// chunk 用二进制帧（实测 5x 快于 base64+JSON）；自定义小帧头带 proxyId 关联。
//   帧格式：[1 字节 idLen][proxyId UTF-8][chunk bytes]
// 控制帧（proxy_head/proxy_end/proxy_error）仍走 JSON 文本帧。
const _PROXY_ABORTS = new Map();  // proxyId → AbortController

function base64ToBytes(b64) {
  const s = String(b64 || '');
  const bin = atob(s);
  const u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return u8;
}

function headersToList(h) {
  const list = [];
  if (!h || !h.forEach) return list;
  h.forEach((v, k) => list.push([k, v]));
  return list;
}

function sendProxyBinaryChunk(proxyId, bytes) {
  if (!proxyWs || proxyWs.readyState !== WebSocket.OPEN || !proxyAuthenticated) return false;
  const idBytes = new TextEncoder().encode(proxyId);
  if (idBytes.length > 255 || idBytes.length === 0) return false;
  const out = new Uint8Array(1 + idBytes.length + bytes.length);
  out[0] = idBytes.length;
  out.set(idBytes, 1);
  out.set(bytes, 1 + idBytes.length);
  try { proxyWs.send(out.buffer); return true; } catch (_) { return false; }
}

async function handleProxyRequest(data) {
  const proxyId = String(data.proxyId || '');
  if (!proxyId) return;
  const ctrl = new AbortController();
  _PROXY_ABORTS.set(proxyId, ctrl);
  const timeoutMs = Number(data.timeoutMs) || 60000;
  const timer = setTimeout(() => { try { ctrl.abort(); } catch (_) {} }, timeoutMs);
  try {
    const init = {
      method: String(data.method || 'GET'),
      headers: data.headers || {},
      cache: 'no-store',
      credentials: data.credentials === 'include' ? 'include' : 'omit',
      redirect: 'follow',
      signal: ctrl.signal,
    };
    const bodyB64 = String(data.bodyB64 || '');
    if (bodyB64 && init.method !== 'GET' && init.method !== 'HEAD') {
      init.body = base64ToBytes(bodyB64);
    }
    const resp = await fetch(String(data.url || ''), init);
    proxySendFrame({
      type: 'proxy_head', proxyId,
      status: resp.status,
      headers: headersToList(resp.headers),
    });
    // 流式回传 body：reader.read() 循环，每块走二进制帧。
    if (resp.body && resp.body.getReader) {
      const reader = resp.body.getReader();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.length) sendProxyBinaryChunk(proxyId, value);
      }
    } else {
      // 没有 body 流（某些响应）：一次读全，二进制回传。
      const buf = new Uint8Array(await resp.arrayBuffer());
      if (buf.length) sendProxyBinaryChunk(proxyId, buf);
    }
    proxySendFrame({ type: 'proxy_end', proxyId });
  } catch (e) {
    proxySendFrame({
      type: 'proxy_error', proxyId,
      code: ctrl.signal.aborted ? 'timeout' : 'fetch_failed',
      message: String((e && e.message) || e).slice(0, 300),
    });
  } finally {
    clearTimeout(timer);
    _PROXY_ABORTS.delete(proxyId);
  }
}

function handleProxyCancel(data) {
  const ctrl = _PROXY_ABORTS.get(String(data.proxyId || ''));
  if (ctrl) { try { ctrl.abort(); } catch (_) {} }
}

// ── SwitchyOmega / ZeroOmega 备份导入 ──────────────────────────────────────
// 只做「能读多少算多少」，不承诺完整映射（见 docs/browser-proxy-design.md §10.3）。
// SO 备份形如：{ profiles: { name: {type:'FixedProfile'|'SwitchProfile'|...} } }，
// 规则用 profileName 引用其它 profile（我们内部用 id，导入时建 name→id 映射）。
function importSwitchyOmegaBackup(backup) {
  const profiles = backup && backup.profiles;
  if (!profiles || typeof profiles !== 'object') throw new Error('不是有效的 SwitchyOmega 备份（缺 profiles）');

  const COND = {
    HostWildcardCondition: 'hostWildcard',
    HostRegexCondition: 'hostRegex',
    UrlWildcardCondition: 'urlWildcard',
    UrlRegexCondition: 'urlRegex',
    KeywordCondition: 'keyword',
  };
  // name → 新 id（唯一化，避免与既有/同名冲突）。
  const nameToId = new Map();
  const used = new Set(proxyState.profiles.map(p => p.id));
  for (const name of Object.keys(profiles)) {
    const base = ('so_' + String(name).replace(/[^A-Za-z0-9_-]/g, '_')).slice(0, 48) || 'so_profile';
    let id = base, n = 1;
    while (used.has(id)) id = base + '_' + (n++);
    used.add(id);
    nameToId.set(name, id);
  }

  const out = [];
  for (const [name, p] of Object.entries(profiles)) {
    if (!p || typeof p !== 'object') continue;
    const id = nameToId.get(name);
    const t = p.type;
    const prof = { id, name: String(p.name || name).slice(0, 128), type: 'direct', scope: 'regular' };
    if (t === 'FixedProfile') {
      const fp = p.fallbackProxy || {};
      prof.type = 'fixed';
      prof.fixed = {
        scheme: ['http', 'https', 'socks4', 'socks5'].includes(fp.scheme) ? fp.scheme : 'http',
        host: String(fp.host || ''),
        port: Number(fp.port) || 0,
        bypass: (p.bypassList || []).map(b => (typeof b === 'string' ? b : (b && b.pattern) || '')).filter(Boolean),
      };
    } else if (t === 'SystemProfile') {
      prof.type = 'system';
    } else if (t === 'PacProfile') {
      prof.type = 'pac';
      prof.pac = { url: String(p.pacUrl || ''), data: String(p.pacScript || '') };
    } else if (t === 'SwitchProfile') {
      prof.type = 'auto';
      prof.auto = {
        rules: (p.rules || []).map(r => ({
          condition: {
            type: COND[(r.condition && r.condition.conditionType)] || 'hostWildcard',
            pattern: String((r.condition && r.condition.pattern) || ''),
          },
          profileId: nameToId.get(r.profileName) || '',
        })).filter(r => r.condition.pattern),
        defaultProfileId: nameToId.get(p.defaultProfileName) || '',
      };
    } else {
      prof.type = 'direct';  // DirectProfile 及未知类型
    }
    out.push(sanitizeProfile(prof));
  }
  return out;
}

// ── 命令入口（popup / 后续 WS 控制帧共用）─────────────────────────────────
async function handleProxyCommand(msg) {
  if (!proxyStateLoaded) await loadProxyState();
  switch (msg.cmd) {
    case 'proxy_list':
      return { ok: true, data: { profiles: proxyState.profiles, status: await getProxyStatus() } };
    case 'proxy_status_get':
      return { ok: true, data: await getProxyStatus() };
    case 'proxy_save': {
      const p = sanitizeProfile(msg.profile);
      if (!p.id) p.id = 'pp_' + Math.random().toString(36).slice(2, 10);
      const idx = proxyState.profiles.findIndex(x => x.id === p.id);
      if (idx >= 0) proxyState.profiles[idx] = p; else proxyState.profiles.push(p);
      await saveProxyState();
      void syncLocalToServer();   // 本地变更 → 上报服务端
      return { ok: true, data: p };
    }
    case 'proxy_delete': {
      const id = String(msg.profileId || '');
      proxyState.profiles = proxyState.profiles.filter(x => x.id !== id);
      if (id) {
        proxyState.deleted = proxyState.deleted || [];
        if (!proxyState.deleted.includes(id)) proxyState.deleted.push(id);
      }
      // 删的是当前活跃 Profile：直接还原，别留着一个指向不存在配置的代理态。
      if (proxyState.active && proxyState.active.activeProfileId === id) await restoreProxy('profile_deleted');
      await saveProxyState();
      void syncLocalToServer();
      return { ok: true };
    }
    // 两步导入 · 第一步：解析来源为候选列表（不落地）。来源 = 本地文件（SO 备份 /
    // 我们的导出）或"另一个浏览器"（其 Profile 列表由 sources 请求预先拉取后传入）。
    case 'proxy_import_parse': {
      let candidates;
      if (msg.kind === 'browser') {
        candidates = Array.isArray(msg.profiles) ? msg.profiles.map(sanitizeProfile).filter(p => p.id) : [];
      } else {
        try {
          candidates = importSwitchyOmegaBackup(msg.data);
        } catch (e) {
          return { ok: false, error: String(e.message || e) };
        }
      }
      return { ok: true, data: { candidates } };
    }
    // 两步导入 · 第二步：把用户勾选的候选写进本地镜像（并上报服务端）。
    case 'proxy_import_apply': {
      const chosen = Array.isArray(msg.profiles) ? msg.profiles.map(sanitizeProfile) : [];
      let added = 0, updated = 0;
      for (const p of chosen) {
        if (!p.id) p.id = 'pp_' + Math.random().toString(36).slice(2, 10);
        const idx = proxyState.profiles.findIndex(x => x.id === p.id);
        if (idx >= 0) { proxyState.profiles[idx] = p; updated++; } else { proxyState.profiles.push(p); added++; }
      }
      await saveProxyState();
      void syncLocalToServer();
      return { ok: true, data: { added, updated, total: proxyState.profiles.length } };
    }
    case 'proxy_apply': {
      try {
        const active = await applyProfile(String(msg.profileId || ''), {
          appliedBy: msg.appliedBy === 'admin' ? 'admin' : 'user',
          leaseMs: msg.leaseMs,
        });
        return { ok: true, data: active };
      } catch (e) { return { ok: false, error: String(e.message || e) }; }
    }
    // 内建项（ZeroOmega 风格）：direct / system 无需在 Profile 库里存在，直接应用。
    case 'proxy_apply_builtin': {
      try {
        const active = await applyBuiltin(String(msg.builtinType || ''), {
          appliedBy: msg.appliedBy === 'admin' ? 'admin' : 'user',
          leaseMs: msg.leaseMs,
        });
        return { ok: true, data: active };
      } catch (e) { return { ok: false, error: String(e.message || e) }; }
    }
    // 「从另一个浏览器导入」第一步的来源拉取（同 owner 域）。
    case 'proxy_fetch_sources': {
      const browsers = await requestProxySources();
      return { ok: true, data: { browsers } };
    }
    case 'proxy_restore':
      return { ok: true, data: await restoreProxy('user') };
    default:
      return { ok: false, error: 'Unknown proxy cmd: ' + msg.cmd };
  }
}
