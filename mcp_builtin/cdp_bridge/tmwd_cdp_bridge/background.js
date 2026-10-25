// background.js - Ai Lubricant 浏览器助手
try { importScripts('config.js'); } catch (_) {}
// 密码管理器本地缓存（离线读写 + AES-GCM 静态加密），只在 SW 里用。
try { importScripts('pwd_cache.js'); } catch (_) {}
// 浏览器出网代理引擎（Profile 模型 + chrome.proxy 应用/还原 + 租约）。
// 独立于桥接业务，见 proxy.js 顶部注释与 docs/browser-proxy-design.md。
try { importScripts('proxy.js'); } catch (_) {}

// 聊天面板按 tabId 存的会话恢复状态（cdpPanel:<tabId>）要给 content script 读写，
// session storage 默认只允许扩展页面访问，这里放开到 untrusted contexts。
try { chrome.storage.session.setAccessLevel({ accessLevel: 'TRUSTED_AND_UNTRUSTED_CONTEXTS' }); } catch (_) {}
// 标签页关闭时清掉对应的面板恢复键，避免 tabId 复用时误恢复。
try {
  chrome.tabs.onRemoved.addListener((removedTabId) => {
    chrome.storage.session.remove(`cdpPanel:${removedTabId}`).catch(() => {});
  });
} catch (_) {}

const DEFAULT_BRIDGE_CONFIG = {
  bridgeUrl: typeof DEFAULT_BRIDGE_URL !== 'undefined' ? DEFAULT_BRIDGE_URL : 'ws://127.0.0.1:8001/mcp/cdp-bridge/session',
  clientToken: typeof DEFAULT_CLIENT_TOKEN !== 'undefined' ? DEFAULT_CLIENT_TOKEN : '',
  bridgeEnabled: true,
};
// already_connected 不是永久错误：另一条连接可能是半开的死连接，服务端空闲读
// 超时（~60s）会释放会话槽。故这里不把它列为永久错误——客户端继续探测重连，
// 待服务端放开后自动接管，而不是永久卡死在"已在其他浏览器连接"。
const PERMANENT_AUTH_ERRORS = new Set([
  'invalid', 'invalid_token', 'disabled', 'permission_denied',
  'protocol_required', 'revoked',
]);
let bridgeConfig = { ...DEFAULT_BRIDGE_CONFIG };
let bridgeWsUrl = normalizeBridgeUrl(bridgeConfig.bridgeUrl);
let connecting = false;
let authenticated = false;
let authError = null;
let reconnectBlocked = false;
let socketEpoch = 0;
let reprovisionRequired = false;
const CONNECTION_TIMEOUT_MS = 10000;
const AUTH_RESPONSE_TIMEOUT_MS = 10000;
// pong 判死窗口（毫秒）：keepalive 每 ~24s 发一次 ping，服务端活着就会立刻回
// pong（或任何帧）。半开连接上 ws.send 永远本地成功、onclose 永不触发，唯一可靠
// 的死活信号就是「发了 ping 却迟迟收不到任何回帧」。60s 与服务端空闲读超时
// （_EXT_IDLE_TIMEOUT）同量级，正常心跳下每 ~24s 至少有一帧，不会误杀。
const PONG_TIMEOUT_MS = 60000;
let lastFrameAt = 0;

function hasOwn(object, key) {
  return Object.prototype.hasOwnProperty.call(object || {}, key);
}

function usableClientToken(token) {
  return String(token || '').trim();
}

async function loadBridgeConfig() {
  const stored = await chrome.storage.local.get([...Object.keys(DEFAULT_BRIDGE_CONFIG), 'bridgeToken']);
  const hadLegacyToken = hasOwn(stored, 'bridgeToken');
  const hasClientToken = Boolean(usableClientToken(stored.clientToken));
  if (hadLegacyToken) {
    // A legacy bridge token is an MCP-user credential, not a CDP client token.
    // Never promote it across the identity boundary; force explicit reprovisioning.
    await chrome.storage.local.remove('bridgeToken');
    reprovisionRequired = !hasClientToken;
  } else if (hasClientToken) {
    reprovisionRequired = false;
  }
  bridgeConfig = {
    bridgeUrl: normalizeBridgeUrl(stored.bridgeUrl),
    clientToken: usableClientToken(stored.clientToken),
    bridgeEnabled: stored.bridgeEnabled !== false,
  };
  bridgeWsUrl = bridgeConfig.bridgeUrl;
  return bridgeConfig;
}

function normalizeBridgeUrl(url) {
  let u = String(url || DEFAULT_BRIDGE_CONFIG.bridgeUrl).trim();
  if (!u) return DEFAULT_BRIDGE_CONFIG.bridgeUrl;
  if (!/^wss?:\/\//i.test(u)) u = 'ws://' + u;
  return u.replace(/\/+$/, '');
}

function isLocalBridge(url) {
  return /^(ws|wss):\/\/(127\.0\.0\.1|localhost)([:\/]|$)/i.test(String(url || ''));
}

// 把当前桥接状态推送给所有可注入的标签页（content.js 监听后更新徽标）。
// 关键动机：content.js 原先只在页面加载/点击徽标时各查一次状态，SW 后续 auth_ok
// 连上后徽标不会自动更新——用户得手动刷新页面才看到「已连接」。改成 SW 在状态
// 变化点主动广播，徽标就能实时跟上。popup 自身仍走 750ms 轮询，不受影响。
async function broadcastBridgeStatus() {
  const status = getBridgeStatus();
  const tabs = await chrome.tabs.query({}).catch(() => []);
  const payload = { cmd: 'bridge_status_push', status };
  for (const t of tabs) {
    if (!isScriptable(t.url)) continue;
    chrome.tabs.sendMessage(t.id, payload).catch(() => { /* tab 不可注入/已关，忽略 */ });
  }
}

function getBridgeStatus() {
  const socketOpen = Boolean(ws && ws.readyState === WebSocket.OPEN);
  const connected = socketOpen && authenticated;
  let state = 'disconnected';
  if (!bridgeConfig.bridgeEnabled) state = 'disabled';
  else if (!bridgeConfig.bridgeUrl || !bridgeConfig.clientToken) state = 'unconfigured';
  else if (connected) state = 'connected';
  else if (authError) state = 'auth_error';
  else if (connecting || socketOpen || (ws && ws.readyState === WebSocket.CONNECTING)) state = 'authenticating';
  return {
    connected,
    authenticated,
    enabled: bridgeConfig.bridgeEnabled,
    state,
    error: authError ? { ...authError } : null,
    reprovisionRequired,
  };
}

chrome.runtime.onInstalled.addListener(() => {
  console.log('Ai Lubricant 浏览器助手 installed');
  // Strip CSP headers to allow eval/inline scripts
  chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: [9999],
    addRules: [{
      id: 9999, priority: 1,
      action: { type: 'modifyHeaders', responseHeaders: [
        { header: 'content-security-policy', operation: 'remove' },
        { header: 'content-security-policy-report-only', operation: 'remove' }
      ]},
      condition: { urlFilter: '*', resourceTypes: ['main_frame', 'sub_frame'] }
    }]
  });
});

async function handleExtMessage(msg, sender) {
  // 插件页面交互事件
  if (msg.cmd === 'bridge_config_get') return { ok: true, data: await loadBridgeConfig() };
  if (msg.cmd === 'bridge_status_get') return { ok: true, data: getBridgeStatus() };
  // content script 查询自己所在标签页 id（会话恢复按 tabId 键控）。
  if (msg.cmd === 'bridge_tab_id') {
    return { ok: true, data: { tabId: sender && sender.tab ? sender.tab.id : null } };
  }
  if (msg.cmd === 'bridge_config_set') {
    const input = msg.config || {};
    const nextConfig = {
      bridgeUrl: normalizeBridgeUrl(input.bridgeUrl),
      clientToken: usableClientToken(input.clientToken),
      bridgeEnabled: input.bridgeEnabled !== false,
    };
    await chrome.storage.local.set(nextConfig);
    await chrome.storage.local.remove('bridgeToken');
    // 换了客户端 token = 换了服务端 owner，上一个账号的密码缓存必须清掉，
    // 否则离线时会把别人的密码填进表单。
    if (nextConfig.clientToken !== bridgeConfig.clientToken) {
      try { await pwdCacheClear(); } catch (_) { /* 缓存不可用则无需清 */ }
    }
    reprovisionRequired = !nextConfig.clientToken;
    await loadBridgeConfig();
    authError = null;
    reconnectBlocked = false;
    authenticated = false;
    const oldSocket = ws;
    ws = null;
    socketEpoch++; // 旧 socket（含正在握手的）全部作废，isCurrent() 会挡掉它们的回调
    connecting = false; // 可能正有 connectWS await 在配置读取：不重置则下面这次调用被守卫吞掉、永不建连
    if (oldSocket) oldSocket.close();
    chrome.alarms.clear('tmwd-ws-probe');
    chrome.alarms.clear('tmwd-ws-keepalive');
    if (bridgeConfig.bridgeEnabled && bridgeConfig.clientToken) connectWS();
    // 代理通道从 bridgeUrl 派生地址：配置变更后同步重连（独立 epoch，不碰控制通道）。
    void resetProxyConn();
    return { ok: true, data: bridgeConfig };
  }

  // 业务事件

  if (msg.cmd === 'cdp') return await handleCDP(msg, sender);
  if (msg.cmd === 'batch') return await handleBatch(msg, sender);
  if (msg.cmd === 'net_start') return await handleNetStart(msg, sender);
  if (msg.cmd === 'net_get') return handleNetGet(msg, sender);
  if (msg.cmd === 'net_stop') return await handleNetStop(msg, sender);
  // 凭据填充（agent 触发）：把 cmd 转给目标 tab 的 content script，由它自己把
  // 值敲进 DOM 并回执。密码明文由扩展本地解析，2FA 码由服务端算好随 cmd 下传——
  // 两者都只经「服务端→扩展→DOM」，绝不回传给模型。请求/响应式：等 content
  // 的 sendResponse。
  if (msg.cmd === 'fill_credential') {
    const tabId = msg.tabId;
    if (tabId === undefined || tabId === null) return { ok: false, error: 'no tabId for fill_credential' };
    try {
      const resp = await chrome.tabs.sendMessage(Number(tabId), msg);
      return resp || { ok: false, error: 'no response from tab' };
    } catch (e) {
      return { ok: false, error: 'tab not reachable: ' + (e && e.message ? e.message : String(e)) };
    }
  }
  if (msg.cmd === 'tabs') {
    try {
      if (msg.method === 'switch') {
        const tab = await chrome.tabs.update(msg.tabId, { active: true });
        await chrome.windows.update(tab.windowId, { focused: true });
        return { ok: true };
      } else if (msg.method === 'create') {
        const url = msg.url || 'about:blank';
        if (msg.newWindow) {
          const win = await chrome.windows.create({ url, focused: true });
          const tab = (win.tabs || [])[0] || {};
          return { ok: true, data: { id: tab.id, url: tab.url, title: tab.title, windowId: win.id } };
        }
        const tab = await chrome.tabs.create({ url, active: msg.active !== false });
        return { ok: true, data: { id: tab.id, url: tab.url, title: tab.title, windowId: tab.windowId } };
      } else if (msg.method === 'close') {
        await chrome.tabs.remove(Number(msg.tabId));
        return { ok: true, data: { id: msg.tabId } };
      } else {
        const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url));
        const data = tabs.map(t => ({ id: t.id, url: t.url, title: t.title, active: t.active, windowId: t.windowId }));
        return { ok: true, data };
      }
    } catch (e) { return { ok: false, error: e.message }; }
  }
  if (msg.cmd === 'management') {
    try {
      if (msg.method === 'list') {
        const all = await chrome.management.getAll();
        return { ok: true, data: all.map(e => ({ id: e.id, name: e.name, enabled: e.enabled, type: e.type, version: e.version })) };
      }
      if (msg.method === 'reload') {
        chrome.alarms.create('tmwd-self-reload', { when: Date.now() + 200 });
        return { ok: true };
      }
      if (msg.method === 'disable') {
        await chrome.management.setEnabled(msg.extId, false);
        return { ok: true };
      }
      if (msg.method === 'enable') {
        await chrome.management.setEnabled(msg.extId, true);
        return { ok: true };
      }
      return { ok: false, error: 'Unknown method: ' + msg.method };
    } catch (e) { return { ok: false, error: e.message }; }
  }
  if (msg.cmd === 'contentSettings') {
    try {
      const type = msg.type || 'automaticDownloads';
      const setting = msg.setting || 'allow';
      const pattern = msg.pattern || '<all_urls>';
      await chrome.contentSettings[type].set({
        primaryPattern: pattern,
        setting: setting
      });
      return { ok: true };
    } catch (e) { return { ok: false, error: e.message }; }
  }
  // 网页侧对话面板：经已鉴权的 cdp WS 通道代理到服务端 agent。
  // chat_watch/chat_unwatch：面板把某会话切到前台/切走，登记或摘除本标签页的观察者
  // 身份（见 _assembleChatFrame），同时透传服务端起/撤该会话的事件泵。
  if (msg.cmd === 'chat_list_agents' || msg.cmd === 'chat_send' || msg.cmd === 'chat_retry' || msg.cmd === 'chat_abort'
      || msg.cmd === 'chat_list_conversations' || msg.cmd === 'chat_new_conversation'
      || msg.cmd === 'chat_load_conversation' || msg.cmd === 'chat_list_models'
      || msg.cmd === 'chat_list_skills'
      || msg.cmd === 'chat_resolve_approval' || msg.cmd === 'chat_watch' || msg.cmd === 'chat_unwatch'
      || msg.cmd === 'chat_update_settings' || msg.cmd === 'chat_upload_attachment'
      || msg.cmd === 'chat_compact') {
    return await sendChatFrame(msg, sender);
  }
  // 密码管理器：经同一已鉴权 WS 通道代理（帧含明文，绝不落日志）。
  if (msg.cmd === 'pwd_list' || msg.cmd === 'pwd_get' || msg.cmd === 'pwd_save' || msg.cmd === 'pwd_delete') {
    return await sendPwdFrame(msg, sender);
  }
  // iframe 内登录捕获 → 中转回顶层画保存卡（小 iframe 里画会挤变形）。
  // 明文只走扩展内部通道：runtime→tabs frameId:0，不经页面消息广播，不跨 web origin。
  // 立即回执 {ok:true}（真正的画卡由顶层异步跑），让 iframe 不走「中转失败」兜底。
  if (msg.cmd === 'pwd_frame_capture') {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    if (tabId == null) return { ok: false, error: 'no tab' };
    const cred = msg.cred || {};
    chrome.tabs.sendMessage(Number(tabId), { cmd: 'pwd_show_save_card', cred },
      { frameId: 0 }).catch(() => { /* 顶层不可达（chrome:// 等），iframe 侧走兜底 */ });
    return { ok: true };
  }
  // 身份验证器（TOTP）：同一已鉴权 WS 通道。与密码不同——密钥永不下发到本机，
  // 本机只拿「当前码 + 绝对失效时刻 expiresAt」，所以**没有离线镜像**，连不上服务端
  // 就如实报错。数据入口是服务端推送（totp_push，订阅即推内存快照）。
  if (msg.cmd === 'totp_list' || msg.cmd === 'totp_get' || msg.cmd === 'totp_by_domain'
      || msg.cmd === 'totp_save' || msg.cmd === 'totp_update' || msg.cmd === 'totp_delete'
      || msg.cmd === 'totp_refresh_codes'
      || msg.cmd === 'totp_subscribe' || msg.cmd === 'totp_unsubscribe'
      || msg.cmd === 'totp_import' || msg.cmd === 'totp_export') {
    return await sendTotpFrame(msg, sender);
  }
  // 浏览器出网代理（Profile 应用/还原/状态）：popup 与后续 WS 控制帧共用。
  if (typeof msg.cmd === 'string' && msg.cmd.startsWith('proxy_')) {
    return await handleProxyCommand(msg);
  }

  return { ok: false, error: 'Unknown cmd: ' + msg.cmd };
}

// --- 网页侧 Agent 对话面板（经 WS 通道代理） ---
// reqId 关联请求与回帧；按 reqId → tabId 记录待投递目标，收到回帧时转发给对应标签页。
const _chatReqs = new Map(); // reqId -> tabId

// 会话观察者：convId → 正在看该会话的 tabId 集合。
// 服务端把 agent 事件按 conversationId 广播过来（无 reqId），由这里扇给所有打开
// 它的标签页——发起方与旁观方看到同一条序列。纯内存路由表：面板切走/标签页关闭
// 即摘除，重开重新登记；任何丢失都由面板自己重连+重拉历史兜底。
const _convWatchers = new Map(); // convId -> Set<tabId>

function addConvWatcher(convId, tabId) {
  if (!convId || tabId == null) return;
  let set = _convWatchers.get(convId);
  if (!set) { set = new Set(); _convWatchers.set(convId, set); }
  set.add(Number(tabId));
}

function removeConvWatcher(convId, tabId) {
  const set = _convWatchers.get(convId);
  if (!set) return;
  set.delete(Number(tabId));
  if (!set.size) _convWatchers.delete(convId);
}

function fanoutChatEvent(data) {
  const set = _convWatchers.get(String(data.conversationId || ""));
  if (!set || !set.size) return;
  for (const tabId of Array.from(set)) {
    chrome.tabs.sendMessage(tabId, { cmd: 'chat_frame', data }).catch(() => {
      set.delete(tabId); // 标签页已关/不可注入：顺手摘掉，不留死订阅
    });
  }
}

// 断连/重连后，服务端的泵（挂在这条 WS 上）随之没了，但本表的登记还在——
// 面板收到重连广播会重新 chat_watch（见 content.js），这里不需要额外动作。

// 建连/握手窗口内到达的聊天帧排队：SW 常被「点重试/发消息」这枚消息本身唤醒——
// top-level 的 connectWS() 才刚开始握手，handleExtMessage 却已同步执行到
// sendChatFrame。立刻失败会让唤醒后的第一个请求永远吃瘪（面板表现为「点了没反应」）。
// 排队后由 auth_ok 的 flushChatFrameQueue 续发；连接彻底失败由 markDisconnected 兜底失败。
const _chatFrameQueue = []; // { reqId, frame, tabId }
const CHAT_QUEUE_TIMEOUT_MS = 8000;
let _chatQueueTimer = null;

function _failChatFrame(reqId, tabId, message) {
  if (tabId == null) return;
  chrome.tabs.sendMessage(Number(tabId), {
    cmd: 'chat_frame',
    data: { type: 'chat_error', reqId, message },
  }).catch(() => { /* 标签页可能已关，忽略 */ });
}

function failChatFrameQueue(message) {
  while (_chatFrameQueue.length) {
    const item = _chatFrameQueue.shift();
    _failChatFrame(item.reqId, item.tabId, message);
  }
  if (_chatQueueTimer !== null) { clearTimeout(_chatQueueTimer); _chatQueueTimer = null; }
}

function flushChatFrameQueue() {
  while (_chatFrameQueue.length) {
    if (!ws || ws.readyState !== WebSocket.OPEN || !authenticated) return; // 还没就绪：继续排队等
    const item = _chatFrameQueue.shift();
    _chatReqs.set(item.reqId, item.tabId);
    try {
      ws.send(JSON.stringify(item.frame));
    } catch (e) {
      // 发送失败按断连口径回投 chat_error，别让面板永远等回帧。
      _chatReqs.delete(item.reqId);
      _failChatFrame(item.reqId, item.tabId, '连接已断开，消息未发出；连上后重试即可');
    }
  }
  if (_chatQueueTimer !== null) { clearTimeout(_chatQueueTimer); _chatQueueTimer = null; }
}

function queueChatFrame(reqId, frame, tabId) {
  _chatFrameQueue.push({ reqId, frame, tabId });
  // 兜底超时：连接一直起不来（服务端没回来）时按断连口径失败，绝不悬挂。
  if (_chatQueueTimer === null) {
    _chatQueueTimer = setTimeout(() => {
      _chatQueueTimer = null;
      if (!authenticated) failChatFrameQueue('连接已断开，消息未发出；连上后重试即可');
      else flushChatFrameQueue();
    }, CHAT_QUEUE_TIMEOUT_MS);
  }
}

function sendChatFrame(msg, sender) {
  const ready = Boolean(ws && ws.readyState === WebSocket.OPEN && authenticated);
  if (!ready) {
    // 建立中或已开但认证握手中（connectWS 的守卫同样用 readyState<=1）→ 排队等
    // auth_ok 续发；完全没在连 → 立刻失败（content 侧会等重连再点）。
    const connectingNow = connecting || Boolean(ws && ws.readyState <= WebSocket.OPEN);
    if (!connectingNow) {
      return { ok: false, error: 'bridge websocket is not authenticated' };
    }
    const queued = _assembleChatFrame(msg, sender);
    queueChatFrame(queued.reqId, queued.frame, queued.tabId);
    return { ok: true, reqId: queued.reqId, queued: true };
  }
  const { reqId, frame, tabId } = _assembleChatFrame(msg, sender);
  _chatReqs.set(reqId, tabId);
  // 清理过期 reqId（防止 SW 长跑后内存累积）。
  if (_chatReqs.size > 200) {
    const firstKey = _chatReqs.keys().next().value;
    _chatReqs.delete(firstKey);
  }
  ws.send(JSON.stringify(frame));
  return { ok: true, reqId };
}

function _assembleChatFrame(msg, sender) {
  const reqId = String(msg.reqId || ('chat_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8)));
  const tabId = msg.tabId != null ? msg.tabId : (sender.tab && sender.tab.id);
  const frame = {
    type: msg.cmd,
    reqId,
    tabId,
    url: msg.url || (sender.tab && sender.tab.url) || '',
    title: msg.title || (sender.tab && sender.tab.title) || '',
  };
  if (msg.cmd === 'chat_send') {
    frame.agentId = msg.agentId;
    frame.content = msg.content;
    frame.conversationId = msg.conversationId || '';
    frame.clientName = msg.clientName || '';
    // 面板协议版本：透传给服务端判断回执形态（见 content.js 的 panelV 注释）。
    if (msg.panelV) frame.panelV = msg.panelV;
    if (msg.maxTurns) frame.maxTurns = msg.maxTurns;
    // 对话级覆盖：模型 / 思考等级 / 执行模式（goal 另带目标与预算）。
    if (msg.model) frame.model = msg.model;
    if (msg.reasoningEffort !== undefined) frame.reasoningEffort = msg.reasoningEffort;
    if (msg.mode) frame.mode = msg.mode;
    if (msg.mode === 'goal') {
      frame.goalObjective = msg.goalObjective || '';
      frame.goalBudgetMinutes = msg.goalBudgetMinutes || 15;
    }
  }
  if (msg.cmd === 'chat_retry') {
    // 重试一条失败的 assistant 消息：不带 content（服务端取上一条 user 消息）。
    frame.conversationId = msg.conversationId || '';
    frame.messageId = msg.messageId;
    // 面板协议版本：与 chat_send 同口径透传。漏了它服务端 _proxy_chat_retry 会把
    // panel_v 判成 0 < 2，回「扩展版本过旧」的 chat_error，新版面板被误判。
    if (msg.panelV) frame.panelV = msg.panelV;
  }
  if (msg.cmd === 'chat_list_models') {
    frame.agentId = msg.agentId;
  }
  if (msg.cmd === 'chat_list_skills') {
    frame.agentId = msg.agentId;
  }
  if (msg.cmd === 'chat_abort') {
    frame.conversationId = msg.conversationId || '';
  }
  if (msg.cmd === 'chat_resolve_approval') {
    frame.conversationId = msg.conversationId || '';
    frame.confirmationId = msg.confirmationId || '';
    frame.result = msg.result || '';
    frame.commandHash = msg.commandHash || '';
  }
  if (msg.cmd === 'chat_new_conversation') {
    frame.agentId = msg.agentId;
    frame.title = msg.title || '';
  }
  if (msg.cmd === 'chat_send' || msg.cmd === 'chat_retry') {
    // 发起方也是该会话的观察者：不额外等一次 chat_watch 往返，事件从第一轮起
    // 就能扇到它（新会话 conversationId 为空，由下面的 chat_send_ok 补登记）。
    addConvWatcher(String(msg.conversationId || ''), tabId);
  }
  if (msg.cmd === 'chat_load_conversation') {
    frame.conversationId = msg.conversationId || '';
    // 翻页：滚到顶部加载更早时面板带 cursor（服务端上一页返回的 next_cursor）。
    if (msg.cursor != null) frame.cursor = msg.cursor;
    if (msg.limit != null) frame.limit = msg.limit;
  }
  // 切换 mode/model/思考等级 → 立即持久化到会话（不再是「下一次发送才生效」）。
  if (msg.cmd === 'chat_update_settings') {
    frame.conversationId = msg.conversationId || '';
    if (msg.mode) frame.mode = msg.mode;
    if (msg.model) frame.model = msg.model;
    if (msg.reasoningEffort !== undefined) frame.reasoningEffort = msg.reasoningEffort;
  }
  // 面板附件上传：base64 文件内容经已鉴权 WS 通道代理（大帧，见 gateway 卡 14MiB）。
  if (msg.cmd === 'chat_upload_attachment') {
    frame.conversationId = msg.conversationId || '';
    frame.filename = msg.filename || '';
    frame.content = msg.content || '';
  }
  // 手动压缩上下文：仅带会话 id（压缩范围由服务端从历史算）。
  if (msg.cmd === 'chat_compact') {
    frame.conversationId = msg.conversationId || '';
  }
  if (msg.cmd === 'chat_watch' || msg.cmd === 'chat_unwatch') {
    frame.conversationId = msg.conversationId || '';
    // 本地观察者登记与转发同步做：服务端随后广播的 chat_event 才能扇到这个 tab。
    if (msg.cmd === 'chat_watch') addConvWatcher(frame.conversationId, tabId);
    else removeConvWatcher(frame.conversationId, tabId);
  }
  if (msg.cmd === 'chat_list_agents' || msg.cmd === 'chat_list_conversations') {
    // 仅取列表，无需额外字段。
  }
  return { reqId, frame, tabId };
}

function forwardChatFrameToTab(data) {
  const tabId = _chatReqs.get(String(data.reqId));
  if (tabId == null) return;
  chrome.tabs.sendMessage(Number(tabId), { cmd: 'chat_frame', data }).catch(() => {
    // 标签页可能已关闭/不可注入，清理即可。
  });
  // 新建会话的第一轮：chat_send 时 conversationId 为空，到这里凭 ack 的真实 ID
  // 补登观察者，这样服务端事件泵广播的后续事件就能扇到发起标签页。
  if (data.type === 'chat_send_ok' && data.conversationId) {
    addConvWatcher(String(data.conversationId), tabId);
  }
  // 一次性应答帧（列表/新建/载入/发送受理/订阅受理）与流终止帧都要清理映射，
  // 避免累积。chat_send_ok 现在就是「本轮已受理」的 ack——发送与订阅分离后，
  // 事件不再挂在这个 reqId 上，而是按 conversationId 扇出。
  const ONE_SHOT = ['chat_agents', 'chat_models', 'chat_conversations', 'chat_conversation_created', 'chat_conversation_loaded', 'chat_approval_resolved', 'chat_send_ok', 'chat_watch_ok', 'chat_unwatch_ok', 'chat_settings_ok', 'chat_attachment_uploaded', 'chat_compacted'];
  if (data.type === 'chat_error' || data.type === 'chat_aborted' ||
      ONE_SHOT.includes(data.type) ||
      (data.type === 'chat_event' && data.event && ['done', 'error'].includes(data.event.type))) {
    _chatReqs.delete(String(data.reqId));
  }
}

// 服务端改了某客户端的可操作 agent 列表时主动推送：广播给所有可注入标签页，
// content.js 收到后若面板开着就重拉一次 picker 列表。
async function broadcastChatAgentsChanged() {
  const tabs = await chrome.tabs.query({}).catch(() => []);
  const payload = { cmd: 'chat_frame', data: { type: 'chat_agents_changed' } };
  for (const t of tabs) {
    if (!isScriptable(t.url)) continue;
    chrome.tabs.sendMessage(t.id, payload).catch(() => { /* tab 不可注入/已关，忽略 */ });
  }
}

// 断连时把挂起的网页对话请求统一失败掉：回帧永远不会来，不主动失败的话面板的
// sendChat promise 永不 resolve → 永远转圈、state.sending 卡死、会话切换被
// sending 守卫挡住。content.js 收到 chat_error 会 finalize 成可重试的错误行。
function failPendingChatRequests(message) {
  if (_chatReqs.size === 0) return;
  for (const [reqId, tabId] of _chatReqs) {
    if (tabId == null) continue;
    chrome.tabs.sendMessage(Number(tabId), {
      cmd: 'chat_frame',
      data: { type: 'chat_error', reqId, message },
    }).catch(() => { /* 标签页可能已关，忽略 */ });
  }
  _chatReqs.clear();
}

// --- 密码管理器（经 WS 通道代理，reqId 多路复用） ---
// 与 chat 同形，但回复目标有两种：content script（sender.tab.id）与 popup
// （长期 port，popup 没有 sender.tab）。全部 pwd 回帧都是一次性：投递后即摘映射。
// ⚠ 帧负载含明文密码：本段不得 console.log 任何 frame/data 内容。
// 在途表存 { target, frame }：连接在回帧前断掉时，写要能回退成本地 pending（见
// failPwdInFlight），只留 target 就没法重放这次写。
const _pwdReqs = new Map(); // reqId -> { target, frame }
// pwd_list 请求的 domain（服务端回帧不带 domain，合并本地镜像时要用它判断范围）。
const _pwdReqDomains = new Map(); // reqId -> domain

const _pwdFrameQueue = []; // { reqId, frame, target, cacheFallback, timer }
const PWD_QUEUE_TIMEOUT_MS = 8000;
// 离线读的宽限：服务端不可达时不让用户干等满 8s——超过这个时间还没发出去就用本地
// 缓存应答。服务端活着时通常几十毫秒内就 flush 了，正常路径不受影响。
const PWD_OFFLINE_READ_GRACE_MS = 1500;
let _pwdQueueTimer = null;

function _pwdReplyTarget(sender) {
  if (sender && sender._pwdPort) return { kind: 'port', port: sender._pwdPort };
  const tabId = sender && sender.tab ? sender.tab.id : null;
  return tabId != null ? { kind: 'tab', tabId } : null;
}

function deliverPwdFrame(target, data) {
  if (!target) return;
  if (target.kind === 'port') {
    try { target.port.postMessage({ cmd: 'pwd_frame', data }); } catch (_) { /* popup 已关，忽略 */ }
    return;
  }
  chrome.tabs.sendMessage(Number(target.tabId), { cmd: 'pwd_frame', data }).catch(() => { /* 标签页可能已关 */ });
}

function _assemblePwdFrame(msg) {
  const reqId = String(msg.reqId || ('pwd_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8)));
  const frame = { type: msg.cmd, reqId };
  if (msg.cmd === 'pwd_list') {
    if (msg.domain) frame.domain = String(msg.domain);
  }
  if (msg.cmd === 'pwd_get' || msg.cmd === 'pwd_delete') {
    frame.id = msg.id;
  }
  if (msg.cmd === 'pwd_save') {
    frame.domain = String(msg.domain || '');
    frame.username = String(msg.username || '');
    frame.password = String(msg.password || '');
    frame.url = String(msg.url || '');
  }
  return { reqId, frame };
}

function _clearPwdQueueItemTimer(item) {
  if (item && item.timer !== null && item.timer !== undefined) {
    clearTimeout(item.timer);
    item.timer = null;
  }
}

// cacheFallback: () => 回帧对象或 Promise<回帧对象>。仅在「排队后迟迟发不出去」
// （服务端不可达）时启用；连上后正常发帧，本地缓存只作镜像。
function queuePwdFrame(reqId, frame, target, cacheFallback) {
  const item = { reqId, frame, target, cacheFallback: cacheFallback || null, timer: null, deadline: 0 };
  if (item.cacheFallback) {
    // 握手/连接还在推进时不抢答：缓存是旧镜像，能等到在线答案就等。只有「连接
    // 迟迟起不来」才回退，上限是排队的 8s 兜底。
    item.deadline = Date.now() + PWD_QUEUE_TIMEOUT_MS;
    const arm = () => {
      item.timer = setTimeout(() => {
        item.timer = null;
        const i = _pwdFrameQueue.indexOf(item);
        if (i < 0) return; // 已经发出去或已被收尾
        if ((connecting || ws) && Date.now() < item.deadline) { arm(); return; }
        _pwdFrameQueue.splice(i, 1);
        _pwdReqDomains.delete(String(item.reqId));
        Promise.resolve()
          .then(() => item.cacheFallback())
          .then((data) => { if (data) deliverPwdFrame(item.target, data); })
          .catch(() => { /* 缓存也不可用：静默，调用方 12s 超时兜底 */ });
      }, PWD_OFFLINE_READ_GRACE_MS);
    };
    arm();
  }
  _pwdFrameQueue.push(item);
  if (_pwdQueueTimer === null) {
    _pwdQueueTimer = setTimeout(() => {
      _pwdQueueTimer = null;
      if (!authenticated) failPwdFrameQueue('连接已断开，密码未保存；连上后重试即可');
      else flushPwdFrameQueue();
    }, PWD_QUEUE_TIMEOUT_MS);
  }
}

function flushPwdFrameQueue() {
  while (_pwdFrameQueue.length) {
    if (!ws || ws.readyState !== WebSocket.OPEN || !authenticated) return; // 还没就绪：继续排队等
    const item = _pwdFrameQueue.shift();
    _clearPwdQueueItemTimer(item);
    _pwdReqs.set(item.reqId, { target: item.target, frame: item.frame });
    try {
      ws.send(JSON.stringify(item.frame));
    } catch (e) {
      _pwdReqs.delete(item.reqId);
      deliverPwdFrame(item.target, { type: 'pwd_error', reqId: item.reqId, message: '连接已断开，密码未保存；连上后重试即可' });
    }
  }
  if (_pwdQueueTimer !== null) { clearTimeout(_pwdQueueTimer); _pwdQueueTimer = null; }
}

function failPwdFrameQueue(message) {
  while (_pwdFrameQueue.length) {
    const item = _pwdFrameQueue.shift();
    _clearPwdQueueItemTimer(item);
    deliverPwdFrame(item.target, { type: 'pwd_error', reqId: item.reqId, message });
  }
  if (_pwdQueueTimer !== null) { clearTimeout(_pwdQueueTimer); _pwdQueueTimer = null; }
}

// 删掉一条记录时，把队列里还没发出去的、同凭据的保存帧一并撤销。
// 否则「离线保存 → 离线删除 → 重连」会把刚删掉的条目又保存回服务端
// （保存帧先于删除帧 flush）。同凭据的请求对用户而言只有一个有效结果：删除。
function _purgeQueuedPwdSaves(domain, username) {
  const wantDomain = String(domain || '').trim().toLowerCase();
  const wantUser = String(username || '');
  if (!wantDomain || !wantUser) return 0;
  let purged = 0;
  for (let i = _pwdFrameQueue.length - 1; i >= 0; i--) {
    const item = _pwdFrameQueue[i];
    const frame = item && item.frame;
    if (!frame || frame.type !== 'pwd_save') continue;
    if (String(frame.domain || '').trim().toLowerCase() !== wantDomain) continue;
    if (String(frame.username || '') !== wantUser) continue;
    _pwdFrameQueue.splice(i, 1);
    _clearPwdQueueItemTimer(item);
    purged++;
  }
  return purged;
}

// ── 本地镜像：服务端回帧 → 加密缓存（离线读写的依据） ──────────────────────
// 无论有没有在途 target 都要跑：重连补同步（syncPwdCache）发的帧没有 target，
// 它的回帧同样要把本地待同步行提升成服务端正式行。
function _mirrorPwdFrame(data) {
  if (!_pwdCacheReady()) return;
  if (!data || typeof data.type !== 'string') return;
  if (data.ok === false || data.type === 'pwd_error') {
    const reqId = String(data.reqId || '');
    _pwdReqDomains.delete(reqId); // 失败的请求不再有回帧，别留悬挂的域记录
    // 补同步被服务端**明确**拒绝（配额超限 / 校验不过 / 记录不存在）：停掉重试，
    // 本地行保留可用——否则每次重连都重发同一条永远失败的写，徒增日志与流量。
    if (_pwdSyncReqs.has(reqId)) {
      const marker = _pwdSyncReqs.get(reqId);
      _pwdSyncReqs.delete(reqId);
      if (marker) {
        if (marker.kind === 'save') void pwdCacheDropPending(marker.id).catch(() => {});
        else if (marker.kind === 'delete') void pwdCacheDropTombstone(marker.id).catch(() => {});
      }
    }
    return;
  }
  if (data.type === 'pwd_list_result') {
    const reqId = String(data.reqId || '');
    const domain = _pwdReqDomains.get(reqId);
    _pwdReqDomains.delete(reqId);
    void pwdCacheMergeList(data.entries, domain).catch(() => {});
    return;
  }
  if (data.type === 'pwd_get_result' && data.entry) {
    void pwdCacheUpsert(data.entry).catch(() => {});
    return;
  }
  if (data.type === 'pwd_save_result' && data.entry) {
    // 服务端确认：把本地临时行（离线保存时建的负数 id）换成真 id 并清 pending。
    void pwdCacheUpsert(data.entry, { confirmed: true }).catch(() => {});
    return;
  }
  if (data.type === 'pwd_delete_result' && data.id !== undefined && data.id !== null) {
    void Promise.all([
      pwdCacheRemove(data.id),
      pwdCacheDropTombstone(data.id),
    ]).catch(() => {});
  }
}

// 离线读回帧（service worker 无法连服务端时用本地镜像应答）。
function _pwdCacheReady() {
  // pwd_cache.js 没加载（importScripts 失败）时这些标识符是 undefined——
  // 一律降级回纯在线路径，绝不因为缓存模块缺失把 sendPwdFrame 抛炸。
  return typeof pwdCacheStorageOk === 'function' && pwdCacheStorageOk();
}

function _offlinePwdFrame(frame) {
  if (!_pwdCacheReady()) return null;
  if (frame.type === 'pwd_list') {
    return pwdCacheList(frame.domain).then((entries) => ({
      type: 'pwd_list_result', reqId: frame.reqId, ok: true, entries, offline: true,
    }));
  }
  if (frame.type === 'pwd_get') {
    return pwdCacheGet(frame.id).then((entry) => (
      entry && entry.password
        ? { type: 'pwd_get_result', reqId: frame.reqId, ok: true, entry, offline: true }
        // 只缓存过 meta、没取过明文：明确报错，别让上层以为拿到空密码是「无密码」。
        : { type: 'pwd_error', reqId: frame.reqId, ok: false, offline: true, message: '离线中，本机未缓存该密码的明文；联网后可查看' }
    ));
  }
  return null;
}

function sendPwdFrame(msg, sender) {
  const target = _pwdReplyTarget(sender);
  if (!target) return { ok: false, error: 'no reply target' };
  const ready = Boolean(ws && ws.readyState === WebSocket.OPEN && authenticated);
  if (!ready) {
    // 未就绪一律排队等 auth_ok 续发（8s 兜底），**不要**在这里判「没在连就失败」：
    // SW 被这条 pwd 消息唤醒时，顶层 connectWS() 是 async 且正 await
    // loadBridgeConfig()，此刻 connecting 仍是 false、ws 仍是 null——
    // 旧逻辑会把这种正常唤醒窗口误判成「未认证」而直接失败（用户点保存报
    // "bridge websocket is not authenticated"）。主动踢一次连接，再排队。
    if (!connecting && !(ws && ws.readyState <= WebSocket.OPEN)) {
      try { void connectWS(); } catch (_) { /* 连接失败由 8s 兜底定时器收尾 */ }
    }
    const { reqId, frame } = _assemblePwdFrame(msg);
    if (frame.type === 'pwd_list') _pwdReqDomains.set(reqId, frame.domain || '');
    if (frame.type === 'pwd_save' && _pwdCacheReady()) {
      // 离线保存先落本地（pending）并立刻回执——服务端不可达时用户也能存下密码。
      // 同时照常排队补发：连上了就由 _mirrorPwdFrame 的 pwd_save_result 提升为正式行。
      return pwdCacheSaveOffline(frame).then((entry) => {
        queuePwdFrame(reqId, frame, target);
        if (!entry) return { ok: true, reqId, queued: true }; // 缓存不可用：仍走在线路径
        deliverPwdFrame(target, {
          type: 'pwd_save_result', reqId, ok: true, offline: true, entry: pwdCacheMeta(entry),
        });
        return { ok: true, reqId, offline: true };
      }).catch(() => {
        queuePwdFrame(reqId, frame, target);
        return { ok: true, reqId, queued: true };
      });
    }
    if (frame.type === 'pwd_delete' && _pwdCacheReady()) {
      // 离线删除：本地立即生效 + 记 tombstone 供重连补发（本地临时行无需补发）。
      // 还要撤销同凭据的在途保存帧，否则重连后保存先 flush 把刚删的条目又建出来。
      return (async () => {
        const existing = await pwdCacheGet(frame.id).catch(() => null);
        const isTemp = Number(frame.id) < 0;
        if (existing) _purgeQueuedPwdSaves(existing.domain, existing.username);
        await pwdCacheRemove(frame.id).catch(() => {});
        if (!isTemp) {
          await pwdCacheAddTombstone(frame.id).catch(() => {});
          queuePwdFrame(reqId, frame, target); // 真实 id：补发服务端删除
        }
        deliverPwdFrame(target, {
          type: 'pwd_delete_result', reqId, ok: true, offline: true, id: frame.id,
        });
        return { ok: true, reqId, offline: true };
      })();
    }
    // 读：先排队等在线；1.5s 内发不出去则由 queuePwdFrame 的 cacheFallback 用缓存应答。
    queuePwdFrame(reqId, frame, target, _offlinePwdFrame.bind(null, frame));
    return { ok: true, reqId, queued: true };
  }
  const { reqId, frame } = _assemblePwdFrame(msg);
  if (frame.type === 'pwd_list') _pwdReqDomains.set(reqId, frame.domain || '');
  _pwdReqs.set(reqId, { target, frame });
  if (_pwdReqs.size > 200) { // 清理过期 reqId，防 SW 长跑内存累积
    _pwdReqs.delete(_pwdReqs.keys().next().value);
  }
  try {
    ws.send(JSON.stringify(frame));
  } catch (e) {
    _pwdReqs.delete(reqId);
    return { ok: false, error: '连接已断开，密码未保存；连上后重试即可' };
  }
  return { ok: true, reqId };
}

function forwardPwdFrame(data) {
  const reqId = String(data.reqId || '');
  _mirrorPwdFrame(data); // 先镜像：补同步的回帧没有 target，也要落缓存
  const item = _pwdReqs.get(reqId);
  if (!item) return;
  _pwdReqs.delete(reqId); // pwd 回帧全部一次性
  deliverPwdFrame(item.target, data);
}

// 在途请求还没等到回帧连接就断了：写回退成本地 pending（重连补同步），读回退缓存，
// 两者都兜不住才回错误帧。绝不静默丢请求。
async function failPwdInFlight(item, reqId, message) {
  if (!item) return;
  const frame = item.frame || {};
  try {
    if (frame.type === 'pwd_save' && _pwdCacheReady()) {
      const entry = await pwdCacheSaveOffline(frame);
      if (entry) {
        deliverPwdFrame(item.target, {
          type: 'pwd_save_result', reqId, ok: true, offline: true, entry: pwdCacheMeta(entry),
        });
        return;
      }
    } else if (frame.type === 'pwd_delete' && _pwdCacheReady()) {
      await Promise.all([
        pwdCacheRemove(frame.id).catch(() => {}),
        pwdCacheAddTombstone(frame.id).catch(() => {}),
      ]);
      deliverPwdFrame(item.target, {
        type: 'pwd_delete_result', reqId, ok: true, offline: true, id: frame.id,
      });
      return;
    } else {
      const cached = await _offlinePwdFrame(frame);
      if (cached) { deliverPwdFrame(item.target, cached); return; }
    }
  } catch (_) { /* 缓存不可用：落回下面的错误帧 */ }
  deliverPwdFrame(item.target, { type: 'pwd_error', reqId, message });
}

function failPendingPwdRequests(message) {
  if (_pwdReqs.size === 0) return;
  const entries = Array.from(_pwdReqs.entries());
  _pwdReqs.clear();
  for (const [reqId, item] of entries) {
    void failPwdInFlight(item, reqId, message);
  }
}

// --- 身份验证器（TOTP，经 WS 通道代理，reqId 多路复用） ---
// 与 pwd 同形，但**无离线镜像**：服务端回帧只含当前码（永不含密钥），本机没有可
// 离线生成码的材料，连不上服务端就如实报错。回帧全部一次性：投递后即摘映射。
// ⚠ totp_save 帧含明文密钥：本段不得 console.log 任何 frame/data 内容。
const _totpReqs = new Map(); // reqId -> target
const TOTP_QUEUE_TIMEOUT_MS = 8000;
const _totpFrameQueue = []; // { reqId, frame, target, timer }
let _totpQueueTimer = null;

function _totpReplyTarget(sender) {
  if (sender && sender._totpPort) return { kind: 'port', port: sender._totpPort };
  const tabId = sender && sender.tab ? sender.tab.id : null;
  return tabId != null ? { kind: 'tab', tabId } : null;
}

function deliverTotpFrame(target, data) {
  if (!target) return;
  if (target.kind === 'port') {
    try { target.port.postMessage({ cmd: 'totp_frame', data }); } catch (_) { /* popup 已关，忽略 */ }
    return;
  }
  chrome.tabs.sendMessage(Number(target.tabId), { cmd: 'totp_frame', data }).catch(() => { /* 标签页可能已关 */ });
}

function _assembleTotpFrame(msg) {
  const reqId = String(msg.reqId || ('totp_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8)));
  const frame = { type: msg.cmd, reqId };
  if (msg.cmd === 'totp_list') {
    if (msg.domain) frame.domain = String(msg.domain);
  }
  if (msg.cmd === 'totp_get' || msg.cmd === 'totp_delete' || msg.cmd === 'totp_update') {
    frame.id = msg.id;
  }
  if (msg.cmd === 'totp_update') {
    // 编辑展示字段（**不含 secret**）：只传要改的字段，服务端按 id 定位、不新增行。
    if (msg.issuer !== undefined) frame.issuer = String(msg.issuer || '');
    if (msg.account !== undefined) frame.account = String(msg.account || '');
    if (msg.label !== undefined) frame.label = String(msg.label || '');
    if (msg.note !== undefined) frame.note = String(msg.note || '');
    if (msg.url !== undefined) frame.url = String(msg.url || '');
    if (msg.domains !== undefined) frame.domains = msg.domains;
  }
  if (msg.cmd === 'totp_by_domain') {
    frame.domain = String(msg.domain || '');
  }
  if (msg.cmd === 'totp_subscribe') {
    // 订阅翻转推送：空 domain = 全部。服务端立即回一份快照 + 之后每次翻转推 totp_push。
    frame.domain = String(msg.domain || '');
  }
  if (msg.cmd === 'totp_refresh_codes') {
    // 只刷到期码：批量 id，服务端仅对这些 id 解密算码（列表常驻、不整表重画）。
    frame.ids = Array.isArray(msg.ids) ? msg.ids.map(Number).filter(Number.isFinite) : [];
  }
  if (msg.cmd === 'totp_save') {
    // secret 可能是裸 base32 或整条 otpauth:// URI（服务端解析）。
    frame.secret = String(msg.secret || '');
    frame.issuer = String(msg.issuer || '');
    frame.account = String(msg.account || '');
    frame.label = String(msg.label || '');
    frame.note = String(msg.note || '');
    frame.url = String(msg.url || '');
    if (msg.domains) frame.domains = msg.domains;
    if (msg.digits) frame.digits = msg.digits;
    if (msg.period) frame.period = msg.period;
    if (msg.algorithm) frame.algorithm = msg.algorithm;
  }
  if (msg.cmd === 'totp_import') {
    frame.content = String(msg.content || '');   // 每行一条 otpauth:// 的纯文本
  }
  if (msg.cmd === 'totp_export') {
    frame.password = String(msg.password || ''); // 二次鉴权；含明文密钥的导出回帧
  }
  return { reqId, frame };
}

function _clearTotpQueueItemTimer(item) {
  if (item && item.timer !== null && item.timer !== undefined) {
    clearTimeout(item.timer);
    item.timer = null;
  }
}

// 未就绪时排队等 auth_ok 续发（8s 兜底）；与 pwd 队列同款但不带 cacheFallback
// （TOTP 无本地镜像可回退，超时只有错误帧一条路）。
function queueTotpFrame(reqId, frame, target) {
  const item = { reqId, frame, target, timer: null };
  _totpFrameQueue.push(item);
  if (_totpQueueTimer === null) {
    _totpQueueTimer = setTimeout(() => {
      _totpQueueTimer = null;
      if (!authenticated) failTotpFrameQueue('连接已断开，操作未完成；连上后重试即可');
      else flushTotpFrameQueue();
    }, TOTP_QUEUE_TIMEOUT_MS);
  }
}

function flushTotpFrameQueue() {
  while (_totpFrameQueue.length) {
    if (!ws || ws.readyState !== WebSocket.OPEN || !authenticated) return;
    const item = _totpFrameQueue.shift();
    _clearTotpQueueItemTimer(item);
    _totpReqs.set(item.reqId, item.target);
    try {
      ws.send(JSON.stringify(item.frame));
    } catch (e) {
      _totpReqs.delete(item.reqId);
      deliverTotpFrame(item.target, { type: 'totp_error', reqId: item.reqId, message: '连接已断开，操作未完成；连上后重试即可' });
    }
  }
  if (_totpQueueTimer !== null) { clearTimeout(_totpQueueTimer); _totpQueueTimer = null; }
}

function failTotpFrameQueue(message) {
  while (_totpFrameQueue.length) {
    const item = _totpFrameQueue.shift();
    _clearTotpQueueItemTimer(item);
    deliverTotpFrame(item.target, { type: 'totp_error', reqId: item.reqId, message });
  }
  if (_totpQueueTimer !== null) { clearTimeout(_totpQueueTimer); _totpQueueTimer = null; }
}

function sendTotpFrame(msg, sender) {
  const target = _totpReplyTarget(sender);
  if (!target) return { ok: false, error: 'no reply target' };
  const ready = Boolean(ws && ws.readyState === WebSocket.OPEN && authenticated);
  if (!ready) {
    // 与 pwd 同款：SW 刚被唤醒时 connecting/ws 还没就位，别把唤醒窗口误判成
    // 「未认证」直接失败——踢一次连接，排队等 auth_ok 续发。
    if (!connecting && !(ws && ws.readyState <= WebSocket.OPEN)) {
      try { void connectWS(); } catch (_) { /* 连接失败由 8s 兜底定时器收尾 */ }
    }
    const { reqId, frame } = _assembleTotpFrame(msg);
    queueTotpFrame(reqId, frame, target);
    return { ok: true, reqId, queued: true };
  }
  const { reqId, frame } = _assembleTotpFrame(msg);
  _totpReqs.set(reqId, target);
  if (_totpReqs.size > 200) {
    _totpReqs.delete(_totpReqs.keys().next().value);
  }
  try {
    ws.send(JSON.stringify(frame));
  } catch (e) {
    _totpReqs.delete(reqId);
    return { ok: false, error: '连接已断开，操作未完成；连上后重试即可' };
  }
  return { ok: true, reqId };
}

function forwardTotpFrame(data) {
  // 服务端主动推的帧（totp_push，无 reqId）：扇给所有开着的验证器 popup。
  // 这是**唯一的数据入口**——订阅后服务端立即推内存快照（首帧），此后每次翻转推新码。
  if (data && data.type === 'totp_push' && !data.reqId) {
    broadcastTotpPush(data);
    return;
  }
  const reqId = String(data.reqId || '');
  // _totpReqs 的值就是 target 本体（与 failPendingTotpRequests 同一约定）。
  const target = _totpReqs.get(reqId);
  if (!target) return;
  _totpReqs.delete(reqId); // 回帧全部一次性
  deliverTotpFrame(target, data);
}

// 在途 totp 请求断连收尾：无离线算码能力，只有错误帧一条路。
function failPendingTotpRequests(message) {
  if (_totpReqs.size === 0 && _totpFrameQueue.length === 0) return;
  const entries = Array.from(_totpReqs.entries());
  _totpReqs.clear();
  for (const [reqId, target] of entries) {
    deliverTotpFrame(target, { type: 'totp_error', reqId, message });
  }
  failTotpFrameQueue(message);
}

// ── 重连补同步：把离线期间落本地的写补送服务端 ──────────────────────────────
// 这些帧没有 target（不经用户请求发起），回帧走 _mirrorPwdFrame 的镜像分支：
// save 命中 (domain,username) 提升为服务端行并清 pending，delete 摘 tombstone。
// 值记 { kind, id }：服务端**明确拒绝**（配额超限/校验不过）时据此停止重试，
// 否则每次重连都会重发同一条永远失败的写。
const _pwdSyncReqs = new Map(); // reqId -> { kind: 'save' | 'delete', id }

function _pwdSyncSend(frame, kind, id) {
  const socket = ws;
  if (!socket || socket.readyState !== WebSocket.OPEN || !authenticated) return false;
  const reqId = 'pwd_sync_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
  frame.reqId = reqId;
  _pwdSyncReqs.set(reqId, { kind, id });
  if (_pwdSyncReqs.size > 200) _pwdSyncReqs.delete(_pwdSyncReqs.keys().next().value);
  try {
    socket.send(JSON.stringify(frame));
    return true;
  } catch (_) {
    _pwdSyncReqs.delete(reqId);
    return false;
  }
}

// auth_ok 后调用。只在缓存可用（pwd_cache.js 已加载且能加解密）时工作。
// 服务端按 (owner,domain,username) upsert、按 id 删除，都是幂等的，重连多发无害。
async function syncPwdCache() {
  if (!_pwdCacheReady()) return;
  let pending = [];
  let tombstones = [];
  try {
    pending = await pwdCachePending();
    tombstones = await pwdCacheTombstones();
  } catch (_) { return; } // 缓存不可用（key 丢失/存储失败）：跳过补同步，不阻断连接
  for (const entry of pending) {
    if (!_pwdSyncSend({
      type: 'pwd_save', domain: entry.domain, username: entry.username,
      password: entry.password || '', url: entry.url || '',
    }, 'save', entry.id)) return; // 连接又断了：剩下的下次重连再补
  }
  for (const id of tombstones) {
    if (!_pwdSyncSend({ type: 'pwd_delete', id }, 'delete', id)) return;
  }
}

// popup 没有 sender.tab：经 name='pwd' 的长期 port 收发密码帧，复用同一张
// 派发表（handleExtMessage），因此 pwd_* 的校验与 sendPwdFrame 只有一份。
try {
  chrome.runtime.onConnect.addListener((port) => {
    if (!port || port.name !== 'pwd') return;
    port.onMessage.addListener((msg) => {
      handleExtMessage(msg, { _pwdPort: port }).then((resp) => {
        try { port.postMessage({ cmd: 'pwd_ack', reqId: msg && msg.reqId, resp }); } catch (_) { /* popup 已关 */ }
      });
    });
  });
} catch (_) {}

// 身份验证器 popup 通道（name='totp'）：与 pwd 同款长期 port（popup 无 sender.tab）。
// _totpPorts 登记所有活着的服务端推送目标：服务端每次翻转推来的无 reqId 帧
// （totp_push）要扇给每个开着的 popup，不能按 reqId 定向（那是一次性请求用的）。
const _totpPorts = new Set();
try {
  chrome.runtime.onConnect.addListener((port) => {
    if (!port || port.name !== 'totp') return;
    _totpPorts.add(port);
    port.onMessage.addListener((msg) => {
      handleExtMessage(msg, { _totpPort: port }).then((resp) => {
        try { port.postMessage({ cmd: 'totp_ack', reqId: msg && msg.reqId, resp }); } catch (_) { /* popup 已关 */ }
      });
    });
    port.onDisconnect.addListener(() => { _totpPorts.delete(port); });
  });
} catch (_) {}

// 服务端主动推的 totp 帧（无 reqId）：扇给所有开着的验证器 popup。
function broadcastTotpPush(data) {
  for (const port of _totpPorts) {
    try { port.postMessage({ cmd: 'totp_frame', data }); } catch (_) { _totpPorts.delete(port); }
  }
}

// 断连收尾：onclose 与 pong 判死（keepalive 强杀半开连接）共用。置 ws=null 后
// 晚到的 onclose 会被 isCurrent() 幂等挡掉；返回被关闭的 socket 供调用方 close。
function markDisconnected() {
  const socket = ws;
  ws = null;
  authenticated = false;
  connecting = false;
  lastFrameAt = 0;
  chrome.alarms.clear('tmwd-ws-keepalive');
  failPendingChatRequests('连接已断开，消息未发出；连上后重试即可');
  failPendingPwdRequests('连接已断开，消息未发出；连上后重试即可'); // 密码保存卡在断连时也要给用户可见失败
  failPendingTotpRequests('连接已断开，消息未发出；连上后重试即可');
  failChatFrameQueue('连接已断开，消息未发出；连上后重试即可'); // 握手中排队未发的帧同样失败掉
  void loadBridgeConfig().then(() => {
    void broadcastBridgeStatus(); // 断开了：推给所有 tab，徽标变灰/认证中
    if (!bridgeConfig.bridgeEnabled || !bridgeConfig.clientToken || reconnectBlocked) return;
    // 撞过 already_connected 时走长退避（旧槽位还没释放，5s 撞一次纯浪费）；
    // 正常断开仍用 5s probe（服务端存活即重连，无需等）。
    if (alreadyConnectedBackoff > 0) scheduleProbe(ALREADY_CONNECTED_BACKOFF_MS[alreadyConnectedBackoff] / 60000);
    else scheduleProbe();
  });
  return socket;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleExtMessage(msg, sender).then(sendResponse);
  return true;
});

const netCaptures = new Map();
const NET_BODY_LIMIT = 1024 * 1024;

function getCapture(tabId) {
  return netCaptures.get(String(tabId));
}

async function attachDebugger(tabId) {
  if (!tabId) throw new Error('no tabId');
  const cap = getCapture(tabId);
  if (cap && cap.attached) return { reused: true };
  await chrome.debugger.attach({ tabId }, '1.3');
  return { reused: false };
}

async function detachDebugger(tabId, attachState) {
  const cap = getCapture(tabId);
  if (cap && cap.attached) return;
  if (!attachState || attachState.reused) return;
  try { await chrome.debugger.detach({ tabId }); } catch (_) {}
}

// 概要字段：只留定位一条请求所需的最小信息。body/headers 不进概要——AI 先看
// 「有哪些请求、各自 method+url+status+mime+大小」，需要哪条再按 requestId 取全量。
function publicRequestSummary(req) {
  return {
    requestId: req.requestId,
    method: req.method || '',
    url: req.url || '',
    status: req.status,
    type: req.type || '',
    mimeType: req.mimeType || '',
    durationMs: req.durationMs,
    encodedDataLength: req.encodedDataLength || 0,
    responseBodyLength: typeof req.responseBody === 'string' ? req.responseBody.length : null,
    responseBodyTruncated: !!req.responseBodyTruncated,
    responseBodyError: req.responseBodyError || null,
  };
}

// 全量字段：AI 明确要某条请求的正文/头部时才取（按 requestId 精确命中，只回一条）。
function publicRequestFull(req) {
  return {
    requestId: req.requestId,
    method: req.method || '',
    url: req.url || '',
    status: req.status,
    type: req.type || '',
    mimeType: req.mimeType || '',
    protocol: req.protocol || '',
    initiator: req.initiator,
    statusText: req.statusText,
    remoteIPAddress: req.remoteIPAddress,
    remotePort: req.remotePort,
    encodedDataLength: req.encodedDataLength || 0,
    durationMs: req.durationMs,
    requestHeaders: req.requestHeaders || {},
    responseHeaders: req.responseHeaders || {},
    responseBody: req.responseBody || '',
    responseBodyBase64Encoded: !!req.responseBodyBase64Encoded,
    responseBodyTruncated: !!req.responseBodyTruncated,
    responseBodyError: req.responseBodyError || null,
  };
}

// 概要快照：默认只回 requestId 索引（url_pattern 过滤后）+ WS/SSE 帧概要，不把
// 每条请求的 body/headers 灌给 AI。AI 需要全量时用 requestId 逐条取（见 handleNetGet）。
function captureSummary(cap) {
  return {
    capturing: cap.capturing,
    tabId: cap.tabId,
    startedAt: cap.startedAt,
    stoppedAt: cap.stoppedAt || null,
    total: cap.requests.size,
    requests: Array.from(cap.requests.values()).map(publicRequestSummary),
    // WebSocket / SSE 帧概要：只回最近 50 帧的 {kind,event,requestId,url,方向,长度}，
    // 不把帧 payload 一次性灌给 AI（payload 按需逐帧取）。
    wsFrames: (cap.wsFrames || []).slice(-50).map(wsFrameSummary),
    wsTotal: cap.wsTotal || 0,
  };
}

function wsFrameSummary(frame) {
  const payload = typeof frame.payload === 'string' ? frame.payload : (frame.data || '');
  return {
    kind: frame.kind,
    event: frame.event,
    requestId: frame.requestId,
    url: frame.url || '',
    opcode: frame.opcode,
    name: frame.name,
    length: typeof payload === 'string' ? payload.length : (payload ? String(payload).length : 0),
  };
}

function notifyCaptureBanner(tabId, action, count) {
  chrome.tabs.sendMessage(tabId, { cmd: 'network_capture_banner', action, count }).catch(() => {});
}

function updateCaptureBanner(tabId, force) {
  const cap = getCapture(tabId);
  if (!cap) return;
  const now = Date.now();
  if (!force && now - cap.lastBannerUpdate < 400) return;
  cap.lastBannerUpdate = now;
  notifyCaptureBanner(Number(tabId), cap.capturing ? 'update' : 'hide', cap.requests.size);
}

function shouldKeepRequest(cap, url) {
  if (!cap.urlPattern) return true;
  return String(url || '').includes(cap.urlPattern);
}

// push 模式：把一条抓到的请求推给服务端（site runner 用它 await 特定响应，
// 不必轮询 net_get）。仅当 cap.push 且连接可用时发；发送失败静默——拉取路径仍在。
function pushNetEvent(cap, req) {
  if (!cap || !cap.push || !cap.captureId) return;
  if (!req || req._pushed) return;
  req._pushed = true;
  const socket = ws;
  if (!socket || socket.readyState !== WebSocket.OPEN || !authenticated) return;
  try {
    socket.send(JSON.stringify({
      type: 'net_event',
      captureId: cap.captureId,
      request: publicRequestFull(req),
    }));
  } catch (_) { /* 连接已断：等待方靠超时兜底 */ }
}

// push 模式：把一条 WS/SSE 帧推给服务端（site runner 等待方靠它拿明文帧，
// 不必轮询 net_get）。pushNetEvent 同形：cap.push + captureId 校验，满队/断连静默。
function pushWsEvent(cap, frame) {
  if (!cap || !cap.push || !cap.captureId || !frame || frame._pushed) return;
  frame._pushed = true;
  const socket = ws;
  if (!socket || socket.readyState !== WebSocket.OPEN || !authenticated) return;
  try {
    socket.send(JSON.stringify({
      type: 'net_event',
      captureId: cap.captureId,
      wsFrame: frame,
    }));
  } catch (_) { /* 连接已断：等待方靠超时兜底 */ }
}

function ensureRequest(cap, requestId) {
  let req = cap.requests.get(requestId);
  if (!req) {
    req = { requestId, events: [] };
    cap.requests.set(requestId, req);
  }
  return req;
}

async function readResponseBody(tabId, cap, requestId) {
  try {
    const body = await chrome.debugger.sendCommand({ tabId }, 'Network.getResponseBody', { requestId });
    const req = cap.requests.get(requestId);
    if (!req) return;
    const text = String(body.body || '');
    req.responseBody = text.length > NET_BODY_LIMIT ? text.slice(0, NET_BODY_LIMIT) : text;
    req.responseBodyBase64Encoded = !!body.base64Encoded;
    req.responseBodyTruncated = text.length > NET_BODY_LIMIT;
  } catch (e) {
    const req = cap.requests.get(requestId);
    if (req) req.responseBodyError = e.message || String(e);
  }
}

function handleDebuggerEvent(source, method, params) {
  const tabId = source.tabId;
  if (!tabId) return;
  const cap = getCapture(tabId);
  if (!cap || !cap.capturing) return;
  const requestId = params && params.requestId;

  // ── WebSocket / SSE 帧：独立通道，不走 requestId/requests map ──
  // Network.enable 就会发这些事件（无需额外 domain），此前落进 else{return} 被
  // 全部丢弃——聊天类 SPA 的核心链路是 WS/SSE，agent 试过 network_start 抓不到
  // 帧，才学的手写 WebSocket.prototype hook。这里补齐后 watch_network 一个入口
  // 就能拿全 HTTP + WS/SSE，不再需要注入页面 JS。
  if (method === 'Network.webSocketCreated') {
    if (cap.urlPattern && !String(params.url || '').includes(cap.urlPattern)) return;
    cap.wsFrames.push({
      kind: 'websocket', event: 'created', requestId: params.requestId,
      url: params.url || '', initiator: params.initiator || null,
      timestamp: params.timestamp || null,
    });
    cap.wsTotal++;
    if (cap.push && cap.captureId) pushWsEvent(cap, cap.wsFrames[cap.wsFrames.length - 1]);
    return;
  }
  if (method === 'Network.webSocketClosed') {
    cap.wsFrames.push({
      kind: 'websocket', event: 'closed', requestId: params && params.requestId,
      timestamp: params && params.timestamp || null,
    });
    return;
  }
  if (method === 'Network.webSocketFrameSent' || method === 'Network.webSocketFrameReceived') {
    const payload = params.response && params.response.payloadData || '';
    cap.wsFrames.push({
      kind: 'websocket',
      event: method === 'Network.webSocketFrameSent' ? 'sent' : 'received',
      requestId: params.requestId,
      opcode: params.response && params.response.opcode,
      // 帧数据可能很大：只保留尾部窗口，超限的整帧替换成截断标记。
      payload: payload.length > NET_BODY_LIMIT ? payload.slice(0, NET_BODY_LIMIT) + '…[TRUNCATED]' : payload,
      timestamp: params.timestamp || null,
    });
    cap.wsTotal++;
    if (cap.wsFrames.length > 500) cap.wsFrames.splice(0, cap.wsFrames.length - 500);
    if (cap.push && cap.captureId) pushWsEvent(cap, cap.wsFrames[cap.wsFrames.length - 1]);
    updateCaptureBanner(tabId, false);
    return;
  }
  if (method === 'Network.eventSourceMessageReceived') {
    cap.wsFrames.push({
      kind: 'sse', event: 'message',
      requestId: params.requestId,
      name: params.eventName || 'message', data: String(params.data || ''),
      timestamp: params.timestamp || null,
    });
    cap.wsTotal++;
    if (cap.wsFrames.length > 500) cap.wsFrames.splice(0, cap.wsFrames.length - 500);
    if (cap.push && cap.captureId) pushWsEvent(cap, cap.wsFrames[cap.wsFrames.length - 1]);
    return;
  }

  if (!requestId) return;

  if (method === 'Network.requestWillBeSent') {
    if (!shouldKeepRequest(cap, params.request && params.request.url)) return;
    const req = ensureRequest(cap, requestId);
    req.events.push(method);
    req.url = params.request.url;
    req.method = params.request.method;
    req.requestHeaders = params.request.headers || {};
    req.type = params.type || req.type;
    req.initiator = params.initiator || null;
    req.wallTime = params.wallTime || null;
    req.startTimestamp = params.timestamp || null;
  } else if (method === 'Network.responseReceived') {
    const req = cap.requests.get(requestId);
    if (!req) return;
    req.events.push(method);
    req.type = params.type || req.type;
    req.responseUrl = params.response && params.response.url;
    req.status = params.response && params.response.status;
    req.statusText = params.response && params.response.statusText;
    req.mimeType = params.response && params.response.mimeType;
    req.protocol = params.response && params.response.protocol;
    req.remoteIPAddress = params.response && params.response.remoteIPAddress;
    req.remotePort = params.response && params.response.remotePort;
    req.responseHeaders = params.response && params.response.headers || {};
    req.responseTimestamp = params.timestamp || null;
  } else if (method === 'Network.loadingFinished') {
    const req = cap.requests.get(requestId);
    if (!req) return;
    req.events.push(method);
    req.endTimestamp = params.timestamp || null;
    req.encodedDataLength = params.encodedDataLength || 0;
    req.durationMs = req.startTimestamp && req.endTimestamp ? Math.round((req.endTimestamp - req.startTimestamp) * 1000) : null;
    req.finished = true;
    if (!req._finished) {
      req._finished = true;
      // push 在拿到响应体之后发：等待方要的是 body，先发空壳没有意义。
      readResponseBody(Number(tabId), cap, requestId).finally(() => {
        updateCaptureBanner(tabId, true);
        pushNetEvent(cap, req);
      });
    }
  } else if (method === 'Network.loadingFailed') {
    const req = cap.requests.get(requestId);
    if (!req) return;
    req.events.push(method);
    req.endTimestamp = params.timestamp || null;
    req.durationMs = req.startTimestamp && req.endTimestamp ? Math.round((req.endTimestamp - req.startTimestamp) * 1000) : null;
    req.failed = true;
    req.errorText = params.errorText || '';
    req.canceled = !!params.canceled;
    // 失败也要推：否则等待方要等到超时才收场，分不清「没发请求」和「请求失败」。
    pushNetEvent(cap, req);
  } else {
    return;
  }
  updateCaptureBanner(tabId, false);
}

chrome.debugger.onEvent.addListener(handleDebuggerEvent);
chrome.debugger.onDetach.addListener((source) => {
  const tabId = source && source.tabId;
  if (!tabId) return;
  const cap = getCapture(tabId);
  if (!cap) return;
  cap.capturing = false;
  cap.attached = false;
  cap.stoppedAt = Date.now();
  notifyCaptureBanner(tabId, 'hide', cap.requests.size);
});

async function handleNetStart(msg, sender) {
  const tabId = msg.tabId || sender.tab?.id;
  if (!tabId) return { ok: false, error: 'no tabId' };
  const key = String(tabId);
  const existing = getCapture(tabId);
  if (existing && existing.capturing) {
    notifyCaptureBanner(Number(tabId), 'show', existing.requests.size);
    return { ok: true, data: captureSummary(existing) };
  }
  const cap = {
    tabId: Number(tabId),
    capturing: true,
    attached: false,
    startedAt: Date.now(),
    stoppedAt: null,
    requests: new Map(),
    // WebSocket / SSE 帧环形缓冲：只留最近 500 帧（见 handleDebuggerEvent），
    // wsTotal 记总数不受缓冲上限影响，便于「抓到了但被挤掉」时仍能看出量级。
    wsFrames: [],
    wsTotal: 0,
    urlPattern: String(msg.urlPattern || ''),
    lastBannerUpdate: 0,
    // push 模式：每条抓到的请求实时推给服务端（site runner 的 await 通道）。
    // 默认关，net_get 拉取路径不受影响。
    push: !!msg.push,
    captureId: String(msg.captureId || ''),
  };
  netCaptures.set(key, cap);
  try {
    await chrome.debugger.attach({ tabId: Number(tabId) }, '1.3');
    cap.attached = true;
    await chrome.debugger.sendCommand({ tabId: Number(tabId) }, 'Network.enable', {
      maxTotalBufferSize: 100000000,
      maxResourceBufferSize: 10000000,
    });
    notifyCaptureBanner(Number(tabId), 'show', 0);
    return { ok: true, data: captureSummary(cap) };
  } catch (e) {
    netCaptures.delete(key);
    try { await chrome.debugger.detach({ tabId: Number(tabId) }); } catch (_) {}
    notifyCaptureBanner(Number(tabId), 'hide', 0);
    return { ok: false, error: e.message || String(e) };
  }
}

// 读抓包数据。默认只回**概要**（requestId 索引 + WS 帧概要），不灌 body/headers。
// AI 拿到概要后按需深入：
//   - requestId=xxx  → 精确取该请求的全量（headers + body）
//   - frameIndex=n   → 取第 n 个 WS/SSE 帧的 payload
//   - search=xxx     → 只回 url 含该子串的请求概要（概要级过滤，省得 AI 自己遍历）
//   - full=true      → 兼容旧行为：全量返回（供需要一次性拿全的调用方，慎用）
function handleNetGet(msg, sender) {
  const tabId = msg.tabId || sender.tab?.id;
  if (!tabId) return { ok: false, error: 'no tabId' };
  const cap = getCapture(tabId);
  if (!cap) return { ok: true, data: { capturing: false, tabId: Number(tabId), total: 0, requests: [], wsFrames: [], wsTotal: 0 } };

  // 单条请求全量
  if (msg.requestId) {
    const req = cap.requests.get(String(msg.requestId));
    if (!req) return { ok: false, error: 'requestId not found' };
    return { ok: true, data: { capturing: cap.capturing, request: publicRequestFull(req) } };
  }
  // 单帧 payload
  if (msg.frameIndex != null) {
    const frames = cap.wsFrames || [];
    const idx = Number(msg.frameIndex);
    if (!Number.isInteger(idx) || idx < 0 || idx >= frames.length) {
      return { ok: false, error: 'frameIndex out of range', frameCount: frames.length };
    }
    return { ok: true, data: { capturing: cap.capturing, frame: frames[idx], frameIndex: idx } };
  }
  // 概要级过滤
  const search = String(msg.search || '');
  if (search) {
    const all = Array.from(cap.requests.values());
    const matched = all.filter((r) => String(r.url || '').includes(search));
    return {
      ok: true,
      data: {
        capturing: cap.capturing,
        tabId: cap.tabId,
        total: matched.length,
        totalAll: all.length,
        search,
        requests: matched.map(publicRequestSummary),
      },
    };
  }
  // 全量（兼容旧调用方）
  if (msg.full) {
    return {
      ok: true,
      data: {
        ...captureSummary(cap),
        requests: Array.from(cap.requests.values()).map(publicRequestFull),
        wsFrames: cap.wsFrames || [],
      },
    };
  }
  return { ok: true, data: captureSummary(cap) };
}

async function handleNetStop(msg, sender) {
  const tabId = msg.tabId || sender.tab?.id;
  if (!tabId) return { ok: false, error: 'no tabId' };
  const cap = getCapture(tabId);
  if (!cap) {
    notifyCaptureBanner(Number(tabId), 'hide', 0);
    return { ok: true, data: { capturing: false, tabId: Number(tabId), total: 0, requests: [] } };
  }
  cap.capturing = false;
  cap.attached = false;
  cap.stoppedAt = Date.now();
  try { await chrome.debugger.sendCommand({ tabId: Number(tabId) }, 'Network.disable'); } catch (_) {}
  try { await chrome.debugger.detach({ tabId: Number(tabId) }); } catch (_) {}
  notifyCaptureBanner(Number(tabId), 'hide', cap.requests.size);
  return { ok: true, data: captureSummary(cap) };
}

async function handleBatch(msg, sender) {
  const R = [];
  let attached = null;
  let attachState = null;
  const resolve$N = (params) => JSON.parse(JSON.stringify(params || {}).replace(/"\$(\d+)\.([^"]+)"/g,
    (_, i, path) => { let v = R[+i]; for (const k of path.split('.')) v = v[k]; return JSON.stringify(v); }));
  try {
    for (const c of msg.commands) {
      if (c.tabId === undefined && msg.tabId !== undefined) c.tabId = msg.tabId;
      if (c.cmd === 'tabs') {
        const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url));
        R.push({ ok: true, data: tabs.map(t => ({ id: t.id, url: t.url, title: t.title, active: t.active, windowId: t.windowId })) });
      } else if (c.cmd === 'cdp') {
        const tabId = c.tabId || msg.tabId || sender.tab?.id;
        if (attached !== tabId) {
          if (attached) { await detachDebugger(attached, attachState); attached = null; attachState = null; }
          attachState = await attachDebugger(tabId);
          attached = tabId;
        }
        R.push(await chrome.debugger.sendCommand({ tabId }, c.method, resolve$N(c.params)));
      } else {
        R.push({ ok: false, error: 'unknown cmd: ' + c.cmd });
      }
    }
    if (attached) await detachDebugger(attached, attachState);
    return { ok: true, results: R };
  } catch (e) {
    if (attached) try { await detachDebugger(attached, attachState); } catch (_) {}
    return { ok: false, error: e.message, results: R };
  }
}

async function handleCDP(msg, sender) {
  const tabId = msg.tabId || sender.tab?.id;
  if (!tabId) return { ok: false, error: 'no tabId' };
  // 截图前隐藏扩展注入的聊天面板/气泡，截完恢复——否则面板会被一起截进画面。
  const hidePanel = msg.method === 'Page.captureScreenshot';
  let panelHidden = false;
  if (hidePanel) {
    panelHidden = await sendTabMessage(tabId, { cmd: 'hide_panel_for_screenshot' }).catch(() => false);
    if (panelHidden) await new Promise((r) => setTimeout(r, 60)); // 等一帧让 display:none 生效
  }
  const cap = getCapture(tabId);
  try {
    if (cap && cap.attached) {
      try {
        const result = await chrome.debugger.sendCommand({ tabId }, msg.method, msg.params || {});
        return { ok: true, data: result };
      } catch (e) { return { ok: false, error: e.message }; }
    }
    let attachState = null;
    try {
      attachState = await attachDebugger(tabId);
      const result = await chrome.debugger.sendCommand({ tabId }, msg.method, msg.params || {});
      await detachDebugger(tabId, attachState);
      return { ok: true, data: result };
    } catch (e) {
      await detachDebugger(tabId, attachState);
      return { ok: false, error: e.message };
    }
  } finally {
    if (panelHidden) sendTabMessage(tabId, { cmd: 'show_panel_after_screenshot' }).catch(() => {});
  }
}

// 给目标 tab 的 content script 发一次性消息；content script 不在（internal 页）
// 或没装面板时返回 false，调用方据此不阻塞截图。
function sendTabMessage(tabId, message) {
  return new Promise((resolve) => {
    try {
      chrome.tabs.sendMessage(tabId, message, (resp) => {
        if (chrome.runtime.lastError) { resolve(false); return; }
        resolve(resp && resp.ok !== false);
      });
    } catch (_) { resolve(false); }
  });
}
// Filter out chrome:// and other internal tabs that can't be scripted
const isScriptable = url => url && /^https?:/.test(url);

// --- Shared page/CDP script builder core ---
function buildExecScript(code, errorHandler) {
  return `(async () => {
    function smartProcessResult(result) {
      if (result === null || result === undefined || typeof result !== 'object') return result;
      try { if (result.window === result && result.document) return '[Window: ' + (result.location?.href || 'about:blank') + ']'; } catch(_){}
      if (typeof jQuery !== 'undefined' && result instanceof jQuery) {
        const elements = []; for (let i = 0; i < result.length; i++) { if (result[i] && result[i].nodeType === 1) elements.push(result[i].outerHTML); } return elements;
      }
      if (result instanceof NodeList || result instanceof HTMLCollection) {
        const elements = []; for (let i = 0; i < result.length; i++) { if (result[i] && result[i].nodeType === 1) elements.push(result[i].outerHTML); } return elements;
      }
      if (result.nodeType === 1) return result.outerHTML;
      if (!Array.isArray(result) && typeof result === 'object' && 'length' in result && typeof result.length === 'number') {
        const firstElement = result[0];
        if (firstElement && firstElement.nodeType === 1) {
          const elements = []; const length = Math.min(result.length, 100);
          for (let i = 0; i < length; i++) { const elem = result[i]; if (elem && elem.nodeType === 1) elements.push(elem.outerHTML); } return elements;
        }
      }
      try { return JSON.parse(JSON.stringify(result, function(key, value) { if (typeof value === 'object' && value !== null) { if (value.nodeType === 1) return value.outerHTML; if (value === window || value === document) return '[Object]'; try { if (value.window === value && value.document) return '[Window]'; } catch(_){} } return value; })); } catch (e) { return '[无法序列化: ' + e.message + ']'; }
    }
    try {
      const jsCode = ${JSON.stringify(code)}.trim();
      const lines = jsCode.split(/\\r?\\n/).filter(l => l.trim());
      const lastLine = lines.length > 0 ? lines[lines.length - 1].trim() : '';
      const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
      let r;
      function _air(c) { const ls = c.split(/\\r?\\n/); let i = ls.length - 1; while (i >= 0 && !ls[i].trim()) i--; if (i < 0) return c; const t = ls[i].trim(); if (/^(return |return;|return$|let |const |var |if |if\\(|for |for\\(|while |while\\(|switch|try |throw |class |function |async |import |export |\\/\\/|})/.test(t)) return c; ls[i] = ls[i].match(/^(\\s*)/)[1] + 'return ' + t; return ls.join('\\n'); }
      if (lastLine.startsWith('return')) {
        r = await (new AsyncFunction(jsCode))();
      } else {
        try { r = eval(jsCode); if (r instanceof Promise) r = await r; } catch (e) {
          if (e instanceof SyntaxError && (/return/i.test(e.message) || /await/i.test(e.message))) { r = await (new AsyncFunction(_air(jsCode)))(); } else throw e;
        }
      }
      return { ok: true, data: smartProcessResult(r) };
    } catch (e) {
      ${errorHandler}
    }
  })()`;
}

function buildPageScript(code) {
  return buildExecScript(code, `
      const errMsg = e.message || String(e);
      return { ok: false, error: { name: e.name || 'Error', message: errMsg, stack: e.stack || '' },
        csp: errMsg.includes('Refused to evaluate') || errMsg.includes('unsafe-eval') || errMsg.includes('Content Security Policy') };
  `);
}

function buildCdpScript(code) {
  return buildExecScript(code, `
      return { ok: false, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' } };
  `);
}

// --- WebSocket Client for TMWebDriver ---
let ws = null;

// already_connected 退避表（毫秒）。旧槽位靠服务端空闲读超时释放，5s 撞一次无效
// 且长时间把徽标停在 auth_error。逐步拉长间隔给服务端时间，最多 60s（与空闲
// 超时同量级，撞到释放即接管）。auth_ok 成功后 alreadyConnectedBackoff 清零。
const ALREADY_CONNECTED_BACKOFF_MS = [5000, 15000, 30000, 60000];
let alreadyConnectedBackoff = 0;

function scheduleProbe(delayMin) {
  // Use chrome.alarms to survive MV3 service worker suspension
  const delay = delayMin != null ? delayMin : 0.083; // 默认 ~5s
  chrome.alarms.create('tmwd-ws-probe', { delayInMinutes: delay });
}

function scheduleKeepalive() {
  // Keep SW alive while WS is connected (~25s, under 30s SW timeout)
  chrome.alarms.create('tmwd-ws-keepalive', { delayInMinutes: 0.4 }); // ~24s
}

async function isServerAlive() {
  try {
    const config = await loadBridgeConfig();
    if (!isLocalBridge(config.bridgeUrl)) return true;
    // 本地探测：探主服务的 HTTP 健康端点（同 host:port）。
    // 注意：/mcp/cdp-bridge/session 是 WebSocket-only 路由，对它发 HTTP GET 会得到
    // 404（见服务端日志噪音），因此探活必须打一个真实返回 200 的 HTTP 路径。
    // /mcp/health 无鉴权、纯内存、稳定 200，且正是本插件要连接的 MCP Runtime 服务。
    const healthUrl = config.bridgeUrl
      .replace(/^ws:\/\//i, 'http://')
      .replace(/^wss:\/\//i, 'https://')
      .replace(/\/+$/, '')
      .replace(/\/mcp\/cdp-bridge\/session$/, '/mcp/health');
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 2000);
    const resp = await fetch(healthUrl, { signal: ctrl.signal });
    return resp.ok; // 2xx → MCP Runtime alive；否则视为不可用
  } catch (e) {
    return false; // Network error (connection refused) or timeout → server not alive
  }
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'tmwd-self-reload') {
    chrome.runtime.reload();
    return;
  }
  if (alarm.name === 'tmwd-ws-keepalive') {
    // Keepalive: ping to keep SW alive + detect dead connections
    const config = await loadBridgeConfig();
    if (!config.bridgeEnabled) {
      // 开关已关：停止保活，连接应已断开。
      return;
    }
    if (ws && ws.readyState === WebSocket.OPEN && authenticated) {
      if (lastFrameAt && Date.now() - lastFrameAt > PONG_TIMEOUT_MS) {
        // 半开死连接：ping 每次都本地发送成功，但 60s 没收到任何回帧（正常心跳
        // 下服务端每个 ping 都立刻回 pong）。主动按断连收尾——广播状态、失败
        // 挂起的对话请求、排 probe 自动重连；close 只关本地句柄，onclose 若晚到
        // 会被 isCurrent() 幂等挡掉。
        console.error('[TMWD-WS] No frame within', PONG_TIMEOUT_MS, 'ms; treating socket as dead');
        const dead = markDisconnected();
        try { dead.close(); } catch (_) {}
        return;
      }
      try { ws.send(JSON.stringify({ type: 'ping' })); } catch (_) {}
      scheduleKeepalive();
    } else {
      // Connection lost, switch to probe mode
      ws = null;
      scheduleProbe();
    }
  }
  if (alarm.name === 'tmwd-ws-probe') {
    const config = await loadBridgeConfig();
    if (!config.bridgeEnabled || !config.clientToken || reconnectBlocked) return;
    if (ws && ws.readyState <= 1) return; // Already connected/connecting
    if (await isServerAlive()) {
      console.log('[TMWD-WS] Server detected, connecting...');
      connectWS();
    } else {
      scheduleProbe(); // Server not up, keep probing
    }
  }
  // 代理租约到期：还原出网路径（见 proxy.js）。独立分支，不碰桥接逻辑。
  if (await onProxyLeaseAlarm(alarm)) return;
  // 代理通道 keepalive/重连：独立 socket，与控制通道互不影响。
  if (alarm.name === 'tmwd-proxy-keepalive') {
    await onProxyKeepalive();
    return;
  }
});

function sendSocketFrame(socket, frame) {
  if (socket !== ws || !authenticated || socket.readyState !== WebSocket.OPEN) return false;
  try {
    socket.send(JSON.stringify(frame));
    return true;
  } catch (_) {
    return false;
  }
}

async function handleWsExec(data, socket) {
  const tabId = data.tabId;
  console.log('[TMWD-WS] Exec request', data.id, 'on tab', tabId);
  if (!sendSocketFrame(socket, { type: 'ack', id: data.id })) return;
  if (!tabId) {
    sendSocketFrame(socket, { type: 'error', id: data.id, error: 'No tabId provided' });
    return;
  }
  // Use onCreated listener to reliably capture new tabs (avoids race condition with query-diff)
  const newTabIds = new Set();
  const onCreated = (tab) => { newTabIds.add(tab.id); };
  chrome.tabs.onCreated.addListener(onCreated);
  try {
    let res;
    try {
      const result = await chrome.scripting.executeScript({
        target: { tabId },
        world: 'MAIN',
        func: async (s) => await eval(s),
        args: [buildPageScript(data.code)]
      });
      res = result[0]?.result;
      if (res === null || res === undefined) {
        console.log('[TMWD-WS] executeScript returned null/undefined, treating as CSP issue');
        res = { ok: false, error: { name: 'Error', message: 'executeScript returned null (possible CSP or context issue)', stack: '' }, csp: true };
      }
    } catch (e) {
      console.log('[TMWD-WS] scripting.executeScript failed:', e.message);
      res = { ok: false, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' }, csp: true };
    }
    // CDP fallback for CSP-restricted pages
    if (res && !res.ok && res.csp) {
      console.log('[TMWD-WS] CDP fallback for tab', tabId);
      const wrappedCode = buildCdpScript(data.code);
      try {
        await chrome.debugger.attach({ tabId }, '1.3');
        const cdpRes = await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
          expression: wrappedCode, awaitPromise: true, returnByValue: true
        });
        await chrome.debugger.detach({ tabId });
        if (cdpRes.exceptionDetails) {
          const desc = cdpRes.exceptionDetails.exception?.description || 'CDP Error';
          res = { ok: false, error: { name: 'Error', message: desc, stack: desc } };
        } else {
          res = cdpRes.result.value;
        }
      } catch (cdpErr) {
        try { await chrome.debugger.detach({ tabId }); } catch (_) {}
        res = { ok: false, error: { name: 'Error', message: 'CDP fallback failed: ' + cdpErr.message, stack: '' } };
      }
    }
    // Grace period for async tab creation (e.g. link click with target=_blank)
    if (newTabIds.size === 0) await new Promise(r => setTimeout(r, 200));
    chrome.tabs.onCreated.removeListener(onCreated);
    // Get full info for captured new tabs
    const newTabs = [];
    for (const id of newTabIds) {
      try { const t = await chrome.tabs.get(id); newTabs.push({id: t.id, url: t.url, title: t.title}); } catch (_) {}
    }
    if (res?.ok) {
      sendSocketFrame(socket, { type: 'result', id: data.id, result: res.data, newTabs });
    } else {
      console.log(res);
      sendSocketFrame(socket, { type: 'error', id: data.id, error: res?.error || 'Unknown error', newTabs });
    }
  } catch (e) {
    sendSocketFrame(socket, { type: 'error', id: data.id, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' } });
  } finally {
    chrome.tabs.onCreated.removeListener(onCreated);
  }
}

async function sendReadyFrames(socket) {
  const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url));
  if (socket !== ws || !authenticated || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({
    type: 'tabs_update',
    tabs: tabs.map(t => ({ id: t.id, url: t.url, title: t.title })),
  }));
  console.log('[TMWD-WS] Authenticated; sent tabs_update with', tabs.length, 'tabs');
  scheduleKeepalive();
}

async function connectWS() {
  if (connecting || (ws && ws.readyState <= 1)) return;
  // 占位必须先于任何 await：原先先 await loadBridgeConfig() 再置 connecting，两个
  // 并发调用（SW 被消息唤醒时顶层 connectWS 正 await 配置读取，pwd/probe/onStartup
  // 又各调一次）会同时穿过守卫、各建一条 WebSocket——后到那条撞 already_connected，
  // 徽标一直显示「该客户端已在其他浏览器连接」，而先到那条成了没人认领的孤儿连接、
  // 仍占着服务端会话槽，要等服务端空闲读超时(~60s)+客户端退避(最长110s)才放出来。
  connecting = true;
  await loadBridgeConfig();
  if (!bridgeConfig.bridgeEnabled || !bridgeConfig.clientToken || reconnectBlocked) {
    connecting = false;
    return;
  }
  // await 期间可能已有别的调用建好/在建连接：二次守卫，仍然只允许一条 socket。
  if (ws && ws.readyState <= 1) { connecting = false; return; }
  authenticated = false;
  authError = null;
  // 开始拨号就推一次状态。SW 常被「点徽标/发消息」这枚消息唤醒，唤醒后各标签页的
  // 徽标仍停在断开前的旧值，而原先下一次推送要等握手结束（成功或失败）才发生——
  // 中间这段空窗在页面上就是「明明在服务端跑着，徽标却一直显示未连接」。
  void broadcastBridgeStatus();
  const epoch = ++socketEpoch;
  lastFrameAt = Date.now(); // 连接期宽限：判死计时从建连起算，避免沿用旧值秒判
  console.log('[TMWD-WS] Connecting to', bridgeWsUrl);
  let socket;
  try {
    socket = new WebSocket(bridgeWsUrl);
    ws = socket;
  } catch (e) {
    console.error('[TMWD-WS] Constructor error:', e);
    if (epoch === socketEpoch) {
      ws = null;
      connecting = false;
      void broadcastBridgeStatus(); // 构造失败不触发 onclose，手动推一次
      scheduleProbe();
    }
    return;
  }
  const isCurrent = () => ws === socket && socketEpoch === epoch;
  let connectionTimer = setTimeout(() => {
    if (!isCurrent() || socket.readyState !== WebSocket.CONNECTING) return;
    authError = { code: 'connection_timeout', message: 'bridge websocket did not open within 10 seconds' };
    reconnectBlocked = false;
    console.error('[TMWD-WS] Connection timed out');
    socket.close();
  }, CONNECTION_TIMEOUT_MS);
  let authTimer = null;
  const clearConnectionTimer = () => {
    if (connectionTimer !== null) {
      clearTimeout(connectionTimer);
      connectionTimer = null;
    }
  };
  const clearAuthTimer = () => {
    if (authTimer !== null) {
      clearTimeout(authTimer);
      authTimer = null;
    }
  };
  socket.onopen = () => {
    clearConnectionTimer();
    if (!isCurrent()) { socket.close(); return; }
    connecting = false;
    console.log('[TMWD-WS] Socket open; authenticating');
    socket.send(JSON.stringify({ type: 'auth', protocol: 2, token: bridgeConfig.clientToken }));
    authTimer = setTimeout(() => {
      if (!isCurrent() || authenticated) return;
      authError = { code: 'auth_timeout', message: 'server did not return auth_ok or auth_error within 10 seconds' };
      reconnectBlocked = false;
      console.error('[TMWD-WS] Authentication timed out');
      socket.close();
    }, AUTH_RESPONSE_TIMEOUT_MS);
  };
  socket.onmessage = async (event) => {
    if (!isCurrent()) return;
    // 任何回帧（auth_ok/pong/chat_*/result）都是「连接活着」的证据。
    lastFrameAt = Date.now();
    try {
      const data = JSON.parse(event.data);
      if (data && data.type === 'auth_ok' && data.protocol === 2) {
        clearAuthTimer();
        authenticated = true;
        authError = null;
        reconnectBlocked = false;
        alreadyConnectedBackoff = 0; // 重连成功，清空退避计数
        await sendReadyFrames(socket);
        flushChatFrameQueue(); // 握手期间排队的聊天帧现在续发（面板重试/首发不再被唤醒竞态吃掉）
        flushPwdFrameQueue(); // 密码保存帧同样续发（断连期间点保存的请求在此落地）
        flushTotpFrameQueue(); // 验证器帧续发（读码/保存，无离线镜像必须在线完成）
        void syncPwdCache(); // 离线期间落本地的写在此补送服务端（幂等 upsert / delete）
        void broadcastBridgeStatus(); // 连上了：推给所有 tab，徽标变绿
        return;
      }
      if (data && data.type === 'auth_error') {
        clearAuthTimer();
        authenticated = false;
        authError = { code: String(data.code || 'invalid'), message: String(data.message || 'authentication failed') };
        reconnectBlocked = PERMANENT_AUTH_ERRORS.has(authError.code);
        // already_connected 专门退避：旧槽位是半开死连接，服务端要等空闲读超时
        // （~60s）才释放。probe 每 5s 撞一次纯属浪费且把徽标长时间停在 auth_error。
        // 改成 already_connected 用专门的退避表，逐步拉长间隔给服务端时间释放。
        if (authError.code === 'already_connected') {
          alreadyConnectedBackoff = Math.min(alreadyConnectedBackoff + 1, ALREADY_CONNECTED_BACKOFF_MS.length - 1);
        }
        console.error('[TMWD-WS] Authentication failed:', authError.code, authError.message);
        void broadcastBridgeStatus(); // 认证失败：推给所有 tab，徽标变红
        socket.close();
        return;
      }
      if (!authenticated) {
        console.warn('[TMWD-WS] Ignoring frame before auth_ok');
        return;
      }
      if (data && typeof data.type === 'string' && data.type.startsWith('chat_')) {
        // 服务端主动推的「该客户端的可操作 agent 变了」：无 reqId，不进 reqId 映射，
        // 直接广播给所有可注入 tab，让开着面板的页面立刻重拉 agent 列表。
        if (data.type === 'chat_agents_changed') {
          void broadcastChatAgentsChanged();
          return;
        }
        // 会话事件（服务端按 conversationId 广播、不带 reqId）→ 扇给该会话的
        // 全部观察者标签页；其余是单请求回帧（chat_send_ok / chat_error / 快照），
        // 仍按 reqId 定向回发起它的标签页。
        if (data.type === 'chat_event' && data.conversationId && !data.reqId) {
          fanoutChatEvent(data);
          return;
        }
        forwardChatFrameToTab(data);
        return;
      }
      if (data && typeof data.type === 'string' && data.type.startsWith('totp_')) {
        forwardTotpFrame(data);
        return;
      }
      if (data && typeof data.type === 'string' && data.type.startsWith('pwd_')) {
        forwardPwdFrame(data);
        return;
      }
      if (data.id && data.code) {
        let code = data.code;
        if (typeof code === 'string') {
          try { const p = JSON.parse(code); if (p && typeof p === 'object') code = p; } catch (_) {}
        }
        if (typeof code === 'object' && code !== null && code.cmd) {
          if (code.tabId === undefined && data.tabId !== undefined) code.tabId = data.tabId;
          const res = await handleExtMessage(code, {});
          if (isCurrent() && authenticated) socket.send(JSON.stringify({ type: res.ok ? 'result' : 'error', id: data.id, result: res.data ?? res.results ?? res, error: res.error }));
        } else if (typeof code === 'string') {
          await handleWsExec(data, socket);
        } else if (typeof code === 'object' && code !== null) {
          const msg = code.tabId === undefined && data.tabId !== undefined ? { ...code, tabId: data.tabId } : code;
          const res = await handleExtMessage(msg, {});
          if (isCurrent() && authenticated) socket.send(JSON.stringify({ type: res.ok ? 'result' : 'error', id: data.id, result: res.data ?? res.results ?? res, error: res.error }));
        }
      }
    } catch (e) {
      console.error('[TMWD-WS] message parse error', e);
    }
  };
  socket.onclose = async () => {
    clearConnectionTimer();
    clearAuthTimer();
    if (!isCurrent()) return;
    console.log('[TMWD-WS] Disconnected');
    markDisconnected();
  };
  socket.onerror = (e) => {
    if (isCurrent()) console.error('[TMWD-WS] Error:', e);
  };
}

// Initial connect + wake-up hooks
connectWS();
chrome.runtime.onStartup.addListener(() => connectWS());
chrome.runtime.onInstalled.addListener(() => connectWS());
// 代理状态对账：SW 重启后 chrome.proxy 的设置可能仍生效但我们丢了内存态，
// 读本地镜像重放一遍（幂等）；租约已过期则还原。不阻塞桥接连接。
reconcileProxyOnStartup();
// 代理通道（数据面出口）独立连接：与控制通道平行，各自重连。
connectProxyWS();

// Sync tab list on changes
async function sendTabsUpdate() {
  const socket = ws;
  if (!socket || socket.readyState !== WebSocket.OPEN || !authenticated) return;
  const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url) && !/streamlit/i.test(t.title));
  if (socket !== ws || !authenticated || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({
    type: 'tabs_update',
    tabs: tabs.map(t => ({ id: t.id, url: t.url, title: t.title })),
  }));
}
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === 'complete') {
    sendTabsUpdate();
    // 整页刷新后内容脚本会重新注入、横幅消失；若该标签页仍在监听，重新提示展示。
    const cap = getCapture(tabId);
    if (cap && cap.capturing) notifyCaptureBanner(tabId, 'show', cap.requests.size);
  }
});
chrome.tabs.onRemoved.addListener((tabId) => {
  // 标签页关闭：摘掉对该页的监听状态，避免内存泄漏与悬挂会话。
  const cap = getCapture(tabId);
  if (cap) {
    cap.capturing = false;
    cap.attached = false;
    cap.stoppedAt = Date.now();
    netCaptures.delete(String(tabId));
  }
  sendTabsUpdate();
});
chrome.tabs.onCreated.addListener(() => sendTabsUpdate());
