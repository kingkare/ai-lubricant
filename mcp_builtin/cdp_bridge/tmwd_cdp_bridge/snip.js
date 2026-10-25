// snip.js - 框选页面二维码（独立扩展页，popup 的「框选页面二维码」入口打开）
//
// 流程：popup 截当前可见区（captureVisibleTab）存 storage.session（totpSnipShot）→
// 打开本页 → 全屏显示截图 → 用户拖框 → 在**原分辨率**上裁块跑 jsQR（预览可缩放，
// 识别永远用原图坐标，质量不受窗口缩放影响）→ 识别到就地填站点/账号/关联域名 →
// 保存走 totp_save 帧（经 background 的 WS 通道）→ 自关窗口。
//
// 为什么独立页面：popup 一点页面外区域就整个关闭，没法承载「拖框」这种跨元素交互；
// 且 popup 只有 420px 宽，截图缩小后拖不准。全屏页缩放显示 + 原图裁块两全。
//
// ⚠ 截图只存 chrome.storage.session（内存态、关浏览器即清），不落 storage.local、
//   不进任何日志；识别出的 otpauth 文本只在内存与 totp_save 帧里存在。

const SNIP_SHOT_KEY = 'totpSnipShot';

// 视图状态
let shotUrl = '';
let shotNatW = 0, shotNatH = 0;
let scale = 1;                 // 显示缩放（fit）
let dragging = false;
let dragStart = null;          // {x, y} 原图坐标
let foundText = '';            // 识别出的 otpauth/密钥文本
let foundParsed = null;

const $ = (id) => document.getElementById(id);
const stage = $('stage'), shot = $('shot'), box = $('box');

// ── 载入截图 ────────────────────────────────────────────────────────────────
async function loadShot() {
  $('empty').hidden = false;
  $('emptyMsg').textContent = '正在载入页面截图…';
  const stored = await chrome.storage.session.get([SNIP_SHOT_KEY]);
  shotUrl = stored && stored[SNIP_SHOT_KEY];
  if (!shotUrl) {
    $('emptyMsg').textContent = '没有截图数据。请从扩展弹窗重新点「框选页面二维码」。';
    return;
  }
  await new Promise((res, rej) => { shot.onload = res; shot.onerror = rej; shot.src = shotUrl; });
  shotNatW = shot.naturalWidth;
  shotNatH = shot.naturalHeight;
  fitShot();
  $('empty').hidden = true;
  // 用完即清：截图不留 session（窗口开着期间内存里已有 img，够用）。
  await chrome.storage.session.remove([SNIP_SHOT_KEY]);
}

function fitShot() {
  const rect = stage.getBoundingClientRect();
  scale = Math.min(1, (rect.width - 24) / shotNatW, (rect.height - 24) / shotNatH);
  shot.style.width = Math.round(shotNatW * scale) + 'px';
  shot.style.height = Math.round(shotNatH * scale) + 'px';
}

// ── 拖框（鼠标事件；坐标换算到原图） ────────────────────────────────────────
function toImageCoords(ev) {
  const r = shot.getBoundingClientRect();
  return {
    x: Math.min(Math.max((ev.clientX - r.left) / scale, 0), shotNatW),
    y: Math.min(Math.max((ev.clientY - r.top) / scale, 0), shotNatH),
  };
}

function drawBox(a, b) {
  const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y);
  const w = Math.abs(a.x - b.x), h = Math.abs(a.y - b.y);
  box.style.display = 'block';
  box.style.left = (x * scale) + 'px';
  box.style.top = (y * scale) + 'px';
  box.style.width = (w * scale) + 'px';
  box.style.height = (h * scale) + 'px';
  box.dataset.dim = `${Math.round(w)}×${Math.round(h)}`;
  return { x, y, w, h };
}

stage.addEventListener('mousedown', (ev) => {
  if (ev.button !== 0 || !shotNatW || $('result').classList.contains('show')) return;
  if (ev.target !== shot && ev.target !== stage && ev.target !== box) return;
  dragging = true;
  dragStart = toImageCoords(ev);
  box.style.display = 'none';
});

window.addEventListener('mousemove', (ev) => {
  if (!dragging) return;
  drawBox(dragStart, toImageCoords(ev));
});

window.addEventListener('mouseup', (ev) => {
  if (!dragging) return;
  dragging = false;
  const rect = drawBox(dragStart, toImageCoords(ev));
  if (rect.w < 12 || rect.h < 12) { box.style.display = 'none'; return; }
  void decodeRegion(rect);
});

// Esc 关闭（拖框中/结果条显示时都能退出）
window.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') { ev.preventDefault(); void closeWindow(); }
});

// ── 识别：原分辨率裁块 → jsQR（含 1.15x 放大重试） ──────────────────────────
async function decodeRegion(rect) {
  const img = new Image();
  img.src = shotUrl;
  await new Promise((res, rej) => { img.onload = res; img.onerror = rej; });
  const jsqr = window.jsQR;
  if (!jsqr) { showResult(null, null); return; }

  // 裁块外扩一圈（二维码静区）：四周各留 rect 宽高的 15% 或 24px。
  const pad = Math.max(24, Math.round(Math.max(rect.w, rect.h) * 0.15));
  const sx = Math.max(0, Math.round(rect.x - pad));
  const sy = Math.max(0, Math.round(rect.y - pad));
  const sw = Math.min(img.naturalWidth - sx, Math.round(rect.w + pad * 2));
  const sh = Math.min(img.naturalHeight - sy, Math.round(rect.h + pad * 2));

  const canvas = document.createElement('canvas');
  canvas.width = sw; canvas.height = sh;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);

  let text = null;
  for (const factor of [1, 1.5, 2]) {
    const cw = Math.min(2400, Math.round(sw * factor));
    const ch = Math.min(2400, Math.round(sh * factor));
    canvas.width = cw; canvas.height = ch;
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, cw, ch);
    const data = ctx.getImageData(0, 0, cw, ch);
    const found = jsqr(data.data, cw, ch);
    if (found && found.data) { text = found.data; break; }
  }
  showResult(text ? (parseOtpauth(text) || parseB32(text)) : null, text);
}

// ── 结果条 ──────────────────────────────────────────────────────────────────
function showResult(parsed, rawText) {
  foundText = rawText || '';
  foundParsed = parsed;
  const el = $('result');
  el.classList.add('show');
  const icon = $('rIcon'), title = $('rTitle');
  if (parsed && parsed.secret) {
    icon.textContent = '✓'; icon.className = 'ok';
    title.textContent = '已识别二维码';
    $('rIssuer').value = parsed.issuer || '';
    $('rAccount').value = parsed.account || '';
    $('rDomains').value = window.openerOriginHost || (parsed.issuer ? parsed.issuer.toLowerCase() : '');
    $('rNote').value = '';
    $('rRaw').textContent = rawText || '';
  } else if (rawText) {
    icon.textContent = '!'; icon.className = 'bad';
    title.textContent = '识别到内容，但不是验证器二维码（otpauth）';
    $('rIssuer').value = ''; $('rAccount').value = ''; $('rDomains').value = ''; $('rNote').value = '';
    $('rRaw').textContent = rawText;
  } else {
    icon.textContent = '×'; icon.className = 'bad';
    title.textContent = '没识别到二维码，请换个框再试（框住二维码 + 四周留些空）';
    $('rRaw').textContent = '';
  }
  if (parsed && parsed.secret) { $('btnSave').focus(); }
  else { $('btnDiscard').focus(); }
}

$('btnDiscard').addEventListener('click', () => {
  $('result').classList.remove('show');
  box.style.display = 'none';
});

$('btnRetry').addEventListener('click', async () => {
  // 重新截图：回到发起页 → 通知 popup 重截。直接在本页发起截图不可靠（本页是
  // 活动页，captureVisibleTab 会截到自己）。简单做法：关掉自己，让用户再点一次。
  void closeWindow();
});

$('btnCancel').addEventListener('click', () => void closeWindow());

async function closeWindow() {
  try { await chrome.storage.session.remove([SNIP_SHOT_KEY]); } catch (_) {}
  window.close();
}

// ── 保存（totp_save 帧，经 background 的 name='totp' 长期 port） ─────────────
// 与 popup 同款：sendMessage 只拿到「已受理」，真正的 totp_save_result 是 background
// 经 port 异步投递的 totp_frame，按 reqId 关联。这里用 port 才能确认真存成功。
let snipPort = null;
const snipPending = new Map(); // reqId -> resolve
let snipSeq = 0;

function ensureSnipPort() {
  if (snipPort) return snipPort;
  snipPort = chrome.runtime.connect({ name: 'totp' });
  snipPort.onMessage.addListener((msg) => {
    if (!msg || msg.cmd !== 'totp_frame' || !msg.data) return;
    const reqId = String(msg.data.reqId || '');
    const resolve = snipPending.get(reqId);
    if (!resolve) return;
    snipPending.delete(reqId);
    const d = msg.data;
    if (d.ok === false || d.type === 'totp_error') resolve({ ok: false, error: d.message || '操作失败' });
    else resolve({ ok: true, data: d });
  });
  snipPort.onDisconnect.addListener(() => { snipPort = null; });
  return snipPort;
}

function sendSnipRequest(cmd, payload) {
  return new Promise((resolve) => {
    let port;
    try { port = ensureSnipPort(); } catch (_) { resolve({ ok: false, error: '扩展后台不可用' }); return; }
    const reqId = 'snip_' + (++snipSeq) + '_' + Date.now();
    const timer = setTimeout(() => { snipPending.delete(reqId); resolve({ ok: false, error: '请求超时，请重试' }); }, 12000);
    snipPending.set(reqId, (res) => { clearTimeout(timer); resolve(res); });
    try { port.postMessage(Object.assign({ cmd, reqId }, payload || {})); }
    catch (_) { clearTimeout(timer); snipPending.delete(reqId); resolve({ ok: false, error: '扩展后台不可用' }); }
  });
}

$('btnSave').addEventListener('click', async () => {
  if (!foundParsed || !foundParsed.secret) return;
  const issuer = $('rIssuer').value.trim();
  const account = $('rAccount').value.trim();
  const domainsRaw = $('rDomains').value.trim();
  const note = $('rNote').value.trim();
  const domains = domainsRaw ? domainsRaw.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean) : [];
  const btn = $('btnSave');
  btn.disabled = true; btn.textContent = '保存中…';
  try {
    // secret 传整条 otpauth URI（服务端解析为准）；手输密钥时传裸文本。
    const secret = /^otpauth:\/\//i.test(foundText) ? foundText : foundParsed.secret;
    const resp = await sendSnipRequest('totp_save', { secret, issuer, account, domains, note });
    if (!resp.ok) {
      btn.disabled = false; btn.textContent = '保存到身份验证器';
      $('rRaw').textContent = '保存失败：' + (resp.error || '未知错误');
      return;
    }
    btn.textContent = '已保存 ✓';
    setTimeout(() => void closeWindow(), 600);
  } catch (e) {
    btn.disabled = false; btn.textContent = '保存到身份验证器';
    $('rRaw').textContent = '保存失败：' + (e && e.message ? e.message : e);
  }
});

// 非 otpauth 但像 base32 密钥（A-Z2-7，长度 ≥ 16）：当密钥用。
function parseB32(text) {
  const s = String(text || '').replace(/\s+/g, '').toUpperCase();
  if (!/^[A-Z2-7]{16,}$/.test(s)) return null;
  return { issuer: '', account: '', secret: s, digits: 6, period: 30, algorithm: 'SHA1' };
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

// 发起页的站点（popup 传进来）：「关联网站」默认值。
let openerOriginHost = '';
(async () => {
  const stored = await chrome.storage.session.get(['totpSnipOrigin']);
  openerOriginHost = (stored && stored['totpSnipOrigin']) || '';
  await chrome.storage.session.remove(['totpSnipOrigin']);
  await loadShot();
})();
