// pwd_cache.js - 密码管理器本地缓存（离线可用）
//
// 由 background.js 顶部 importScripts('pwd_cache.js') 引入，只在 Service Worker
// 里使用（content.js / popup.js 一律经 background 的消息通道读写，保持单一写入方）。
//
// 动机：密码的权威副本在服务端（browser_password_store，AES-256-GCM，密钥只在
// 服务端）。原先断网时 pwd_list / pwd_get / pwd_save 全部失败，整个密码管理器不可用。
// 这里在本地留一份镜像：在线时随服务端回帧更新，离线时读走它、写先落它并标 pending，
// 重连后由 background 的 syncPwdCache 补送服务端（服务端按 (owner,domain,username)
// upsert，天然幂等）。
//
// ⚠ 威胁模型（有意为之的取舍）：明文密码要能离线填充，就必须在本机可解。整表用
//   WebCrypto AES-GCM 加密后落 chrome.storage.local，密钥也存本地（同目录同权限）。
//   这挡得住「误备份 / 云同步 / 明文 grep」，挡不住已拿到浏览器 profile 的攻击者。
//   与服务端加密口径对齐（都只做静态加密，不做抗本地攻击）。
//
// ⚠ 明文只允许出现在本文件的加解密管线与内存变量里：绝不 console.log / 写 DOM 属性
//   / 进页面标题，与 background.js 密码段的约定一致。

const PWD_CACHE_KEY_STORE = 'cdpPwdCacheKey';   // { k: <base64 raw key> }
const PWD_CACHE_STORE = 'cdpPwdCache';          // { v: 1, iv: <b64>, ct: <b64> }
const PWD_CACHE_VERSION = 1;

let _pwdCacheKeyPromise = null;
// 串行化 read-modify-write：SW 里多个 await 会交错，并发 upsert 会互相覆盖。
let _pwdCacheChain = Promise.resolve();

function pwdCacheStorageOk() {
  return typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local
    && typeof crypto !== 'undefined' && crypto.subtle;
}

function _pwdB64FromBytes(bytes) {
  const arr = new Uint8Array(bytes);
  let out = '';
  for (let i = 0; i < arr.length; i++) out += String.fromCharCode(arr[i]);
  return btoa(out);
}

function _pwdBytesFromB64(b64) {
  const raw = atob(String(b64));
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

// 惰性生成/加载本机 AES-GCM key。生成后导出 raw 存 storage（下次可复用）；
// importKey 用 extractable=false——此后任何代码都拿不回原始 key 字节。
function pwdCacheKey() {
  if (!_pwdCacheKeyPromise) {
    _pwdCacheKeyPromise = (async () => {
      const stored = await chrome.storage.local.get([PWD_CACHE_KEY_STORE]);
      const existing = stored && stored[PWD_CACHE_KEY_STORE];
      if (existing && existing.k) {
        return crypto.subtle.importKey(
          'raw', _pwdBytesFromB64(existing.k), { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);
      }
      const key = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
      const raw = await crypto.subtle.exportKey('raw', key);
      await chrome.storage.local.set({ [PWD_CACHE_KEY_STORE]: { k: _pwdB64FromBytes(raw) } });
      return key;
    })().catch((err) => {
      _pwdCacheKeyPromise = null; // 下次重试，别把失败钉死
      throw err;
    });
  }
  return _pwdCacheKeyPromise;
}

async function _pwdCacheLoad() {
  const stored = await chrome.storage.local.get([PWD_CACHE_STORE]);
  const blob = stored && stored[PWD_CACHE_STORE];
  if (!blob || !blob.iv || !blob.ct) return { entries: [], tombstones: [] };
  const key = await pwdCacheKey();
  const plain = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: _pwdBytesFromB64(blob.iv) }, key, _pwdBytesFromB64(blob.ct));
  const parsed = JSON.parse(new TextDecoder().decode(plain));
  return {
    entries: Array.isArray(parsed && parsed.entries) ? parsed.entries : [],
    tombstones: Array.isArray(parsed && parsed.tombstones) ? parsed.tombstones : [],
  };
}

async function _pwdCacheStore(state) {
  const key = await pwdCacheKey();
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const data = new TextEncoder().encode(JSON.stringify({
    entries: state.entries || [],
    tombstones: state.tombstones || [],
  }));
  const cipher = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, data);
  await chrome.storage.local.set({
    [PWD_CACHE_STORE]: { v: PWD_CACHE_VERSION, iv: _pwdB64FromBytes(iv), ct: _pwdB64FromBytes(cipher) },
  });
}

// 串行 read-modify-write。mutator(state) 可直接改 state 并返回结果。
function _pwdCacheMutate(mutator) {
  const run = _pwdCacheChain.then(async () => {
    const state = await _pwdCacheLoad();
    const result = await mutator(state);
    await _pwdCacheStore(state);
    return result;
  });
  // 链条本身不因单次失败断掉；调用方各自拿 run 处理错误。
  _pwdCacheChain = run.then(() => {}, () => {});
  return run;
}

function _pwdNormDomain(domain) {
  return String(domain || '').trim().toLowerCase();
}

function _pwdSameCredential(entry, domain, username) {
  return entry && _pwdNormDomain(entry.domain) === _pwdNormDomain(domain)
    && String(entry.username || '') === String(username || '');
}

// 本地临时 id：负数（服务端 id 恒为正），避免与真 id 撞车。
function _pwdTempId() {
  return -Date.now();
}

// ── 读 ──────────────────────────────────────────────────────────────────────

async function pwdCacheList(domain) {
  const state = await _pwdCacheLoad();
  const want = _pwdNormDomain(domain);
  const rows = want ? state.entries.filter((e) => _pwdNormDomain(e.domain) === want) : state.entries;
  // 回给 UI 的一律是 meta（不带 password），与服务端 pwd_list 口径一致。
  return rows.map(pwdCacheMeta);
}

function pwdCacheMeta(entry) {
  return {
    id: entry.id,
    domain: entry.domain || '',
    username: entry.username || '',
    url: entry.url || '',
    source: entry.source || 'browser',
    createdAt: entry.createdAt || '',
    updatedAt: entry.updatedAt || '',
    pending: Boolean(entry.pending),
  };
}

async function pwdCacheGet(id) {
  const state = await _pwdCacheLoad();
  const row = state.entries.find((e) => String(e.id) === String(id));
  if (!row) return null;
  return {
    ...pwdCacheMeta(row),
    // 没缓存到明文（在线时只列过 meta 没取过密码）就回空串，由上层给出可读提示。
    password: String(row.password || ''),
  };
}

async function pwdCacheFindByCredential(domain, username) {
  const state = await _pwdCacheLoad();
  const row = state.entries.find((e) => _pwdSameCredential(e, domain, username));
  return row ? pwdCacheMeta(row) : null;
}

async function pwdCachePending() {
  const state = await _pwdCacheLoad();
  return state.entries.filter((e) => e.pending).map((e) => ({ ...e }));
}

async function pwdCacheTombstones() {
  const state = await _pwdCacheLoad();
  return state.tombstones.slice();
}

// ── 写 ──────────────────────────────────────────────────────────────────────

// 合并一条记录。命中 (domain, username) 时复用其 id/password（除非新值显式给出），
// 这样服务端回帧（真实 id + 无密码）能把本地临时行提升成正式行而不丢已缓存的明文。
function pwdCacheUpsert(entry, options) {
  const opts = options || {};
  return _pwdCacheMutate((state) => {
    const domain = _pwdNormDomain(entry.domain);
    const username = String(entry.username || '');
    if (!domain || !username) return null;
    let row = state.entries.find((e) => _pwdSameCredential(e, domain, username));
    const now = new Date().toISOString();
    if (!row) {
      row = {
        id: entry.id !== undefined && entry.id !== null ? entry.id : _pwdTempId(),
        domain, username,
        url: '', source: 'browser', createdAt: now, updatedAt: now,
        password: '', pending: false,
      };
      state.entries.push(row);
    }
    if (entry.id !== undefined && entry.id !== null) row.id = entry.id;
    if (entry.url !== undefined) row.url = String(entry.url || '');
    if (entry.source !== undefined) row.source = String(entry.source || 'browser');
    if (entry.createdAt !== undefined && entry.createdAt) row.createdAt = String(entry.createdAt);
    if (entry.updatedAt !== undefined && entry.updatedAt) row.updatedAt = String(entry.updatedAt);
    // password 只认非空字符串：服务端回帧不带 password，别把已缓存的明文冲掉。
    // 待同步行（本地改过、还没补送到服务端）的明文是**更新的**，只有服务端确认
    // （opts.confirmed，即 pwd_save_result）或本地显式写入（forcePassword，离线
    // 保存）才允许覆盖——否则一次在线 pwd_get 会用服务端的旧值把刚存的密码顶掉。
    const overwrite = Boolean(opts.confirmed || opts.forcePassword);
    if (entry.password && (overwrite || !row.pending)) row.password = String(entry.password);
    if (opts.pending !== undefined) row.pending = Boolean(opts.pending);
    else if (entry.pending !== undefined) row.pending = Boolean(entry.pending);
    // 服务端确认过的行（带真 id）不再是待同步，且从 tombstone 里摘掉。
    if (opts.confirmed) {
      row.pending = false;
      state.tombstones = state.tombstones.filter((t) => String(t) !== String(row.id));
    }
    return { ...row };
  });
}

function pwdCacheRemove(id) {
  return _pwdCacheMutate((state) => {
    const before = state.entries.length;
    state.entries = state.entries.filter((e) => String(e.id) !== String(id));
    return state.entries.length !== before;
  });
}

function pwdCacheAddTombstone(id) {
  return _pwdCacheMutate((state) => {
    // 本地临时行（还没同步过）直接消失即可，不需要服务端删除。
    if (Number(id) < 0) return false;
    const key = String(id);
    if (!state.tombstones.some((t) => String(t) === key)) state.tombstones.push(id);
    return true;
  });
}

function pwdCacheDropTombstone(id) {
  return _pwdCacheMutate((state) => {
    state.tombstones = state.tombstones.filter((t) => String(t) !== String(id));
  });
}

// 服务端明确拒绝这条补同步（配额/校验/记录已不存在）：摘掉 pending，停止无限重试。
// 本地行保留——用户填表仍需它；只是不再尝试同步。
function pwdCacheDropPending(id) {
  return _pwdCacheMutate((state) => {
    const row = state.entries.find((e) => String(e.id) === String(id));
    if (row) row.pending = false;
  });
}

// 服务端 pwd_list 结果合并：返回的都是 meta（无密码）。
// domain 为空表示全量列表 → 服务端是权威，清掉本地非 pending 的幽灵行；
// domain 非空只是该域的子集，不动其它域。
function pwdCacheMergeList(entries, domain) {
  const list = Array.isArray(entries) ? entries : [];
  const scoped = _pwdNormDomain(domain);
  return _pwdCacheMutate((state) => {
    const seen = new Set(list.map((e) => String(e.id)));
    if (!scoped) {
      state.entries = state.entries.filter((e) => e.pending || seen.has(String(e.id)));
    }
    for (const meta of list) {
      const d = _pwdNormDomain(meta.domain);
      const u = String(meta.username || '');
      if (!d || !u) continue;
      // 待同步的本地行优先：服务端还没有它（或值更旧），别被列表覆盖。
      if (state.entries.some((e) => _pwdSameCredential(e, d, u) && e.pending)) continue;
      const existing = state.entries.find((e) => _pwdSameCredential(e, d, u));
      if (existing) {
        existing.id = meta.id;
        existing.url = String(meta.url || '');
        existing.source = String(meta.source || 'browser');
        if (meta.createdAt) existing.createdAt = String(meta.createdAt);
        if (meta.updatedAt) existing.updatedAt = String(meta.updatedAt);
        existing.pending = false;
      } else {
        state.entries.push({
          id: meta.id, domain: d, username: u,
          url: String(meta.url || ''), source: String(meta.source || 'browser'),
          createdAt: String(meta.createdAt || ''), updatedAt: String(meta.updatedAt || ''),
          password: '', pending: false,
        });
      }
    }
  });
}

// 离线保存：命中同 (domain, username) 就地改密码并标 pending；否则新建临时行。
function pwdCacheSaveOffline(frame) {
  return pwdCacheUpsert({
    domain: frame.domain, username: frame.username,
    password: frame.password, url: frame.url,
    updatedAt: new Date().toISOString(),
  }, { pending: true, forcePassword: true });
}

// 清空整表（换客户端 token / 归属变更时调用，避免上一个账号的密码残留）。
function pwdCacheClear() {
  return _pwdCacheMutate((state) => {
    state.entries = [];
    state.tombstones = [];
  });
}
