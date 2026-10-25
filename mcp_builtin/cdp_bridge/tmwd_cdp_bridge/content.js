// --- 注入节点存活守卫 ---
// 内容脚本只在整页导航时注入一次，而 SPA 路由切换/框架整段重渲染会把 body 里的
// 节点连同我们注入的气泡与聊天面板一起清掉，于是二者消失、只有刷新才回来。
// 这里统一登记「取节点 + 重挂」两个回调，节点一旦脱离文档就重新挂回去。
// 同一扩展的内容脚本共享 isolated world 的 window，故挂在 window 上跨 IIFE 复用。
(function () {
  if (window.__tmwdMountGuard) return;
  const guards = [];
  let scheduled = false;
  // 少数页面会主动清掉不认识的节点，重挂又触发下一轮观察，可能与页面互相拉锯。
  // 每个守卫给一个短窗口内的重挂上限，超了就停手，宁可气泡不回来也不能把页面卡死。
  const BURST_MS = 2000;
  const BURST_MAX = 12;
  function run() {
    scheduled = false;
    const now = Date.now();
    for (const g of guards) {
      try {
        const n = g.get();
        if (!n || !n.isConnected) {
          if (now - g.windowStart > BURST_MS) { g.windowStart = now; g.count = 0; }
          if (g.count >= BURST_MAX) continue;
          g.count++;
          g.mount();
        }
      } catch (_) { /* ignore */ }
    }
  }
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    // 合并同一批 DOM 变更；后台标签页 rAF 不触发，退回定时器。
    if (document.hidden) setTimeout(run, 120);
    else requestAnimationFrame(run);
  }
  // 返回「注销函数」：调用方主动收起节点（关弹卡/下拉）时必须注销守卫，否则
  // 守卫看到节点脱离文档会立刻挂回来——弹框「点取消也关不掉」就是这个漏的。
  window.__tmwdMountGuard = function (get, mount) {
    const entry = { get, mount, windowStart: Date.now(), count: 0 };
    guards.push(entry);
    schedule();
    return function release() {
      const i = guards.indexOf(entry);
      if (i >= 0) guards.splice(i, 1);
    };
  };
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  // 软导航与标签页切回：DOM 变更观察偶有漏网（先清空、后异步渲染），补一层兜底。
  for (const evt of ['popstate', 'hashchange', 'pageshow', 'visibilitychange']) {
    window.addEventListener(evt, schedule, true);
  }
})();

;(function(){ if (/streamlit/i.test(document.title)) return;

// Remove meta CSP tags
document.querySelectorAll('meta[http-equiv="Content-Security-Policy"]').forEach(e => e.remove());

// Indicator badge at bottom-right (userscript style)
(function(){
  if(window.self!==window.top)return;
  if(document.getElementById('ljq-ind'))return;
  // 当前实际加载的扩展版本。unpacked 扩展改了磁盘代码不会自动生效，必须去
  // chrome://extensions 点刷新；把版本挂在徽标 tooltip 上，任何页面悬停就能确认
  // 「浏览器里跑的到底是不是最新那份」，不必再去翻 preferences 文件。
  let EXT_VER='';
  try { EXT_VER=(chrome.runtime.getManifest&&chrome.runtime.getManifest().version)||''; } catch(_){}
  const verSuffix=EXT_VER?(' · v'+EXT_VER):'';
  const d=document.createElement('div');
  d.id='ljq-ind';
  d.setAttribute('role','button');
  d.setAttribute('aria-label','Ai Lubricant 正在检查连接状态');
  d.title='Ai Lubricant 正在检查连接状态'+verSuffix;
  d.dataset.state='authenticating';
  d.innerHTML='<span class="ljq-ind-icon"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg></span><span class="ljq-ind-dot"></span><span class="ljq-ind-label"><span class="ljq-ind-text">网页 Agent</span><span class="ljq-ind-status">正在连接</span></span>';
  const style=document.createElement('style');
  style.textContent=`
    #ljq-ind{position:fixed;right:20px;bottom:20px;display:inline-flex;align-items:center;justify-content:center;width:52px;height:52px;border-radius:50%;background:linear-gradient(135deg,#0f766e 0%,#0d9488 100%);color:#fff;box-shadow:0 10px 28px rgba(15,118,110,.38),0 4px 10px rgba(15,23,42,.14);z-index:2147483647;cursor:grab;user-select:none;transition:transform .18s ease,box-shadow .18s ease;}
    #ljq-ind:hover{transform:translateY(-2px) scale(1.04);box-shadow:0 14px 34px rgba(15,118,110,.44),0 6px 14px rgba(15,23,42,.16);}
    #ljq-ind:active{transform:scale(1)}
    #ljq-ind .ljq-ind-icon{display:inline-flex;align-items:center;justify-content:center;color:#fff;pointer-events:none}
    #ljq-ind .ljq-ind-dot{position:absolute;right:5px;bottom:5px;width:14px;height:14px;border-radius:50%;background:#94a3b8;border:2.5px solid #fff;box-shadow:0 1px 4px rgba(15,23,42,.25);flex:0 0 auto;}
    #ljq-ind[data-state="connected"] .ljq-ind-dot{background:#10b981;animation:tmwd-pulse 2.4s ease-in-out infinite}
    #ljq-ind[data-state="authenticating"] .ljq-ind-dot{background:#f59e0b}
    #ljq-ind[data-state="disconnected"] .ljq-ind-dot,#ljq-ind[data-state="disabled"] .ljq-ind-dot,#ljq-ind[data-state="auth_error"] .ljq-ind-dot{background:#ef4444}
    #ljq-ind[data-state="unconfigured"] .ljq-ind-dot{background:#94a3b8}
    @keyframes tmwd-pulse{0%,100%{box-shadow:0 0 0 0 rgba(16,185,129,.5),0 1px 4px rgba(15,23,42,.25)}50%{box-shadow:0 0 0 6px rgba(16,185,129,0),0 1px 4px rgba(15,23,42,.25)}}
    #ljq-ind .ljq-ind-label{position:absolute;right:62px;top:50%;transform:translateY(-50%) translateX(10px);display:inline-flex;align-items:center;gap:8px;background:#fff;color:#0f172a;border:1px solid rgba(15,23,42,.08);border-radius:999px;padding:0 14px;height:34px;white-space:nowrap;font:600 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;box-shadow:0 8px 22px rgba(15,23,42,.16);opacity:0;pointer-events:none;transition:opacity .18s ease,transform .18s ease}
    #ljq-ind:hover .ljq-ind-label{opacity:1;transform:translateY(-50%) translateX(0)}
    #ljq-ind .ljq-ind-text{white-space:nowrap}
    #ljq-ind .ljq-ind-status{padding-left:8px;border-left:1px solid rgba(15,23,42,.12);color:#64748b;font-size:11px;font-weight:500}
    #ljq-ind[data-state="connected"] .ljq-ind-status{color:#0f766e}
    #ljq-ind[data-state="authenticating"] .ljq-ind-status{color:#b45309}
    #ljq-ind[data-state="disconnected"] .ljq-ind-status,#ljq-ind[data-state="disabled"] .ljq-ind-status,#ljq-ind[data-state="auth_error"] .ljq-ind-status{color:#b91c1c}
  `;
  function setBridgeStatus(status){
    const state=status&&status.state||'disconnected';
    const labels={connected:'已连接',authenticating:'正在连接…',auth_error:'连接失败',unconfigured:'未配置',disabled:'已停用',disconnected:'未连接'};
    const label=labels[state]||labels.disconnected;
    d.dataset.state=state;
    d.title='Ai Lubricant '+label+verSuffix;
    d.setAttribute('aria-label','Ai Lubricant '+label);
    const statusEl=d.querySelector('.ljq-ind-status');
    if(statusEl)statusEl.textContent=label;
  }
  function showBridgeNotice(message,title){
    if(!document.getElementById('tmwd-bridge-notice-style')){
      const s=document.createElement('style');
      s.id='tmwd-bridge-notice-style';
      s.textContent=`
        .tmwd-bridge-notice{position:fixed;right:14px;bottom:52px;z-index:2147483647;width:min(360px,calc(100vw - 28px));display:grid;grid-template-columns:26px 1fr;gap:10px;align-items:start;padding:12px 13px;border:1px solid rgba(18,24,38,.10);border-radius:12px;background:rgba(255,255,255,.95);color:#182033;box-shadow:0 10px 28px rgba(18,24,38,.14);font:400 13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;letter-spacing:0;opacity:0;transform:translateY(6px);transition:opacity .18s ease,transform .18s ease;backdrop-filter:saturate(140%) blur(10px);-webkit-backdrop-filter:saturate(140%) blur(10px);}
        .tmwd-bridge-notice.is-visible{opacity:1;transform:translateY(0);}
        .tmwd-bridge-notice-icon{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;background:#eef8f3;color:#168456;font:700 13px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;}
        .tmwd-bridge-notice-title{color:#182033;font-weight:650;margin:0 0 3px;}
        .tmwd-bridge-notice-message{color:#4c566a;margin:0;max-height:96px;overflow:hidden;word-break:break-word;}
      `;
      (document.head||document.documentElement).appendChild(s);
    }
    document.querySelectorAll('.tmwd-bridge-notice').forEach(n=>n.remove());
    const n=document.createElement('div');
    n.className='tmwd-bridge-notice';
    n.innerHTML='<div class="tmwd-bridge-notice-icon">i</div><div><div class="tmwd-bridge-notice-title"></div><p class="tmwd-bridge-notice-message"></p></div>';
    n.querySelector('.tmwd-bridge-notice-title').textContent=title||'Ai Lubricant';
    n.querySelector('.tmwd-bridge-notice-message').textContent=message;
    (document.body||document.documentElement).appendChild(n);
    requestAnimationFrame(()=>n.classList.add('is-visible'));
    setTimeout(()=>n.classList.remove('is-visible'),3200);
    setTimeout(()=>n.remove(),3450);
  }
  // Drag support — make the badge draggable
  let _dragging=false,_hasDragged=false,_sX,_sY,_sL,_sT;
  function _start(cx,cy,e){
    _dragging=true;_hasDragged=false;
    const r=d.getBoundingClientRect();
    _sX=cx;_sY=cy;_sL=r.left;_sT=r.top;
    e.preventDefault();
  }
  function _move(cx,cy){
    if(!_dragging)return;
    const dx=cx-_sX,dy=cy-_sY;
    if(!_hasDragged&&(Math.abs(dx)>3||Math.abs(dy)>3)){
      _hasDragged=true;
      d.style.left=_sL+'px';d.style.top=_sT+'px';
      d.style.right='auto';d.style.bottom='auto';
      d.style.cursor='grabbing';d.style.transition='none';
    }
    if(_hasDragged){d.style.left=(_sL+dx)+'px';d.style.top=(_sT+dy)+'px';}
  }
  function _end(){
    if(!_dragging)return;
    _dragging=false;
    if(_hasDragged){d.style.cursor='grab';d.style.transition='';d._preventClick=true;}
  }
  d.addEventListener('mousedown',e=>{if(e.button===0)_start(e.clientX,e.clientY,e);});
  d.addEventListener('touchstart',e=>{const t=e.touches[0];_start(t.clientX,t.clientY,e);},{passive:false});
  document.addEventListener('mousemove',e=>_move(e.clientX,e.clientY));
  document.addEventListener('touchmove',e=>{const t=e.touches[0];_move(t.clientX,t.clientY);},{passive:false});
  document.addEventListener('mouseup',_end);
  document.addEventListener('touchend',_end);
  d._setBridgeStatus=setBridgeStatus;
  d._showBridgeNotice=showBridgeNotice;
  function mount(){
    if(!style.isConnected)(document.head||document.documentElement).appendChild(style);
    if(!d.isConnected)(document.body||document.documentElement).appendChild(d);
  }
  mount();
  // 复用同一个节点重挂：拖拽位置、data-state 徽标状态与事件监听都留在节点上，
  // SPA 重渲染后气泡回到原处而不是重置到右下角。
  window.__tmwdMountGuard&&window.__tmwdMountGuard(()=>d,mount);
})();

// --- 网络监听顶部通栏横幅 ---
(function(){
  if (window.self !== window.top) return; // 仅顶层框架显示
  const BANNER_ID = 'tmwd-net-capture-banner';

  function ensureBannerStyle(){
    if (document.getElementById(BANNER_ID + '-style')) return;
    const s = document.createElement('style');
    s.id = BANNER_ID + '-style';
    s.textContent = `
      #${BANNER_ID}{position:fixed;top:0;left:0;right:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;gap:10px;height:34px;padding:0 14px;background:linear-gradient(90deg,#b32020,#d13333);color:#fff;font:600 13px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;letter-spacing:.2px;box-shadow:0 2px 10px rgba(179,32,32,.35);transform:translateY(-100%);transition:transform .2s ease;}
      #${BANNER_ID}.is-visible{transform:translateY(0);}
      #${BANNER_ID} .tmwd-net-dot{width:9px;height:9px;border-radius:50%;background:#fff;box-shadow:0 0 0 0 rgba(255,255,255,.7);animation:tmwd-net-pulse 1.4s infinite;flex:0 0 auto;}
      @keyframes tmwd-net-pulse{0%{box-shadow:0 0 0 0 rgba(255,255,255,.6);}70%{box-shadow:0 0 0 7px rgba(255,255,255,0);}100%{box-shadow:0 0 0 0 rgba(255,255,255,0);}}
      #${BANNER_ID} .tmwd-net-count{font-variant-numeric:tabular-nums;opacity:.92;font-weight:500;}
      #${BANNER_ID} .tmwd-net-stop{margin-left:6px;height:22px;padding:0 12px;border:1px solid rgba(255,255,255,.55);border-radius:999px;background:rgba(255,255,255,.14);color:#fff;font:600 12px/1 inherit;cursor:pointer;transition:background .15s ease,border-color .15s ease;}
      #${BANNER_ID} .tmwd-net-stop:hover{background:rgba(255,255,255,.26);border-color:#fff;}
    `;
    (document.head || document.documentElement).appendChild(s);
  }

  function showBanner(count){
    ensureBannerStyle();
    let b = document.getElementById(BANNER_ID);
    if (!b){
      b = document.createElement('div');
      b.id = BANNER_ID;
      b.setAttribute('role', 'status');
      b.innerHTML = '<span class="tmwd-net-dot"></span>'
        + '<span class="tmwd-net-label">正在监听网络请求</span>'
        + '<span class="tmwd-net-count"></span>'
        + '<button type="button" class="tmwd-net-stop">停止监听</button>';
      (document.body || document.documentElement).appendChild(b);
      b.querySelector('.tmwd-net-stop').addEventListener('click', () => {
        b.querySelector('.tmwd-net-stop').textContent = '停止中…';
        chrome.runtime.sendMessage({ cmd: 'net_stop' }).catch(() => {});
      });
    }
    b.querySelector('.tmwd-net-count').textContent = '· 已捕获 ' + (count || 0) + ' 条';
    requestAnimationFrame(() => b.classList.add('is-visible'));
  }

  function hideBanner(){
    const b = document.getElementById(BANNER_ID);
    if (!b) return;
    b.classList.remove('is-visible');
    setTimeout(() => b.remove(), 220);
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || msg.cmd !== 'network_capture_banner') return;
    if (msg.action === 'hide') hideBanner();
    else showBanner(msg.count); // 'show' | 'update'
  });
})();

new MutationObserver(muts => {
  for (const m of muts) for (const n of m.addedNodes) {
    if (n.id === TID || (n.querySelector && n.querySelector('#' + TID))) {
      const el = n.id === TID ? n : n.querySelector('#' + TID);
      handle(el);
    }
  }
}).observe(document.documentElement, { childList: true, subtree: true });

async function handle(el) {
  try {
    const text = el.textContent.trim();
    if (!text) { el.textContent = JSON.stringify({ ok: false, error: 'empty request' }); return; }
    const req = JSON.parse(text);
    const cmd = req.cmd;
    let resp;
    if (cmd === 'cdp') {
      resp = await chrome.runtime.sendMessage({ cmd: 'cdp', method: req.method, params: req.params || {}, tabId: req.tabId });
    } else if (cmd === 'batch') {
      resp = await chrome.runtime.sendMessage({ cmd: 'batch', commands: req.commands, tabId: req.tabId });
    } else if (cmd === 'tabs') {
      resp = await chrome.runtime.sendMessage({ cmd: 'tabs', method: req.method, tabId: req.tabId, url: req.url, newWindow: req.newWindow, active: req.active });
    } else {
      resp = { ok: false, error: 'unknown cmd: ' + cmd };
    }
    el.textContent = JSON.stringify(resp);
  } catch (e) {
    el.textContent = JSON.stringify({ ok: false, error: e.message });
  }
}

// --- 密码管理器：登录捕获 / 保存弹卡 / 候选填充 / 随机生成 ---
// 全框架（顶层 + iframe）都跑：iframe 内登录表单同样捕获，弹卡经 background
// 中转回传顶层画（小 iframe 里画会挤变形，明文不得跨 web origin 广播）。
// 已鉴权桥接 WS（pwd_* 帧）。明文密码只在浏览器内与 WS 帧中存在，不进 chat 消息流。
(function(){
  // PWD_IFRAME_OK —— 密码 IIFE 不再只跑顶层；iframe 内也照常触发捕获/成功判定，
  // 仅「弹卡渲染」与「整页跳转续存恢复」收归顶层（见 D 阶段）。
  if (window.__tmwdPwdInit) return;
  window.__tmwdPwdInit = true;

  const PWD_PENDING_KEY = 'cdpPwdPending';
  const PWD_PROMPTED_KEY = 'cdpPwdPrompted';
  // 最近一次生成的随机密码（按站点记）。用户在页面上生成后若没复制就关掉了下拉，
  // 可回插件 popup 的「本次生成」里找回。会话级存储：浏览器重启即清。
  const PWD_LASTGEN_KEY = 'cdpPwdLastGen';
  const PWD_CAPTURE_MAX_AGE = 120000;
  const PWD_SUCCESS_CHECK_DELAY = 1200;
  const PWD_SUCCESS_RECHECK_DELAY = 1500;
  const PWD_PROMPTED_TTL = 30 * 60 * 1000;
  const PWD_CARD_HOST_ID = 'tmwd-pwd-card';
  const PWD_SUGGEST_HOST_ID = 'tmwd-pwd-suggest';

  // 状态（提前声明，避免函数引用 TDZ）
  let pendingLogin = null;
  let loginAttemptKey = null;
  let loginPromptShown = false;
  let loginCheckTimer = null;
  let lastUrl = location.href;
  // 成功判定快照：密码元素引用（不进 session 序列化，跨整页导航后置空退化到 URL 分支）
  // 与捕获时标题（SPA 原地登录常改 title 不改 URL）。
  let capturedPwdElRef = null;
  let capturedTitle = document.title;
  let capturedAt = 0;
  // 整页跳转续存只在顶层恢复（iframe 内 pendingLogin 只在本 frame 生命周期内存活），
  // 避免多 frame 各自恢复同一条 → 弹多张卡。见阶段3。
  const isTopFrame = (() => { try { return window.self === window.top; } catch (_) { return false; } })();

  const USERNAME_INPUT_TYPES = new Set(['text', 'email', 'tel', 'search', 'url', 'number']);
  const USERNAME_HINT_RE = /(user|login|email|mail|account|acct|phone|mobile|name|账号|帐号|账户|用户名|邮箱|手机号|手机)/i;
  const PASSWORD_HINT_RE = /(pass|pwd|password|密码)/i;
  const LOGIN_ACTION_RE = /(登录|登陆|log\s*in|sign\s*in|submit|进入|继续|下一步|next|continue|confirm|确认|立即)/i;
  const LOGIN_ERROR_RE = /(密码错误|账号错误|帐号错误|账户错误|用户名或密码|账号或密码|帐号或密码|登录失败|登陆失败|log\s*in\s*fail|sign\s*in\s*fail|login\s*failed|invalid\s*(password|credentials|username|account)|wrong\s*password|incorrect\s*(password|credentials|username)|用户不存在|账户不存在)/i;
  const LOGIN_SUCCESS_TEXT_RE = /(登录成功|登陆成功|欢迎回来|欢迎您|欢迎登录|log\s*in\s*success|signed\s*in|退出登录|sign\s*out|log\s*out|个人中心|我的账户|注销|登出|切换账号)/i;
  const LOGIN_SUCCESS_URL_RE = /(home|dashboard|overview|profile|account|welcome|index|success|portal|console|admin|user\/|me\b|mine|main)/i;
  const LOGIN_PATH_RE = /(login|signin|sign-in|auth|logon|log-on|登陆|登录)/i;

  function isUsernameInput(input) {
    if (!input || input.tagName !== 'INPUT' || input.disabled || input.readOnly) return false;
    // 隐藏蜜罐框（骗 Chrome 原生保存的 fakeusername/fakepassword，恒空且排在真实字段
    // 前）不能占住 username 槽——否则真实 Identity_input 被跳过，捕获到的用户名恒空、
    // 弹卡保存按钮 disabled，等价于「识别不出这个登录」。规则：非渲染中的字段只有
    // 自带值才算用户名（合法预填的隐藏字段仍收，空的蜜罐被排掉）。
    if (!isRendered(input) && !String(input.value || '').trim()) return false;
    const type = String(input.type || '').toLowerCase();
    if (type === 'password') return false;
    if (!USERNAME_INPUT_TYPES.has(type)) return false;
    const ac = String(input.getAttribute('autocomplete') || '').toLowerCase();
    if (/(username|email|tel)/i.test(ac)) return true;
    if (/(new-password|current-password)/i.test(ac)) return false;
    const haystack = [input.name, input.id, input.getAttribute('aria-label'), input.placeholder].join(' ');
    if (PASSWORD_HINT_RE.test(haystack)) return false;
    if (USERNAME_HINT_RE.test(haystack)) return true;
    return type === 'email' || type === 'tel';
  }
  function isCandidateUsernameInput(input) {
    if (!input || input.tagName !== 'INPUT' || input.disabled || input.readOnly) return false;
    if (!isRendered(input) && !String(input.value || '').trim()) return false;
    const type = String(input.type || '').toLowerCase();
    if (!USERNAME_INPUT_TYPES.has(type)) return false;
    const haystack = [input.name, input.id, input.getAttribute('aria-label'), input.placeholder].join(' ');
    return !PASSWORD_HINT_RE.test(haystack);
  }
  // 仅把「真正渲染出来的密码框」当登录作用域里的密码字段。Arco/Ant 这类组件库的
  // 登录常以 Modal 承载，登录成功后 Modal 多半不卸载、只是 display:none（或留壳），
  // 密码框仍在 DOM 里；若依旧算它，findLoginScope 会一直命中已关闭的登录框，
  // inferLoginSuccess 永远走不到「表单已消失」的成功分支，保存弹卡迟迟不弹。
  // 取渲染态而非仅 connected：display:none / 祖先隐藏都不算活跃表单；fixed/absolute
  // 仍算（登录框常是定位元素）。捕获发生在点击/提交瞬间，那时表单必然可见，不受影响。
  function isRendered(el) {
    if (!el || !el.isConnected) return false;
    if (typeof el.checkVisibility === 'function') {
      try { return el.checkVisibility(); } catch (_) { /* 老内核降级到 getClientRects */ }
    }
    return el.getClientRects().length > 0;
  }
  function isPasswordInput(input) {
    return !!input && input.tagName === 'INPUT' && String(input.type || '').toLowerCase() === 'password' && isRendered(input);
  }
  // Shadow DOM 穿透：组件库常把整表单封进 open shadowRoot，document.querySelectorAll
  // 看不见。递归 open shadowRoot 收集；closed 读不到（与 Chrome autofill 限制一致）。
  // 加节点规模护栏避免巨型页面卡死。
  // ⚠ 排除我们自己注入的 UI（弹卡/下拉/面板里也有 input/password，穿透后会被当成
  // 页面登录表单——「点弹卡按钮→把弹卡再捕获一次」的自指环必须掐死）。
  function isInjectedUi(el) {
    let n = el;
    while (n && n.nodeType === 1) {
      if (n.id && (/^tmwd/.test(n.id) || /^ljq/.test(n.id))) return true;
      const root = n.getRootNode ? n.getRootNode() : null;
      if (root && root.host) { n = root.host; continue; }
      n = n.parentElement;
    }
    return false;
  }
  function deepQueryAll(root, sel) {
    root = root || document;
    const out = [];
    const MAX_NODES = 20000;
    let seen = 0;
    const visit = (node) => {
      if (!node || !node.querySelectorAll) return;
      let list = null;
      try { list = node.querySelectorAll(sel); } catch (_) { return; }
      if (list) for (const el of list) { if (seen++ > MAX_NODES) return; if (!isInjectedUi(el)) out.push(el); }
      let hosts = null;
      try { hosts = node.querySelectorAll('*'); } catch (_) { hosts = null; }
      if (hosts) for (const el of hosts) { if (seen > MAX_NODES) return; if (el.shadowRoot && !isInjectedUi(el)) visit(el.shadowRoot); }
    };
    visit(root);
    return out;
  }
  function deepQuerySelector(root, sel) { const a = deepQueryAll(root, sel); return a.length ? a[0] : null; }
  function deepQueryAllInputs(root) { return deepQueryAll(root, 'input'); }
  function findLoginScopeIn(root) {
    root = root || document;
    const forms = root.tagName === 'FORM' ? [root] : (root.querySelectorAll ? Array.from(root.querySelectorAll('form')) : []);
    for (const form of forms) {
      let username = null, password = null;
      const inputs = deepQueryAllInputs(form);
      for (const inp of inputs) {
        if (!password && isPasswordInput(inp)) password = inp;
        else if (!username && isUsernameInput(inp)) username = inp;
      }
      if (password && username) return { form, username, password };
      // 降级①：严格用户名没命中，用候选用户名（只排除密码类）补
      if (password && !username) {
        for (const inp of inputs) {
          if (inp !== password && isCandidateUsernameInput(inp)) { username = inp; break; }
        }
      }
      if (password) return { form, username: username || null, password }; // password-only 也返回
    }
    const pwdInputs = deepQueryAll(root, 'input[type="password"]').filter(isPasswordInput);
    if (!pwdInputs.length) return null;
    const password = pwdInputs[0];
    const container = password.closest('div, section, article, fieldset, dialog, [role="dialog"], [role="form"]') || root;
    // 降级链：严格用户名 → 候选用户名 → null（仍返回 scope，password-only 不阻断捕获）
    const containerInputs = deepQueryAllInputs(container);
    let username = null;
    for (const inp of containerInputs) { if (isUsernameInput(inp)) { username = inp; break; } }
    if (!username) {
      for (const inp of containerInputs) {
        if (inp !== password && isCandidateUsernameInput(inp)) { username = inp; break; }
      }
    }
    if (!username) {
      const all = deepQueryAllInputs(root);
      for (let i = all.indexOf(password) - 1; i >= 0; i--) { if (isUsernameInput(all[i])) { username = all[i]; break; } }
      if (!username) {
        for (let i = all.indexOf(password) - 1; i >= 0; i--) {
          const inp = all[i];
          if (inp !== password && isCandidateUsernameInput(inp)) { username = inp; break; }
        }
      }
    }
    return { form: null, username: username || null, password };
  }
  function findLoginScope() { return findLoginScopeIn(document); }
  function findPasswordFieldFor(input) {
    if (!input) return null;
    const form = input.closest('form');
    if (form) { const pwd = deepQuerySelector(form, 'input[type="password"]'); if (isPasswordInput(pwd)) return pwd; }
    const container = input.closest('div, section, article, fieldset, dialog, [role="dialog"], [role="form"]');
    if (container) { const pwd = deepQuerySelector(container, 'input[type="password"]'); if (isPasswordInput(pwd)) return pwd; }
    return null;
  }
  function looksLikeLoginButton(el) {
    if (!el) return false;
    if (el.tagName === 'INPUT' && /submit|button/i.test(el.type || '') && LOGIN_ACTION_RE.test(String(el.value || ''))) return true;
    if (el.tagName === 'BUTTON') {
      if (String(el.type || '').toLowerCase() === 'submit') return true;
      if (LOGIN_ACTION_RE.test([el.textContent, el.getAttribute('title'), el.getAttribute('aria-label')].join(' '))) return true;
    }
    return false;
  }
  function getFormData(scope) {
    if (!scope) return { username: '', password: '' };
    let username = scope.username ? String(scope.username.value || '') : '';
    const password = scope.password ? String(scope.password.value || '') : '';
    if (!username) {
      const root = scope.form || document;
      const alt = deepQuerySelector(root, 'input');
      if (alt && alt !== scope.password && isCandidateUsernameInput(alt) && alt.value) username = String(alt.value);
    }
    return { username, password };
  }

  for (const evt of ['pageshow', 'popstate', 'hashchange']) {
    window.addEventListener(evt, () => { lastUrl = location.href; }, true);
  }
  function safeUrl(href) { try { return new URL(href); } catch (_) { return null; } }

  // --- 定点错误扫描（修「整页 4000 字误杀」）---
  // 只扫三处：登录作用域容器、常见报错位（[role=alert]/[aria-live]/.error/.message/.fail）、
  // 捕获之后新变更的文本（MutationObserver 记录，≤30 条）。页脚/帮助区的
  // 「登录失败？」静态文案不再一票否决成功信号。
  let recentTexts = [];
  let domWatchObserver = null;
  function startDomWatch() {
    stopDomWatch();
    recentTexts = [];
    try {
      domWatchObserver = new MutationObserver((muts) => {
        for (const m of muts) {
          if (recentTexts.length >= 30) return;
          if (m.type === 'characterData' && m.target) {
            const t = String(m.target.textContent || '').trim();
            if (t) recentTexts.push(t.slice(0, 300));
          } else if (m.type === 'childList') {
            for (const n of m.addedNodes) {
              if (recentTexts.length >= 30) return;
              const t = String(n.nodeType === 3 ? n.textContent : (n.textContent || '')).trim();
              if (t) recentTexts.push(t.slice(0, 300));
            }
          }
        }
      });
      domWatchObserver.observe(document.body || document.documentElement, { childList: true, characterData: true, subtree: true });
    } catch (_) { domWatchObserver = null; }
  }
  function stopDomWatch() {
    if (domWatchObserver) { try { domWatchObserver.disconnect(); } catch (_) {} domWatchObserver = null; }
    recentTexts = [];
  }
  function scopedErrorScan(scopeHint) {
    const probes = [];
    const scope = scopeHint || findLoginScope();
    const near = scope && (scope.form || (scope.password && scope.password.closest('div, section, article, fieldset, dialog, [role="dialog"], [role="form"]')));
    if (near) { const t = String(near.innerText || '').slice(0, 1200); if (t) probes.push(t); }
    try {
      const alerts = document.querySelectorAll('[role="alert"],[aria-live="assertive"],[aria-live="polite"],.error,.message,.fail');
      for (const el of alerts) {
        if (probes.length >= 12) break;
        const t = String((el && (el.innerText || el.textContent)) || '').trim().slice(0, 300);
        if (t) probes.push(t);
      }
    } catch (_) {}
    for (const t of recentTexts) probes.push(t);
    for (const t of probes) { if (LOGIN_ERROR_RE.test(t)) return true; }
    return false;
  }
  function inferLoginSuccess() {
    // 内嵌 iframe 登录（阿里 Havana passport.* 的 mini_login 这类）：iframe 自身看不到
    // 成功——成功信号是「顶层在 iframe 提交后整页 reload/跳转、iframe 随之销毁」。能在
    // reload 后的新顶层文档里走到这里，且 pending 来自 iframe，就说明那次刷新发生过
    // （失败只在 iframe 里报错、不动顶层）。跨域读不到 iframe 内的报错，交给 30min 同
    // 凭据去重 + 用户取消兜底。modelscope.cn 的登录正是这条路径。
    if (isTopFrame && pendingLogin && pendingLogin.fromIframe && !scopedErrorScan()) return true;
    const currentUrl = window.location.href;
    const referenceUrl = (pendingLogin && pendingLogin.url) || lastUrl;
    const old = safeUrl(referenceUrl);
    const now = safeUrl(currentUrl);
    const nowPath = (now && now.pathname) || '';
    const loginScope = findLoginScope();
    const scopeUp = !!(loginScope && loginScope.password && isRendered(loginScope.password));
    // 定点报错按住一切：作用域附近/报错区/捕获后的新文本出现登录失败文案 → 不弹。
    // 范围远小于旧「整页 4000 字」，帮助区静态文案不再误杀。惰性求值+缓存：
    // 轮询每 1.5s 一次，没走到成功分支就不做 DOM 扫描。
    let errCache;
    const noFreshErr = () => (errCache === undefined ? (errCache = !scopedErrorScan(loginScope)) : errCache);
    if (old && now && old.origin === now.origin) {
      if (old.pathname !== now.pathname) {
        if (LOGIN_PATH_RE.test(old.pathname) && !LOGIN_PATH_RE.test(nowPath) && noFreshErr()) return true;
        if (LOGIN_SUCCESS_URL_RE.test(nowPath) && noFreshErr()) return true;
      }
      if (old.href !== now.href && !LOGIN_PATH_RE.test(nowPath) && noFreshErr()) return true;
    }
    // SPA 原地登录：标题变了 + 登录表单不在 + 不在登录路径
    if (document.title !== capturedTitle && !scopeUp && !LOGIN_PATH_RE.test(nowPath) && noFreshErr()) return true;
    // 强成功：捕获时那个密码框从「有值」变「消失/隐藏/卸载」（Modal 收起/换步）
    if (capturedPwdElRef && !isRendered(capturedPwdElRef) && noFreshErr()) return true;
    const bodyText = ((document.body && document.body.innerText) || '').slice(0, 4000);
    if (LOGIN_SUCCESS_TEXT_RE.test(bodyText) && noFreshErr()) return true;
    if (!scopeUp && !LOGIN_PATH_RE.test(nowPath) && noFreshErr()) return true;
    return false; // 没有明确成功信号不当成功（保守）
  }

  // --- 30 分钟同凭据去重（指纹存 chrome.storage.session，profile 级共享）---
  function credentialFingerprint(username, password, originHint) {
    const origin = originHint || window.location.origin;
    const raw = `${origin}|${username}|${password}`;
    let hash = 0;
    for (let i = 0; i < raw.length; i++) hash = ((hash << 5) - hash + raw.charCodeAt(i)) | 0;
    return `${origin}|${username}|${hash}`;
  }
  function loadPromptedKeys(cb) {
    chrome.storage.session.get([PWD_PROMPTED_KEY], (r) => cb((r && r[PWD_PROMPTED_KEY]) || {}));
  }
  function markCredentialPrompted(fingerprint) {
    loadPromptedKeys((map) => {
      map = map || {};
      map[fingerprint] = Date.now();
      const cutoff = Date.now() - PWD_PROMPTED_TTL;
      for (const k of Object.keys(map)) if (map[k] < cutoff) delete map[k];
      chrome.storage.session.set({ [PWD_PROMPTED_KEY]: map });
    });
  }
  function wasCredentialPrompted(fingerprint, cb) {
    loadPromptedKeys((map) => {
      const ts = map && map[fingerprint];
      cb(!!ts && (Date.now() - ts) < PWD_PROMPTED_TTL);
    });
  }

  // --- 待存登录态持久化（跨整页跳转）---
  function setPendingSession(data) { chrome.storage.session.set({ [PWD_PENDING_KEY]: data }); }
  function loadPendingSession(cb) { chrome.storage.session.get([PWD_PENDING_KEY], (r) => cb((r && r[PWD_PENDING_KEY]) || null)); }
  // 分步登录自动关联：记住本 origin 最近一次输入的用户名，password-only 屏带出
  // （服务端 username_required，不允许空用户名入库；不引占位符防污染去重）。
  const PWD_USER_KEY_PREFIX = 'cdpPwdUser@';
  function rememberUsername(username) {
    const u = String(username || '').trim();
    if (!u) return;
    try { chrome.storage.session.set({ [PWD_USER_KEY_PREFIX + window.location.origin]: { u, at: Date.now() } }); } catch (_) {}
  }
  function recallUsername(cb) {
    const key = PWD_USER_KEY_PREFIX + window.location.origin;
    try {
      chrome.storage.session.get([key], (r) => {
        const hit = r && r[key];
        cb(hit && hit.u ? String(hit.u) : '');
      });
    } catch (_) { cb(''); }
  }
  // 记录本次生成的随机密码，供 popup「本次生成」回看。按站点存最近一条，
  // 保留 10 分钟（用户当下没复制才有意义，久了就是过期垃圾）。
  const PWD_LASTGEN_TTL = 10 * 60 * 1000;
  function recordGeneratedPassword(password) {
    if (!password) return;
    try {
      chrome.storage.session.get([PWD_LASTGEN_KEY], (r) => {
        const map = (r && r[PWD_LASTGEN_KEY]) || {};
        map[currentSiteHost()] = { password, at: Date.now() };
        // 顺手清掉过期项，避免 session 存储无限增长
        const now = Date.now();
        for (const host of Object.keys(map)) {
          if (!map[host] || now - (map[host].at || 0) > PWD_LASTGEN_TTL) delete map[host];
        }
        chrome.storage.session.set({ [PWD_LASTGEN_KEY]: map });
      });
    } catch (_) { /* session 存储不可用时静默：只是回看便利，不该影响生成 */ }
  }
  function clearPendingLogin() {
    if (loginCheckTimer) { clearTimeout(loginCheckTimer); loginCheckTimer = null; }
    stopDomWatch();
    capturedPwdElRef = null;
    pendingLogin = null;
    loginAttemptKey = null;
    loginPromptShown = false;
    // 任何一方清 pending 都要抹掉共享快照：iframe 弹卡失败/取消时顶层才不会凭旧
    // pending 在无关的整页跳转后误弹一次（chrome.storage.session 跨 frame 共享）。
    setPendingSession(null);
  }
  function capturePendingLogin(username, password, pwdEl) {
    if (!password) return; // password-only 也捕获（分步登录第二屏/改密码页），username 走自动关联
    const key = `${window.location.origin}|${username}|${password}`;
    if (loginAttemptKey === key && pendingLogin) return;
    loginAttemptKey = key;
    loginPromptShown = false;
    capturedPwdElRef = pwdEl && pwdEl.isConnected ? pwdEl : null;
    capturedTitle = document.title;
    capturedAt = Date.now();
    startDomWatch();
    rememberUsername(username); // 有用户名就记住，供后续 password-only 屏带出
    pendingLogin = { username, password, domain: currentSiteHost(), url: window.location.href, capturedAt, fromIframe: !isTopFrame };
    setPendingSession(pendingLogin);
    scheduleSuccessCheck(PWD_SUCCESS_CHECK_DELAY);
  }
  function scheduleSuccessCheck(delay) {
    if (loginCheckTimer) clearTimeout(loginCheckTimer);
    loginCheckTimer = setTimeout(() => {
      loginCheckTimer = null;
      if (!pendingLogin) return;
      if (Date.now() - pendingLogin.capturedAt > PWD_CAPTURE_MAX_AGE) { clearPendingLogin(); return; }
      if (inferLoginSuccess()) tryPromptSave(pendingLogin);
      else scheduleSuccessCheck(PWD_SUCCESS_RECHECK_DELAY);
    }, delay);
  }
  function tryPromptSave(captured) {
    if (loginPromptShown) return;
    loginPromptShown = true;
    const fp = credentialFingerprint(captured.username || '', captured.password || '', captured.domain);
    wasCredentialPrompted(fp, (already) => {
      if (already) { clearPendingLogin(); return; }
      // ⚠ 不在这里标记：chrome.storage.session 跨 frame 共享，iframe 先标记会让
      // 顶层接收方误判「已弹过」→ iframe 登录永远弹不出卡。标记只交给真正画卡的一方。
      void ensureUsername(captured).then((captured2) => {
        if (isTopFrame) { markCredentialPrompted(fp); doPromptSave(captured2); return; }
        // iframe：经 background 中转回顶层画卡（顶层自己查去重并标记）。明文只走扩展
        // 内部通道（runtime→tabs frameId:0），不经页面消息广播，不跨 web origin。
        try {
          chrome.runtime.sendMessage({ cmd: 'pwd_frame_capture', cred: captured2 }, (resp) => {
            if (chrome.runtime.lastError || !resp || !resp.ok) {
              markCredentialPrompted(fp); // 中转失败→就地画卡，此时才标记
              doPromptSave(captured2);
            }
          });
        } catch (_) { markCredentialPrompted(fp); doPromptSave(captured2); }
      });
    });
  }
  // 密码-only 自动关联同域账号：① session 里本 origin 刚输过的用户名 → ② 服务端该域唯一账号
  async function ensureUsername(captured) {
    if (captured.username) return captured;
    let username = await new Promise((res) => recallUsername(res));
    if (!username) {
      try {
        const resp = await sendPwdRequest('pwd_list', { domain: captured.domain });
        const entries = (resp && resp.ok && resp.entries) || [];
        if (entries.length === 1) username = entries[0].username || '';
      } catch (_) { /* 服务端不可达就留空，弹卡里补 */ }
    }
    return { ...captured, username };
  }
  async function doPromptSave(captured) {
    const { username, password, domain, url } = captured;
    const resp = await sendPwdRequest('pwd_list', { domain });
    const entries = (resp && resp.ok && resp.entries) || [];
    const existing = entries.find((e) => e.username === username);
    let mode = 'save';
    if (existing) {
      // 已有同账号记录：**先比密码是否真的变了**，没变就静默跳过——
      // 否则每次登录都问「更新登录信息？」，用户烦。
      const cur = await sendPwdRequest('pwd_get', { id: existing.id });
      const stored = (cur && cur.ok && cur.entry && cur.entry.password) || '';
      if (stored && stored === password) {
        clearPendingLogin();
        return; // 一模一样，不打扰
      }
      mode = 'update';
    }
    showSaveConfirm(username, domain, password, mode, async (result) => {
      if (!result) { clearPendingLogin(); return; }
      const finalUsername = (result.username || username).trim();
      const finalPassword = result.password || password;
      if (!finalUsername) { clearPendingLogin(); return; }
      const saveResp = await sendPwdRequest('pwd_save', { domain, username: finalUsername, password: finalPassword, url });
      if (saveResp && saveResp.ok) {
        // 先关弹框，再在右下角短暂提示「已保存」——用户确认后立刻收起，
        // 不要停留在弹框上等 2 秒。离线保存（未连服务端）存的是本地缓存，
        // 重连后由 background 自动补送，文案要说清楚免得用户以为同步完了。
        closeSaveCard();
        showSavedToast(saveResp.offline ? '已保存到本地，联网后自动同步' : '密码已保存');
      }
      else { setSaveStatus('保存失败：' + ((saveResp && saveResp.error) || '操作失败'), false); }
    });
    clearPendingLogin();
  }

  // 保存成功提示：独立 Shadow host，`all:initial` 隔离页面 CSS（与弹卡同款做法），
  // 3 秒后自消。文案是固定串，无用户数据。
  function showSavedToast(text) {
    const host = document.createElement('div');
    host.id = 'tmwd-pwd-toast';
    host.style.cssText = 'all:initial;position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
    const shadow = host.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = '.toast{position:fixed;right:20px;bottom:20px;pointer-events:auto;'
      + 'background:#18a058;color:#fff;font:600 13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;'
      + 'padding:10px 16px;border-radius:10px;box-shadow:0 8px 24px rgba(18,24,38,.24);'
      + 'opacity:0;transform:translateY(6px);transition:opacity .18s ease,transform .18s ease;}'
      + '.toast.on{opacity:1;transform:none;}';
    const box = document.createElement('div');
    box.className = 'toast';
    box.textContent = text;
    shadow.append(style, box);
    (document.body || document.documentElement).appendChild(host);
    requestAnimationFrame(() => box.classList.add('on'));
    setTimeout(() => {
      box.classList.remove('on');
      setTimeout(() => host.remove(), 220);
    }, 2600);
  }

  // --- 右下角保存弹卡（Shadow DOM，全部节点 createElement+textContent 防 XSS）---
  let pwdCardHost = null;
  let pwdCardEls = {};
  let pwdCardOnConfirm = null;
  let pwdCardAutoDismissTimer = null;
  // 弹卡的「注销守卫」句柄。closeSaveCard 必须先 release，否则 SPA 重挂守卫
  // 看到节点脱离文档会立刻挂回来——弹框点取消/保存都关不掉。
  let pwdCardReleaseGuard = null;
  function mountPwdCardHost() {
    if (pwdCardHost && pwdCardHost.parentNode) return;
    if (!pwdCardHost) {
      pwdCardHost = document.createElement('div');
      pwdCardHost.id = PWD_CARD_HOST_ID;
      pwdCardHost.style.cssText = 'all:initial;position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
      const shadow = pwdCardHost.attachShadow({ mode: 'open' });
      const style = document.createElement('style');
      style.textContent = `
        :host{ --tmwd-primary:#b45309; --tmwd-primary-hover:#92400e; --tmwd-primary-fg:#fff;
          --tmwd-bg:#fff; --tmwd-fg:#0f172a; --tmwd-muted:#94a3b8; --tmwd-border:rgba(15,23,42,.10); --tmwd-border-strong:rgba(15,23,42,.22); }
        @media (prefers-color-scheme: dark){ :host{ --tmwd-primary:#4ade80; --tmwd-primary-hover:#22c55e; --tmwd-primary-fg:#0d1117;
          --tmwd-bg:#161b22; --tmwd-fg:#e6edf3; --tmwd-muted:#8b949e; --tmwd-border:rgba(240,246,252,.10); --tmwd-border-strong:rgba(240,246,252,.22); } }
        @keyframes tmwd-pwd-slide{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
        .card{position:fixed;bottom:20px;right:20px;width:340px;max-width:calc(100vw - 40px);display:flex;flex-direction:column;gap:9px;
          padding:16px 18px;background:var(--tmwd-bg);border:1px solid var(--tmwd-border);border-radius:14px;
          box-shadow:0 12px 32px rgba(15,23,42,.20),0 4px 10px rgba(15,23,42,.08);pointer-events:auto;
          font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;color:var(--tmwd-fg);animation:tmwd-pwd-slide .22s ease}
        .title{font-weight:650;font-size:14px}
        .domain{font-size:12px;color:var(--tmwd-muted)}
        .flabel{display:block;font-size:12px;color:var(--tmwd-muted);margin-bottom:3px}
        .input{width:100%;padding:7px 9px;border:1px solid var(--tmwd-border);border-radius:7px;background:var(--tmwd-bg);color:var(--tmwd-fg);font:inherit;font-size:13px;box-sizing:border-box}
        .input:focus{outline:none;border-color:var(--tmwd-primary)}
        .actions{display:flex;gap:8px;justify-content:flex-end;margin-top:2px}
        .btn{padding:6px 14px;border-radius:8px;border:1px solid var(--tmwd-border);background:var(--tmwd-bg);color:var(--tmwd-fg);cursor:pointer;font:inherit;font-size:13px}
        .btn:hover{border-color:var(--tmwd-border-strong)}
        .btn.cancel{background:#f5f5f5}
        @media (prefers-color-scheme: dark){ .btn.cancel{background:#21262d} }
        .btn.ok{background:var(--tmwd-primary);color:var(--tmwd-primary-fg);border-color:var(--tmwd-primary)}
        .btn.ok:hover{background:var(--tmwd-primary-hover)}
        .btn:disabled{opacity:.5;cursor:not-allowed}
        .status{font-size:12px;min-height:16px}
        .status.err{color:#b91c1c}
        .status.ok{color:var(--tmwd-primary)}
      `;
      shadow.appendChild(style);
      const card = document.createElement('div'); card.className = 'card';
      const title = document.createElement('div'); title.className = 'title'; card.appendChild(title);
      const domainEl = document.createElement('div'); domainEl.className = 'domain'; card.appendChild(domainEl);
      const ul = document.createElement('label'); ul.className = 'flabel'; ul.textContent = '用户名 / 账号名';
      const ui = document.createElement('input'); ui.className = 'input'; ui.type = 'text'; ul.appendChild(ui); card.appendChild(ul);
      const pl = document.createElement('label'); pl.className = 'flabel'; pl.textContent = '密码';
      const pi = document.createElement('input'); pi.className = 'input'; pi.type = 'password'; pl.appendChild(pi); card.appendChild(pl);
      const status = document.createElement('div'); status.className = 'status'; card.appendChild(status);
      const actions = document.createElement('div'); actions.className = 'actions';
      const cancel = document.createElement('button'); cancel.className = 'btn cancel'; cancel.textContent = '取消'; cancel.type = 'button';
      const ok = document.createElement('button'); ok.className = 'btn ok'; ok.type = 'button';
      actions.appendChild(cancel); actions.appendChild(ok); card.appendChild(actions);
      shadow.appendChild(card);
      pwdCardEls = { title, domain: domainEl, username: ui, password: pi, status, ok, cancel };
      ui.addEventListener('input', () => { ok.disabled = !ui.value.trim(); });
      cancel.addEventListener('click', () => closeSaveCard());
      ok.addEventListener('click', () => {
        if (ok.disabled) return;
        const cb = pwdCardOnConfirm; pwdCardOnConfirm = null;
        if (cb) cb({ username: ui.value.trim(), password: pi.value });
      });
      pi.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); ok.click(); } });
    }
    document.documentElement.appendChild(pwdCardHost);
    // SPA 重渲染 detach 后复用同一节点，输入值不丢。重复挂时先释放旧守卫避免累积。
    if (pwdCardReleaseGuard) { try { pwdCardReleaseGuard(); } catch (_) {} pwdCardReleaseGuard = null; }
    if (window.__tmwdMountGuard) pwdCardReleaseGuard = window.__tmwdMountGuard(() => pwdCardHost, mountPwdCardHost);
  }
  function showSaveConfirm(username, domain, password, mode, onConfirm) {
    removeSuggestions();
    mountPwdCardHost();
    pwdCardEls.title.textContent = mode === 'update' ? '更新登录信息？' : '保存登录信息？';
    pwdCardEls.domain.textContent = '域名：' + domain;
    pwdCardEls.username.value = username || '';
    pwdCardEls.password.value = password || '';
    pwdCardEls.ok.textContent = mode === 'update' ? '更新' : '保存';
    pwdCardEls.ok.disabled = !String(username || '').trim();
    pwdCardEls.status.textContent = '';
    pwdCardEls.status.className = 'status';
    pwdCardOnConfirm = onConfirm;
    try { pwdCardEls.username.focus(); pwdCardEls.username.select(); } catch (_) {}
    if (pwdCardAutoDismissTimer) clearTimeout(pwdCardAutoDismissTimer);
    pwdCardAutoDismissTimer = setTimeout(() => { if (pwdCardHost && pwdCardHost.parentNode) closeSaveCard(true); }, 30000);
  }
  function setSaveStatus(msg, isOk) {
    if (!pwdCardEls.status) return;
    pwdCardEls.status.textContent = msg || '';
    pwdCardEls.status.className = 'status ' + (isOk ? 'ok' : 'err');
    if (!isOk && pwdCardEls.ok) pwdCardEls.ok.disabled = false; // 失败允许重试
  }
  function closeSaveCard(autoDismissed) {
    if (pwdCardAutoDismissTimer) { clearTimeout(pwdCardAutoDismissTimer); pwdCardAutoDismissTimer = null; }
    // 先注销重挂守卫，否则移除节点会触发守卫把它挂回来——弹框关不掉的根因。
    if (pwdCardReleaseGuard) { try { pwdCardReleaseGuard(); } catch (_) {} pwdCardReleaseGuard = null; }
    if (pwdCardHost && pwdCardHost.parentNode) pwdCardHost.parentNode.removeChild(pwdCardHost);
    if (autoDismissed && pwdCardOnConfirm) { const cb = pwdCardOnConfirm; pwdCardOnConfirm = null; cb(null); }
    else pwdCardOnConfirm = null;
  }

  // --- 候选下拉 + 随机生成（同时只存在一个下拉）---
  let suggestHost = null;
  let suppressSuggestUntil = 0;
  let suggestSeq = 0;
  let suggestTargetInput = null;
  function removeSuggestions() {
    suggestSeq++; // 让在途回调失效
    if (suggestHost && suggestHost.parentNode) suggestHost.parentNode.removeChild(suggestHost);
    suggestHost = null;
  }
  function setInputValue(input, value) {
    if (!input) return;
    const proto = input.constructor && input.constructor.prototype;
    const setter = (proto && Object.getOwnPropertyDescriptor(proto, 'value') || {}).set
      || (Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value') || {}).set;
    if (setter) setter.call(input, value); else input.value = value;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: value }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }
  // 站点标识用 location.host（含非默认端口）而非 hostname——否则
  // 127.0.0.1:7890 与 127.0.0.1:8080 会被当成同一站点，密码互相串台。
  // 默认端口（http:80 / https:443）在 location.host 里本就省略，故正常站点不受影响。
  function currentSiteHost() {
    const host = String(window.location.host || '').toLowerCase();
    return host || String(window.location.hostname || '').toLowerCase();
  }
  function normalizeHost(value) {
    const s = String(value || '').trim().replace(/^[a-z]+:\/\//i, '').replace(/^www\./i, '');
    const i = s.indexOf('/');
    // 去掉默认端口，让 example.com:443 与 example.com 视为同一站点
    return (i >= 0 ? s.slice(0, i) : s).toLowerCase().replace(/:(80|443)$/, '');
  }
  // 端口拆出来：sub.example.com:8080 → ['sub.example.com', '8080']
  function splitHostPort(value) {
    const s = normalizeHost(value);
    const i = s.lastIndexOf(':');
    if (i < 0) return [s, ''];
    return [s.slice(0, i), s.slice(i + 1)];
  }
  function matchesCurrentDomain(pwdDomain) {
    const a = normalizeHost(pwdDomain), b = normalizeHost(currentSiteHost());
    if (!a || !b) return false;
    if (a === b) return true;
    // 子域放宽仅在端口一致时才适用，否则 a.com:8080 会匹配到 a.com:9090
    const [aHost, aPort] = splitHostPort(a);
    const [bHost, bPort] = splitHostPort(b);
    if (aPort !== bPort) return false;
    return aHost.endsWith('.' + bHost) || bHost.endsWith('.' + aHost);
  }
  function positionDropdown(list, inputElement) {
    if (!list || !inputElement) return;
    const r = inputElement.getBoundingClientRect();
    const w = Math.min(360, Math.max(220, r.width));
    const spaceBelow = window.innerHeight - r.bottom;
    const showBelow = spaceBelow >= 220 || spaceBelow >= window.innerHeight - r.top;
    list.style.left = r.left + 'px';
    list.style.width = w + 'px';
    if (showBelow) { list.style.top = (r.bottom + 4) + 'px'; list.style.bottom = 'auto'; }
    else { list.style.top = 'auto'; list.style.bottom = (window.innerHeight - r.top + 4) + 'px'; }
  }
  function createSuggestHost() {
    removeSuggestions();
    const host = document.createElement('div');
    host.id = PWD_SUGGEST_HOST_ID;
    host.style.cssText = 'all:initial;position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
    const shadow = host.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = `
      .list{position:fixed;pointer-events:auto;background:#fff;color:#0f172a;border:1px solid rgba(15,23,42,.12);
        border-radius:10px;box-shadow:0 10px 26px rgba(15,23,42,.18);font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
        max-height:260px;overflow-y:auto;min-width:220px}
      @media (prefers-color-scheme: dark){ .list{background:#161b22;color:#e6edf3;border-color:rgba(240,246,252,.12)} }
      .item{padding:8px 12px;cursor:pointer;display:flex;flex-direction:column;gap:2px}
      .item:hover{background:rgba(180,83,9,.08)}
      @media (prefers-color-scheme: dark){ .item:hover{background:rgba(74,222,128,.12)} }
      .item+.item{border-top:1px solid rgba(15,23,42,.06)}
      .u{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .h{font-size:11px;color:#94a3b8}
    `;
    shadow.appendChild(style);
    const list = document.createElement('div'); list.className = 'list';
    shadow.appendChild(list);
    document.documentElement.appendChild(host);
    suggestHost = host;
    return list;
  }
  function fillLoginForm(username, password) {
    let scope = findLoginScope();
    if (!scope && suggestTargetInput) {
      const p = findPasswordFieldFor(suggestTargetInput);
      if (p) scope = { username: suggestTargetInput, password: p };
    }
    if (!scope) return false;
    if (scope.username) setInputValue(scope.username, username);
    if (scope.password) setInputValue(scope.password, password);
    return true;
  }
  function maybeShowSuggestions(input) {
    if (Date.now() < suppressSuggestUntil) return;
    const scope = findLoginScope();
    const passwordInput = findPasswordFieldFor(input) || (scope && scope.username === input ? scope.password : null);
    const strict = isUsernameInput(input);
    const relaxed = isCandidateUsernameInput(input) && !!passwordInput;
    if (!strict && !relaxed) { removeSuggestions(); return; }
    suggestTargetInput = input;
    const seq = ++suggestSeq;
    sendPwdRequest('pwd_list', { domain: currentSiteHost() }).then((resp) => {
      if (seq !== suggestSeq || document.activeElement !== input) return;
      let entries = (resp && resp.ok && resp.entries) || [];
      const finish = (list) => {
        const q = String(input.value || '').trim().toLowerCase();
        if (q) list = list.filter((e) => String(e.username || '').toLowerCase().includes(q));
        renderSuggestions(list, input, passwordInput);
      };
      if (entries.length) { finish(entries); return; }
      // 精确域无果 → 全量查 + 客户端归一化匹配
      sendPwdRequest('pwd_list', {}).then((r2) => {
        if (seq !== suggestSeq || document.activeElement !== input) return;
        finish(((r2 && r2.ok && r2.entries) || []).filter(matchesCurrentDomain));
      });
    });
  }
  function renderSuggestions(entries, input, passwordInput) {
    if (!entries.length) { removeSuggestions(); return; }
    const mode = passwordInput ? 'full' : 'username-only';
    const list = createSuggestHost();
    for (const entry of entries) {
      const item = document.createElement('div'); item.className = 'item';
      const u = document.createElement('div'); u.className = 'u'; u.textContent = entry.username || ''; item.appendChild(u);
      const h = document.createElement('div'); h.className = 'h'; h.textContent = mode === 'full' ? '点击填充' : '点击填入账号'; item.appendChild(h);
      item.addEventListener('mousedown', (e) => { e.preventDefault(); e.stopPropagation(); });
      item.addEventListener('click', (e) => {
        e.preventDefault(); e.stopPropagation();
        suppressSuggestUntil = Date.now() + 600;
        removeSuggestions();
        if (mode === 'username-only') { setInputValue(input, entry.username || ''); return; }
        sendPwdRequest('pwd_get', { id: entry.id }).then((r) => {
          if (r && r.ok && r.entry) fillLoginForm(entry.username || '', r.entry.password || '');
        });
      });
      list.appendChild(item);
    }
    positionDropdown(list, input);
  }
  function renderGeneratePreview(passwordInput, pwd) {
    recordGeneratedPassword(pwd); // 每次预览即记录，用户没点「使用」也能回 popup 找回
    const list = createSuggestHost();
    const prev = document.createElement('div'); prev.className = 'item';
    const code = document.createElement('div'); code.className = 'u'; code.textContent = pwd; prev.appendChild(code);
    const useIt = document.createElement('div'); useIt.className = 'h'; useIt.textContent = '点击使用此密码'; prev.appendChild(useIt);
    prev.addEventListener('mousedown', (e) => { e.preventDefault(); e.stopPropagation(); });
    prev.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      suppressSuggestUntil = Date.now() + 600;
      removeSuggestions();
      setInputValue(passwordInput, pwd);
    });
    list.appendChild(prev);
    const regen = document.createElement('div'); regen.className = 'item';
    const rl = document.createElement('div'); rl.className = 'u'; rl.textContent = '🔄 重新生成'; regen.appendChild(rl);
    regen.addEventListener('mousedown', (e) => { e.preventDefault(); e.stopPropagation(); });
    regen.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      renderGeneratePreview(passwordInput, (typeof tmwdGeneratePassword === 'function') ? tmwdGeneratePassword(16) : '');
    });
    list.appendChild(regen);
    positionDropdown(list, passwordInput);
  }
  function showGenerateDropdown(passwordInput) {
    const list = createSuggestHost();
    const item = document.createElement('div'); item.className = 'item';
    const u = document.createElement('div'); u.className = 'u'; u.textContent = '🎲 生成随机密码'; item.appendChild(u);
    item.addEventListener('mousedown', (e) => { e.preventDefault(); e.stopPropagation(); });
    item.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      renderGeneratePreview(passwordInput, (typeof tmwdGeneratePassword === 'function') ? tmwdGeneratePassword(16) : '');
    });
    list.appendChild(item);
    positionDropdown(list, passwordInput);
  }

  // --- 请求助手（仿 sendChat：reqId 多路复用 + 12s 超时）---
  const pwdReqListeners = new Map(); // reqId -> cb(data)
  function sendPwdRequest(cmd, payload) {
    return new Promise((resolve) => {
      const reqId = 'pwd_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
      let settled = false;
      const done = (v) => { if (!settled) { settled = true; pwdReqListeners.delete(reqId); resolve(v); } };
      pwdReqListeners.set(reqId, (data) => {
        if (!data) return done({ ok: false, error: '操作失败' });
        if (data.type === 'pwd_error') return done({ ok: false, error: data.message || '操作失败' });
        if (data.type === 'pwd_list_result') return done({ ok: true, entries: data.entries || [], offline: !!data.offline });
        if (data.type === 'pwd_get_result') return done({ ok: true, entry: data.entry || null, offline: !!data.offline });
        if (data.type === 'pwd_save_result') return done({ ok: true, entry: data.entry || null, offline: !!data.offline });
        if (data.type === 'pwd_delete_result') return done({ ok: true, id: data.id, offline: !!data.offline });
        done({ ok: false, error: '操作失败' });
      });
      chrome.runtime.sendMessage({ cmd, reqId, ...payload }, (resp) => {
        if (!resp || !resp.ok) done({ ok: false, error: (resp && resp.error) || '连接失败，请确认服务已启动、地址与 Token 正确' });
      });
      setTimeout(() => done({ ok: false, error: '请求超时，请稍后重试' }), 12000);
    });
  }

  // --- 统一捕获入口：唯一硬闸门 =「渲染中且有值的密码框」---
  // 防搜索框/普通点击误报的关键：任何触发路径都先过这道闸。
  function captureLatestLogin(reason, root) {
    // 有 form 上下文时只认该 form 内的作用域（findLoginScopeIn(form) 的 password 必在
    // form 子树内）——form 里没有渲染中的密码框就直接放弃，绝不回退全局抓别处的密码框，
    // 否则「表单 A 提交却捕了表单 B 的密码」。root 为空（SPA 无 form 按钮/回车在无 form
    // 输入框）才走全局。
    let scope = null;
    if (root && root.tagName === 'FORM') {
      scope = findLoginScopeIn(root);
      if (!scope) return;
    } else {
      scope = findLoginScope();
    }
    if (!scope || !scope.password) return;
    const pwd = scope.password;
    // 闸门：渲染中、非我们自己的 UI、且有值。防搜索框/弹卡自指/普通点击误报。
    if (isInjectedUi(pwd) || !isRendered(pwd) || !String(pwd.value || '')) return;
    const { username, password } = getFormData(scope);
    removeSuggestions();
    capturePendingLogin(username || '', password || pwd.value, pwd);
  }
  // 取真实内部节点：shadow DOM 里事件 e.target 会被 retarget 成 host
  function evtInnerTarget(e) {
    const path = (typeof e.composedPath === 'function' ? e.composedPath() : null) || [];
    return path.length ? path[0] : e.target;
  }

  // --- 捕获触发（submit / 任意点击 / 回车 / 离开页面兜底）---
  document.addEventListener('submit', (e) => {
    const form = evtInnerTarget(e);
    if (form && form.tagName === 'FORM') captureLatestLogin('submit', form);
  }, true);
  document.addEventListener('click', (e) => {
    const inner = evtInnerTarget(e);
    if (!inner || isInjectedUi(inner)) return;
    const btn = inner.closest ? inner.closest('button, input[type="submit"], input[type="button"], a, [role="button"]') : null;
    if (!btn) return;
    // 放宽：不再要求按钮文案匹配。有值密码框作用域内的任何可点元素、或登录样按钮
    // 都先捕获——误置的 pending 只会在真出现成功信号时弹卡，120s 超时自动清。
    const scope = findLoginScope();
    const inScope = !!(scope && scope.password && (
      (scope.form && scope.form.contains(btn)) ||
      ((scope.password.closest('div, section, article, fieldset, dialog, [role="dialog"], [role="form"]') || document).contains(btn))));
    if (!looksLikeLoginButton(btn) && !inScope) return;
    if (!scope) {
      // 分步登录第一屏（页面还没有密码框）：记住已输用户名，第二屏 password-only 带出
      const root = btn.closest('form') || btn.closest('div, section, article, dialog, [role="dialog"], [role="form"]') || document;
      for (const inp of deepQueryAllInputs(root)) {
        if (inp !== btn && String(inp.value || '').trim() && isUsernameInput(inp)) { rememberUsername(String(inp.value).trim()); break; }
      }
      return;
    }
    captureLatestLogin('click', btn.closest('form'));
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const inner = evtInnerTarget(e);
    if (!inner || inner.tagName !== 'INPUT' || isInjectedUi(inner)) return;
    // 回车在用户名框也算（第一屏回车进第二屏，先记住用户名；密码框回车照旧捕获）
    if (!isPasswordInput(inner) && String(inner.value || '').trim()
      && (isUsernameInput(inner) || isCandidateUsernameInput(inner))) {
      rememberUsername(String(inner.value).trim());
    }
    captureLatestLogin('enter', inner.closest('form'));
  }, true);
  // 兜底：无按钮点击的 SPA 原地登录/回车后自跳转——离开页面前若密码框仍渲染且有值
  // 再补一次捕获。由成功判定决定弹不弹，多捕无害。
  function captureOnPageLeave() {
    if (!pendingLogin) captureLatestLogin('pageleave');
  }
  window.addEventListener('pagehide', captureOnPageLeave, true);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') captureOnPageLeave(); }, true);

  // --- 候选 / 生成下拉触发 ---
  // focusin（非 focus）：composed 事件穿 shadow DOM；文档层 e.target 被 retarget 成
  // host，取 composedPath()[0] 拿真实内部 input。
  document.addEventListener('focusin', (e) => {
    const t = evtInnerTarget(e);
    if (!t || t.tagName !== 'INPUT' || isInjectedUi(t)) return;
    if (isUsernameInput(t)) { maybeShowSuggestions(t); return; }
    if (isPasswordInput(t)) showGenerateDropdown(t);
  }, true);
  document.addEventListener('input', (e) => {
    const t = evtInnerTarget(e);
    if (t && t.tagName === 'INPUT' && !isInjectedUi(t) && isUsernameInput(t) && suggestHost) maybeShowSuggestions(t);
  }, true);
  document.addEventListener('mousedown', (e) => {
    if (suggestHost && e.composedPath && !e.composedPath().includes(suggestHost)) removeSuggestions();
  }, true);

  // 启动时读回待存登录态（跨整页跳转场景）——只在顶层恢复：iframe 各自恢复同一条
  // 会弹多张卡；iframe 的 pendingLogin 只在本 frame 生命周期内存活（弹卡中转见上）。
  if (isTopFrame) loadPendingSession((stored) => {
    if (stored && stored.capturedAt && (Date.now() - stored.capturedAt) <= PWD_CAPTURE_MAX_AGE) {
      pendingLogin = stored;
      loginAttemptKey = null;
      scheduleSuccessCheck(PWD_SUCCESS_CHECK_DELAY + 800);
    } else if (stored) {
      clearPendingLogin();
    }
  });

  // 顶层：接收 background 转发的 iframe 捕获，把保存卡画在顶层（凭据明文只经扩展通道）。
  if (isTopFrame) {
    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
      if (!msg || msg.cmd !== 'pwd_show_save_card' || !msg.cred) return;
      // 必须同步回执：去重/画卡在 chrome.storage 回调里异步跑，晚回 sendResponse
      // 会让发送方（iframe）收到 runtime.lastError 走兜底 → iframe 也画一张 → 双卡。
      sendResponse({ ok: true });
      const cred = msg.cred;
      const fp = credentialFingerprint(cred.username || '', cred.password || '', cred.domain || '');
      wasCredentialPrompted(fp, (already) => {
        if (!already) {
          markCredentialPrompted(fp);
          doPromptSave({ username: cred.username || '', password: cred.password || '', domain: cred.domain || currentSiteHost(), url: cred.url || window.location.href });
        }
      });
    });
  }

  // 服务端回帧：按 reqId 派发给等待中的请求
  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || msg.cmd !== 'pwd_frame' || !msg.data) return;
    const cb = pwdReqListeners.get(String(msg.data.reqId));
    if (cb) cb(msg.data);
  });

  // 截图隐藏：与面板的监听共存，这里不 sendResponse / 不 return true（面板拥有响应权）。
  // 弹卡含明文账号名 + 真实密码框，Page.captureScreenshot 会把 Shadow DOM 合进 base64
  // 回传 agent，必须隐藏。
  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg) return;
    const hide = msg.cmd === 'hide_panel_for_screenshot';
    const show = msg.cmd === 'show_panel_after_screenshot';
    if (!hide && !show) return;
    for (const el of [pwdCardHost, suggestHost]) if (el) el.style.display = hide ? 'none' : '';
  });

  // --- agent 触发的凭据填充：值只进 DOM，绝不回给模型 ---
  // 与「人点下拉」复用同一套 fill 逻辑（setInputValue / findLoginScope /
  // fillLoginForm），只是入口由 web(fill_credential) 命令触发。密码明文由扩展
  // 本地（pwd_cache 离线镜像或服务端 pwd_get）解析，服务端连明文都看不到；2FA 码
  // 由服务端算好随命令下传。回执只含「填没填、填哪个字段、匹配到哪个条目」。
  const OTP_HINT_RE = /(code|otp|totp|2fa|mfa|verify|verification|one[-\s]?time|auth|验证码|动态码|校验码|短信|手机验证)/i;
  function findOtpField(selector) {
    if (selector) {
      const el = deepQuerySelector(document, selector);
      if (el && el.tagName === 'INPUT' && isRendered(el)) return el;
    }
    const byAc = deepQuerySelector(document, 'input[autocomplete="one-time-code"]');
    if (byAc && isRendered(byAc) && !byAc.disabled && !byAc.readOnly) return byAc;
    const inputs = deepQueryAllInputs(document).filter((el) => isRendered(el) && !el.disabled && !el.readOnly);
    const maxLen = (el) => { const n = parseInt(el.getAttribute('maxlength') || el.maxLength, 10); return Number.isFinite(n) ? n : 0; };
    // ① 关键字命中（name/id/aria-label/placeholder）且是短数字框
    for (const el of inputs) {
      if (isPasswordInput(el)) continue;
      const hay = [el.name, el.id, el.getAttribute('aria-label'), el.placeholder, el.getAttribute('data-testid')].join(' ');
      if (!OTP_HINT_RE.test(hay)) continue;
      const ml = maxLen(el);
      if (ml >= 4 && ml <= 8) return el;
    }
    // ② 数字型 + 4~8 位上限（无关键字也认，很多站点只有 inputmode=numeric）
    for (const el of inputs) {
      if (isPasswordInput(el)) continue;
      const type = String(el.type || '').toLowerCase();
      const im = String(el.getAttribute('inputmode') || '').toLowerCase();
      if (type === 'number' || im === 'numeric') {
        const ml = maxLen(el);
        if (ml >= 4 && ml <= 8) return el;
      }
    }
    // ③ 登录作用域里唯一剩下的非用户/密码输入框
    const scope = findLoginScope();
    if (scope && scope.form) {
      const cand = deepQueryAllInputs(scope.form).filter(
        (el) => isRendered(el) && !isPasswordInput(el) && el !== scope.username && !isUsernameInput(el),
      );
      if (cand.length === 1) return cand[0];
    }
    return null;
  }

  function describeField(el) {
    if (!el) return null;
    return {
      tag: 'input',
      name: el.name || '',
      id: el.id || '',
      type: String(el.type || '').toLowerCase(),
      autocomplete: el.getAttribute('autocomplete') || '',
    };
  }

  async function handleFillCredential(msg) {
    const kind = String(msg.kind || '').toLowerCase();
    if (kind === 'otp') {
      const field = findOtpField(String(msg.selector || ''));
      if (!field) return { ok: false, filled: false, reason: 'otp_field_not_found' };
      const value = String(msg.value || '');
      if (!value) return { ok: false, filled: false, reason: 'empty_code' };
      setInputValue(field, value);
      const receipt = { ok: true, filled: true, field: describeField(field) };
      if (msg.expiresAt !== undefined) receipt.expiresAt = msg.expiresAt;
      return receipt; // 不含 code
    }
    if (kind === 'password') {
      let password = '';
      let username = String(msg.username || '');
      if (msg.entryId !== undefined && msg.entryId !== null) {
        const r = await sendPwdRequest('pwd_get', { id: msg.entryId });
        if (r && r.ok && r.entry) { password = String(r.entry.password || ''); username = String(r.entry.username || username); }
        else return { ok: false, filled: false, reason: (r && r.error) || 'password_unavailable' };
      }
      if (!password) return { ok: false, filled: false, reason: 'password_unavailable' };
      const scope = findLoginScope();
      if (!scope || !scope.password) return { ok: false, filled: false, reason: 'login_form_not_found' };
      if (scope.username) setInputValue(scope.username, username);
      setInputValue(scope.password, password);
      return { ok: true, filled: true, field: describeField(scope.password), username }; // 不含 password
    }
    return { ok: false, filled: false, reason: 'unknown_kind' };
  }

  // content.js 在每个 frame 都注入，chrome.tabs.sendMessage 会广播给所有 frame。
  // 因此**只有能真正填充的 frame 才回执**（同步判断，能填才 return true 挂起通道），
  // 其余 frame 静默 opt-out——否则「找不到字段的 frame」会抢先把 ok:false 当结果，
  // 而真正含表单的 iframe（SSO 常见）永远轮不上。全 frame 都无法填充时 sendMessage
  // 以 undefined/异常收尾，由 background 归一为 field_not_found。
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.cmd !== 'fill_credential') return;
    const kind = String(msg.kind || '').toLowerCase();
    const canFill = kind === 'otp'
      ? !!findOtpField(String(msg.selector || ''))
      : (kind === 'password' && (() => { const s = findLoginScope(); return !!(s && s.password); })());
    if (!canFill) return; // 本 frame 无目标字段：不响应，让别的 frame 接
    handleFillCredential(msg).then(sendResponse).catch((e) => sendResponse({ ok: false, filled: false, reason: String(e) }));
    return true; // 异步回执
  });
})();
})();

// --- 网页侧 Agent 对话面板 ---
(function () {
  if (window.self !== window.top) return; // 仅顶层框架挂面板

  const PANEL_ID = 'tmwd-chat-panel';
  const reqListeners = new Map(); // reqId -> callback(event)
  let statusQueryPending = false;

  function updateBridgeIndicator(status) {
    const ind = document.getElementById('ljq-ind');
    if (ind && typeof ind._setBridgeStatus === 'function') ind._setBridgeStatus(status);
  }

  function showBridgeUnavailable(status) {
    const ind = document.getElementById('ljq-ind');
    if (!ind || typeof ind._showBridgeNotice !== 'function') return;
    const errorLabels={invalid:'Token 无效',invalid_token:'Token 无效或已撤销',disabled:'客户端已停用',permission_denied:'没有连接权限',already_connected:'已在另一个浏览器里连接',protocol_required:'服务端版本过旧，需升级',revoked:'客户端授权已撤销'};
    let message;
    if(status&&status.state==='disabled') message='连接已关闭，请先在扩展设置中启用';
    else if(status&&status.state==='unconfigured') message='尚未配置 Token，请在扩展设置中粘贴管理端生成的 Token';
    else if(status&&status.state==='auth_error') message=errorLabels[status.error&&status.error.code]||(status.error&&status.error.message)||'连接失败';
    else if(status&&status.state==='authenticating') message='连接服务中，请稍候…';
    else message='连接失败，请确认服务已启动，并检查扩展设置里的地址和 Token';
    ind._showBridgeNotice(message, 'Ai Lubricant 连接状态');
  }

  function getBridgeStatus() {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ cmd: 'bridge_status_get' }, (resp) => {
        if (chrome.runtime.lastError || !resp || !resp.ok || !resp.data) {
          resolve({ connected: false, authenticated: false, enabled: true, state: 'disconnected', error: null });
          return;
        }
        resolve(resp.data);
      });
    });
  }

  // --- 等桥接连上（重试按钮在桥接断开时的体验）---
  // 点「重试」时若桥接未连上（SW 刚被这次点击唤醒、connectWS 还在握手，或服务端
  // 还没回来），立刻发请求只会被「连接未就绪」吃掉——用户看到的就是「点了没反应」。
  // waitForBridgeReady 让这次点击变成「等重连，连上自动续跑」；轮询为主，
  // bridge_status_push 的 connected 广播到达时立即唤醒，避免干等下一个轮询点。
  const _bridgeReadyWaiters = [];
  function notifyBridgeReady() {
    const waiters = _bridgeReadyWaiters.splice(0);
    for (const fn of waiters) { try { fn(); } catch (_) { /* ignore */ } }
  }
  function bridgeConnected() {
    return getBridgeStatus().then((status) => !!(status && status.connected)).catch(() => false);
  }
  function waitForBridgeReady(timeoutMs = 30000) {
    return new Promise((resolve) => {
      let settled = false;
      let timer = null;
      const started = Date.now();
      const finish = (value) => {
        if (settled) return;
        settled = true;
        if (timer !== null) clearTimeout(timer);
        // 无论以何种方式结束都摘掉广播唤醒钩子，防泄漏。
        const i = _bridgeReadyWaiters.indexOf(onReady);
        if (i >= 0) _bridgeReadyWaiters.splice(i, 1);
        resolve(value);
      };
      const onReady = () => finish(true);
      const stopWaiting = () => finish(false);
      const poll = async () => {
        if (!state.open || state.sending) { stopWaiting(); return; }
        if (await bridgeConnected()) { onReady(); return; }
        if (Date.now() - started >= timeoutMs) { stopWaiting(); return; }
        timer = setTimeout(poll, 2000);
      };
      _bridgeReadyWaiters.push(onReady);
      // 先给 SW 半秒（这次点击本身可能就是唤醒源，连接刚起）；之后 2s 一查。
      timer = setTimeout(poll, 500);
    });
  }

  // 接收 background 转发的对话回帧。
  // 两类帧：
  //  - 一次性应答（reqId）：chat_agents / chat_send_ok / chat_load_conversation …
  //  - 会话广播（conversationId、无 reqId）：agent 一轮运行的实时事件。它们由服务端
  //    按会话扇给所有正在看它的标签页——「发起方」不再有特权，谁订阅谁看到同一条序列。
  // 面板自己维护「我现在在看哪条会话」，只消费它。
  let _watchedConvId = '';
  let _liveSink = null; // 当前活跃的流处理（发起的那轮 / 载入后待续看的尾消息）
  // 缺口恢复的并发护栏：reset/offset-gap 触发的重拉只允许同时跑一个，
  // 避免高频 delta 下的多次重拉互相覆盖 + 把服务端打爆。
  let _gapRecovering = false;

  // ── 滚动跟随（stick-to-bottom）──
  // renderMessages / 流式增量原先无条件把 body 滚到最底：接管轮询（3s 一次）、
  // 工具事件、重连刷新，任何一次重渲染都会把往上翻历史记录的用户拽回底部。
  // 这里记录用户是否贴着底部：贴底才自动跟随；往上翻过/翻回贴底由 scroll 事件
  // 实时更新。只有用户自己的动作（发消息/切会话）才直接置 _stickToBottom=true 强制回底。
  let _stickToBottom = true;
  const _STICK_GAP_PX = 80;
  function bodyNearBottom() {
    const el = els.body;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= _STICK_GAP_PX;
  }
  function stickScrollToBottom() {
    const el = els.body;
    if (!el || !_stickToBottom) return;
    el.scrollTop = el.scrollHeight;
  }

  function setWatchedConversation(convId) {
    const next = String(convId || '');
    if (next === _watchedConvId) return;
    const prev = _watchedConvId;
    _watchedConvId = next;
    // 退订/订阅都尽力而为：SW 不在时静默失败，重连广播会再触发一次 watch。
    if (prev) {
      try { chrome.runtime.sendMessage({ cmd: 'chat_unwatch', conversationId: prev }, () => { void chrome.runtime.lastError; }); } catch (_) { /* ignore */ }
    }
    if (next) {
      try { chrome.runtime.sendMessage({ cmd: 'chat_watch', conversationId: next }, () => { void chrome.runtime.lastError; }); } catch (_) { /* ignore */ }
    }
  }

  // 强制重新登记观察者：setWatchedConversation 对同 id 会早退，但桥接重连后服务端
  // 的事件泵随旧 socket 一起没了、background 的扇出表也可能随 SW 重启清空，必须
  // 重发一次 chat_watch 才能续上实时事件。
  function rewatchConversation(convId) {
    const next = String(convId || '');
    if (!next) return;
    try { chrome.runtime.sendMessage({ cmd: 'chat_watch', conversationId: next }, () => { void chrome.runtime.lastError; }); } catch (_) { /* ignore */ }
  }

  // 桥接重连后的会话自愈。fanout 流式事件不挂 reqId，断流时不会有 chat_error 来
  // 收尾 → state.sending 卡在 true、尾条卡在 streaming，用户看到的就是会话卡死
  // （原问题 2、3 同源）。这里清掉卡住的发送态、重建事件订阅、整页回拉服务端
  // 权威状态（服务端 _persist_progress 已把进行中内容渐进落库，回拉安全），仍
  // streaming 就交给 watchStreamingIfNeeded 续看；输入区停止按钮由 turnRunning()
  // 维持为「停止」，随时可中止服务端那一轮。
  async function recoverAfterReconnect() {
    if (!state.open || !state.conversationId) return;
    rewatchConversation(state.conversationId);
    // 断连时本地事件流已死、无终态帧：先解掉卡住的发送态，refreshCurrentSession
    // 才会放行（它发送中直接返回），随后回拉服务端权威状态。
    if (state.sending) setSending(false);
    await refreshCurrentSession();
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || msg.cmd !== 'chat_frame' || !msg.data) return;
    // 服务端主动推的「可操作 agent 变了」：无 reqId，不进 reqListeners 派发，
    // 直接刷新面板列表（服务端改了 cdp_client 的 agent_ids 后不必重开面板）。
    if (msg.data.type === 'chat_agents_changed') {
      void onAgentsChanged();
      return;
    }
    // 会话广播：conversationId 无 reqId 的实时事件交给当前流；只看正在看的那条。
    if (msg.data.type === 'chat_event' && msg.data.conversationId && !msg.data.reqId) {
      if (String(msg.data.conversationId) !== _watchedConvId) return;
      const ev = msg.data.event;
      if (!ev) return;
      if (!_liveSink) _liveSink = ensureLiveSink();
      if (!_liveSink) return;
      _liveSink(ev);
      if (ev.type === 'done' || ev.type === 'error' || ev.type === 'aborted') _liveSink = null;
      return;
    }
    const cb = reqListeners.get(String(msg.data.reqId));
    if (cb) cb(msg.data);
  });

  // 没有主动 sink 时（刚载入历史、服务端已在跑），尾条 assistant 若还是 streaming，
  // 就把历史消息对象接到流上续看——与「重载后续看」是同一条路径，只是事件实时了。
  function ensureLiveSink() {
    const last = state.messages[state.messages.length - 1];
    if (last && last.role === 'assistant' && last.status === 'streaming') {
      return makeStreamHandler(last);
    }
    return null;
  }

  // agent 列表变更后的就地刷新：picker 开着就重渲染；当前选中的 agent 已被移除
  // 就清空选择退回 picker（否则会对着一个已不可用的 agent 发消息）。
  async function onAgentsChanged() {
    if (!state.open) return;
    await refreshAgents();
    if (state.view === 'picker') { showPicker(); return; }
    if (state.selectedAgent && !state.agents.some((a) => a.id === state.selectedAgent)) {
      state.selectedAgent = null;
      showPicker();
    }
  }

  // 接收 SW 主动推送的桥接状态：连上/断开/认证失败时 background 广播，
  // 徽标实时跟上，无需刷新页面或点徽标才重查。
  // 连上后顺带恢复会话：刷新页面时 SW 多半还在重连，加载阶段的自动恢复会
  // 因 !connected 跳过；这里在收到 connected 时补拉 storage 里的 conversationId。
  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || msg.cmd !== 'bridge_status_push' || !msg.status) return;
    updateBridgeIndicator(msg.status);
    // 连上：先唤醒所有「等桥接」的等待者（重试按钮的自动续跑），再走面板恢复。
    if (msg.status.connected) notifyBridgeReady();
    if (msg.status.connected) {
      // 有会话在看的重连：重建事件订阅 + 回拉真实进度。服务端的事件泵绑在断掉的
      // 那条 WS 上，不重发 chat_watch 就永远收不到后续事件（会话看起来卡死）。
      void recoverAfterReconnect();
    } else {
      // 断开：本地事件流随旧 socket 死了，但发送态不会自己解（无终态帧）。解除
      // 卡死的 sending 让 refreshCurrentSession/顶部刷新得以放行；输入区停止按钮
      // 由 turnRunning()（尾条仍 streaming）维持为「停止」，重连后自动回拉续看。
      if (state.open && state.sending && lastStreamingAssistant()) {
        _liveSink = null;
        setSending(false);
        if (els.hint) els.hint.textContent = '连接中断，恢复后自动刷新进度';
      }
    }
    // SW 刚连上：若面板开着但还没会话内容，触发 openFlow 重拉 agents 并恢复
    // 上次会话。若此刻已有一个 openFlow 在跑（_resuming=true），挂起 _pendingResume，
    // 那个 openFlow 跑完的 finally 会据此再跑一次——避免"加载时 SW 没连→空
    // picker→SW 连上但 connected 只推一次→卡死在空列表"的时序漏洞。
    if (msg.status.connected && state.open && !state.conversationId && !state.messages.length) {
      if (state._resuming) { state._pendingResume = true; return; }
      runOpenFlow();
    }
  });

  function runOpenFlow() {
    if (state._resuming) return;
    state._resuming = true;
    void openFlow().finally(() => {
      state._resuming = false;
      if (state._pendingResume) {
        state._pendingResume = false;
        runOpenFlow(); // 跑期间又收到 connected 请求，补跑一次
      }
    });
  }

  // 截图时临时隐藏聊天面板/气泡，截完恢复。只动我们自己注入的 #tmwd-chat-panel，
  // 不碰页面其它元素。面板尚未懒加载出来时直接回 ok:false，background 照常截图。
  let screenshotHidden = false;
  let prevDisplay = '';
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg) return;
    if (msg.cmd === 'hide_panel_for_screenshot') {
      if (!host) { sendResponse && sendResponse({ ok: false }); return true; }
      if (!screenshotHidden) {
        prevDisplay = host.style.display || '';
        host.style.display = 'none';
        screenshotHidden = true;
      }
      sendResponse && sendResponse({ ok: true });
      return true;
    }
    if (msg.cmd === 'show_panel_after_screenshot') {
      if (host && screenshotHidden) {
        host.style.display = prevDisplay;
        screenshotHidden = false;
      }
      sendResponse && sendResponse({ ok: true });
      return true;
    }
  });

  function sendChat(cmd, payload, onEvent) {
    return new Promise((resolve) => {
      const reqId = 'chat_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
      let settled = false;
      const done = (value) => { if (!settled) { settled = true; reqListeners.delete(reqId); resolve(value); } };
      reqListeners.set(reqId, (frame) => {
        if (frame.type === 'chat_event' && frame.event) {
          onEvent(frame.event);
          if (frame.event.type === 'done' || frame.event.type === 'error') done(frame.event);
        } else if (frame.type === 'chat_error') {
          onEvent({ type: 'error', message: frame.message || '对话出错', code: frame.code || '' });
          done({ type: 'error', message: frame.message, code: frame.code || '' });
        } else if (frame.type === 'chat_aborted') {
          onEvent({ type: 'aborted' });
          done({ type: 'aborted' });
        } else if (frame.type === 'chat_send_ok') {
          // 发送受理：本轮事件改由会话泵按 conversationId 广播，这里只回执受理结果。
          done({ conversationId: frame.conversationId || '', assistantMessageId: frame.assistantMessageId });
        } else if (frame.type === 'chat_agents') {
          done(frame.agents || []);
        } else if (frame.type === 'chat_models') {
          done({ models: frame.models || [], defaultModel: frame.defaultModel || '' });
        } else if (frame.type === 'chat_skills') {
          done(frame.skills || []);
        } else if (frame.type === 'chat_conversations') {
          done({ conversations: frame.conversations || [] });
        } else if (frame.type === 'chat_conversation_created') {
          done({ conversation: frame.conversation || null });
        } else if (frame.type === 'chat_conversation_loaded') {
          done({
            conversation: frame.conversation || null,
            messages: frame.messages || [],
            pending_approvals: frame.pending_approvals || [],
            page: frame.page || { next_cursor: null, has_more: false },
          });
        } else if (frame.type === 'chat_approval_resolved') {
          done({ ok: !!frame.ok, result: frame.result || '', message: frame.message || '' });
        } else if (frame.type === 'chat_attachment_uploaded') {
          done({
            path: frame.path || '',
            filename: frame.filename || '',
            size: Number(frame.size || 0),
          });
        } else if (frame.type === 'chat_compacted') {
          done({ ok: !!frame.ok, summarized: !!frame.summarized, summaryChars: Number(frame.summaryChars || 0) });
        } else if (frame.type === 'chat_settings_ok') {
          done({
            ok: true,
            mode: frame.mode || null,
            model: frame.model || null,
            reasoningEffort: frame.reasoningEffort || null,
            releasedApprovals: Number(frame.releasedApprovals || 0),
          });
        }
      });
      // 不带 tabId：background 用 sender.tab.id 即当前标签页。
      chrome.runtime.sendMessage({ cmd, reqId, ...payload }, (resp) => {
        if (!resp || !resp.ok) {
          // background 的内部错误原文是英文（'bridge websocket is not authenticated'），
          // 直接透传进气泡对用户不可读——统一翻成面板口径的中文。
          const raw = (resp && resp.error) || '';
          const text = raw === 'bridge websocket is not authenticated'
            ? '连接已断开，消息未发出；连上后重试即可'
            : (raw || '连接失败，消息未发出；请确认服务已启动');
          onEvent({ type: 'error', message: text });
          done({ type: 'error', message: text });
        }
      });
    });
  }

  // 立即持久化本会话的对话级设置（mode/model/思考等级）。切换即写库，服务端把
  // 它当作**会话状态**——重试/后续轮次直接沿用，不是「下一次发送才生效」的请求
  // 参数。切到 auto 时服务端还会把在途的 pending 审批一并放行。
  let _settingsPushPending = null;
  function pushConversationSettings(patch) {
    if (!state.conversationId) return; // 还没建会话，状态留在本地，建会话时一并带上
    // 同一事件循环内的连续切换合并成一次请求（用户快速点选时不排队多次）。
    _settingsPushPending = { ..._settingsPushPending, ...patch };
    if (pushConversationSettings._t) clearTimeout(pushConversationSettings._t);
    pushConversationSettings._t = setTimeout(() => {
      pushConversationSettings._t = null;
      const body = { conversationId: state.conversationId, ..._settingsPushPending };
      _settingsPushPending = null;
      void sendChat('chat_update_settings', body, () => {}).then((resp) => {
        if (resp && resp.ok && resp.releasedApprovals > 0) {
          const msg = `已切换到自动模式，放行 ${resp.releasedApprovals} 个待审批`;
          // 统一进通知中心：铃铛未读 + 历史列表 + 页面弹框几秒。
          pushNotice(msg);
          // 放行后在途审批卡会收到终态事件；保险起见本地也把 pending 卡置为 allowed。
          renderMessages();
        }
      }).catch(() => { /* 设置静默失败：本地状态已是新值，下一次发送仍带新值 */ });
    }, 150);
  }

  // 思考等级选项与用户端 Agent 聊天页同源（reasoning-effort.tsx）。
  const REASONING_OPTIONS = [
    { value: '', label: '关闭思考' },
    { value: 'low', label: '低' },
    { value: 'medium', label: '中' },
    { value: 'high', label: '高' },
    { value: 'xhigh', label: '极高' },
  ];
  const DEFAULT_REASONING = 'medium';
  const MODE_OPTIONS = [
    { value: 'interact', label: '普通', title: '普通交互：一问一答' },
    { value: 'auto', label: '自动', title: '自动模式：code_run / file_write 审批全部自动放行，不弹审批卡' },
    { value: 'plan', label: '规划', title: '规划模式：复杂任务先规划再分步执行' },
    { value: 'goal', label: '目标', title: '目标模式：开放目标+时间预算，自驱跑到预算耗尽' },
  ];
  const AGENT_STORAGE_KEY = 'cdpChatAgentId';

  // 空态示例问题（点一下填进输入框，不直接发送）。贴合「网页 Agent」场景：
  // 操作当前页面/提取页面信息，而不是通用问答。
  const EMPTY_PROMPT_SUGGESTIONS = [
    '总结当前页面的内容要点',
    '提取页面上的表格数据并整理',
    '把这篇长文翻译成英文',
    '找出页面上的登录入口',
    '注册一个账号（我来提供信息）',
  ];

  let state = {
    open: false,
    view: 'chat', // 'picker' = 首屏 Agent 选择；'chat' = 对话
    agents: [],
    selectedAgent: null,
    conversationId: '',
    sending: false,
    currentReqId: null,
    messages: [], // {role:'user'|'assistant', content, toolCalls:[{name,args,status,result}], status}
    sessions: [], // 会话列表摘要 {id,title,agent_id,updated_at}
    drawerOpen: false,
    loadingSessions: false,
    // 对话级参数（与用户端 Agent 聊天页对齐）
    models: [],
    selectedModel: '',
    defaultModel: '', // agent 记录里配置的模型，下拉里打「默认」标
    modelQuery: '',   // 模型下拉搜索词
    reasoningEffort: DEFAULT_REASONING,
    mode: 'interact',
    goalObjective: '',
    goalBudgetMinutes: 15,
    lastUsage: null, // {prompt,completion,total,cached,cacheCreation,reasoning}
    _resuming: false, // connected 推送触发的恢复进行中守卫，防重复触发
    _pendingResume: false, // 恢复期间又收到 connected 请求时挂起，跑完补跑一次
    // 翻页：滚到顶部加载更早。nextCursor 为空 = 已到最早；loadingMore 防重入。
    nextCursor: null,
    hasMore: false,
    loadingMore: false,
    // 上下文压缩：compactPending = 手动压缩进行中（禁用按钮）；compactNotified =
    // 本轮用量已因超阈值提醒过（避免每帧重弹）。
    compactPending: false,
    compactNotified: false,
    // 通知中心：notifications = 历史通知（最新在前，{id,text,ts}）；
    // notificationsUnread = 未读角标数；selectedNotif = 当前选中通知 id。
    notifications: [],
    notificationsUnread: 0,
    selectedNotif: '',
  };

  function storedAgentId() {
    return new Promise((resolve) => {
      try {
        chrome.storage.local.get([AGENT_STORAGE_KEY], (res) => {
          const v = Number(res && res[AGENT_STORAGE_KEY]);
          resolve(Number.isFinite(v) && v > 0 ? v : null);
        });
      } catch (_) { resolve(null); }
    });
  }

  function rememberAgentId(id) {
    try { chrome.storage.local.set({ [AGENT_STORAGE_KEY]: Number(id) || null }); } catch (_) { /* ignore */ }
  }

  // --- ask_user 附件直链的绝对化 ---
  // 服务端给的附件签名直链是相对路径（/agent/attachments/...），挂在本页面里会被
  // 解析成当前网站的地址。主服务与桥接 WS 同源（同 host:port），从 background 的
  // 桥接配置里取 ws 地址换算成 http origin 即可。面板打开时预热一次。
  let _serverOrigin = null; // null=未取过；''=取过但没有
  function primeServerOrigin() {
    if (_serverOrigin !== null) return;
    try {
      chrome.runtime.sendMessage({ cmd: 'bridge_config_get' }, (resp) => {
        const bridgeUrl = resp && resp.data && resp.data.bridgeUrl;
        try {
          _serverOrigin = new URL(String(bridgeUrl || '').replace(/^ws:/i, 'http:').replace(/^wss:/i, 'https:')).origin;
        } catch (_) { _serverOrigin = ''; }
      });
    } catch (_) { _serverOrigin = ''; }
  }
  function absolutizeMediaUrl(src) {
    if (!src) return '';
    if (/^(https?:)?\/\//i.test(src)) return src;
    // origin 还没取到（极早期事件）：返回空，渲染端降级为文件名 chip；done 后
    // 的下一次 renderMessages 会带上已缓存的 origin 补齐图片。
    if (!_serverOrigin) return '';
    return _serverOrigin + src;
  }

  // --- 按 tabId 的会话级持久化（整页导航后面板/会话恢复用）---
  // session_key 在网关侧是 "{client_id}:{tabId}"，导航不换 tab，key 稳定；这里按
  // tabId 存「面板是否开着 + 当前 conversationId + 选中 agent」，导航重注入后恢复。
  let tabId = null;

  function panelStorageKey() {
    return tabId != null ? `cdpPanel:${tabId}` : null;
  }

  function getTabId() {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ cmd: 'bridge_tab_id' }, (resp) => {
          if (chrome.runtime.lastError || !resp || !resp.ok) { resolve(null); return; }
          resolve((resp.data && resp.data.tabId) || null);
        });
      } catch (_) { resolve(null); }
    });
  }

  function readPanelState() {
    return new Promise((resolve) => {
      const key = panelStorageKey();
      if (!key) { resolve(null); return; }
      try {
        chrome.storage.session.get([key], (res) => {
          resolve((res && res[key]) || null);
        });
      } catch (_) { resolve(null); }
    });
  }

  function writePanelState(patch) {
    const key = panelStorageKey();
    if (!key) return;
    try {
      chrome.storage.session.get([key], (res) => {
        const cur = (res && res[key]) || {};
        const next = { ...cur, ...patch };
        try { chrome.storage.session.set({ [key]: next }); } catch (_) { /* ignore */ }
      });
    } catch (_) { /* ignore */ }
  }

  // 本页上次手动选的模型。只在 agentId 对得上时才认，避免换 agent 后串用旧选择。
  async function savedModelForAgent(agentId) {
    const saved = await readPanelState();
    if (!saved || !saved.model) return '';
    if (saved.agentId && Number(saved.agentId) !== Number(agentId)) return '';
    return String(saved.model);
  }

  // 懒加载面板 DOM（含 Shadow DOM 隔离页面样式）。
  let host, shadow, els = {};
  function ensurePanel() {
    if (host) return;
    host = document.createElement('div');
    host.id = PANEL_ID;
    // z-index 用 CSS 上限（2^31-1）：部分站点会把自家弹层顶到 2147483647，host 停在
    // …646 时整块面板（尤其模型下拉/用量弹层这类浮出的部分）会被盖住。徽标
    // (#ljq-ind) 早就是 2147483647；同值时靠 DOM 顺序决胜——打开面板时把 host
    // 挪到 body 末尾（raiseHost），保证我们是同级里最后一个。
    host.style.cssText = 'all:initial;position:fixed;top:0;right:0;width:0;height:100vh;z-index:2147483647;';
    shadow = host.attachShadow({ mode: 'open' });
    const ICON = {
      send: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="M22 2 15 22 11 13 2 9 22 2Z"/></svg>',
      stop: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><rect x="6.5" y="6.5" width="11" height="11" rx="2"/></svg>',
      plus: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>',
      list: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>',
      close: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
      refresh: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
      eraser: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 21h13M17 5 8.5 13.5a2.12 2.12 0 0 0 0 3L11 19"/><path d="m15 7 4-4 3 3-4 4"/><path d="m5 11 6 6"/></svg>',
      wrench: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
      bot: '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4M8 16h.01M16 16h.01"/></svg>',
      chevron: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>',
      shield: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>',
      paperclip: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>',
      close12: '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
      rotate: '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>',
      bell: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>',
      bellFill: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a6 6 0 0 0-6 6c0 4.5-1.6 6.6-2.4 7.4-.5.5-.2 1.6.6 1.6h15.6c.8 0 1.1-1.1.6-1.6-.8-.8-2.4-2.9-2.4-7.4a6 6 0 0 0-6-6z" fill="currentColor" stroke="none"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" fill="none"/></svg>',
    };
    shadow.innerHTML = `
      <style>
        /* 配色对齐用户端：主色橙 oklch(0.555 0.163 48.998)≈#b45309；面板在第三方页
           Shadow DOM 内取不到宿主变量，故用固定近似值；暗色系统主题切绿(#4ade80)。 */
        :host{
          --tmwd-primary:#b45309;--tmwd-primary-hover:#92400e;--tmwd-primary-fg:#fff;
          --tmwd-primary-soft:rgba(180,83,9,.10);--tmwd-primary-ring:rgba(180,83,9,.12);
          --tmwd-bg:#fff;--tmwd-body:#f8fafc;--tmwd-fg:#0f172a;--tmwd-muted:#94a3b8;
          --tmwd-border:rgba(15,23,42,.10);--tmwd-border-strong:rgba(15,23,42,.22);
        }
        @media (prefers-color-scheme: dark){
          :host{
            --tmwd-primary:#4ade80;--tmwd-primary-hover:#22c55e;--tmwd-primary-fg:#0d1117;
            --tmwd-primary-soft:rgba(74,222,128,.14);--tmwd-primary-ring:rgba(74,222,128,.18);
            --tmwd-bg:#161b22;--tmwd-body:#0d1117;--tmwd-fg:#e6edf3;--tmwd-muted:#8b949e;
            --tmwd-border:rgba(240,246,252,.10);--tmwd-border-strong:rgba(240,246,252,.22);
          }
        }
        :host, *{box-sizing:border-box}
        /* overflow:visible：模型下拉/用量弹层这类浮出元素要越过面板边界悬浮，
           overflow:hidden 会把它们裁掉（实测用量弹层 left 越界面板左缘 41px 被切）。
           圆角改由 header/footer 自己裁；滑出的会话抽屉改用 visibility 隐藏。 */
        .wrap{position:fixed;top:16px;right:16px;bottom:16px;width:420px;max-width:calc(100vw - 32px);display:flex;flex-direction:column;background:var(--tmwd-bg);border:1px solid var(--tmwd-border);border-radius:20px;box-shadow:0 24px 64px rgba(15,23,42,.22),0 8px 20px rgba(15,23,42,.10);transform:translateX(calc(100% + 32px));transition:transform .28s cubic-bezier(.4,0,.2,1);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;color:var(--tmwd-fg);overflow:visible;}
        .wrap.open{transform:translateX(0)}
        @media (max-width:480px){.wrap{top:0;right:0;bottom:0;left:0;width:100vw;max-width:100vw;border-radius:0;border:0;height:100vh}header,footer{border-radius:0}.drawer{border-radius:0}}
        header{display:flex;align-items:center;gap:8px;padding:14px 16px;border-bottom:1px solid var(--tmwd-border);background:var(--tmwd-bg);border-radius:20px 20px 0 0}
        header .title{font-weight:650;font-size:15px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .iconbtn{cursor:pointer;border:1px solid var(--tmwd-border);background:var(--tmwd-bg);border-radius:8px;height:32px;width:32px;display:grid;place-items:center;color:var(--tmwd-muted);padding:0;line-height:1;transition:color .15s ease,background .15s ease,border-color .15s ease}
        .iconbtn:hover{color:var(--tmwd-fg);background:var(--tmwd-body);border-color:var(--tmwd-border-strong)}
        .iconbtn:active{transform:translateY(1px)}
        .iconbtn:disabled{opacity:.45;cursor:not-allowed}
        .body{flex:1;overflow-y:auto;overflow-x:hidden;padding:16px;display:flex;flex-direction:column;gap:12px;background:var(--tmwd-body)}
        /* 消息气泡：用户与 Agent 均靠左，呈时间线流（不再左右分边）。
           面板就一张窄卡，右侧没有第二栏，气泡铺满行宽；工具/审批卡在气泡内
           自身限宽，读感不受影响。 */
        .msg-row{display:flex;min-width:0;justify-content:flex-start}
        .bubble{width:100%;min-width:0;display:flex;flex-direction:column;gap:6px;padding:9px 12px;border-radius:14px;font-size:13.5px;line-height:1.55}
        .bubble.user{background:var(--tmwd-primary);color:var(--tmwd-primary-fg)}
        .bubble.assistant{background:var(--tmwd-bg);color:var(--tmwd-fg);border:1px solid var(--tmwd-border);box-shadow:0 1px 2px rgba(15,23,42,.04)}
        .bubble.assistant.err{background:#fef2f2;border-color:rgba(185,28,28,.18)}
        .msg-head{display:flex;align-items:center;gap:8px;font-size:10.5px;opacity:.65}
        .msg-head .msg-time{margin-left:auto}
        .msg-head .msg-model{padding:1px 6px;border-radius:5px;background:var(--tmwd-body);border:1px solid var(--tmwd-border);font-size:9.5px;font-weight:600;color:var(--tmwd-muted);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .msg-text{white-space:pre-wrap;word-break:break-word}
        /* 思考过程折叠块（与 web ReasoningBlock 同形）：reasoning 增量独立累积，
           不进 content、不回灌模型，仅展示。流式中默认展开，落定后仍可展开回看。 */
        .reasoning-block{border:1px solid var(--tmwd-border);border-radius:9px;background:var(--tmwd-body);overflow:hidden;font-size:12px}
        .reasoning-block .rb-head{display:flex;align-items:center;gap:6px;width:100%;padding:6px 9px;border:0;background:none;cursor:pointer;text-align:left;color:var(--tmwd-muted);font:inherit}
        .reasoning-block .rb-head .rb-caret{display:inline-flex;transition:transform .14s ease}
        .reasoning-block.open .rb-head .rb-caret{transform:rotate(90deg)}
        .reasoning-block .rb-title{font-weight:600;color:var(--tmwd-fg);opacity:.7}
        .reasoning-block .rb-toggle{margin-left:auto}
        .reasoning-block .rb-body{display:none;border-top:1px solid var(--tmwd-border);padding:7px 9px;white-space:pre-wrap;word-break:break-word;font-size:11px;color:var(--tmwd-muted);max-height:220px;overflow:auto}
        .reasoning-block.open .rb-body{display:block}
        /* markdown 正文：done 态切 innerHTML 渲染，流式中仍用 textContent 避免抖动 */
        .msg-text.md{white-space:normal}
        .msg-text.md p{margin:0 0 .5em}
        .msg-text.md p:last-child{margin-bottom:0}
        .msg-text.md ul,.msg-text.md ol{margin:0 0 .5em;padding-left:1.4em}
        .msg-text.md li{margin:.15em 0}
        .msg-text.md h1,.msg-text.md h2,.msg-text.md h3{font-size:1em;font-weight:700;margin:.4em 0 .25em}
        .msg-text.md a{color:var(--tmwd-primary)}
        .msg-text.md code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em;background:var(--tmwd-body);padding:.1em .35em;border-radius:4px}
        .msg-text.md pre{margin:0 0 .5em;padding:8px 10px;background:var(--tmwd-body);border:1px solid var(--tmwd-border);border-radius:8px;overflow-x:auto}
        .msg-text.md pre code{background:none;padding:0;border-radius:0;font-size:.9em}
        .msg-text.md blockquote{margin:0 0 .5em;padding-left:.8em;border-left:3px solid var(--tmwd-border);color:var(--tmwd-muted)}
        @media (prefers-color-scheme: dark){
          .msg-text.md code{background:#0d1117}
          .msg-text.md pre{background:#0d1117;border-color:var(--tmwd-border)}
        }
        .bubble.assistant.streaming .msg-text.cursor::after{content:'▋';display:inline-block;margin-left:2px;animation:tmwd-blink 1s steps(2,start) infinite;color:var(--tmwd-muted)}
        @keyframes tmwd-blink{to{opacity:0}}
        .thinking{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--tmwd-muted)}
        .retry-hint{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--tmwd-muted);padding:3px 0}
        .msg-error{font-size:11.5px;color:#b91c1c;line-height:1.5;word-break:break-word;padding:3px 0}
        .msg-retry{margin-top:6px;align-self:flex-start;padding:3px 10px;font-size:12px;line-height:1.6;border:1px solid var(--tmwd-border);border-radius:6px;background:rgba(148,163,184,.1);color:var(--tmwd-fg);cursor:pointer}
        .msg-retry:hover{color:var(--tmwd-primary);border-color:var(--tmwd-primary);background:var(--tmwd-primary-soft)}
        /* ask_user 候选答案快捷回复：一行可换行的 chip 按钮，点击即作为下一条消息发送 */
        .q-cands{display:flex;flex-wrap:wrap;gap:6px}
        .q-cand{padding:4px 10px;font-size:12px;line-height:1.5;border:1px solid var(--tmwd-border-strong);border-radius:999px;background:var(--tmwd-bg);color:var(--tmwd-fg);cursor:pointer;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;transition:all .14s ease}
        .q-cand:hover{border-color:var(--tmwd-primary);color:var(--tmwd-primary);background:var(--tmwd-primary-soft)}
        .q-cand.active{border-color:var(--tmwd-primary);background:var(--tmwd-primary);color:var(--tmwd-primary-fg)}
        /* ask_user 多题表单：tabs 卡片，每题一个 tab（题号 + 已答圆点）；
           一切以勾选为准——自定义输入也须勾选「其他」才计入答案。 */
        .q-form{display:flex;flex-direction:column;gap:8px;border:1px solid var(--tmwd-primary);border-radius:10px;background:var(--tmwd-primary-soft);padding:9px 10px}
        .q-form .q-tabs{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none}
        .q-form .q-tabs::-webkit-scrollbar{display:none}
        .q-form .q-tab{flex:0 0 auto;height:24px;padding:0 10px;border:1px solid var(--tmwd-border-strong);border-radius:999px;background:var(--tmwd-bg);color:var(--tmwd-muted);font:600 11px/1 inherit;cursor:pointer;display:inline-flex;align-items:center;gap:5px;transition:all .14s ease}
        .q-form .q-tab:hover{border-color:var(--tmwd-primary);color:var(--tmwd-primary)}
        .q-form .q-tab.active{border-color:var(--tmwd-primary);background:var(--tmwd-primary);color:var(--tmwd-primary-fg)}
        .q-form .q-tab .qt-dot{width:6px;height:6px;border-radius:50%;border:1px solid currentColor;background:transparent;flex:0 0 auto}
        .q-form .q-tab.answered .qt-dot{background:currentColor;border-color:currentColor}
        .q-form .q-block{display:flex;flex-direction:column;gap:6px}
        .q-form .q-title{font-size:12px;font-weight:600;color:var(--tmwd-fg);line-height:1.5;white-space:pre-wrap;word-break:break-word}
        .q-form .q-opt-desc{font-size:10.5px;color:var(--tmwd-muted);line-height:1.5;white-space:pre-wrap;word-break:break-word}
        .q-form .q-custom-row{display:flex;align-items:center;gap:6px}
        .q-form .q-custom{flex:1;min-width:0;height:28px;padding:0 9px;font-size:12px;border:1px solid var(--tmwd-border-strong);border-radius:7px;background:var(--tmwd-bg);color:var(--tmwd-fg);outline:none}
        .q-form .q-custom:focus{border-color:var(--tmwd-primary)}
        .q-form .q-custom.dim{opacity:.6}
        .q-form .q-hint{font-size:10.5px;color:#b45309;line-height:1.4}
        .q-form .q-foot{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
        .q-form .q-progress{font-size:11px;color:var(--tmwd-muted);margin-right:auto}
        .q-form .q-nav{height:26px;padding:0 12px;border:1px solid var(--tmwd-border-strong);border-radius:7px;background:var(--tmwd-bg);color:var(--tmwd-fg);font:600 12px/1 inherit;cursor:pointer}
        .q-form .q-nav:hover:not(:disabled){border-color:var(--tmwd-primary);color:var(--tmwd-primary)}
        .q-form .q-nav:disabled{opacity:.5;cursor:not-allowed}
        .q-form .q-submit{height:28px;padding:0 16px;border:0;border-radius:8px;background:var(--tmwd-primary);color:var(--tmwd-primary-fg);font:600 12px/1 inherit;cursor:pointer}
        .q-form .q-submit:hover:not(:disabled){background:var(--tmwd-primary-hover)}
        .q-form .q-submit:disabled{opacity:.55;cursor:not-allowed}
        .q-cand:disabled{opacity:.55;cursor:not-allowed}
        /* ask_user 附件：图片内联大图（点击新标签页看原图），非图片/无法直链的降级为文件名 chip */
        .q-media{display:flex;flex-direction:column;gap:6px}
        .q-media img{max-width:100%;max-height:240px;border:1px solid var(--tmwd-border);border-radius:9px;display:block;cursor:pointer;background:var(--tmwd-body)}
        .q-media .qm-chip{display:inline-flex;align-items:center;gap:6px;align-self:flex-start;max-width:100%;padding:4px 10px;font-size:11.5px;border:1px solid var(--tmwd-border);border-radius:8px;background:var(--tmwd-body);color:var(--tmwd-muted);cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .q-media .qm-chip:hover{border-color:var(--tmwd-primary);color:var(--tmwd-primary)}
        .spinner{width:12px;height:12px;border:2px solid rgba(148,163,184,.35);border-top-color:#64748b;border-radius:50%;animation:tmwd-spin .7s linear infinite;flex:0 0 auto}
        @keyframes tmwd-spin{to{transform:rotate(360deg)}}
        .empty{align-self:center;margin:auto;display:flex;flex-direction:column;align-items:center;gap:12px;color:var(--tmwd-muted);text-align:center;font-size:12.5px;max-width:250px}
        .empty-icon{width:48px;height:48px;border-radius:16px;display:grid;place-items:center;background:var(--tmwd-primary-soft);color:var(--tmwd-primary)}
        .empty-title{font-size:13.5px;font-weight:600;color:var(--tmwd-fg)}
        /* 空态示例问题 chips：点击即填入输入框（对齐用户端首屏引导） */
        .empty-chips{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:4px}
        .empty-chips .ec-chip{max-width:100%;padding:5px 10px;border:1px solid var(--tmwd-border);border-radius:999px;background:var(--tmwd-bg);color:var(--tmwd-fg);font:12px/1.4 inherit;cursor:pointer;text-align:left;transition:border-color .14s ease,color .14s ease,background .14s ease}
        .empty-chips .ec-chip:hover{border-color:var(--tmwd-primary);color:var(--tmwd-primary);background:var(--tmwd-primary-soft)}
        /* 首屏 Agent 选择 */
        .picker{flex:1;display:flex;flex-direction:column;gap:10px;padding:4px}
        .picker-head{font-size:13.5px;font-weight:650;color:var(--tmwd-fg)}
        .picker-sub{font-size:12px;color:var(--tmwd-muted);margin-top:-4px}
        .picker-list{display:flex;flex-direction:column;gap:8px;overflow-y:auto}
        .picker-card{display:flex;flex-direction:column;gap:2px;padding:11px 12px;border:1px solid var(--tmwd-border);border-radius:11px;background:var(--tmwd-bg);cursor:pointer;transition:all .14s ease}
        .picker-card:hover{border-color:var(--tmwd-primary);background:var(--tmwd-primary-soft)}
        .picker-card .pc-name{font-size:13.5px;font-weight:650;color:var(--tmwd-fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .picker-card .pc-desc{font-size:11.5px;color:var(--tmwd-muted);overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
        /* 工具调用卡片（可折叠）*/
        .tool-card{border:1px solid var(--tmwd-border);border-radius:9px;background:var(--tmwd-body);overflow:hidden;font-size:12px}
        .tool-card .tc-head{display:flex;align-items:center;gap:7px;width:100%;padding:7px 9px;background:transparent;border:0;cursor:pointer;text-align:left;color:#334155}
        .tool-card .tc-icon{color:#64748b;flex:0 0 auto;display:inline-flex}
        .tool-card .tc-name{font-weight:600;color:var(--tmwd-fg);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .tool-card .tc-status{font-size:9.5px;padding:2px 7px;border-radius:6px;background:#e2e8f0;color:#475569;font-weight:600;flex:0 0 auto}
        .tool-card .tc-status.running{background:#dbeafe;color:#1d4ed8}
        .tool-card .tc-status.done{background:#dcfce7;color:#15803d}
        .tool-card .tc-status.error{background:#fee2e2;color:#b91c1c}
        .tool-card .tc-caret{color:var(--tmwd-muted);flex:0 0 auto;display:inline-flex;transition:transform .15s ease}
        .tool-card.open .tc-caret{transform:rotate(180deg)}
        .tool-card .tc-body{display:none;border-top:1px solid var(--tmwd-border);padding:8px 9px;gap:6px;flex-direction:column}
        .tool-card.open .tc-body{display:flex}
        .tool-card .tc-block{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:10.5px;color:#475569;white-space:pre-wrap;word-break:break-word;max-height:140px;overflow:auto;background:var(--tmwd-bg);border:1px solid var(--tmwd-border);border-radius:6px;padding:6px 8px}
        .tool-card .tc-label{font-size:10px;color:var(--tmwd-muted);font-weight:600;margin-bottom:2px}
        /* 子 Agent 卡片（与 web SubagentBlock 同形）：主色描边区分于普通工具卡，
           可展开看子 agent 的流式正文与内嵌工具卡。 */
        .subagent-card{border:1px solid var(--tmwd-primary-ring);border-radius:9px;background:var(--tmwd-primary-soft);overflow:hidden;font-size:12px}
        .subagent-card .sa-head{display:flex;align-items:center;gap:7px;width:100%;padding:7px 9px;background:transparent;border:0;cursor:pointer;text-align:left;color:var(--tmwd-fg);font:inherit}
        .subagent-card .sa-icon{color:var(--tmwd-primary);flex:0 0 auto;display:inline-flex}
        .subagent-card .sa-icon svg{width:13px;height:13px}
        .subagent-card .sa-name{font-weight:600;flex:0 0 auto}
        .subagent-card .sa-task{flex:1;min-width:0;font-size:11px;color:var(--tmwd-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .subagent-card .sa-state{font-size:9.5px;padding:2px 7px;border-radius:6px;font-weight:600;flex:0 0 auto}
        .subagent-card .sa-state.running{background:#dbeafe;color:#1d4ed8}
        .subagent-card .sa-state.done{background:#dcfce7;color:#15803d}
        .subagent-card .sa-caret{color:var(--tmwd-muted);flex:0 0 auto;display:inline-flex;transition:transform .15s ease}
        .subagent-card.open .sa-caret{transform:rotate(180deg)}
        .subagent-card .sa-body{display:none;border-top:1px solid var(--tmwd-border);padding:8px 9px;display:none}
        .subagent-card.open .sa-body{display:flex;flex-direction:column;gap:6px}
        .subagent-card .sa-text{white-space:pre-wrap;word-break:break-word;font-size:11.5px;color:var(--tmwd-fg);max-height:220px;overflow:auto}
        .subagent-card .sa-summary{font-size:11px;color:var(--tmwd-muted);line-height:1.5;border-top:1px dashed var(--tmwd-border);padding-top:5px}
        .subagent-card .sa-error{font-size:11px;color:#b91c1c;line-height:1.5;word-break:break-word;border-top:1px dashed var(--tmwd-border);padding-top:5px}
        /* 代码执行审批卡：待裁决时用主色描边强调，必须由人点过才继续。 */
        .approval-card{border:1px solid var(--tmwd-primary);border-radius:9px;background:var(--tmwd-primary-soft);overflow:hidden;font-size:12px}
        .approval-card .ac-head{display:flex;align-items:center;gap:7px;padding:7px 9px;color:#334155}
        .approval-card .ac-icon{color:var(--tmwd-primary);flex:0 0 auto;display:inline-flex}
        .approval-card .ac-name{font-weight:600;color:var(--tmwd-fg);flex:1;min-width:0}
        .approval-card .ac-state{font-size:9.5px;padding:2px 7px;border-radius:6px;background:#e2e8f0;color:#475569;font-weight:600;flex:0 0 auto}
        .approval-card .ac-state.pending,.approval-card .ac-state.resolving{background:#fef3c7;color:#b45309}
        .approval-card .ac-state.allowed{background:#dcfce7;color:#15803d}
        .approval-card .ac-state.denied,.approval-card .ac-state.expired{background:#fee2e2;color:#b91c1c}
        .approval-card .ac-body{display:flex;flex-direction:column;gap:6px;border-top:1px solid var(--tmwd-border);padding:8px 9px}
        .approval-card .ac-msg{font-size:11px;color:#475569;line-height:1.5}
        .approval-card .ac-code{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:10.5px;color:#334155;white-space:pre-wrap;word-break:break-word;max-height:180px;overflow:auto;background:var(--tmwd-bg);border:1px solid var(--tmwd-border);border-radius:6px;padding:6px 8px}
        .approval-card .ac-actions{display:flex;gap:8px;padding:0 9px 9px}
        .approval-card .ac-actions button{height:28px;padding:0 14px;border:0;border-radius:8px;font:12px/1 inherit;font-weight:600;cursor:pointer}
        .approval-card .ac-actions button:disabled{opacity:.55;cursor:not-allowed}
        .approval-card .ac-actions .ac-countdown{margin-right:auto;font-size:10.5px;color:var(--tmwd-muted);align-self:center}
        .approval-card .ac-allow{background:var(--tmwd-primary);color:var(--tmwd-primary-fg)}
        .approval-card .ac-allow:hover:not(:disabled){background:var(--tmwd-primary-hover)}
        .approval-card .ac-deny{background:var(--tmwd-bg);color:#b91c1c;border:1px solid var(--tmwd-border-strong)!important}
        .approval-card .ac-deny:hover:not(:disabled){background:#fef2f2}
        footer{padding:12px 14px 14px;border-top:1px solid var(--tmwd-border);background:var(--tmwd-bg);border-radius:0 0 20px 20px}
        .composer{display:flex;flex-direction:column;gap:8px;border:1px solid var(--tmwd-border-strong);border-radius:14px;padding:8px;background:var(--tmwd-bg);transition:border-color .15s ease,box-shadow .15s ease}
        /* 会话状态固定条：位于消息区与输入框之间，常驻显示当前状态。 */
        .status-bar{display:none;align-items:center;gap:7px;min-height:22px;padding:3px 4px;font-size:11.5px;color:var(--tmwd-muted);line-height:1.4}
        .status-bar.show{display:flex}
        .status-bar .sb-dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:0 0 auto}
        .status-bar[data-status="sending"] .sb-dot,
        .status-bar[data-status="waiting_upstream"] .sb-dot,
        .status-bar[data-status="thinking"] .sb-dot,
        .status-bar[data-status="processing"] .sb-dot,
        .status-bar[data-status="executing"] .sb-dot,
        .status-bar[data-status="retrying"] .sb-dot{animation:sbPulse 1s ease-in-out infinite}
        .status-bar[data-status="waiting_approval"]{color:var(--tmwd-primary)}
        .status-bar[data-status="waiting_input"]{color:var(--tmwd-muted);opacity:.75}
        .status-bar[data-status="error"],
        .status-bar[data-status="aborted"],
        .status-bar[data-status="approval_failed"]{color:#b91c1c}
        @keyframes sbPulse{0%,100%{opacity:.35;transform:scale(.85)}50%{opacity:1;transform:scale(1)}}
        .status-bar .sb-retry{margin-left:auto;height:24px;padding:0 12px;border:1px solid var(--tmwd-border-strong);border-radius:7px;background:var(--tmwd-bg);color:#b91c1c;font:600 11px/1 inherit;cursor:pointer}
        .status-bar .sb-retry:hover{background:#fef2f2}
        .composer:focus-within{border-color:var(--tmwd-primary);box-shadow:0 0 0 3px var(--tmwd-primary-soft)}
        /* 附件条：已选文件的 chip 列表（上传中带进度、失败可重试、可移除） */
        .attach-strip{display:none;flex-direction:column;gap:4px}
        .attach-strip.show{display:flex}
        .attach-chip{display:flex;align-items:center;gap:6px;padding:4px 7px;border:1px solid var(--tmwd-border);border-radius:8px;background:var(--tmwd-body);font-size:11.5px;color:var(--tmwd-fg);min-width:0}
        .attach-chip.err{border-color:rgba(185,28,28,.35);background:#fef2f2}
        .attach-chip .ac-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .attach-chip .ac-meta{flex:0 0 auto;font-size:10px;color:var(--tmwd-muted)}
        .attach-chip .ac-x,.attach-chip .ac-retry{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border:0;border-radius:5px;background:transparent;color:var(--tmwd-muted);cursor:pointer;padding:0}
        .attach-chip .ac-x:hover{color:#b91c1c;background:rgba(185,28,28,.08)}
        .attach-chip .ac-retry:hover{color:var(--tmwd-primary);background:var(--tmwd-primary-soft)}
        .attach-chip .ac-bar{position:relative;flex:1;height:3px;border-radius:2px;background:var(--tmwd-border);overflow:hidden}
        .attach-chip .ac-bar > i{position:absolute;left:0;top:0;bottom:0;background:var(--tmwd-primary);transition:width .15s ease}
        .attach-btn{width:28px;height:28px;flex:0 0 auto}
        .attach-btn:disabled{opacity:.4;cursor:not-allowed}
        textarea{width:100%;resize:none;height:52px;min-height:52px;max-height:150px;border:0;outline:none;font:14px inherit;background:transparent;color:var(--tmwd-fg);line-height:1.5;padding:5px 4px}
        textarea::placeholder{color:var(--tmwd-muted)}
        .composer-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
        /* 输入区控件（思考等级/模式/模型下拉，与用户端 MessageComposer 同序） */
        .ctl{position:relative;flex:0 0 auto}
        .ctl-trigger{display:flex;align-items:center;gap:4px;height:28px;padding:0 8px;border:0;border-radius:999px;background:transparent;color:var(--tmwd-muted);font:12px/1 inherit;cursor:pointer;transition:background .15s ease,color .15s ease}
        .ctl-trigger:hover{background:var(--tmwd-body);color:var(--tmwd-fg)}
        .ctl-trigger .ct-caret{opacity:.6;flex:0 0 auto;display:inline-flex}
        .ctl-menu{position:absolute;bottom:calc(100% + 6px);left:0;min-width:140px;max-height:260px;overflow-y:auto;background:var(--tmwd-bg);border:1px solid var(--tmwd-border);border-radius:10px;box-shadow:0 12px 28px rgba(15,23,42,.16);padding:4px;z-index:6;display:none}
        .ctl-menu.open{display:block}
        .ctl-opt{display:flex;flex-direction:column;gap:1px;padding:7px 9px;border-radius:7px;cursor:pointer;font-size:12.5px;color:var(--tmwd-fg)}
        .ctl-opt:hover{background:var(--tmwd-body)}
        .ctl-opt.active{background:var(--tmwd-primary-soft);color:var(--tmwd-primary)}
        .ctl-opt .co-desc{font-size:10.5px;color:var(--tmwd-muted)}
        /* 触发器文字过长时截断，完整值放 title */
        .ctl-trigger .ct-text{max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        /* 模型下拉带搜索，菜单更宽 */
        .ctl-menu.wide{min-width:236px;max-width:300px}
        .ctl-search{position:sticky;top:0;z-index:1;background:var(--tmwd-bg);padding:2px 2px 6px}
        .ctl-search input{width:100%;box-sizing:border-box;height:28px;border:1px solid var(--tmwd-border);border-radius:8px;background:var(--tmwd-bg);color:var(--tmwd-fg);font:12px/1 inherit;padding:0 8px;outline:none}
        .ctl-search input:focus{border-color:var(--tmwd-primary)}
        .ctl-opt .co-name{display:flex;align-items:center;gap:5px;min-width:0}
        .ctl-opt .co-name .co-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .ctl-opt .co-tag{flex:0 0 auto;padding:1px 5px;border-radius:5px;background:var(--tmwd-body);color:var(--tmwd-muted);font-size:10px;line-height:1.5}
        .ctl-opt.active .co-tag{background:var(--tmwd-bg)}
        .ctl-empty{padding:7px 9px;font-size:12px;color:var(--tmwd-muted)}
        .co-badge{flex:0 0 auto;font-size:9.5px;line-height:1.5;padding:0 5px;border-radius:999px;background:var(--tmwd-body);color:var(--tmwd-muted)}
        .ctl-empty{padding:7px 9px;font-size:12px;color:var(--tmwd-muted)}
        /* 通知中心：模型旁铃铛 + 未读角标 + 历史列表弹层 */
        .notif-trigger{position:relative}
        .notif-trigger.has-unread{color:var(--tmwd-primary)}
        .notif-trigger .notif-icon{display:inline-flex;line-height:0}
        .notif-badge{position:absolute;top:-2px;right:-2px;min-width:14px;height:14px;padding:0 3px;border-radius:999px;background:#dc2626;color:#fff;font:600 9.5px/14px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;text-align:center;box-sizing:border-box;pointer-events:none}
        .notif-menu{left:auto;right:0;min-width:260px;max-width:320px;max-height:320px}
        .notif-item{display:flex;flex-direction:column;gap:2px;padding:8px 9px;border-radius:7px;cursor:pointer;font-size:12.5px;color:var(--tmwd-fg);border-left:2px solid transparent}
        .notif-item:hover{background:var(--tmwd-body)}
        .notif-item.unread{border-left-color:var(--tmwd-primary)}
        .notif-item.selected{background:var(--tmwd-primary-soft)}
        .notif-item .ni-text{white-space:normal;word-break:break-word;line-height:1.45}
        .notif-item .ni-time{font-size:10px;color:var(--tmwd-muted)}
        .notif-empty{padding:10px 9px;font-size:12px;color:var(--tmwd-muted);text-align:center}
        /* 技能（/ 斜杠命令）弹层：输入框内任意位置打 / 时在 composer 上方浮出 */
        .composer{position:relative}
        .skill-pop{position:absolute;left:8px;right:8px;bottom:calc(100% + 8px);max-height:240px;overflow-y:auto;background:var(--tmwd-bg);border:1px solid var(--tmwd-border);border-radius:10px;box-shadow:0 12px 28px rgba(15,23,42,.16);padding:4px;z-index:7;display:none}
        .skill-pop.open{display:block}
        .skill-pop .sp-item{display:flex;align-items:center;gap:6px;padding:6px 8px;border-radius:7px;cursor:pointer;font-size:12.5px;color:var(--tmwd-fg)}
        .skill-pop .sp-item:hover,.skill-pop .sp-item.active{background:var(--tmwd-body)}
        .skill-pop .sp-item.active{background:var(--tmwd-primary-soft)}
        .skill-pop .sp-slash{color:var(--tmwd-muted);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;flex:0 0 auto}
        .skill-pop .sp-name{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;flex:0 0 auto;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .skill-pop .sp-desc{flex:1 1 auto;min-width:0;font-size:11px;color:var(--tmwd-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .skill-pop .sp-kind{flex:0 0 auto;font-size:9.5px;line-height:1.5;padding:0 5px;border-radius:999px;background:var(--tmwd-body);color:var(--tmwd-muted)}
        .skill-pop .sp-empty{padding:7px 9px;font-size:12px;color:var(--tmwd-muted)}
        /* goal 输入 */
        .goal-inputs{display:flex;align-items:center;gap:4px;flex:1 1 100%}
        .goal-inputs input{height:28px;border:1px solid var(--tmwd-border);border-radius:8px;background:var(--tmwd-bg);color:var(--tmwd-fg);font:12px/1 inherit;padding:0 8px;outline:none}
        .goal-inputs input.g-obj{flex:1;min-width:0}
        .goal-inputs input.g-min{width:56px}
        .goal-inputs .g-unit{font-size:11px;color:var(--tmwd-muted)}
        /* 使用量圈 */
        .usage-btn{position:relative;height:28px;width:28px;border:0;border-radius:999px;background:transparent;cursor:pointer;display:grid;place-items:center;color:var(--tmwd-muted);flex:0 0 auto}
        .usage-btn:hover{background:var(--tmwd-body)}
        .usage-ring{transform:rotate(-90deg)}
        .usage-pop{position:absolute;bottom:calc(100% + 8px);right:0;width:220px;background:var(--tmwd-bg);border:1px solid var(--tmwd-border);border-radius:10px;box-shadow:0 12px 28px rgba(15,23,42,.16);padding:10px 12px;z-index:7;display:none;font-size:12px;color:var(--tmwd-fg)}
        .usage-pop.open{display:block}
        .usage-pop .up-row{display:flex;justify-content:space-between;gap:10px;padding:2px 0}
        .usage-pop .up-row span:first-child{color:var(--tmwd-muted)}
        /* 用量弹层压缩入口：与 web TokenProgressPopover 的「压缩上下文」同形 */
        .usage-pop .up-actions{border-top:1px solid var(--tmwd-border);margin-top:6px;padding-top:8px}
        .usage-pop .up-compact{width:100%;height:28px;border:0;border-radius:8px;background:var(--tmwd-primary);color:var(--tmwd-primary-fg);font:600 12px/1 inherit;cursor:pointer}
        .usage-pop .up-compact:hover:not(:disabled){background:var(--tmwd-primary-hover)}
        .usage-pop .up-compact:disabled{opacity:.55;cursor:not-allowed}
        /* 使用量圈挪到标题栏后，弹层要向下展开 */
        #usageCtl{position:relative;flex:0 0 auto}
        #usageCtl .usage-pop{top:calc(100% + 8px);bottom:auto}
        /* Agent 选择器（自定义下拉，替代原生 select） */
        .agent-picker{position:relative;flex:0 1 auto;min-width:0;max-width:160px}
        .agent-trigger{display:flex;align-items:center;gap:6px;height:28px;min-width:0;padding:0 8px;border:1px solid var(--tmwd-border);border-radius:9px;background:var(--tmwd-bg);color:var(--tmwd-fg);font:12px/1 inherit;cursor:pointer;transition:border-color .15s ease,box-shadow .15s ease}
        .agent-trigger:hover{border-color:var(--tmwd-primary);background:var(--tmwd-body)}
        .agent-trigger.open{border-color:var(--tmwd-primary);box-shadow:0 0 0 3px var(--tmwd-primary-soft)}
        .agent-trigger .at-label{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600;color:var(--tmwd-fg)}
        .agent-trigger .at-label.placeholder{color:var(--tmwd-muted);font-weight:500}
        .agent-trigger .at-caret{color:var(--tmwd-muted);flex:0 0 auto;display:inline-flex;transition:transform .15s ease}
        .agent-trigger.open .at-caret{transform:rotate(180deg)}
        .agent-menu{position:absolute;bottom:calc(100% + 6px);left:0;min-width:100%;max-width:min(80vw,260px);max-height:280px;overflow-y:auto;background:var(--tmwd-bg);border:1px solid var(--tmwd-border);border-radius:10px;box-shadow:0 12px 28px rgba(15,23,42,.16);padding:4px;z-index:5;display:none}
        .agent-menu.open{display:block}
        .agent-opt{display:flex;flex-direction:column;gap:1px;padding:8px 9px;border-radius:7px;cursor:pointer;min-width:0}
        .agent-opt:hover{background:var(--tmwd-body)}
        .agent-opt.active{background:var(--tmwd-primary-soft)}
        .agent-opt .ao-name{font-size:12.5px;font-weight:600;color:var(--tmwd-fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .agent-opt .ao-desc{font-size:10.5px;color:var(--tmwd-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .agent-opt.disabled{opacity:.5;cursor:not-allowed}
        .agent-opt.disabled:hover{background:transparent}
        .composer-row .spacer{margin-left:auto}
        .send{height:30px;width:30px;border:0;border-radius:9px;background:var(--tmwd-primary);color:var(--tmwd-primary-fg);cursor:pointer;display:grid;place-items:center;flex:0 0 auto;transition:background .15s ease,transform .15s ease}
        .send:hover{background:var(--tmwd-primary-hover)}
        .send:active{transform:translateY(1px)}
        .send:disabled{background:#cbd5e1;cursor:not-allowed}
        .send.stop{background:#dc2626}
        .send.stop:hover{background:#b91c1c}
        /* 会话抽屉 */
        /* .wrap 改为 overflow:visible 后，滑出的抽屉不再被面板裁掉，必须自己
           用 visibility 隐藏（过渡结束再置 hidden，避免打断滑出动画）。 */
        .drawer{position:absolute;inset:0;background:var(--tmwd-bg);display:flex;flex-direction:column;transform:translateX(100%);visibility:hidden;transition:transform .22s ease,visibility 0s linear .22s;z-index:2;border-radius:20px;overflow:hidden}
        .drawer.open{transform:translateX(0);visibility:visible;transition:transform .22s ease,visibility 0s linear 0s}
        .drawer header{background:var(--tmwd-bg)}
        .drawer header .title{flex:1}
        .drawer-list{flex:1;overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:6px;background:var(--tmwd-body)}
        .sess{display:flex;align-items:center;gap:10px;padding:11px 12px;border:1px solid var(--tmwd-border);border-radius:10px;cursor:pointer;background:var(--tmwd-bg);transition:all .14s ease}
        .sess:hover{background:var(--tmwd-body);border-color:var(--tmwd-border-strong)}
        .sess.active{border-color:var(--tmwd-primary);background:var(--tmwd-primary-soft);box-shadow:0 0 0 1px var(--tmwd-primary-ring)}
        .sess-main{flex:1;min-width:0}
        .sess-title{font-size:13px;font-weight:600;color:var(--tmwd-fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .sess-meta{font-size:10.5px;color:var(--tmwd-muted);margin-top:2px}
        .drawer-empty{align-self:center;color:var(--tmwd-muted);font-size:12.5px;margin-top:40px}
      </style>
      <div class="wrap" id="wrap">
        <header>
          <span class="title" id="title">网页 Agent 对话</span>
          <!-- 使用量（放在标题栏，输入区那一栏挤不下）-->
          <div class="ctl" id="usageCtl">
            <button type="button" class="usage-btn" id="usageTrigger" title="上下文 Token">
              <svg class="usage-ring" width="20" height="20" viewBox="0 0 20 20">
                <circle cx="10" cy="10" r="8" fill="none" stroke="var(--tmwd-border)" stroke-width="3"/>
                <circle id="usageArc" cx="10" cy="10" r="8" fill="none" stroke="var(--tmwd-primary)" stroke-width="3" stroke-linecap="round" stroke-dasharray="50.26" stroke-dashoffset="50.26"/>
              </svg>
            </button>
            <div class="usage-pop" id="usagePop"></div>
          </div>
          <button class="iconbtn" id="refreshChat" title="刷新当前会话" aria-label="刷新当前会话">${ICON.refresh}</button>
          <button class="iconbtn" id="switchAgent" title="切换 Agent" aria-label="切换 Agent">${ICON.bot}</button>
          <button class="iconbtn" id="sessions" title="会话列表" aria-label="会话列表">${ICON.list}</button>
          <button class="iconbtn" id="newChat" title="新建会话" aria-label="新建会话">${ICON.plus}</button>
          <button class="iconbtn" id="close" title="关闭" aria-label="关闭">${ICON.close}</button>
        </header>
        <div class="body" id="body"></div>
        <!-- 会话状态固定条：会话最下面、输入框之上。conversation_state 事件驱动，
             展示当前状态（思考中/执行中/等待审批/...）+ 附言。异常态挂重试按钮。 -->
        <div class="status-bar" id="statusBar" data-status="">
          <span class="sb-dot"></span>
          <span class="sb-text" id="statusBarText"></span>
          <button class="sb-retry" id="statusRetry" type="button" hidden>重试</button>
        </div>
        <footer id="footer">
          <div class="composer">
            <div class="skill-pop" id="skillPop" role="listbox"></div>
            <textarea id="input" rows="1" placeholder="描述你想在当前页面完成的事…（Enter 发送，Shift+Enter 换行）"></textarea>
            <div class="attach-strip" id="attachStrip"></div>
            <input type="file" id="attachInput" multiple hidden
              accept="image/*,text/*,.json,.xml,.yaml,.yml,.toml,.md,.csv,.ts,.tsx,.js,.jsx,.py,.go,.rs,.java,.c,.cpp,.h,.hpp">
            <div class="goal-inputs" id="goalInputs" style="display:none">
              <input type="text" class="g-obj" id="goalObjective" placeholder="目标（一句话）">
              <input type="number" class="g-min" id="goalMinutes" min="1" value="15">
              <span class="g-unit">分钟</span>
            </div>
            <div class="composer-row">
              <!-- 附件上传（与用户端 MessageComposer 同款纸夹按钮） -->
              <button type="button" class="iconbtn attach-btn" id="attachBtn" title="添加文件" aria-label="添加文件">${ICON.paperclip}</button>
              <!-- 思考等级 -->
              <div class="ctl" id="reasoningCtl">
                <button type="button" class="ctl-trigger" id="reasoningTrigger" title="思考等级，下一轮消息生效" aria-haspopup="listbox">
                  <span id="reasoningLabel">中</span><span class="ct-caret">${ICON.chevron}</span>
                </button>
                <div class="ctl-menu" id="reasoningMenu" role="listbox"></div>
              </div>
              <!-- 执行模式（与思考等级同款 pill 下拉） -->
              <div class="ctl" id="modeCtl">
                <button type="button" class="ctl-trigger" id="modeTrigger" title="执行模式" aria-haspopup="listbox">
                  <span id="modeLabel">普通</span><span class="ct-caret">${ICON.chevron}</span>
                </button>
                <div class="ctl-menu" id="modeMenu" role="listbox"></div>
              </div>
              <!-- 模型 -->
              <div class="ctl" id="modelCtl">
                <button type="button" class="ctl-trigger" id="modelTrigger" title="切换模型，下一轮消息生效" aria-haspopup="listbox">
                  <span class="ct-text" id="modelLabel">选择模型</span><span class="ct-caret">${ICON.chevron}</span>
                </button>
                <div class="ctl-menu wide" id="modelMenu" role="listbox">
                  <div class="ctl-search"><input type="text" id="modelSearch" placeholder="搜索模型…" autocomplete="off" spellcheck="false"></div>
                  <div id="modelOptions"></div>
                </div>
              </div>
              <!-- 通知中心：模型旁铃铛，未读角标 + 点击展开历史通知列表。
                   新通知（如「已切换到自动模式，放行 N 个待审批」）会先在页面弹框显示几秒。 -->
              <div class="ctl" id="notifCtl">
                <button type="button" class="ctl-trigger notif-trigger" id="notifTrigger" title="通知" aria-haspopup="listbox">
                  <span class="notif-icon" id="notifIcon">${ICON.bell}</span>
                  <span class="notif-badge" id="notifBadge" hidden></span>
                </button>
                <div class="ctl-menu wide notif-menu" id="notifMenu" role="listbox"></div>
              </div>
              <span class="spacer"></span>
              <button class="iconbtn" id="clearChat" title="清空对话" aria-label="清空对话">${ICON.eraser}</button>
              <button class="send" id="send" title="发送" aria-label="发送">${ICON.send}</button>
            </div>
          </div>
        </footer>
        <div class="drawer" id="drawer">
          <header>
            <span class="title">会话列表</span>
            <button class="iconbtn" id="drawerRefresh" title="刷新" aria-label="刷新">${ICON.refresh}</button>
            <button class="iconbtn" id="drawerClose" title="关闭" aria-label="关闭">${ICON.close}</button>
          </header>
          <div class="drawer-list" id="drawerList"></div>
        </div>
      </div>`;
    els.ICON = ICON;
    const mountHost = () => {
      if (!host.isConnected) (document.body || document.documentElement).appendChild(host);
    };
    mountHost();
    // 同 z-index 时靠 DOM 顺序决胜：把 host 挪到 body 最后一个子节点，保证面板
    // （及其浮出的下拉/弹层）盖过同层级里后插入的页面元素。打开面板时也会再调用。
    const raiseHost = () => {
      try {
        const parent = document.body || document.documentElement;
        if (host && parent && parent.lastChild !== host) parent.appendChild(host);
      } catch (_) { /* ignore */ }
    };
    els.raiseHost = raiseHost;
    // 面板整棵树在 Shadow DOM 里，重挂同一个 host 即可原样恢复消息流、输入框内容
    // 与全部事件监听，不必重建也不会丢正在进行的会话。
    if (window.__tmwdMountGuard) window.__tmwdMountGuard(() => host, mountHost);
    els.wrap = shadow.getElementById('wrap');
    els.title = shadow.getElementById('title');
    els.body = shadow.getElementById('body');
    els.footer = shadow.getElementById('footer');
    els.input = shadow.getElementById('input');
    els.send = shadow.getElementById('send');
    // 旧「输入框下方提示」已废弃：所有提示统一进通知中心（铃铛历史 + 页面弹框几秒）。
    // 用一个只读文本 + setter 的 shim 兜住历史调用点（els.hint.textContent = ...），
    // 写入非空串即转成一条通知；写空串 = 清除（通知中心无清除语义，直接忽略）。
    // 这样散落各处的提示无需逐个改写，且新代码若再写 els.hint 也会自动进通知。
    let _hintText = '';
    els.hint = {
      get textContent() { return _hintText; },
      set textContent(v) {
        const t = String(v == null ? '' : v);
        if (t === _hintText) return; // 与上一条相同：不重复推通知（旧元素是覆盖语义）
        _hintText = t;
        if (t) pushNotice(t);
      },
    };
    els.skillPop = shadow.getElementById('skillPop');
    els.drawer = shadow.getElementById('drawer');
    els.drawerList = shadow.getElementById('drawerList');
    els.switchAgent = shadow.getElementById('switchAgent');
    // 思考等级
    els.reasoningCtl = shadow.getElementById('reasoningCtl');
    els.reasoningTrigger = shadow.getElementById('reasoningTrigger');
    els.reasoningLabel = shadow.getElementById('reasoningLabel');
    els.reasoningMenu = shadow.getElementById('reasoningMenu');
    // 模式
    els.modeCtl = shadow.getElementById('modeCtl');
    els.modeTrigger = shadow.getElementById('modeTrigger');
    els.modeLabel = shadow.getElementById('modeLabel');
    els.modeMenu = shadow.getElementById('modeMenu');
    els.goalInputs = shadow.getElementById('goalInputs');
    els.goalObjective = shadow.getElementById('goalObjective');
    els.goalMinutes = shadow.getElementById('goalMinutes');
    // 模型
    els.modelCtl = shadow.getElementById('modelCtl');
    els.modelTrigger = shadow.getElementById('modelTrigger');
    els.modelLabel = shadow.getElementById('modelLabel');
    els.modelMenu = shadow.getElementById('modelMenu');
    els.modelSearch = shadow.getElementById('modelSearch');
    els.modelOptions = shadow.getElementById('modelOptions');
    els.attachBtn = shadow.getElementById('attachBtn');
    els.attachInput = shadow.getElementById('attachInput');
    els.attachStrip = shadow.getElementById('attachStrip');
    els.statusBar = shadow.getElementById('statusBar');
    els.statusBarText = shadow.getElementById('statusBarText');
    els.statusRetry = shadow.getElementById('statusRetry');
    // 使用量
    els.usageCtl = shadow.getElementById('usageCtl');
    els.usageTrigger = shadow.getElementById('usageTrigger');
    els.usageArc = shadow.getElementById('usageArc');
    els.usagePop = shadow.getElementById('usagePop');
    // 通知中心
    els.notifCtl = shadow.getElementById('notifCtl');
    els.notifTrigger = shadow.getElementById('notifTrigger');
    els.notifIcon = shadow.getElementById('notifIcon');
    els.notifBadge = shadow.getElementById('notifBadge');
    els.notifMenu = shadow.getElementById('notifMenu');

    shadow.getElementById('close').addEventListener('click', () => togglePanel(false));
    shadow.getElementById('drawerClose').addEventListener('click', () => toggleDrawer(false));
    shadow.getElementById('clearChat').addEventListener('click', () => { if (!state.sending) startFreshSession(); });
    els.switchAgent.addEventListener('click', () => { if (!state.sending) showPicker(true); });
    els.send.addEventListener('click', onSend);
    // 附件上传：纸夹按钮 → 隐藏 file input；选中的文件读成 base64 经 WS 上传。
    if (els.attachBtn) els.attachBtn.addEventListener('click', () => { if (!state.sending) els.attachInput && els.attachInput.click(); });
    if (els.attachInput) els.attachInput.addEventListener('change', onAttachPicked);
    const autoGrow = () => { const t = els.input; t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 150) + 'px'; };
    els.input.addEventListener('input', autoGrow);
    els.input.addEventListener('input', () => { void onSkillInput(); });
    els.input.addEventListener('click', () => { void onSkillInput(); });
    els.input.addEventListener('compositionstart', () => { skillComposing = true; });
    els.input.addEventListener('compositionend', () => { skillComposing = false; void onSkillInput(); });
    els.input.addEventListener('keydown', (e) => {
      if (skillPopOpen()) {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); skillMove(e.key === 'ArrowDown' ? 1 : -1); return; }
        if (e.key === 'Enter' && !e.shiftKey) {
          // 选中项 → 插入技能 token 并拦下发；无匹配（如路径）→ 放行正常发送。
          if (skillPickActive()) { e.preventDefault(); return; }
          skillClose();
        } else if (e.key === 'Tab') {
          e.preventDefault();
          if (!skillPickActive()) skillClose();
          return;
        } else if (e.key === 'Escape') { e.preventDefault(); skillClose(); return; }
      }
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSend(); }
    });
    els.skillPop.addEventListener('mousedown', (e) => {
      const item = e.target.closest('.sp-item'); if (!item) return;
      e.preventDefault(); skillPick(item.dataset.name || '');
    });
    // 面板内点击空白处收起技能弹层（shadow 感知：composedPath 含 skillPop/input 则保留）。
    shadow.addEventListener('mousedown', (e) => {
      if (!skillPopOpen()) return;
      const path = (typeof e.composedPath === 'function' ? e.composedPath() : []) || [];
      if (path.indexOf(els.skillPop) === -1 && path.indexOf(els.input) === -1) skillClose();
    });

    // 思考等级下拉
    renderReasoningMenu();
    els.reasoningTrigger.addEventListener('click', (e) => { e.stopPropagation(); toggleCtlMenu(els.reasoningMenu, !els.reasoningMenu.classList.contains('open')); });
    els.reasoningMenu.addEventListener('click', (e) => {
      const opt = e.target.closest('.ctl-opt'); if (!opt) return;
      state.reasoningEffort = opt.dataset.value || '';
      renderReasoningMenu(); renderReasoningLabel(); toggleCtlMenu(els.reasoningMenu, false);
      // 立即生效：写进会话设置，不等下一次发送。
      pushConversationSettings({ reasoningEffort: state.reasoningEffort });
    });
    // 模式下拉（与思考等级同款 pill）
    renderModeMenu();
    els.modeTrigger.addEventListener('click', (e) => { e.stopPropagation(); toggleCtlMenu(els.modeMenu, !els.modeMenu.classList.contains('open')); });
    els.modeMenu.addEventListener('click', (e) => {
      const opt = e.target.closest('.ctl-opt'); if (!opt) return;
      state.mode = opt.dataset.value || 'interact';
      renderModeMenu(); renderModeLabel();
      if (els.goalInputs) els.goalInputs.style.display = state.mode === 'goal' ? 'flex' : 'none';
      toggleCtlMenu(els.modeMenu, false);
      // 立即生效：切 auto 时服务端会把在途 pending 审批一并放行（不等下一轮）。
      pushConversationSettings({ mode: state.mode });
    });
    // 模型下拉（带搜索框）
    els.modelTrigger.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = !els.modelMenu.classList.contains('open');
      toggleCtlMenu(els.modelMenu, open);
      if (open) {
        // 每次打开都从全量列表起步，避免上次的过滤词把选项藏起来。
        state.modelQuery = '';
        if (els.modelSearch) els.modelSearch.value = '';
        renderModelMenu();
        setTimeout(() => els.modelSearch && els.modelSearch.focus(), 0);
      }
    });
    els.modelSearch.addEventListener('input', () => {
      state.modelQuery = els.modelSearch.value || '';
      renderModelMenu();
    });
    // 搜索框内的按键不要冒泡到面板/页面（Enter 选中首个匹配项，Esc 收起）。
    els.modelSearch.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Escape') { toggleCtlMenu(els.modelMenu, false); return; }
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const first = filteredModels()[0];
      if (first) applyModelSelection(first.id);
    });
    els.modelMenu.addEventListener('click', (e) => {
      const opt = e.target.closest('.ctl-opt'); if (!opt || !opt.dataset.value) return;
      applyModelSelection(opt.dataset.value);
    });
    // 使用量弹层
    els.usageTrigger.addEventListener('click', (e) => { e.stopPropagation(); els.usagePop.classList.toggle('open'); });
    // 通知中心：展开时把未读清零（列表即为已读入口）。
    els.notifTrigger.addEventListener('click', (e) => {
      e.stopPropagation();
      const menu = els.notifMenu;
      const willOpen = !menu.classList.contains('open');
      toggleCtlMenu(menu, willOpen);
      if (willOpen) {
        renderNotifications(); // 先渲染（保留未读标记给用户看到）
        // 展开即视为「已查看」：清未读角标 + 逐条未读标记，下次重开不再高亮。
        state.notifications.forEach((n) => { n.unread = false; });
        state.notificationsUnread = 0;
        renderNotifBadge();
      }
    });
    // 点列表里的某条 = 选中（高亮），再点一次收起。
    els.notifMenu.addEventListener('click', (e) => {
      const item = e.target.closest('.notif-item'); if (!item || !item.dataset.id) return;
      selectNotification(item.dataset.id);
    });
    // 点击面板外关闭所有下拉/弹层
    document.addEventListener('click', (e) => {
      const path = e.composedPath?.() || [];
      if (els.reasoningMenu?.classList.contains('open') && !path.includes(els.reasoningCtl)) toggleCtlMenu(els.reasoningMenu, false);
      if (els.modeMenu?.classList.contains('open') && !path.includes(els.modeCtl)) toggleCtlMenu(els.modeMenu, false);
      if (els.modelMenu?.classList.contains('open') && !path.includes(els.modelCtl)) toggleCtlMenu(els.modelMenu, false);
      if (els.usagePop?.classList.contains('open') && !path.includes(els.usageCtl)) els.usagePop.classList.remove('open');
      if (els.notifMenu?.classList.contains('open') && !path.includes(els.notifCtl)) toggleCtlMenu(els.notifMenu, false);
    });
    shadow.getElementById('newChat').addEventListener('click', onNewSession);
    shadow.getElementById('sessions').addEventListener('click', () => toggleDrawer(true));
    shadow.getElementById('drawerRefresh').addEventListener('click', refreshSessions);
    shadow.getElementById('refreshChat').addEventListener('click', onRefreshChat);
    // 用户滚动实时更新「是否贴底」：往上翻就停止自动跟随（轮询/事件重渲染不再
    // 把人拽回底部），翻回贴底恢复跟随。passive：不阻止页面滚动。
    els.body.addEventListener('scroll', () => {
      _stickToBottom = bodyNearBottom();
      // 翻到接近顶部：加载更早轮次（hasMore 且未在加载/发送中）。服务端按用户
      // 轮次分页，跟 web 端「向上滚动加载更早」同口径。
      if (els.body.scrollTop <= 48 && state.hasMore && !state.loadingMore && !state.sending) {
        void loadEarlier();
      }
    }, { passive: true });
  }

  function toggleCtlMenu(menu, open) {
    if (!menu) return;
    // 互斥：打开一个先收起其它。
    if (open) {
      [els.reasoningMenu, els.modeMenu, els.modelMenu, els.notifMenu].forEach((m) => { if (m && m !== menu) m.classList.remove('open'); });
      if (els.usagePop) els.usagePop.classList.remove('open');
    }
    menu.classList.toggle('open', open);
  }

  function renderReasoningMenu() {
    if (!els.reasoningMenu) return;
    els.reasoningMenu.innerHTML = '';
    for (const o of REASONING_OPTIONS) {
      const el = document.createElement('div');
      el.className = 'ctl-opt' + (o.value === state.reasoningEffort ? ' active' : '');
      el.dataset.value = o.value;
      el.textContent = o.label;
      els.reasoningMenu.appendChild(el);
    }
  }

  function renderReasoningLabel() {
    if (!els.reasoningLabel) return;
    const o = REASONING_OPTIONS.find((x) => x.value === state.reasoningEffort) || REASONING_OPTIONS[0];
    els.reasoningLabel.textContent = o.label;
  }

  function renderModeMenu() {
    if (!els.modeMenu) return;
    els.modeMenu.innerHTML = '';
    for (const m of MODE_OPTIONS) {
      const el = document.createElement('div');
      el.className = 'ctl-opt' + (m.value === state.mode ? ' active' : '');
      el.dataset.value = m.value;
      el.title = m.title;
      el.textContent = m.label;
      els.modeMenu.appendChild(el);
    }
  }

  function renderModeLabel() {
    if (!els.modeLabel) return;
    const m = MODE_OPTIONS.find((x) => x.value === state.mode) || MODE_OPTIONS[0];
    els.modeLabel.textContent = m.label;
  }

  // 选项与触发器统一显示模型 ID：显示名往往很长，那一栏放不下。
  function modelText(m) {
    return (m && (m.id || m.name)) || '';
  }

  // 按搜索词过滤：名称/ID/说明任一命中即保留，空词返回全量。
  function filteredModels() {
    const q = (state.modelQuery || '').trim().toLowerCase();
    if (!q) return state.models;
    return state.models.filter((m) => {
      const hay = [m.id, m.name, m.description].filter(Boolean).join(' ').toLowerCase();
      return hay.includes(q);
    });
  }

  function applyModelSelection(id) {
    state.selectedModel = id || '';
    state.modelQuery = '';
    if (els.modelSearch) els.modelSearch.value = '';
    // 记住选择：刷新页面/重开面板后仍显示用户选的模型，而不是回退到 agent 默认。
    // 连 agentId 一起写，换 agent 时旧选择不会串到新 agent 上。
    writePanelState({ model: state.selectedModel, agentId: state.selectedAgent });
    renderModelMenu(); renderModelLabel(); renderUsage();
    toggleCtlMenu(els.modelMenu, false);
    // 立即生效：写进会话设置，不等下一次发送。
    pushConversationSettings({ model: state.selectedModel });
  }

  function renderModelMenu() {
    if (!els.modelOptions) return;
    els.modelOptions.innerHTML = '';
    const list = filteredModels();
    if (!list.length) {
      const el = document.createElement('div');
      el.className = 'ctl-opt';
      el.textContent = state.models.length ? '无匹配模型' : '暂无可用模型';
      els.modelOptions.appendChild(el); return;
    }
    for (const m of list) {
      const el = document.createElement('div');
      el.className = 'ctl-opt' + (m.id === state.selectedModel ? ' active' : '');
      el.dataset.value = m.id;
      // 选项只留一行 ID，显示名/说明放 tooltip，避免菜单被撑高撑宽。
      el.title = [m.id, m.name && m.name !== m.id ? m.name : '', m.description].filter(Boolean).join('\n');
      const name = document.createElement('div'); name.className = 'co-name';
      const text = document.createElement('span'); text.className = 'co-text'; text.textContent = modelText(m);
      name.appendChild(text);
      // 标出 agent 自身配置的模型，便于在长列表里认出「默认那一个」。
      if (m.id && m.id === state.defaultModel) {
        const tag = document.createElement('span'); tag.className = 'co-tag'; tag.textContent = '默认';
        name.appendChild(tag);
      }
      el.appendChild(name);
      els.modelOptions.appendChild(el);
    }
  }

  function renderModelLabel() {
    if (!els.modelLabel) return;
    const m = state.models.find((x) => x.id === state.selectedModel);
    const full = m ? modelText(m) : (state.selectedModel || '');
    els.modelLabel.textContent = full || '选择模型';
    // 触发器上做省略号截断，完整名称落在 tooltip 里。
    if (els.modelTrigger) {
      els.modelTrigger.title = full
        ? `当前模型：${state.selectedModel || full}${state.selectedModel && state.selectedModel === state.defaultModel ? '（Agent 默认）' : ''}\n点击切换，下一轮消息生效`
        : '切换模型，下一轮消息生效';
    }
  }

  function selectedModelMaxTokens() {
    const m = state.models.find((x) => x.id === state.selectedModel);
    return Number(m && m.max_context_tokens) || 0;
  }

  function renderUsage() {
    if (!els.usageArc || !els.usagePop) return;
    const u = state.lastUsage || {};
    const input = Math.max(0, Number(u.prompt || 0));
    const max = selectedModelMaxTokens();
    const ratio = max > 0 ? Math.min(Math.max(input / max, 0), 1) : 0;
    const circ = 2 * Math.PI * 8; // r=8
    els.usageArc.setAttribute('stroke-dasharray', String(circ));
    els.usageArc.setAttribute('stroke-dashoffset', String(circ * (1 - ratio)));
    const fmt = (n) => {
      n = Number(n || 0);
      if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
      if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K';
      return String(n);
    };
    const rows = [
      ['当前输入', fmt(input) + ' tokens'],
      ['模型上限', max > 0 ? fmt(max) + ' tokens' : '上限未知'],
      ['当前输出', fmt(u.completion) + ' tokens'],
    ];
    if ((u.cached || 0) > 0) rows.push(['缓存读取', fmt(u.cached) + ' tokens']);
    if ((u.cacheCreation || 0) > 0) rows.push(['缓存写入', fmt(u.cacheCreation) + ' tokens']);
    if ((u.reasoning || 0) > 0) rows.push(['推理 Token', fmt(u.reasoning) + ' tokens']);
    rows.push(['实际占比', max > 0 ? Math.round(ratio * 100) + '%' : '无法计算']);
    if (u.total != null) rows.push(['当前总计', fmt(u.total) + ' tokens']);
    els.usagePop.innerHTML = '';
    for (const [k, v] of rows) {
      const row = document.createElement('div'); row.className = 'up-row';
      const a = document.createElement('span'); a.textContent = k;
      const b = document.createElement('span'); b.textContent = v;
      row.appendChild(a); row.appendChild(b); els.usagePop.appendChild(row);
    }
    // 压缩入口：手动压缩上下文（与 /compact 同链路）。没会话时禁用。
    const actions = document.createElement('div');
    actions.className = 'up-actions';
    const compactBtn = document.createElement('button');
    compactBtn.type = 'button';
    compactBtn.className = 'up-compact';
    compactBtn.textContent = state.compactPending ? '压缩中…' : '压缩上下文';
    compactBtn.title = '让模型总结并精简历史，下一轮生效（/compact）';
    compactBtn.disabled = state.compactPending || !state.conversationId;
    compactBtn.addEventListener('click', () => { void runCompact(); });
    actions.appendChild(compactBtn);
    els.usagePop.appendChild(actions);
    // 超阈值提醒：token 占比达到 agent 配置的 compact_threshold（默认 0.8）时
    // 主动提醒一次；压缩成功后复位允许再次提醒。纯展示提醒，不阻塞输入。
    if (max > 0 && ratio >= compactThresholdRatio() && state.conversationId && !state.compactNotified) {
      state.compactNotified = true;
      const msg = `上下文已用 ${Math.round(ratio * 100)}%，建议压缩（/compact 或用量弹层里的压缩按钮）`;
      pushNotice(msg); // 通知中心：页面弹框几秒 + 铃铛历史列表
    }
  }

  // 压缩提醒阈值：agent 配置的 compact_threshold（0~1），缺省 0.8。
  function compactThresholdRatio() {
    const agent = state.agents.find((a) => a.id === state.selectedAgent);
    const t = Number(agent && agent.compact_threshold);
    return (Number.isFinite(t) && t > 0 && t <= 1) ? t : 0.8;
  }

  // ── 通知中心 ─────────────────────────────────────────────────────────────
  // 模型旁铃铛：新通知先在页面右下角弹框显示几秒（不抢输入框焦点），同时进
  // 历史列表；点列表里的某条=选中（高亮）。纯前端记录，不落库。
  function pushNotice(text) {
    const t = String(text || '').trim();
    if (!t) return;
    const notice = { id: 'ntf_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), text: t, ts: Date.now(), unread: true };
    state.notifications.unshift(notice);
    if (state.notifications.length > 50) state.notifications.length = 50; // 上限防长会话膨胀
    state.notificationsUnread = Math.min(99, state.notificationsUnread + 1);
    state.selectedNotif = notice.id;
    renderNotifBadge();
    if (els.notifMenu?.classList.contains('open')) renderNotifications();
    showPageNotice(t, notice.id);
  }

  function renderNotifBadge() {
    if (!els.notifBadge || !els.notifTrigger || !els.notifIcon) return;
    const n = state.notificationsUnread;
    els.notifBadge.hidden = n <= 0;
    els.notifBadge.textContent = n > 99 ? '99+' : String(n);
    els.notifTrigger.classList.toggle('has-unread', n > 0);
    els.notifIcon.innerHTML = n > 0 ? els.ICON.bellFill : els.ICON.bell;
    els.notifTrigger.title = n > 0 ? `通知（${n} 条未读）` : '通知';
  }

  function notifTime(ts) {
    const d = new Date(ts);
    const now = new Date();
    const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    return d.toDateString() === now.toDateString() ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  }

  function renderNotifications() {
    if (!els.notifMenu) return;
    els.notifMenu.innerHTML = '';
    if (!state.notifications.length) {
      const empty = document.createElement('div');
      empty.className = 'notif-empty';
      empty.textContent = '暂无通知';
      els.notifMenu.appendChild(empty);
      return;
    }
    for (const n of state.notifications) {
      const item = document.createElement('div');
      item.className = 'notif-item'
        + (n.unread ? ' unread' : '')
        + (state.selectedNotif === n.id ? ' selected' : '');
      item.dataset.id = n.id;
      const text = document.createElement('span');
      text.className = 'ni-text';
      text.textContent = n.text;
      const time = document.createElement('span');
      time.className = 'ni-time';
      time.textContent = notifTime(n.ts);
      item.appendChild(text); item.appendChild(time);
      els.notifMenu.appendChild(item);
    }
  }

  function selectNotification(id) {
    if (!id) return;
    // 选中 = 已读这条：清掉它的未读标记（角标数在展开列表时已清零）。
    const n = state.notifications.find((x) => x.id === id);
    if (n && n.unread) n.unread = false;
    state.selectedNotif = state.selectedNotif === id ? '' : id;
    renderNotifications();
  }

  // 页面级通知弹框：独立 Shadow host（与保存提示同款 all:initial 隔离），默认
  // 3 秒自消；点击=选中这条通知并立即关掉。同一时间只显示最新一条。
  let pageNoticeHost = null;
  let pageNoticeTimer = null;
  function showPageNotice(text, noticeId) {
    // 复用旧 host：先拆掉，保证重弹的是最新一条。
    if (pageNoticeHost) { pageNoticeHost.remove(); pageNoticeHost = null; }
    if (pageNoticeTimer) { clearTimeout(pageNoticeTimer); pageNoticeTimer = null; }
    pageNoticeHost = document.createElement('div');
    pageNoticeHost.style.cssText = 'all:initial;position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
    const shadow = pageNoticeHost.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = '.notice{position:fixed;left:20px;bottom:24px;pointer-events:auto;cursor:pointer;'
      + 'background:var(--tmwd-bg,#fff);color:var(--tmwd-fg,#0f172a);'
      + 'border:1px solid var(--tmwd-border,rgba(15,23,42,.12));'
      + 'font:500 13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;'
      + 'padding:10px 16px;border-radius:10px;box-shadow:0 8px 24px rgba(15,23,42,.18);'
      + 'max-width:340px;opacity:0;transform:translateY(6px);transition:opacity .18s ease,transform .18s ease;}'
      + '.notice.on{opacity:1;transform:none;}';
    const box = document.createElement('div');
    box.className = 'notice';
    box.textContent = text;
    box.addEventListener('click', () => {
      // 点击弹框 = 在通知中心里选中这条（面板已打开才高亮可见）。
      if (noticeId) { state.selectedNotif = noticeId; renderNotifications(); }
      if (pageNoticeHost) { pageNoticeHost.remove(); pageNoticeHost = null; }
      if (pageNoticeTimer) { clearTimeout(pageNoticeTimer); pageNoticeTimer = null; }
    });
    shadow.append(style, box);
    (document.body || document.documentElement).appendChild(pageNoticeHost);
    requestAnimationFrame(() => box.classList.add('on'));
    pageNoticeTimer = setTimeout(() => {
      box.classList.remove('on');
      const host = pageNoticeHost;
      pageNoticeHost = null; pageNoticeTimer = null;
      setTimeout(() => host && host.remove(), 220);
    }, 3000);
  }

  function togglePanel(open) {
    ensurePanel();
    state.open = open !== undefined ? open : !state.open;
    els.wrap.classList.toggle('open', state.open);
    // 每次打开都把 host 顶到 body 末尾，压倒同 z-index(2147483647) 的站点弹层。
    if (state.open && els.raiseHost) els.raiseHost();
    // 面板打开时隐藏入口气泡，关掉再显示。
    const ind = document.getElementById('ljq-ind');
    if (ind) ind.style.display = state.open ? 'none' : '';
    writePanelState({ open: state.open });
    if (state.open) runOpenFlow();
  }

  // 面板打开流程：拉可选 agent → 有记忆且仍可用则直接进聊天，否则进首屏选择。
  // 若本 tab 存过进行中的会话（导航重注入场景），直接恢复该会话历史。
  async function openFlow() {
    primeServerOrigin(); // ask_user 附件直链绝对化要用主服务 origin，先预热缓存
    await refreshAgents();
    if (!state.agents.length) { showPicker(); return; }
    if (!state.selectedAgent) {
      const remembered = await storedAgentId();
      if (remembered && state.agents.some(a => a.id === remembered)) {
        state.selectedAgent = remembered;
      }
    }
    if (state.selectedAgent) {
      await enterChat();
      if (!state.conversationId && !state.messages.length) {
        await resumeConversationIfStored();
      }
    } else { showPicker(); }
  }

  // 从 storage 恢复本 tab 上次的会话（刷新/导航重注入后 SW 一旦连上即触发）。
  // 没存 conversationId 或恢复失败都静默——空会话态让用户正常发新消息即可。
  async function resumeConversationIfStored() {
    if (state.conversationId || state.messages.length) return;
    const saved = await readPanelState();
    const savedConv = saved && saved.conversationId;
    if (!savedConv) return;
    await loadSession(savedConv);
  }

  async function refreshAgents() {
    const agents = await sendChat('chat_list_agents', {}, () => {});
    state.agents = Array.isArray(agents) ? agents : [];
    if (state.selectedAgent && !state.agents.some(a => a.id === state.selectedAgent)) {
      state.selectedAgent = null;
    }
  }

  // 首屏：正文区渲染 agent 卡片列表；隐藏输入区。
  // refresh=true（切换 Agent 按钮）时先重拉一次：服务端刚改过该客户端的可控制
  // agent 列表，面板开着时内存里的 state.agents 是旧的，不重拉就永远显示旧列表。
  async function showPicker(refresh) {
    state.view = 'picker';
    if (els.footer) els.footer.style.display = 'none';
    els.body.innerHTML = '';
    if (refresh) {
      try { await refreshAgents(); } catch (_) { /* 拉不到就用现有列表渲染 */ }
    }
    const wrap = document.createElement('div');
    wrap.className = 'picker';
    const head = document.createElement('div'); head.className = 'picker-head'; head.textContent = '选择一个 Agent';
    const sub = document.createElement('div'); sub.className = 'picker-sub';
    sub.textContent = '这些 Agent 已获授权操作当前浏览器客户端。';
    wrap.appendChild(head); wrap.appendChild(sub);
    if (!state.agents.length) {
      const empty = document.createElement('div'); empty.className = 'drawer-empty';
      empty.textContent = '该客户端未授权任何 Agent。请在资源页为客户端勾选并授权 Agent。';
      wrap.appendChild(empty);
    } else {
      const list = document.createElement('div'); list.className = 'picker-list';
      for (const a of state.agents) {
        const card = document.createElement('div'); card.className = 'picker-card';
        const name = document.createElement('div'); name.className = 'pc-name';
        name.textContent = a.display_name || a.name || ('Agent #' + a.id);
        card.appendChild(name);
        if (a.description || a.name) {
          const d = document.createElement('div'); d.className = 'pc-desc';
          d.textContent = a.description || a.name; card.appendChild(d);
        }
        card.addEventListener('click', () => {
          state.selectedAgent = a.id;
          rememberAgentId(a.id);
          // 换 agent 清空当前选择，让 refreshModels 去读这个 agent 自己的
          // 上次选择/默认模型，避免上一个 agent 的选择串过来。
          state.selectedModel = '';
          void enterChat();
        });
        list.appendChild(card);
      }
      wrap.appendChild(list);
    }
    els.body.appendChild(wrap);
  }

  // 进入聊天视图：显示输入区、初始化控件、拉模型列表、渲染空态。
  async function enterChat() {
    state.view = 'chat';
    if (els.footer) els.footer.style.display = '';
    renderReasoningLabel();
    renderModeMenu();
    renderModeLabel();
    renderModelLabel();
    renderUsage();
    setSending(state.sending);
    renderMessages();
    setTimeout(() => els.input && els.input.focus(), 120);
    await refreshModels();
  }

  async function refreshModels() {
    if (!state.selectedAgent) return;
    const res = await sendChat('chat_list_models', { agentId: state.selectedAgent }, () => {});
    const models = (res && Array.isArray(res.models)) ? res.models : [];
    state.models = models;
    // agent 自身配置的模型（服务端 default_model），用于菜单里标注「Agent 默认」。
    state.defaultModel = (res && res.defaultModel) || '';
    // 选中优先级：内存里已选 > 本页上次手动选的 > agent 默认 > 第一项。
    // 手动选择要盖过 agent 默认，否则每次重载面板都会把用户的选择打回默认。
    if (!state.selectedModel) {
      state.selectedModel = await savedModelForAgent(state.selectedAgent);
    }
    if (!state.selectedModel || !models.some(m => m.id === state.selectedModel)) {
      state.selectedModel = state.defaultModel || (models[0] && models[0].id) || '';
    }
    renderModelMenu();
    renderModelLabel();
    renderUsage();
  }

  function renderMessages() {
    // 整树重建会把 scrollTop 清零：贴底时重渲染后回底；用户翻在上面时恢复原
    // scrollTop，阅读位置不因后台轮询/工具事件/重连刷新而跳动（stick-to-bottom）。
    const _prevScrollTop = els.body.scrollTop;
    const _prevStick = _stickToBottom;
    // 消息状态可能刚变（流结束/接管续看/载入历史）：输入区按钮据此在「发送/停止」
    // 间同步，保证接管态（服务端仍在跑）也能就地停止。
    syncComposer();
    els.body.innerHTML = '';
    if (!state.messages.length) {
      const agent = state.agents.find(a => a.id === state.selectedAgent);
      const empty = document.createElement('div');
      empty.className = 'empty';
      const icon = document.createElement('div');
      icon.className = 'empty-icon';
      icon.innerHTML = els.ICON.bot;
      const title = document.createElement('div');
      title.className = 'empty-title';
      title.textContent = agent ? ('与 ' + (agent.display_name || agent.name) + ' 开始对话') : '选择一个 Agent';
      const desc = document.createElement('div');
      desc.textContent = '描述你想在当前页面完成的事，Agent 会在这里展示回复与工具调用过程。';
      empty.appendChild(icon);
      empty.appendChild(title);
      empty.appendChild(desc);
      // 空态示例问题 chips：点一下即填入输入框（不直接发送，让用户可改）。
      // 文案贴合「网页 Agent」场景——操作当前页面，而不是通用问答。
      const chips = document.createElement('div');
      chips.className = 'empty-chips';
      for (const text of EMPTY_PROMPT_SUGGESTIONS) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'ec-chip';
        btn.textContent = text;
        btn.addEventListener('click', () => {
          if (state.sending || !els.input) return;
          els.input.value = text;
          autoGrowSafe();
          els.input.focus();
        });
        chips.appendChild(btn);
      }
      empty.appendChild(chips);
      els.body.appendChild(empty);
      return;
    }
    // 流式增量用：记录「最后一条 assistant 消息」末段 text 的 DOM，content 增量直接改它。
    let lastStreamingTextEl = null;
    for (const m of state.messages) {
      // 历史 tool 行归入独立卡片行（无宿主气泡时单独展示）。
      if (m.role === 'tool') {
        const row = document.createElement('div');
        row.className = 'msg-row assistant';
        const holder = document.createElement('div');
        holder.className = 'bubble assistant';
        holder.appendChild(renderToolCard(m));
        row.appendChild(holder);
        els.body.appendChild(row);
        continue;
      }
      const isUser = m.role === 'user';
      const isLastMsg = m === state.messages[state.messages.length - 1];
      const row = document.createElement('div');
      row.className = 'msg-row ' + m.role;
      const bubble = document.createElement('div');
      const streaming = !isUser && m.status === 'streaming';
      bubble.className = 'bubble ' + m.role
        + (!isUser && m.status === 'error' ? ' err' : '')
        + (streaming ? ' streaming' : '');

      const head = document.createElement('div');
      head.className = 'msg-head';
      const who = document.createElement('span');
      who.textContent = isUser ? '你' : 'Agent';
      head.appendChild(who);
      // 模型：assistant 消息显示本轮实际生效的模型（与 web 端同口径）；空则不显示。
      if (!isUser && m.model) {
        const modelEl = document.createElement('span');
        modelEl.className = 'msg-model';
        modelEl.textContent = shortModelName(m.model);
        modelEl.title = m.model;
        head.appendChild(modelEl);
      }
      if (m.createdAt) {
        const time = document.createElement('span');
        time.className = 'msg-time';
        time.textContent = formatMessageTime(m.createdAt);
        head.appendChild(time);
      }
      bubble.appendChild(head);

      // 思考过程块：有 parts 时思考已按到达顺序交织进时间线（m.parts 里的
      // {type:'reasoning'} 段），不再在顶部单列一块（避免重复显示）；只有旧消息
      // （无 parts）才退回顶部整段渲染（与 web ReasoningBlock 同口径）。
      if (!isUser && m.reasoning && !(Array.isArray(m.parts) && m.parts.length)) {
        bubble.appendChild(renderReasoningBlock(m));
      }

      // 子 agent 卡片统一渲染在气泡顶部（与 web SubagentBlock 同位序），实时与
      // 刷新都从 m.subagents 读 —— 刷新==实时天然成立，不再依赖 inline push。
      if (!isUser && Array.isArray(m.subagents) && m.subagents.length) {
        for (const sub of m.subagents) bubble.appendChild(renderSubagentCard(sub));
      }

      // 有序 parts 时间线：text 段与工具卡片按事件顺序交错渲染。
      // 旧消息没有 parts（历史会话回放）→ 退化为工具在前 + 正文在后。
      const parts = Array.isArray(m.parts) && m.parts.length ? m.parts : null;
      if (!isUser && parts) {
        for (let pi = 0; pi < parts.length; pi += 1) {
          const p = parts[pi];
          if (p.type === 'text' && p.text) {
            const t = document.createElement('div'); t.className = 'msg-text';
            // 流式中保持 textContent（content 事件增量直写，避免每 delta 重解析
            // markdown 抖动）；done/error/历史回放时切 innerHTML 渲染 markdown。
            if (streaming) {
              t.textContent = stripAgentMetaTags(p.text);
              if (isLastMsg) lastStreamingTextEl = t;
            } else {
              t.classList.add('md');
              t.innerHTML = renderMarkdown(p.text);
            }
            bubble.appendChild(t);
          } else if (p.type === 'reasoning' && p.text) {
            // 思考段：与正文/工具卡按到达顺序交织（不再整轮糊在顶部一块）。
            // renderReasoningBlock 把 _reasonEl 记在 part 上，流式增量按段直写。
            // 转圈只挂在「末段且消息在流式」——中间段已写完，不该各自转圈。
            p.status = (streaming && pi === parts.length - 1) ? 'streaming' : 'done';
            bubble.appendChild(renderReasoningBlock(p));
            if (isLastMsg) lastStreamingTextEl = null;
          } else if (p.type === 'tool' && p.call) {
            bubble.appendChild(renderToolCard(p.call));
            // 工具段在 text 之后 → 之前的 text 段不再是末段，增量直写引用失效。
            if (isLastMsg) lastStreamingTextEl = null;
          } else if (p.type === 'approval' && p.approval) {
            bubble.appendChild(renderApprovalCard(p.approval));
            if (isLastMsg) lastStreamingTextEl = null;
          } else if (p.type === 'candidates' && Array.isArray(p.candidates)) {
            bubble.appendChild(renderCandidates(p.candidates));
            if (isLastMsg) lastStreamingTextEl = null;
          } else if (p.type === 'questions' && Array.isArray(p.questions)) {
            bubble.appendChild(renderQuestionForm(p.questions));
            if (isLastMsg) lastStreamingTextEl = null;
          } else if (p.type === 'media' && p.media) {
            bubble.appendChild(renderMedia(p.media));
            if (isLastMsg) lastStreamingTextEl = null;
          } else if (p.type === 'subagent' && p.subId) {
            const sub = (m.subagents || []).find((s) => s.id === p.subId);
            if (sub) bubble.appendChild(renderSubagentCard(sub));
            if (isLastMsg) lastStreamingTextEl = null;
          }
        }
      } else if (!isUser && Array.isArray(m.toolCalls) && m.toolCalls.length) {
        for (const tc of m.toolCalls) bubble.appendChild(renderToolCard(tc));
      }

      const hasTools = !isUser && Array.isArray(m.toolCalls) && m.toolCalls.length > 0;
      // 兼容只有 content（无 parts 或 parts 无 text 段）的旧消息。
      const hasTextInParts = parts && parts.some((p) => p.type === 'text' && p.text);
      if (m.content && !hasTextInParts) {
        const text = document.createElement('div');
        text.className = 'msg-text' + (streaming ? '' : ' md');
        if (streaming) {
          text.textContent = stripAgentMetaTags(m.content);
          if (isLastMsg) lastStreamingTextEl = text;
        } else {
          text.innerHTML = renderMarkdown(m.content);
        }
        bubble.appendChild(text);
      } else if (streaming && !hasTools && !hasTextInParts) {
        const thinking = document.createElement('div');
        thinking.className = 'thinking';
        thinking.innerHTML = '<span class="spinner"></span>思考中…';
        bubble.appendChild(thinking);
      }
      if (!isUser && streaming && m.retryHint) {
        const hint = document.createElement('div');
        hint.className = 'retry-hint';
        hint.innerHTML = '<span class="spinner"></span>' + escapeHtml(m.retryHint);
        bubble.appendChild(hint);
      }

      // 错误独立成行（与 web「错误：{message.error}」同口径）：不并入正文，刷新后
      // 历史回放由 mapLoadedMessages 回填 m.error，位置在重试按钮之前。
      if (!isUser && m.error) {
        const errEl = document.createElement('div');
        errEl.className = 'msg-error';
        errEl.textContent = '错误：' + m.error;
        bubble.appendChild(errEl);
      }

      // 失败的最后一条 assistant 消息：显示重试按钮。刚失败的 live 消息没有真实
      // 消息 id（前端对象），onRetry 会先按会话回查后端最后一条 error 消息的 id。
      // 这里不要求 state.conversationId：建连阶段就失败的那一轮从没收到过
      // conversation 事件，会话 id 是空的，而那恰恰是最需要重试的情况。
      if (!isUser && m.status === 'error' && isLastMsg) {
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'msg-retry';
        retry.textContent = '重试';
        retry.addEventListener('click', () => onRetry(m));
        bubble.appendChild(retry);
      }

      row.appendChild(bubble);
      els.body.appendChild(row);
    }
    // 让 onEvent 的 content 增量路径直接复用末段 text DOM，不整棵重建。
    const _lastMsg = state.messages[state.messages.length - 1];
    if (_lastMsg && _lastMsg.role === 'assistant') _lastMsg._lastTextEl = lastStreamingTextEl;
    // 流式光标只挂在「最后一条 streaming 消息的最后一个 text 段」上；前面的 text 段
    // 已输出完，不该再闪。末尾是工具卡时 lastStreamingTextEl=null，无光标（工具 spinner 替代）。
    if (lastStreamingTextEl) lastStreamingTextEl.classList.add('cursor');
    if (_prevStick) els.body.scrollTop = els.body.scrollHeight;
    else els.body.scrollTop = _prevScrollTop;
  }

  function formatMessageTime(value) {
    const d = value ? new Date(value) : new Date();
    if (isNaN(d.getTime())) return '';
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay
      ? d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      : d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  }

  // 模型名显示缩写：opus#scheme:1 → opus；claude-opus-4-8 原样。气泡头放不下时
  // 也靠 .msg-model 的 ellipsis 兜底，这里只做最常见的剥离。
  function shortModelName(model) {
    const s = String(model || '');
    const hashIdx = s.indexOf('#');
    return (hashIdx > 0 ? s.slice(0, hashIdx) : s) || s;
  }

  function renderToolCard(tc) {
    const card = document.createElement('div');
    // 保留折叠态：流式增量重建气泡时，工具卡的 open 状态记在 call 上，避免被重置。
    card.className = 'tool-card' + (tc._open ? ' open' : '');
    const status = tc.status === 'running' ? '运行中' : tc.status === 'error' ? '失败' : '完成';
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'tc-head';
    head.innerHTML =
      '<span class="tc-icon">' + (tc.status === 'running' ? '<span class="spinner"></span>' : els.ICON.wrench) + '</span>' +
      '<span class="tc-name"></span>' +
      '<span class="tc-status ' + (tc.status || 'done') + '">' + status + '</span>' +
      '<span class="tc-caret">' + els.ICON.chevron + '</span>';
    head.querySelector('.tc-name').textContent = tc.name || '工具';
    head.addEventListener('click', () => { tc._open = !tc._open; card.classList.toggle('open', tc._open); });
    card.appendChild(head);

    const body = document.createElement('div');
    body.className = 'tc-body';
    if (tc.args) {
      const wrap = document.createElement('div');
      const label = document.createElement('div');
      label.className = 'tc-label';
      label.textContent = '参数';
      const block = document.createElement('div');
      block.className = 'tc-block';
      block.textContent = tc.args;
      wrap.appendChild(label);
      wrap.appendChild(block);
      body.appendChild(wrap);
    }
    if (tc.result !== undefined && tc.result !== null && tc.result !== '') {
      const wrap = document.createElement('div');
      const label = document.createElement('div');
      label.className = 'tc-label';
      label.textContent = '结果';
      const block = document.createElement('div');
      block.className = 'tc-block';
      block.textContent = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result);
      wrap.appendChild(label);
      wrap.appendChild(block);
      body.appendChild(wrap);
    }
    if (body.childNodes.length) card.appendChild(body);
    return card;
  }

  // 思考过程折叠块（reasoning 独立于 content 累积，对齐 web 的 ReasoningBlock）。
  // 默认展开（与 web 同口径），折叠态记在 m._reasonOpen 上，流式增量重建气泡时
  // 不被重置（同工具卡 _open）。rb-body 限高滚动，展开也不会把气泡顶穿。
  // m 可以是消息（顶部整段兜底渲染）或一个 reasoning part（时间线交织渲染）——
  // DOM 引用记在传入对象上（_reasonEl），流式增量直写由各分支自己找自己的 part。
  function renderReasoningBlock(m) {
    if (m._reasonOpen === undefined) m._reasonOpen = true;
    const wrap = document.createElement('div');
    wrap.className = 'reasoning-block' + (m._reasonOpen ? ' open' : '');
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'rb-head';
    head.innerHTML =
      '<span class="rb-caret">' + els.ICON.chevron + '</span>' +
      '<span class="rb-title">思考过程</span>' +
      '<span class="rb-toggle"></span>';
    const toggle = head.querySelector('.rb-toggle');
    toggle.textContent = m._reasonOpen ? '收起' : '展开';
    if (m.status === 'streaming') {
      const sp = document.createElement('span');
      sp.className = 'spinner';
      toggle.prepend(sp);
    }
    head.addEventListener('click', () => {
      m._reasonOpen = !m._reasonOpen;
      wrap.classList.toggle('open', m._reasonOpen);
      toggle.textContent = m._reasonOpen ? '收起' : '展开';
    });
    wrap.appendChild(head);
    const body = document.createElement('div');
    body.className = 'rb-body';
    // 顶部整段渲染传消息（.reasoning），时间线交织传 reasoning part（.text）——两处都兼容。
    body.textContent = m.reasoning || m.text || '';
    wrap.appendChild(body);
    // 流式增量直写 body：reasoning 事件高频，避免每次 delta 整棵重建气泡
    // 把已展开的思考块折叠回去。复用 DOM，与 content 的 _lastTextEl 同形。
    m._reasonEl = body;
    return wrap;
  }

  function renderApprovalCard(approval) {
    const card = document.createElement('div');
    card.className = 'approval-card' + (approval.state === 'pending' || approval.state === 'resolving' ? ' open' : '');
    const isFileWrite = approval.toolName === 'file_write' || approval.toolName === 'file_patch';
    // 三种标题与 web ApprovalCard 同口径：code_run=代码执行、file_write/patch=文件写入，
    // 其余（shell/命令类）=命令执行。原先非文件一律叫「代码执行审批」，跑 shell 的
    // 审批被误标成执行代码。
    const approvalTitle = isFileWrite ? '文件写入审批'
      : (approval.toolName === 'code_run' || !approval.toolName) ? '代码执行审批'
      : '命令执行审批';
    const head = document.createElement('div');
    head.className = 'ac-head';
    head.innerHTML =
      '<span class="ac-icon">' + els.ICON.shield + '</span>' +
      '<span class="ac-name">' + approvalTitle + '</span>' +
      '<span class="ac-state ' + (approval.state || 'pending') + '"></span>';
    const stateLabel = {
      pending: '待裁决', resolving: '提交中…', allowed: '已允许',
      denied: '已拒绝', expired: '已过期',
    }[approval.state || 'pending'] || '待裁决';
    head.querySelector('.ac-state').textContent = stateLabel;
    card.appendChild(head);

    const body = document.createElement('div');
    body.className = 'ac-body';
    if (approval.message) {
      const msg = document.createElement('div');
      msg.className = 'ac-msg';
      msg.textContent = approval.message;
      body.appendChild(msg);
    }
    if (isFileWrite) {
      // 越界写入：逐条给出「写到哪、为什么越界、写的是什么」，人核对完再放行。
      const writes = Array.isArray(approval.writes) ? approval.writes : [];
      for (const w of writes) {
        const wrap = document.createElement('div');
        wrap.className = 'ac-run';
        const tag = document.createElement('div');
        tag.className = 'tc-label';
        tag.textContent = String((w && w.tool) || 'file_write') + '：' + String((w && w.path) || '')
          + '（越界：' + String((w && w.reason) || '未知') + '）';
        wrap.appendChild(tag);
        if (w && w.tool === 'file_patch') {
          const before = document.createElement('div');
          before.className = 'ac-msg';
          before.textContent = '替换前 ' + String(w.old_bytes || 0) + 'B sha256:' + String(w.old_sha256 || '')
            + (w.old_truncated ? '（预览已截断）' : '');
          wrap.appendChild(before);
          const oldBlock = document.createElement('pre');
          oldBlock.className = 'ac-code';
          oldBlock.textContent = String(w.old_preview || '');
          wrap.appendChild(oldBlock);
          const after = document.createElement('div');
          after.className = 'ac-msg';
          after.textContent = '替换后 ' + String(w.new_bytes || 0) + 'B sha256:' + String(w.new_sha256 || '')
            + (w.new_truncated ? '（预览已截断）' : '');
          wrap.appendChild(after);
          const newBlock = document.createElement('pre');
          newBlock.className = 'ac-code';
          newBlock.textContent = String(w.new_preview || '');
          wrap.appendChild(newBlock);
        } else if (w) {
          const meta = document.createElement('div');
          meta.className = 'ac-msg';
          meta.textContent = String(w.mode || 'overwrite') + ' · ' + String(w.bytes || 0) + 'B sha256:'
            + String(w.content_sha256 || '') + (w.truncated ? '（预览已截断）' : '');
          wrap.appendChild(meta);
          const block = document.createElement('pre');
          block.className = 'ac-code';
          block.textContent = String(w.preview || '');
          wrap.appendChild(block);
        }
        body.appendChild(wrap);
      }
    } else {
      // 逐条展示要执行的代码，让人核对后再放行。
      const runs = Array.isArray(approval.runs) ? approval.runs : [];
      for (const r of runs) {
        const lang = String((r && r.type) || 'code');
        const code = String((r && r.code) || '');
        const wrap = document.createElement('div');
        wrap.className = 'ac-run';
        const tag = document.createElement('div');
        tag.className = 'tc-label';
        tag.textContent = lang;
        const block = document.createElement('pre');
        block.className = 'ac-code';
        block.textContent = code;
        wrap.appendChild(tag);
        wrap.appendChild(block);
        body.appendChild(wrap);
      }
    }
    card.appendChild(body);

    // 待裁决：允许 / 拒绝。resolving/终态：只读。
    if (approval.state === 'pending' || approval.state === 'resolving') {
      const actions = document.createElement('div');
      actions.className = 'ac-actions';
      // 倒计时：待裁决且有到期时刻时显示「剩余 X 分 Y 秒」；0 = 不过期。
      // 到期由全局 ticker（startApprovalTicker）翻牌成「已过期」并禁用按钮。
      const countdown = document.createElement('span');
      countdown.className = 'ac-countdown';
      countdown.dataset.cid = String(approval.confirmationId || '');
      countdown.textContent = approvalCountdownText(approval);
      actions.appendChild(countdown);
      const allow = document.createElement('button');
      allow.type = 'button';
      allow.className = 'ac-allow';
      allow.textContent = '允许';
      allow.disabled = approval.state === 'resolving';
      allow.addEventListener('click', () => resolveApproval(approval, 'allow'));
      const deny = document.createElement('button');
      deny.type = 'button';
      deny.className = 'ac-deny';
      deny.textContent = '拒绝';
      deny.disabled = approval.state === 'resolving';
      deny.addEventListener('click', () => resolveApproval(approval, 'deny'));
      actions.appendChild(allow);
      actions.appendChild(deny);
      card.appendChild(actions);
    }
    return card;
  }

  // 审批卡倒计时文案。expiresAt 为 POSIX 秒→毫秒后的时刻；0 = 不过期。
  // 只对 pending 卡计时；已裁决/过期卡不显示。
  // 服务端 expires_at 是 POSIX **秒**，必须 ×1000 成毫秒再与 Date.now() 比——
  // 早先各端直接拿秒去比恒成立（秒 < 毫秒），卡片出现一秒就被判过期。
  function approvalExpiresAtMs(raw) {
    const seconds = Number(raw ?? 0);
    if (!Number.isFinite(seconds) || seconds <= 0) return 0;
    return seconds * 1000;
  }
  function approvalCountdownText(approval) {
    if (!approval || approval.state !== 'pending') return '';
    const exp = Number(approval.expiresAt) || 0;
    if (exp <= 0) return '不过期';
    const secs = Math.max(0, Math.round((exp - Date.now()) / 1000));
    if (secs <= 0) return '已超时';
    const days = Math.floor(secs / 86400);
    const hours = Math.floor((secs % 86400) / 3600);
    const mins = Math.floor((secs % 3600) / 60);
    if (days > 0) return `剩余 ${days} 天 ${hours} 小时`;
    if (hours > 0) return `剩余 ${hours} 小时 ${mins} 分`;
    if (mins > 0) return `剩余 ${mins} 分 ${secs % 60} 秒`;
    return `剩余 ${secs} 秒`;
  }

  // 审批到期翻牌：每秒扫一遍消息里的 pending 审批卡，到点即翻「已过期」并禁用按钮
  // （与服务端 is_expired 同口径：now > expires_at 即过期）。web 端有 useApprovalExpiry，
  // 这里补齐 CDP。全局单定时器，空闲（无 pending 卡）时不空转。
  let _approvalTicker = null;
  function startApprovalTicker() {
    if (_approvalTicker) return;
    _approvalTicker = setInterval(() => {
      let changed = false;
      for (const m of state.messages) {
        if (m.role !== 'assistant' || !Array.isArray(m.parts)) continue;
        for (const p of m.parts) {
          const a = p.type === 'approval' ? p.approval : null;
          if (!a || a.state !== 'pending') continue;
          if (a.expiresAt > 0 && a.expiresAt <= Date.now()) {
            a.state = 'expired';
            changed = true;
          }
        }
      }
      if (changed) { renderMessages(); return; }
      // 没到期的：就地刷新倒计时文本（不整棵重建，避免闪烁 + 打断展开态）。
      // 按 data-cid 精确匹配，避免与 resolving 卡的计数节点错位。
      if (!els.body) return;
      const map = new Map();
      for (const m of state.messages) {
        if (m.role !== 'assistant' || !Array.isArray(m.parts)) continue;
        for (const p of m.parts) {
          if (p.type === 'approval' && p.approval && p.approval.state === 'pending'
              && p.approval.confirmationId) {
            map.set(String(p.approval.confirmationId), p.approval);
          }
        }
      }
      for (const node of els.body.querySelectorAll('.ac-countdown')) {
        const a = map.get(String(node.dataset.cid || ''));
        if (a) node.textContent = approvalCountdownText(a);
      }
    }, 1000);
  }
  startApprovalTicker();

  // ask_user 候选答案：点击直接作为下一条用户消息发送（onSend 自带 sending/
  // agent 选择守卫；question 事件后必跟 done，sending 已复位）。
  function renderCandidates(candidates) {
    const wrap = document.createElement('div');
    wrap.className = 'q-cands';
    for (const text of candidates) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'q-cand';
      btn.textContent = text;
      btn.title = text;
      btn.addEventListener('click', () => {
        if (state.sending) return;
        els.input.value = text;
        onSend();
      });
      wrap.appendChild(btn);
    }
    return wrap;
  }

  // ask_user 附件：图片内联（点击新标签页看原图；页面 CSP 拦不掉顶级导航），
  // 加载失败或无直链时降级为文件名 chip，绝不显示破图。
  function renderMedia(m) {
    const wrap = document.createElement('div');
    wrap.className = 'q-media';
    const isImg = /^image\//i.test(m.mime || '') || (!m.mime && /\.(png|jpe?g|gif|webp|avif|svg)$/i.test(m.name || ''));
    const abs = absolutizeMediaUrl(m.src || '');
    const chip = () => {
      const c = document.createElement('button');
      c.type = 'button';
      c.className = 'qm-chip';
      c.textContent = m.name || '附件';
      if (abs) c.addEventListener('click', () => { try { window.open(abs, '_blank', 'noopener'); } catch (_) { /* ignore */ } });
      wrap.appendChild(c);
    };
    if (isImg && abs) {
      const img = document.createElement('img');
      img.alt = m.name || '附件图片';
      img.loading = 'lazy';
      img.addEventListener('click', () => { try { window.open(abs, '_blank', 'noopener'); } catch (_) { /* ignore */ } });
      img.addEventListener('error', () => { wrap.innerHTML = ''; chip(); });
      img.src = abs;
      wrap.appendChild(img);
    } else {
      chip();
    }
    return wrap;
  }

  // 工具结果里的附件 → media part。与 web / 服务端 _collect_attachment_media 同形：
  // 结果自身是附件（kind=url|attachment，旧 show_file 形状）或带 media 列表
  // （ask_user 形状）都收，按 attachment_id / url 去重。
  function mediaPartFromShape(item) {
    if (!item || typeof item !== 'object') return null;
    const kind = item.kind;
    let src = '';
    if (kind === 'url') src = String(item.url || '');
    else if (kind === 'attachment') src = absolutizeMediaUrl(String(item.content_url || item.url || ''));
    else if (item.type === 'attachment') src = absolutizeMediaUrl(String(item.content_url || item.url || ''));
    else return null;
    const name = String(item.name || '');
    const mime = String(item.mime_type || '');
    if (!src && !name) return null;
    const key = item.id || item.attachment_id || src || name;
    return { part: { type: 'media', media: { src, name, mime } }, key: String(key) };
  }

  function collectToolResultMedia(data, assistant) {
    if (!data || typeof data !== 'object') return;
    if (!Array.isArray(assistant.parts)) assistant.parts = [];
    const candidates = [mediaPartFromShape(data)];
    if (Array.isArray(data.media)) for (const item of data.media) candidates.push(mediaPartFromShape(item));
    for (const c of candidates) {
      if (!c) continue;
      const dup = assistant.parts.some((p) => p.type === 'media' && p.media
        && String(p.media.name || '') === c.part.media.name && String(p.media.src || '') === c.part.media.src);
      if (!dup) assistant.parts.push(c.part);
    }
  }

  // 子 Agent 事件归并：start 建块、end 置终态、content/tool_* 归到对应 id 的块。
  // 与 web 端 agent-stream-events 的 subagent_* 语义一致（仅一级，不嵌套）。
  function handleSubagentEvent(ev, assistant) {
    if (!Array.isArray(assistant.subagents)) assistant.subagents = [];
    const sid = String(ev.subagent_id || 'subagent');
    if (ev.type === 'subagent_start') {
      const sub = {
        id: sid,
        name: String(ev.subagent_name || '子 Agent'),
        task: String(ev.task || ''),
        status: 'running',
        content: '',
        reasoning: '',
        toolCalls: [],
      };
      assistant.subagents.push(sub);
      // 卡片统一渲染在气泡顶部（renderMessages 直接读 m.subagents），不再往
      // parts 里塞 inline 项——实时与历史回放走同一条渲染路径。
      renderMessages();
      return;
    }
    const sub = assistant.subagents.find((s) => s.id === sid);
    if (!sub) return;
    if (ev.type === 'subagent_end') {
      sub.status = 'done';
      if (ev.summary != null) sub.summary = String(ev.summary);
    } else if (ev.type === 'subagent_error') {
      sub.status = 'done';
      sub.error = String(ev.message || '子 agent 出错');
    } else if (ev.type === 'subagent_done' && ev.usage && typeof ev.usage === 'object') {
      // 子 agent 每轮 done 都带 usage；取最后一个（末轮上下文占用），与主对话口径一致。
      sub.usage = ev.usage;
    } else if (ev.type === 'subagent_content') {
      sub.content = (sub.content || '') + String(ev.text || '');
    } else if (ev.type === 'subagent_reasoning') {
      // 子 agent 的思考：与主消息 reasoning 同待遇——独立累积、随卡片落库、
      // 刷新后历史从 subagents[].reasoning 恢复（原先只实时展示、不落库，刷新即丢）。
      // 段 DOM 还挂着就直写增量，避免高频 delta 整棵重建把展开态折叠回去。
      sub.reasoning = (sub.reasoning || '') + String(ev.text || '');
      if (sub._reasonEl && sub._reasonEl.isConnected) {
        sub._reasonEl.textContent = sub.reasoning;
      }
    } else if (ev.type === 'subagent_tool_call') {
      sub.toolCalls.push({
        name: String(ev.name || 'unknown'),
        args: typeof ev.args === 'string' ? ev.args : JSON.stringify(ev.args || {}),
        status: 'running',
      });
    } else if (ev.type === 'subagent_tool_result') {
      // 按 name 回填最近一个同名 running 调用（与 web 同口径）。
      for (let i = sub.toolCalls.length - 1; i >= 0; i -= 1) {
        const tc = sub.toolCalls[i];
        if (tc.name === ev.name && tc.status === 'running') { tc.status = 'done'; tc.result = ev.data; break; }
      }
    }
  }

  // 子 Agent 卡片：主色描边，头 = 名称 + 任务摘要 + 状态；展开看子 agent 正文
  // 与内嵌工具卡（与 web SubagentBlock 同形）。折叠态记在 sub._open 上。
  function renderSubagentCard(sub) {
    if (sub._open === undefined) sub._open = sub.status === 'running';
    const card = document.createElement('div');
    card.className = 'subagent-card' + (sub._open ? ' open' : '');
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'sa-head';
    head.innerHTML =
      '<span class="sa-icon">' + els.ICON.bot + '</span>' +
      '<span class="sa-name"></span>' +
      '<span class="sa-task"></span>' +
      '<span class="sa-state ' + (sub.status || 'running') + '">' + (sub.status === 'running' ? '运行中' : '完成') + '</span>' +
      '<span class="sa-caret">' + els.ICON.chevron + '</span>';
    head.querySelector('.sa-name').textContent = sub.name || '子 Agent';
    head.querySelector('.sa-task').textContent = sub.task || '';
    head.querySelector('.sa-task').title = sub.task || '';
    head.addEventListener('click', () => { sub._open = !sub._open; card.classList.toggle('open', sub._open); });
    card.appendChild(head);

    const body = document.createElement('div');
    body.className = 'sa-body';
    // 子 agent 的思考渲染在正文之前（与 web SubagentBlock 同位序）：思考块复用
    // renderReasoningBlock（_reasonEl 记在 sub 上，subagent_reasoning 增量直写）。
    // 刷新后历史从 subagents[].reasoning 恢复，实时==刷新天然成立。
    if (sub.reasoning) {
      body.appendChild(renderReasoningBlock(sub));
    }
    if (sub.content) {
      const t = document.createElement('div');
      t.className = 'sa-text';
      t.textContent = sub.content;
      body.appendChild(t);
    }
    for (const tc of (sub.toolCalls || [])) body.appendChild(renderToolCard(tc));
    if (sub.summary) {
      const s = document.createElement('div');
      s.className = 'sa-summary';
      s.textContent = '小结：' + sub.summary;
      body.appendChild(s);
    }
    if (sub.error) {
      const e = document.createElement('div');
      e.className = 'sa-error';
      e.textContent = sub.error;
      body.appendChild(e);
    }
    card.appendChild(body);
    return card;
  }

  // ask_user 多题表单：tabs 卡片，每题一个 tab（题号 + 已答圆点），一次只看一题。
  // 一切以「勾选」为准：自定义文本输入后仍不算作答，须再勾选「其他」才计入该题
  // 答案并随提交发出；单选题里选项与「其他」勾选互斥。
  // 提交时把各题答案按「题干：答案」逐行拼成一条文本，作为下一条消息发出（协议 A）。
  // questions 项形状：{question, options:[{label,description?}], multiSelect, allowCustom, recommended, reason}。
  function renderQuestionForm(questions) {
    const wrap = document.createElement('div');
    wrap.className = 'q-form';
    const sel = new Map();          // qi -> Set<已勾选 label>：勾选是唯一事实源
    const customOn = new Map();     // qi -> 自定义答案是否已勾选
    const customText = new Map();   // qi -> 自定义文本（仅勾选后生效）
    const blocks = [];
    const tabs = [];
    const optBtns = [];
    const customInputs = [];
    let submitted = false;
    let cur = 0;

    const answered = (qi) => {
      const s = sel.get(qi);
      if (s && s.size > 0) return true;
      return !!(customOn.get(qi) && (customText.get(qi) || '').trim());
    };

    // --- tabs 行（单题时整行隐藏，直接展示题目） ---
    const tabBar = document.createElement('div');
    tabBar.className = 'q-tabs';
    if (questions.length <= 1) tabBar.style.display = 'none';
    wrap.appendChild(tabBar);

    let navPrev = null, navNext = null;
    const show = (qi) => {
      cur = qi;
      blocks.forEach((b, i) => { b.style.display = (i === qi) ? '' : 'none'; });
      tabs.forEach((t, i) => t.classList.toggle('active', i === qi));
      refreshNav(); // 提交后仍可翻看（各题控件已禁用，只读）
    };
    const refreshNav = () => {
      if (navPrev) navPrev.disabled = submitted || cur === 0;
      if (navNext) navNext.disabled = submitted || cur === questions.length - 1;
    };

    questions.forEach((q, qi) => {
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'q-tab';
      const dot = document.createElement('i');
      dot.className = 'qt-dot';
      tab.appendChild(dot);
      const num = document.createElement('span');
      num.textContent = String(qi + 1);
      tab.appendChild(num);
      tab.title = q.question || '';
      tab.addEventListener('click', () => show(qi));
      tabs.push(tab);
      tabBar.appendChild(tab);

      const block = document.createElement('div');
      block.className = 'q-block';
      if (qi !== 0) block.style.display = 'none';
      blocks.push(block);

      const title = document.createElement('div');
      title.className = 'q-title';
      title.textContent = q.question || '';
      block.appendChild(title);

      const chips = document.createElement('div');
      chips.className = 'q-cands';
      const pick = (label) => {
        const s = sel.get(qi) || new Set();
        if (q.multiSelect) {
          if (s.has(label)) s.delete(label); else s.add(label);
        } else {
          s.clear(); s.add(label);
          // 单选语义：换选项即收回自定义勾选，一题同时只有一个答案生效。
          if (customOn.get(qi)) { customOn.set(qi, false); block._syncCustom && block._syncCustom(); }
        }
        sel.set(qi, s);
        for (const el of chips.children) el.classList.toggle('active', s.has(el.dataset.label));
        refresh();
      };
      const curBtns = [];
      (q.options || []).forEach((opt) => {
        const label = typeof opt === 'string' ? opt : String(opt.label || '');
        if (!label) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'q-cand';
        btn.dataset.label = label;
        btn.textContent = label + (q.recommended === label ? '（推荐）' : '');
        if (opt && opt.description) btn.title = opt.description;
        btn.addEventListener('click', () => { if (!submitted) pick(label); });
        chips.appendChild(btn);
        curBtns.push(btn);
      });
      optBtns.push(curBtns);
      block.appendChild(chips);

      if ((q.options || []).some((o) => o && o.description)) {
        const dl = document.createElement('div');
        dl.className = 'q-opt-desc';
        for (const o of q.options) {
          if (o && o.description) {
            const line = document.createElement('div');
            line.textContent = '· ' + (o.label || '') + '：' + o.description;
            dl.appendChild(line);
          }
        }
        block.appendChild(dl);
      }
      if (q.recommended && q.reason) {
        const rr = document.createElement('div');
        rr.className = 'q-opt-desc';
        rr.textContent = '推荐「' + q.recommended + '」：' + q.reason;
        block.appendChild(rr);
      }

      if (q.allowCustom !== false) {
        const row = document.createElement('div');
        row.className = 'q-custom-row';
        // 自定义答案也要勾选：输入本身不算作答，勾了「其他」这条才会提交。
        const cchk = document.createElement('button');
        cchk.type = 'button';
        cchk.className = 'q-cand';
        cchk.textContent = '其他';
        cchk.title = '勾选后自定义答案才会提交';
        const ci = document.createElement('input');
        ci.type = 'text';
        ci.className = 'q-custom';
        ci.placeholder = '输入自定义答案，勾选「其他」后才会提交';
        const note = document.createElement('div');
        note.className = 'q-hint';
        const syncCustom = () => {
          const on = !!customOn.get(qi);
          cchk.classList.toggle('active', on);
          ci.classList.toggle('dim', !on);
          note.textContent = (!on && (customText.get(qi) || '').trim())
            ? '已输入但未勾选，提交时不会包含这条答案'
            : '';
        };
        cchk.addEventListener('click', () => {
          if (submitted) return;
          if (customOn.get(qi)) {
            customOn.set(qi, false);
          } else {
            customOn.set(qi, true);
            // 单选题勾选「其他」= 收回选项勾选（勾选互斥）。
            if (!q.multiSelect) {
              sel.set(qi, new Set());
              for (const el of chips.children) el.classList.remove('active');
            }
          }
          syncCustom();
          refresh();
        });
        ci.addEventListener('input', () => {
          customText.set(qi, ci.value.trim());
          syncCustom();
          refresh();
        });
        row.appendChild(cchk);
        row.appendChild(ci);
        block.appendChild(row);
        block.appendChild(note);
        customInputs.push(ci);
        block._syncCustom = syncCustom;
      }
      wrap.appendChild(block);
    });

    // --- 底部：进度 + 上一题/下一题 + 提交 ---
    const foot = document.createElement('div');
    foot.className = 'q-foot';
    const prog = document.createElement('span');
    prog.className = 'q-progress';
    foot.appendChild(prog);
    const multi = questions.length > 1;
    if (multi) {
      navPrev = document.createElement('button');
      navPrev.type = 'button';
      navPrev.className = 'q-nav';
      navPrev.textContent = '上一题';
      navPrev.addEventListener('click', () => { if (cur > 0) show(cur - 1); });
      navNext = document.createElement('button');
      navNext.type = 'button';
      navNext.className = 'q-nav';
      navNext.textContent = '下一题';
      navNext.addEventListener('click', () => { if (cur < questions.length - 1) show(cur + 1); });
      foot.appendChild(navPrev);
      foot.appendChild(navNext);
    }
    const submit = document.createElement('button');
    submit.type = 'button';
    submit.className = 'q-submit';
    submit.textContent = '提交';
    foot.appendChild(submit);
    wrap.appendChild(foot);

    function refresh() {
      const done = questions.reduce((n, _, qi) => n + (answered(qi) ? 1 : 0), 0);
      prog.textContent = multi ? ('已答 ' + done + '/' + questions.length) : '';
      tabs.forEach((t, i) => t.classList.toggle('answered', answered(i)));
      const can = questions.every((_, qi) => answered(qi));
      submit.disabled = submitted || !can;
      submit.title = (submitted || can) ? '' : '还有 ' + (questions.length - done) + ' 题未勾选答案';
      refreshNav();
    }
    submit.addEventListener('click', () => {
      if (submitted || submit.disabled || state.sending) return;
      const lines = questions.map((q, qi) => {
        const picked = [...(sel.get(qi) || [])];
        if (q.allowCustom !== false && customOn.get(qi) && (customText.get(qi) || '').trim()) {
          picked.push(customText.get(qi).trim());
        }
        return (q.question || '') + '：' + picked.join('、');
      });
      submitted = true;
      submit.disabled = true;
      submit.textContent = '已提交';
      submit.title = '';
      prog.textContent = '';
      for (const btns of optBtns) for (const b of btns) b.disabled = true;
      for (const ci of customInputs) ci.disabled = true;
      // 拼成一条用户消息直接发出（与 onSend 同路径：值守/选择守卫一致）。
      els.input.value = lines.join('\n');
      onSend();
    });
    refresh();
    return wrap;
  }

  function resolveApproval(approval, result) {
    approval.state = 'resolving';
    renderMessages();
    sendChat('chat_resolve_approval', {
      conversationId: state.conversationId,
      confirmationId: approval.confirmationId,
      result,
      commandHash: approval.commandHash,
    }, () => {}).then((resp) => {
      // 后端 ok=true 且 result=allow → 已允许；result=deny → 已拒绝；
      // ok=false（已被处理/过期/哈希不符）→ 已过期，本轮 agent 自己会因 future 拿不到 allow 收尾。
      if (resp && resp.ok && resp.result === 'allow') {
        approval.state = 'allowed';
      } else if (resp && resp.ok && resp.result === 'deny') {
        approval.state = 'denied';
      } else {
        approval.state = 'expired';
      }
      renderMessages();
    });
  }

  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  // Agent meta-prompt 要求每轮回复带 <summary>...</summary> 作为内部 turn-history 锚点
  // （meta_memory.py Output Discipline / agent_loop.py [TIPS] 提示），<thinking> 是思考
  // 脚手架。这些是协议/历史用途，不该以字面标签露出。web 端（ReactMarkdown + rehype-raw
  // + rehype-sanitize）会把 <summary>/<thinking> 当 HTML 元素解析掉：标签不可见、内部文字
  // 保留成纯文本。CDP 面板流式路径是 textContent（不解析 HTML），标签原样露出就成了
  // 「不兼容」的字面量。这里对齐 web：只剥标签、保留内容，且对流式中尚未闭合的
  // <summary>（closing 未到）也成立——逐个剥开/闭标签而非整块正则，避免中间态漏网。
  function stripAgentMetaTags(text) {
    return String(text || '')
      .replace(/<\/?summary>/gi, '')
      .replace(/<\/?thinking>/gi, '');
  }

  // Agent 回复按 markdown 渲染。marked + DOMPurify（agent 可能回显页面/用户内容，
  // 必须做 XSS 兜底）。流式中不调用本函数（仍 textContent），只在 done/历史回放时用。
  // marked/purify 在 manifest content_scripts 里于 content.js 之前加载；若因 CSP/加载
  // 失败缺失，回退成转义纯文本，绝不让原始 markdown 直接进 innerHTML。
  function renderMarkdown(text) {
    const src = stripAgentMetaTags(text);
    try {
      const raw = marked.parse(src, { breaks: true, gfm: true });
      if (typeof DOMPurify !== 'undefined') {
        return DOMPurify.sanitize(raw, {
          ALLOWED_TAGS: ['p','br','strong','em','del','code','pre','a','ul','ol','li','blockquote','h1','h2','h3','h4','h5','h6','hr','span','table','thead','tbody','tr','th','td','img','sup','sub','s'],
          ALLOWED_ATTR: ['href','title','src','alt','target','rel'],
        });
      }
      // DOMPurify 缺失：不能直接信 marked 输出，回退转义。安全优先于格式。
      return escapeHtml(src).replace(/\n/g, '<br>');
    } catch (_) {
      return escapeHtml(src).replace(/\n/g, '<br>');
    }
  }

  // 主链路重试事件 → 面板里显示的一行提示。网关内部选路重试（inner_retry /
  // candidate_switch / group_*）已在服务端彻底切断，不再冒泡到对话层——这里只
  // 处理 agent 自己的重试（scope=stream 的 429 退避 / scope=network 的网络重试，
  // 都带 delay_ms）。网关重试静默，详情去 RequestLogs 查。
  function retryHintText(ev) {
    const reason = ev.reason || '';
    const at = (ev.attempt && ev.max) ? `（第${ev.attempt}/${ev.max}次）` : '';
    if (ev.scope === 'network') return `网络重试：${reason}`;
    if (ev.scope === 'stream') return `上游异常重试${at}：${reason}`.trim();
    return `重试中${at}：${reason}`.trim();
  }

  // 把一次 agent 事件流应用到给定 assistant 消息对象上（send 与 retry 共用）。
  function finalizeAssistant(assistant, status) {
    // 流程结束兜底：任一 tool_call 因 id/index 匹配失败仍停在 running 的，
    // 统一收尾成最终态，避免「工具其实执行完了但卡片一直转圈」。
    if (Array.isArray(assistant.toolCalls)) {
      for (const c of assistant.toolCalls) {
        if (c.status === 'running') c.status = status === 'error' ? 'error' : 'done';
      }
    }
    // 审批卡同理：流结束了还停在 pending/resolving 的，说明没人裁决（超时/中止），
    // 翻成过期态，避免永久停在可操作按钮上。
    if (Array.isArray(assistant.parts)) {
      for (const p of assistant.parts) {
        if (p.type === 'approval' && p.approval && (p.approval.state === 'pending' || p.approval.state === 'resolving')) {
          p.approval.state = 'expired';
        }
      }
    }
    assistant.retryHint = null;
  }

  function makeStreamHandler(assistant) {
    return (ev) => {
      if (ev.type === 'conversation') {
        state.conversationId = ev.conversation_id || state.conversationId;
        writePanelState({ conversationId: state.conversationId, agentId: state.selectedAgent });
        return;
      }
      if (ev.type === 'reset') {
        // 服务端 conversation_hub 在丢帧后补发的恢复信号：本地已缺一段，
        // 仅靠后续增量对不齐（offset 缺口会触发整页重拉）。直接重拉权威快照补全，
        // 这是唯一能在流式中途把缺口补上的路径。
        void recoverFromGap();
        return;
      }
      if (ev.type === 'content') {
        const txt = ev.text || '';
        // 快照-增量对齐：快照里的 collected_content 永远是权威前缀。晚加入的观察者
        // 载入历史后可能收到中途重叠的增量，offset < 当前长度 = 已含 → 跳过；
        // 缺口（>）→ 错过中间段，整页重拉一次对齐。
        if (ev.offset !== undefined) {
          const cur = (assistant.content || '').length;
          if (ev.offset < cur) return;
          if (ev.offset > cur) { void recoverFromGap(); return; }
        }
        assistant.content = (assistant.content || '') + txt;
        const last = assistant.parts[assistant.parts.length - 1];
        if (last && last.type === 'text') {
          last.text += txt;
          // 增量：流式中只改当前气泡末段 text 的 textContent，不整棵重建，
          // 避免每次 delta 把已展开的工具卡折叠回去 + 高频闪烁。
          if (assistant._lastTextEl && assistant._lastTextEl.isConnected) {
            assistant._lastTextEl.textContent = stripAgentMetaTags(last.text);
            stickScrollToBottom();
            return;
          }
        } else {
          assistant.parts.push({ type: 'text', text: txt });
        }
        renderMessages();
      } else if (ev.type === 'reasoning') {
        // 思考增量：独立累积到 assistant.reasoning，绝不并入 content（不回灌模型）。
        // offset 判重与 content 同型——快照里的 reasoning 是权威前缀。
        const rtxt = ev.text || '';
        if (ev.offset !== undefined) {
          const cur = (assistant.reasoning || '').length;
          if (ev.offset < cur) return;
          if (ev.offset > cur) { void recoverFromGap(); return; }
        }
        assistant.reasoning = (assistant.reasoning || '') + rtxt;
        // 与正文同型的时间线交织：连续思考续写当前 reasoning 段，遇到正文/工具
        // （tool_call/content 分支会 push 新 part）自然断段，下一段思考另起一块。
        const rlast = assistant.parts[assistant.parts.length - 1];
        if (rlast && rlast.type === 'reasoning') {
          rlast.text += rtxt;
          // 段 DOM 还挂着就直写增量，不整棵重建（避免高频闪烁 + 折叠态丢失）。
          if (rlast._reasonEl && rlast._reasonEl.isConnected) {
            rlast._reasonEl.textContent = rlast.text;
            stickScrollToBottom();
            return;
          }
        } else {
          assistant.parts.push({ type: 'reasoning', text: rtxt });
        }
        renderMessages();
      } else if (ev.type === 'tool_call') {
        // id = agent_loop 的 tool_call_id；tool_result 回帧带同一个 id，跨轮用它精确匹配。
        const call = { id: ev.id, name: ev.name, args: typeof ev.args === 'string' ? ev.args : JSON.stringify(ev.args), status: 'running', result: undefined, index: ev.index };
        assistant.toolCalls.push(call);
        // ask_user 不渲染成工具卡：它的输出（问题正文/候选/附件）由紧随其后的
        // question 事件以「向用户提问」的形态呈现，再挂一张 ask_user JSON 工具卡
        // 只会把同一个问题重复显示一遍。仍进 toolCalls 供 tool_result 按 id 配对。
        if (call.name !== 'ask_user') assistant.parts.push({ type: 'tool', call });
        renderMessages();
      } else if (ev.type === 'tool_result') {
        // agent_loop 的 index 是「本轮第几个 tool_call」，跨轮会重置成 0，不能当
        // 全局下标用。优先按 id 匹配（同一 tool_call_id 在 call/result 上一致），
        // 匹配不到再回退 index / 末位。
        let call = null;
        if (ev.id != null) {
          const resultId = String(ev.id);
          call = assistant.toolCalls.find((c) => c.id != null && String(c.id) === resultId) || null;
        }
        if (!call && typeof ev.index === 'number') {
          // index 在每轮内重置；优先找同 index 的仍在执行调用，避免把结果
          // 错配给前一轮已经完成的同下标工具。
          call = assistant.toolCalls.find((c) => c.status === 'running' && c.index === ev.index) || null;
        }
        if (!call) {
          call = assistant.toolCalls.find((c) => c.status === 'running') || null;
        }
        if (call) { call.status = 'done'; call.result = ev.data; }
        // show_file 工具的附件/图片：与 ask_user 的 media 同形（kind=url|attachment），
        // 抽成 media part 内联渲染（与 web MediaPartView 同口径）。与工具名无关：
        // 任何工具结果带附件形状都收（见 _collect_attachment_media）。
        collectToolResultMedia(ev.data, assistant);
        renderMessages();
      } else if (ev.type === 'subagent_start' || ev.type === 'subagent_end'
          || ev.type === 'subagent_error' || ev.type === 'subagent_done'
          || ev.type === 'subagent_content' || ev.type === 'subagent_reasoning'
          || ev.type === 'subagent_tool_call' || ev.type === 'subagent_tool_result') {
        // 子 Agent 事件（agent_main 的 forward 改写为 subagent_* 前缀）。原先面板
        // 完全没有这些分支——子 agent 跑了整整一轮却一个字都不显示。按 web 端
        // agent-stream-events 同一套归并语义：启动建块、结束置终态、正文与工具
        // 归到对应 id 的块里。
        handleSubagentEvent(ev, assistant);
        // subagent_start 分支内部已 renderMessages（卡片在气泡顶部），其余类型
        // 在这里统一重渲染。subagent_reasoning 有 _reasonEl 直写快路径时内部
        // 不会 return，多渲染一次无害（幂等）。
        renderMessages();
      } else if (ev.type === 'question') {
        // ask_user 的提问不是普通工具调用：问题正文 + 候选答案 + 附件都要按
        // 「Agent 向用户提问」呈现，而不是只留一张 ask_user 工具卡（历史缺陷：
        // question 事件被完全忽略，问题正文一个字都不显示）。
        const data = (ev.data && typeof ev.data === 'object') ? ev.data : {};
        // ask_user 多题：questions[]（服务端已归一，旧单题形状会合成单题）。
        // 渲染成表单（选项 + 自定义 + 提交），答案按「题干：答案」拼一条文本发出。
        const rawQs = Array.isArray(data.questions) ? data.questions : [];
        const questions = [];
        for (const item of rawQs) {
          if (!item || typeof item !== 'object' || !String(item.question || '').trim()) continue;
          const entry = { question: String(item.question).trim(), options: [] };
          if (Array.isArray(item.options)) {
            for (const o of item.options) {
              if (typeof o === 'string') { if (o.trim()) entry.options.push({ label: o }); continue; }
              if (o && typeof o === 'object' && String(o.label || '').trim()) {
                const oe = { label: String(o.label) };
                if (o.description) oe.description = String(o.description);
                entry.options.push(oe);
              }
            }
          }
          if (item.multi_select) entry.multiSelect = true;
          if (item.allow_custom === false) entry.allowCustom = false;
          if (item.recommended) entry.recommended = String(item.recommended);
          if (item.reason) entry.reason = String(item.reason);
          questions.push(entry);
        }
        if (!questions.length) {
          // 旧形状兜底：question + candidates → 单题（选项来自 candidates）。
          const legacyQ = String(data.question || ev.message || '').trim();
          const legacyCands = (Array.isArray(data.candidates) ? data.candidates : [])
            .filter((c) => typeof c === 'string' && c.trim())
            .map((c) => ({ label: c }));
          if (legacyQ) questions.push({ question: legacyQ, options: legacyCands });
        }
        // 有题 → 挂表单（表单自带题干文本，不再把 question 并入正文重复显示）；
        // 无题（纯展示附件）→ 回退旧行为，把 message 文本并入正文。
        if (questions.length) {
          assistant.parts.push({ type: 'questions', questions });
        } else {
          const q = String(data.question || ev.message || '');
          if (q) {
            assistant.content = (assistant.content || '') + (assistant.content ? '\n\n' : '') + q;
            const last = assistant.parts[assistant.parts.length - 1];
            if (last && last.type === 'text') last.text += (last.text ? '\n\n' : '') + q;
            else assistant.parts.push({ type: 'text', text: q });
          }
        }
        const media = Array.isArray(data.media) ? data.media : [];
        for (const item of media) {
          if (!item || typeof item !== 'object') continue;
          const src = item.kind === 'url'
            ? String(item.url || '')
            : absolutizeMediaUrl(String(item.content_url || ''));
          assistant.parts.push({
            type: 'media',
            media: { src, name: String(item.name || ''), mime: String(item.mime_type || '') },
          });
        }
        // 问题后不再有正文流式增量：末段 text 直写引用交给本次整树渲染重建。
        assistant._lastTextEl = null;
        renderMessages();
      } else if (ev.type === 'retry') {
        assistant.retryHint = retryHintText(ev);
        renderMessages();
      } else if (ev.type === 'confirmation_required') {
        // 服务端一次性审批：code_run 弹代码、file_write 弹越界写入的文件与正文。
        // 本轮 agent 会 await 裁决，裁决后才继续吐事件，所以卡片必须可操作。
        const cid = ev.confirmation_id;
        if (cid) {
          // 去重：同一 confirmation（如 retry 重发）不重复挂卡。
          if (!assistant.parts.some((p) => p.type === 'approval' && p.approval && p.approval.confirmationId === cid)) {
            const meta = (ev.metadata && typeof ev.metadata === 'object') ? ev.metadata : {};
            const approval = {
              confirmationId: cid,
              commandHash: ev.command_hash || '',
              toolName: ev.tool_name || 'code_run',
              runs: Array.isArray(meta.runs) ? meta.runs : [],
              writes: Array.isArray(meta.writes) ? meta.writes : [],
              message: ev.message || '',
              expiresAt: approvalExpiresAtMs(ev.expires_at),
              state: 'pending', // pending → resolving → allowed / denied / expired
            };
            assistant.parts.push({ type: 'approval', approval });
            renderMessages();
          }
        }
      } else if (ev.type === 'run_state' || ev.type === 'conversation_state') {
        // 会话运行状态：服务端阶段切换（发送/思考/执行/重试/审批/终态）时广播。
        // 更新底栏状态行（含异常/中止/审批失败的重试按钮），不产生消息气泡——
        // 它只是一条「最新状态」，不是对话内容。run_state 为旧事件名，保留兼容。
        showRunState((ev.state || (ev.type === 'run_state' ? ev : null)) || null);
      } else if (ev.type === 'approval_resolved') {
        // 审批在其他端被裁决（或切 auto 批量放行）：按 id 找到这张卡翻状态。
        // 服务端广播给本会话全部订阅者，不能只更新发起裁决的那个标签页。
        const cid = String(ev.confirmation_id || '');
        const result = String(ev.result || '');
        if (cid) {
          let touched = false;
          for (const msg of state.messages) {
            if (msg.role !== 'assistant' || !Array.isArray(msg.parts)) continue;
            for (const p of msg.parts) {
              if (p.type === 'approval' && p.approval && p.approval.confirmationId === cid
                  && (p.approval.state === 'pending' || p.approval.state === 'resolving')) {
                p.approval.state = result === 'allow' ? 'allowed' : result === 'deny' ? 'denied' : 'expired';
                touched = true;
              }
            }
          }
          if (touched) renderMessages();
        }
      } else if (ev.type === 'done') {
        if (ev.usage && typeof ev.usage === 'object') {
          const g = (k1, k2) => Number(ev.usage[k1] ?? ev.usage[k2]) || 0;
          const prompt = g('prompt_tokens', 'input_tokens');
          const completion = g('completion_tokens', 'output_tokens');
          state.lastUsage = {
            prompt, completion,
            total: Number(ev.usage.total_tokens) || (prompt + completion),
            cached: g('cached_tokens', 'cache_read_input_tokens'),
            cacheCreation: g('cache_creation_tokens', 'cache_creation_input_tokens'),
            reasoning: g('reasoning_tokens', 'reasoning_output_tokens'),
          };
          renderUsage();
        }
        finalizeAssistant(assistant, 'done');
        // done 事件带本轮实际生效的模型（agent_loop 在收尾时发 model）。发送时
        // assistant.model 预填的是面板下拉选中值（可能为空=用 agent 默认），这里用
        // 服务端权威值覆盖，气泡头才能显示真正跑的那个模型（与 web 端同口径）。
        if (ev.model) assistant.model = String(ev.model);
        assistant.status = 'done'; setSending(false); renderMessages();
        // 本轮落定：会话 updated_at 已变，抽屉开着就重拉列表，否则会话列表停在旧时间。
        if (state.drawerOpen) void refreshSessions();
      } else if (ev.type === 'error') {
        finalizeAssistant(assistant, 'error');
        assistant.status = 'error';
        // agent_loop 发的是 {type:"error", error:err}，chat_error 转发的是 message。
        // 原先只读 ev.message → loop 侧的错误（空响应超限 / max_tokens 超限）全被
        // 兜底成「出错」，真实原因一个字都看不到。两个字段都读，error 优先。
        const errMsg = String(ev.error || ev.message || '').trim() || '出错';
        // 错误进独立 error 字段，不再追加进正文/parts（与 web 同口径）：正文是模型
        // 真实产出，错误是运行状态，混在一起会让刷新后历史回放把报错当成正文重复显示。
        // 落库的 error 列由 mapLoadedMessages 回填，实时==刷新。
        assistant.error = errMsg;
        setSending(false); renderMessages();
        if (state.drawerOpen) void refreshSessions();
      } else if (ev.type === 'aborted') {
        finalizeAssistant(assistant, 'error');
        assistant.status = 'error';
        assistant.error = '已中止';
        setSending(false); renderMessages();
        if (state.drawerOpen) void refreshSessions();
      }
    };
  }

  // ── 技能（/ 斜杠命令）弹层 ──────────────────────────────────────
  // 任意位置行内打 / 触发；token 精确/前缀命中技能目录才显示列表。选中插入
  // "/名字 " 文本，服务端从消息文本识别标记（见 agent/skill_slash.py）。
  const skillCache = { agentId: null, items: [] };
  let skillIdx = 0;          // 高亮行
  let skillTok = null;       // { start, end, query } 光标前正在输入的 token
  let skillComposing = false; // IME 组合期不触发
  let skillSeq = 0;          // 异步取目录的竞态护栏

  async function skillEnsureCatalog() {
    if (!state.selectedAgent) return;
    if (skillCache.agentId === state.selectedAgent && skillCache.items.length) return;
    try {
      const res = await sendChat('chat_list_skills', { agentId: state.selectedAgent }, () => {});
      if (Array.isArray(res)) { skillCache.items = res; skillCache.agentId = state.selectedAgent; }
    } catch (e) { /* 目录拉取失败不阻断输入，下次按键重试 */ }
  }

  function skillTokenAt() {
    const el = els.input;
    const pos = (typeof el.selectionStart === 'number') ? el.selectionStart : el.value.length;
    if (el.selectionEnd !== pos) return null; // 有选区不触发
    const before = el.value.slice(0, pos);
    // 边界（行首或空白/开括号引号）+ 斜杠 + 非空白非斜杠片段；URL 的 "//"、路径二级段不匹配。
    const m = before.match(/(^|[\s（(\[【"'])\/([^\s/（(\[【"']*)$/);
    if (!m) return null;
    const tok = m[0].slice(m[1].length);
    return { start: pos - tok.length, end: pos, query: tok.slice(1) };
  }

  function skillFilter(query) {
    const q = String(query || '').toLowerCase();
    const items = skillCache.items || [];
    if (!q) return items.slice();
    const prefix = [], sub = [];
    for (const it of items) {
      const n = String(it.name || '').toLowerCase();
      const d = String(it.display_name || '').toLowerCase();
      if (n.startsWith(q) || d.startsWith(q)) prefix.push(it);
      else if (n.includes(q) || d.includes(q)) sub.push(it);
    }
    return prefix.concat(sub);
  }

  function skillPopOpen() { return !!els.skillPop && els.skillPop.classList.contains('open'); }

  function skillRender() {
    if (!els.skillPop) return;
    if (!skillTok) { els.skillPop.classList.remove('open'); els.skillPop.innerHTML = ''; return; }
    const list = skillFilter(skillTok.query);
    els.skillPop.innerHTML = '';
    if (!list.length) {
      const empty = document.createElement('div');
      empty.className = 'sp-empty';
      empty.textContent = '无匹配技能';
      els.skillPop.appendChild(empty);
      els.skillPop.classList.add('open');
      return;
    }
    if (skillIdx >= list.length) skillIdx = 0;
    list.forEach((it, i) => {
      const row = document.createElement('div');
      row.className = 'sp-item' + (i === skillIdx ? ' active' : '');
      row.dataset.name = it.name || '';
      row.innerHTML = '<span class="sp-slash">/</span><span class="sp-name"></span><span class="sp-desc"></span><span class="sp-kind"></span>';
      row.querySelector('.sp-name').textContent = it.display_name || it.name || '';
      row.querySelector('.sp-desc').textContent = it.description || '';
      row.querySelector('.sp-kind').textContent = it.kind === 'mcp' ? 'MCP' : 'SOP';
      els.skillPop.appendChild(row);
    });
    els.skillPop.classList.add('open');
  }

  async function onSkillInput() {
    if (skillComposing) return;
    const seq = ++skillSeq;
    skillTok = skillTokenAt();
    skillIdx = 0;
    if (!skillTok) { skillRender(); return; }
    if (skillCache.agentId !== state.selectedAgent || !skillCache.items.length) {
      await skillEnsureCatalog();
      if (seq !== skillSeq) return; // 已被后续按键取代
      skillTok = skillTokenAt();
      if (!skillTok) { skillRender(); return; }
    }
    skillRender();
  }

  function skillMove(delta) {
    if (!skillTok) return;
    const list = skillFilter(skillTok.query);
    if (!list.length) return;
    skillIdx = (skillIdx + delta + list.length) % list.length;
    skillRender();
  }

  function skillPick(name) {
    if (!name || !skillTok || !els.input) return;
    const el = els.input;
    const insert = '/' + name + ' ';
    el.value = el.value.slice(0, skillTok.start) + insert + el.value.slice(skillTok.end);
    const caret = skillTok.start + insert.length;
    try { el.setSelectionRange(caret, caret); } catch (e) { /* ignore */ }
    skillTok = null;
    skillRender();
    autoGrowSafe();
    el.focus();
  }

  function skillPickActive() {
    if (!skillTok) return false;
    const list = skillFilter(skillTok.query);
    const it = list[skillIdx];
    if (!it) return false;
    skillPick(it.name);
    return true;
  }

  function skillClose() { skillTok = null; skillRender(); }

  function autoGrowSafe() {
    const t = els.input; if (!t) return;
    t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 150) + 'px';
  }

  // ── 附件上传（与用户端 MessageComposer 同款流程）────────────────────
  // 选中文件 → 读成 base64 → 经已鉴权 WS 通道上传到该会话工作区 → 返回相对路径。
  // 路径随消息正文一起发出（见 onSend），Agent 用 file 工具读——与 web 完全同口径。
  const ATTACH_MAX_FILES = 10;
  const ATTACH_MAX_SIZE = 10 * 1024 * 1024;
  const attachItems = []; // {id, name, size, status:'uploading'|'uploaded'|'error', progress, path, error, file}

  function renderAttachStrip() {
    const strip = els.attachStrip;
    if (!strip) return;
    if (!attachItems.length) { strip.classList.remove('show'); strip.innerHTML = ''; return; }
    strip.classList.add('show');
    strip.innerHTML = '';
    for (const item of attachItems) {
      const chip = document.createElement('div');
      chip.className = 'attach-chip' + (item.status === 'error' ? ' err' : '');
      const name = document.createElement('span');
      name.className = 'ac-name';
      name.textContent = item.name;
      name.title = item.error || item.name;
      chip.appendChild(name);
      if (item.status === 'uploading') {
        const meta = document.createElement('span');
        meta.className = 'ac-meta';
        meta.textContent = (item.progress || 0) + '%';
        chip.appendChild(meta);
      } else if (item.status === 'error') {
        const retry = document.createElement('button');
        retry.type = 'button'; retry.className = 'ac-retry'; retry.title = '重试上传';
        retry.innerHTML = els.ICON.rotate;
        retry.addEventListener('click', () => { void runAttachUpload(item); });
        chip.appendChild(retry);
      }
      const x = document.createElement('button');
      x.type = 'button'; x.className = 'ac-x'; x.title = '移除附件';
      x.innerHTML = els.ICON.close12;
      x.addEventListener('click', () => {
        const i = attachItems.indexOf(item);
        if (i >= 0) attachItems.splice(i, 1);
        renderAttachStrip(); syncAttachButton();
      });
      chip.appendChild(x);
      strip.appendChild(chip);
      if (item.status === 'uploading') {
        const bar = document.createElement('div');
        bar.className = 'ac-bar';
        const fill = document.createElement('i');
        fill.style.width = (item.progress || 0) + '%';
        bar.appendChild(fill);
        strip.appendChild(bar);
      }
    }
  }

  function syncAttachButton() {
    if (!els.attachBtn) return;
    const full = attachItems.length >= ATTACH_MAX_FILES;
    els.attachBtn.disabled = state.sending || full || !state.selectedAgent;
    els.attachBtn.title = !state.selectedAgent ? '请先选择 Agent'
      : full ? `最多 ${ATTACH_MAX_FILES} 个附件` : '添加文件';
  }

  function onAttachPicked(e) {
    const files = Array.from((e.target && e.target.files) || []);
    if (e.target) e.target.value = '';
    if (!files.length) return;
    const slots = Math.max(0, ATTACH_MAX_FILES - attachItems.length);
    const accepted = files.slice(0, slots);
    const tooLarge = accepted.filter((f) => f.size > ATTACH_MAX_SIZE);
    const ok = accepted.filter((f) => f.size <= ATTACH_MAX_SIZE);
    if (files.length > slots) els.hint.textContent = `最多保留 ${ATTACH_MAX_FILES} 个附件`;
    if (tooLarge.length) els.hint.textContent = `${tooLarge.map((f) => f.name).join('、')} 超过单文件 10 MiB 限制`;
    for (const file of ok) {
      const item = { id: 'a' + Date.now() + '_' + Math.random().toString(36).slice(2, 6), name: file.name, size: file.size, status: 'uploading', progress: 0, path: '', file };
      attachItems.push(item);
      void runAttachUpload(item);
    }
    renderAttachStrip();
    syncAttachButton();
  }

  function readFileBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || '');
        const comma = result.indexOf(',');
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = () => reject(reader.error || new Error('读取文件失败'));
      reader.readAsDataURL(file);
    });
  }

  async function runAttachUpload(item) {
    item.status = 'uploading';
    item.progress = 0;
    item.error = '';
    renderAttachStrip();
    syncAttachButton();
    // 上传要落到会话工作区：没有会话先建一个（与 web 的 ensureConversation 同义）。
    if (!state.conversationId) {
      if (!state.selectedAgent) { item.status = 'error'; item.error = '请先选择 Agent'; renderAttachStrip(); syncAttachButton(); return; }
      const res = await sendChat('chat_new_conversation', { agentId: state.selectedAgent, title: '' }, () => {});
      const conv = res && res.conversation;
      if (conv && conv.id) {
        state.conversationId = conv.id;
        writePanelState({ conversationId: conv.id, agentId: state.selectedAgent });
        setWatchedConversation(state.conversationId);
      } else {
        item.status = 'error'; item.error = '创建会话失败';
        renderAttachStrip(); syncAttachButton();
        return;
      }
    }
    try {
      const b64 = await readFileBase64(item.file);
      item.progress = 50;
      renderAttachStrip();
      const resp = await sendChat('chat_upload_attachment', {
        conversationId: state.conversationId,
        filename: item.name,
        content: b64,
      }, () => {});
      if (resp && resp.path) {
        item.status = 'uploaded';
        item.progress = 100;
        item.path = resp.path;
        item.name = resp.filename || item.name;
      } else {
        item.status = 'error';
        item.error = (resp && resp.message) || '上传失败';
      }
    } catch (err) {
      item.status = 'error';
      item.error = (err && err.message) || '上传失败';
    }
    renderAttachStrip();
    syncAttachButton();
  }

  // 已上传附件的路径列表（拼进消息正文用）；上传中/失败的不算。
  function uploadedAttachPaths() {
    return attachItems.filter((a) => a.status === 'uploaded' && a.path).map((a) => a.path);
  }

  async function onSend() {
    // 按钮已是「停止」（本轮在跑——本地发送中或接管服务端仍在跑的那一轮）：
    // 点按 = 中止服务端 agent 任务，不发新消息。
    if (turnRunning()) { void onAbort(); return; }
    skillClose();
    const text = els.input.value.trim();
    // /compact：手动压缩上下文（不发给模型）。转成 chat_compact 帧，服务端
    // LLM 摘要历史并落 <history_summary> 标记消息，跨轮生效。
    if (text === '/compact') {
      if (state.sending || !state.selectedAgent) return;
      els.input.value = ''; autoGrowSafe();
      void runCompact();
      return;
    }
    if (state.sending || !state.selectedAgent) return;
    // 附件上传未完成：拦下发并明示（与 web canSend 的 hasUnreadyAttachment 同义）。
    const pendingAttach = attachItems.some((a) => a.status === 'uploading');
    if (pendingAttach) { els.hint.textContent = '请等待附件上传完成'; return; }
    const failedAttach = attachItems.some((a) => a.status === 'error');
    if (failedAttach) { els.hint.textContent = '有附件上传失败，请重试或移除'; return; }
    const attachPaths = uploadedAttachPaths();
    // 有附件时允许空正文（只发文件）——与 web 「typed 为空但有附件也能发」同口径。
    if (!text && !attachPaths.length) return;
    // goal 模式必须填目标。（校验放连接等待之前：填错字段要立刻报，不该先白等 20s）
    if (state.mode === 'goal') {
      state.goalObjective = (els.goalObjective && els.goalObjective.value.trim()) || '';
      state.goalBudgetMinutes = Math.max(1, Number(els.goalMinutes && els.goalMinutes.value) || 15);
      if (!state.goalObjective) { els.hint.textContent = '目标模式请填写目标'; return; }
    }
    // 发送前的连接体检。原先这里不看状态：气泡先落成「正在流」、请求发给 background
    // 才被拒——消息根本没发出去，界面却已经演了一出「在跑」，重试也就永远对不上账
    // （面板报失败 / 服务端啥也没有的分裂态就是这么来的）。现在先确认连接：没连上
    // 就等重连（上限 20s，期间每 2s 一查；查询本身会唤醒 SW，触发它重连），期间
    // 一个消息都不落；等不到才明说「消息没发出去」。重试路径本就有同样的体检，
    // 这里补齐首发，两者口径一致。
    if (!(await bridgeConnected())) {
      els.hint.textContent = '未连接，正在重连…';
      const ready = await waitForBridgeReady(20000);
      if (!state.open) return;
      if (!ready) { els.hint.textContent = '连接失败，消息未发出；请确认服务已启动'; return; }
    }
    els.hint.textContent = '';
    // 本地即将接管活跃事件流：停掉重载后可能残留的续看轮询，避免与 live 增量打架。
    stopStreamWatch();
    // 附件路径拼进正文，与 web 的 handleSend 完全同形：Agent 拿到路径后用 file 工具读。
    const content = attachPaths.length
      ? `${text}\n\n[附件（在工作区内，可用文件工具读取）]\n${attachPaths.map((p) => `- ${p}`).join('\n')}`.trim()
      : text;
    const now = new Date().toISOString();
    state.messages.push({ role: 'user', content, createdAt: now });
    // assistant 消息用有序 parts 时间线：text 段与 tool 卡片按事件顺序交错，
    // 而不是「全部工具在前 + 全部正文在后」。这样能看出哪句话对应哪个动作。
    const assistant = { role: 'assistant', content: '', toolCalls: [], parts: [], status: 'streaming', createdAt: new Date().toISOString(), model: (state.selectedModel || '') };
    state.messages.push(assistant);
    els.input.value = ''; els.input.style.height = 'auto'; setSending(true);
    // 发送成功后清空附件条（与 web 提交后 resetAttachments 同义），避免下一条重复带上。
    attachItems.length = 0;
    renderAttachStrip(); syncAttachButton();
    _stickToBottom = true; // 用户刚发消息：回到底部看新消息，无视此前是否往上翻过
    renderMessages();

    dispatchTurn(content, assistant);
  }

  // 发一轮 chat_send。onSend 与「没有会话可重试」时的 onRetry 共用：后者要重发
  // 原来那句 user 文本，参数必须和首发完全一致，否则重试会换掉模型/模式。
  // 发送受理（chat_send_ok）的等待上限：受理是纯落库+起 task，正常亚秒完成。
  // 若桥接半死（chat_send_ok 永不到），sendChat 永不 resolve → state.sending 永远
  // true → 新建会话/切换会话/再发消息全部在守卫里静默卡死（onRetry 的同类坑，
  // 这里同样要兜）。受理帧也是 panel 自己从未见过的帧类型：旧 content.js 配新
  // 服务端时同样等不到——所以无论哪种原因，超时必须能解锁并给出可读错误。
  //
  // 30s（原 10s）：服务端受理要走一次进程内对 agent 端点的派发，事件循环繁忙时
  // 会明显变慢；10s 太紧，容易在服务端其实已受理时先判死，造成「面板报失败、
  // 服务端在跑」的分裂。超时后也不直接判死，而是回拉服务端对账（见
  // reconcileAfterAckTimeout）——受理帧只是「快」，不是「必须」。
  const SEND_ACK_TIMEOUT_MS = 30000;

  function dispatchTurn(text, assistant) {
    // 发送与受理已分离：sendChat 只等 chat_send_ok 这一帧回执；本轮事件由会话泵
    // 按 conversationId 广播，经 _liveSink 喂进同一个 handler（终止帧后自动摘 sink）。
    const handler = makeStreamHandler(assistant);
    const route = (ev) => {
      if (!ev) return;
      handler(ev);
      if (ev.type === 'done' || ev.type === 'error' || ev.type === 'aborted') _liveSink = null;
    };
    _liveSink = route;
    // 受理帧超时未到 ≠ 服务端没受理：受理链路（进程内派发 + 落库 + 起 task）可能
    // 比看门狗慢，服务端其实已经在跑这一轮。超时后不直接判死，而是回拉服务端对账
    // （见 reconcileAfterAckTimeout）：服务端在跑就接管这一轮，确实没跑才报错。
    // 这从根上消掉「面板报失败、服务端在跑」的分裂（用户点重试永远没用）。
    let settled = false;
    const finish = (ev) => {
      if (settled) return;
      settled = true;
      clearTimeout(watchdog);
      route(ev);
    };
    const watchdog = setTimeout(() => {
      if (settled) return;
      settled = true;
      els.hint.textContent = '服务响应慢，正在核对本轮状态…';
      void reconcileAfterAckTimeout(assistant);
    }, SEND_ACK_TIMEOUT_MS);
    void sendChat('chat_send', {
      agentId: state.selectedAgent,
      content: text,
      conversationId: state.conversationId,
      url: location.href,
      title: document.title,
      model: state.selectedModel || '',
      reasoningEffort: state.reasoningEffort,
      mode: state.mode,
      goalObjective: state.goalObjective,
      goalBudgetMinutes: state.goalBudgetMinutes,
      // 面板协议版本：服务端据此判断对端认不认 chat_send_ok（ack 拆分后的新型
      // 回执）。不带它的对端是「发送与订阅分离」之前装的旧 content script——它
      // 只认 chat_event.done/error，收不到 ack 就永远卡在 sending。服务端对它
      // 回一条明确的 chat_error 提示刷新，而不是让连接无声挂住。
      panelV: 2,
    }, (ev) => {
      // 事件既可能是 chat_send_ok（受理回执，sendChat 据此 resolve），
      // 也可能是 chat_error（受理失败，sendChat 会喂 error 再 resolve）——
      // 两者都终止等待。
      //
      // 例外：chat_error 带 code=turn_running（409，上一轮还在跑）不喂给 handler
      // ——error 分支会把气泡翻成失败态并 setSending(false)，而对账需要 sending
      // 仍为 true 才能接管（见 reconcileAfterAckTimeout 的护栏）。.then 里统一接管。
      if (ev && ev.type === 'error' && ev.code === 'turn_running') return;
      finish(ev);
    }).then((ack) => {
      // 受理失败：409 turn_running 不是真失败——服务端已受理并仍在跑这一轮，
      // 回拉对账接管，别把它当错误报给用户（用户看到「正在执行中」却报错会懵）。
      if (ack && ack.type === 'error' && ack.code === 'turn_running') {
        if (settled) return; // 看门狗已先触发过对账，别重复回拉
        settled = true;
        clearTimeout(watchdog);
        els.hint.textContent = '上一轮仍在执行，正在接管…';
        void reconcileAfterAckTimeout(assistant);
        return;
      }
      if (settled && !ack) return;
      settled = true;
      clearTimeout(watchdog);
      // 受理前就失败（无权/桥接断）→ sendChat 已把 error 喂给 route 收尾；
      // 这里不改会话与订阅。
      if (!ack || ack.type === 'error' || ack.conversationId === undefined) return;
      const convId = String(ack.conversationId || state.conversationId || '');
      if (convId) {
        if (convId !== state.conversationId) {
          state.conversationId = convId;
          writePanelState({ conversationId: convId, agentId: state.selectedAgent });
        }
        setWatchedConversation(convId);
      }
    });
  }

  // 受理回执超时/409 后的对账：回拉服务端权威状态，判断本轮到底有没有在跑。
  // - 服务端最后一条 assistant 仍是 streaming → 本轮真的在跑：接管它（重建 sink +
  //   重订阅 + 保持 sending），把「受理超时」变成「正常续看」，用户无感。
  // - 服务端没有在跑的轮次（真失败/桥接半死）→ 才落成可重试的 error。
  // 对账本身也加超时：桥接半死时 sendChat 永不 resolve，不能让这里再次悬挂。
  async function reconcileAfterAckTimeout(assistant) {
    const convAtCheck = state.conversationId;
    // 全新会话（还没有 conversationId）：受理超时时无从回查（ID 本应由 ack 带回），
    // 按本地错误收尾——新建会话偶发超时，用户重发即可。
    if (!convAtCheck) {
      _liveSink = null;
      assistant.status = 'error';
      finalizeAssistant(assistant, 'error');
      assistant.content = (assistant.content || '') + '发送超时（服务未响应），请刷新页面后重试';
      assistant.parts.push({ type: 'text', text: '发送超时（服务未响应），请刷新页面后重试' });
      setSending(false);
      renderMessages();
      els.hint.textContent = '发送超时，请刷新页面后重试';
      return;
    }
    const data = await Promise.race([
      sendChat('chat_load_conversation', { conversationId: convAtCheck }, () => {}),
      new Promise((r) => setTimeout(() => r(null), RETRY_QUERY_TIMEOUT_MS)),
    ]);
    // 对账期间用户关了面板 / 中止了本轮 / 切了会话：交还现场，不再插手。
    if (!state.open || !state.sending || state.conversationId !== convAtCheck) return;
    if (data && data.conversation) {
      replaceMessagesFromPayload(data);
      renderMessages();
    }
    const streaming = lastStreamingAssistant();
    if (data && data.conversation && streaming) {
      // 服务端在跑：接管这一轮。历史整体替换后，本地那条 assistant 已是新对象，
      // 把 sink 接到它上，并重订阅（事件泵绑在重连/重拉的这条链路上）。
      _liveSink = makeStreamHandler(streaming);
      setWatchedConversation(state.conversationId);
      els.hint.textContent = '服务端已受理，正在执行…';
      return;
    }
    // 确认没有在跑的轮次：这才落成可重试的 error。
    _liveSink = null;
    setSending(false);
    if (data && data.conversation) {
      // 已回拉到服务端权威历史：以它为准，不再往已被替换掉的本地对象上写错误正文，
      // 只解锁发送态并提示（服务端那条消息已是终态，回拉已渲染出来）。
      renderMessages();
      els.hint.textContent = '本轮未在服务端执行，请重试';
      return;
    }
    // 回拉也失败（桥接半死）：本地这条仍是流尾对象，就地收尾成可重试错误。
    assistant.status = 'error';
    finalizeAssistant(assistant, 'error');
    assistant.content = (assistant.content || '') + '发送超时（服务未响应），请刷新页面后重试';
    assistant.parts.push({ type: 'text', text: '发送超时（服务未响应），请刷新页面后重试' });
    renderMessages();
    els.hint.textContent = '发送受理超时，请刷新页面后重试';
  }

  // 停止按钮：取消正在跑的一轮。服务端链路是 chat_abort → sse_gateway
  // （取消本 runtime 的转发任务 + 通知主服务取消 run_agent）→ 给原流 reqId 补发
  // chat_aborted。终止帧到达时 makeStreamHandler 的 aborted 分支负责收尾；
  // 这里在 abort 请求回来后给流 3s 收尾窗口，没等到（SSE 已死等）就地解锁兜底，
  // 绝不把面板卡死在 sending。
  //
  // 两种在跑形态都要能停：
  //  - 本地发送中（state.sending）：等终态帧/超时后就地收尾。
  //  - 接管态（state.sending=false，但尾条仍 streaming——刷新/重连后续看）：本轮
  //    事件不挂 reqId，abort 后不会有终态帧回来，靠 refreshCurrentSession 回拉
  //    服务端权威状态（服务端已把该轮落成 error「已中止」），再兜底就地收尾。
  let _stopPending = false;
  async function onAbort() {
    if (_stopPending) return;
    const convAtClick = state.conversationId;
    const takeover = !state.sending && !!lastStreamingAssistant();
    if (!state.sending && !takeover) return;
    if (!convAtClick) { els.hint.textContent = '尚未建立会话，无可停止的任务'; return; }
    _stopPending = true;
    els.hint.textContent = '正在停止…';
    try {
      // sendChat 只在回帧到达时 resolve；桥接半死不活时回帧永远不来，
      // 不加超时这里会无限挂起（sending 卡死、按钮永远是停止态）。2s 后
      // 无论结果如何都进入本地收尾。
      await Promise.race([
        sendChat('chat_abort', { conversationId: convAtClick }, () => {}),
        new Promise((r) => setTimeout(r, 2000)),
      ]).catch(() => { /* 连接可能已断：走下面的兜底解锁 */ });
      if (takeover) {
        // 接管态：回拉服务端权威状态即可，不要本地兜底覆盖——refreshCurrentSession
        // 结束时会重启 watchStreamingIfNeeded 轮询，尾条若仍 streaming 说明服务端
        // 还没收尾（或回拉失败），让轮询/重连自愈接管；本地抢标 error 会被下一拍
        // 轮询又拉回 streaming，反而闪烁。服务端「有流无任务」的孤儿 streaming 由
        // 载入路径的 _reconcile_orphaned_streaming 翻终态。
        await refreshCurrentSession();
      } else {
        const deadline = Date.now() + 3000;
        while (state.sending && Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, 150));
          if (state.conversationId !== convAtClick) break; // 期间切了会话，交还新会话
        }
        // 本地发送中：等了 3s 还没等到终态帧（SSE 已死等）→ 就地收尾，绝不卡死。
        if (state.sending && state.conversationId === convAtClick) {
          const m = state.messages[state.messages.length - 1];
          if (m && m.role === 'assistant' && m.status === 'streaming') {
            finalizeAssistant(m, 'error');
            m.status = 'error';
            // 与 live aborted 分支同口径：错误独立成字段，不污染正文。
            m.error = '已中止';
            renderMessages();
          }
          setSending(false);
        }
      }
    } finally {
      // 任何异常路径都保证解锁，否则按钮永远卡在「停止」且再也点不动。
      _stopPending = false;
      if (els.hint.textContent === '正在停止…') els.hint.textContent = '';
    }
  }

  // 把一条失败的 assistant 消息翻回「正在流」。**不清空已有内容**：重试是续跑
  // （服务端保留该条已完成的工具调用与结果，模型接着中断处做），本地气泡也必须
  // 保留旧正文/工具卡，新事件按序追加在后面；清空会与续跑语义冲突（面板显示从头
  // 再来，实际服务端没重放）。只把结尾的失败态（error 文本/未完成的工具卡）收一下。
  function resetForRetry(message) {
    message.status = 'streaming';
    // 上一次失败追加的 error 正文段与末态标记去掉，避免和新一轮输出混在一起。
    if (Array.isArray(message.parts)) {
      message.parts = message.parts.filter((p) => {
        if (p.type === 'text') return true;
        if (p.type === 'tool' && p.call) return p.call.status !== 'error';
        if (p.type === 'approval' && p.approval) return p.approval.state !== 'expired';
        return true;
      });
      // 去掉结尾那条纯 error 文本（若它就是失败原因），保留真正的正文。
      const last = message.parts[message.parts.length - 1];
      if (last && last.type === 'text' && message.status === 'streaming') {
        // 保守起见不动文本内容——正文可能是模型真实产出，宁多留不误删。
      }
    }
    message._lastTextEl = null;
    setSending(true);
    _stickToBottom = true; // 用户点了重试：回到底部看后续输出
    renderMessages();
  }

  // 重试最后一条失败的 assistant 消息。
  //
  // 三条路径，取决于服务端到底有没有这轮对话：
  // - 有 conversationId → chat_retry，服务端复用同一条消息、用上一条 user 消息重跑。
  // - 没有 → 这轮在建连阶段就失败了（连接超时那类），服务端既没有会话也没有消息行，
  //   chat_retry 无从下手；此时按首发重发原来那句 user 文本。之前这里直接 return，
  //   于是按钮点了没有任何请求发出。
  // - 桥接断开/SW 刚被唤醒：先等重连再继续（waitForBridgeReady），否则这次点击会被
  //   「连接未就绪」吃掉——面板上表现为「点了重试没反应」，只能再点一次碰运气。
  //
  // 重试链路的两个超时。sendChat 只在回帧到达时 resolve，桥接半死时它永久挂起：
  // 一次挂起就把 state.sending 锁在 true，之后点重试被首个守卫静默吃掉，面板表现
  // 成「点了没反应」。查询类请求一次往返就该回来；重试流只卡「首个事件」——一旦有
  // 事件到达就是正常长流，agent 跑多久都行。
  const RETRY_QUERY_TIMEOUT_MS = 8000;
  const RETRY_FIRST_FRAME_TIMEOUT_MS = 45000;
  let _bridgeRetryWaiting = false;

  // 底栏重试按钮入口：取最后一条 assistant 消息交给 onRetry（异常/中止/审批失败态）。
  function onRetryLastAssistant() {
    for (let i = state.messages.length - 1; i >= 0; i -= 1) {
      if (state.messages[i].role === 'assistant') { void onRetry(state.messages[i]); return; }
    }
    els.hint.textContent = '当前会话没有可重试的消息';
  }

  async function onRetry(message) {
    // 每个提前返回都写 hint：这个按钮原先有一批静默 return，用户看到的就是「点了
    // 没反应」，连为什么都不知道。
    if (!message) return;
    if (_bridgeRetryWaiting) { els.hint.textContent = '正在等待重连…'; return; }
    if (state.sending) { els.hint.textContent = '正在执行中，请等本轮结束再重试'; return; }
    // 允许重试的本地态：error 消息，或会话级 run_status 是可重试态（审批失败暂停/已中止）。
    // 后者来自 conversation_state 事件——审批被拒/超时后 assistant 消息可能仍显示 done，
    // 但会话级状态会停在 approval_failed，重试按钮据此可点。
    const convRetryable = state.lastRunState && (state.lastRunState.run_status === 'error'
      || state.lastRunState.run_status === 'aborted' || state.lastRunState.run_status === 'approval_failed');
    if (message.role !== 'assistant' || (message.status !== 'error' && !convRetryable)) {
      els.hint.textContent = '这条消息已不是失败态，无需重试';
      return;
    }
    const convAtClick = state.conversationId;

    // 桥接未连上：等重连后续跑这次重试，等待期间 hint 提示；面板关闭/用户先发了
    // 新消息/切了会话则放弃（waitForBridgeReady 内部按 state.open/sending 判定）。
    // 60s 上限：Chrome ≥120 把 chrome.alarms 最小间隔钳到 30s，SW 挂起后的
    // probe 重连一个周期就要 ~30s，30s 上限会在重连前一瞬超时。
    if (!(await bridgeConnected())) {
      _bridgeRetryWaiting = true;
      els.hint.textContent = '连接断开，等待重连后自动重试…';
      let ready = false;
      try {
        ready = await waitForBridgeReady(60000);
      } finally {
        // try/finally：等待期间任何异常都不能让这个标志永久留真，否则之后每次点击
        // 都在上面的守卫里静默返回，按钮彻底变摆设。
        _bridgeRetryWaiting = false;
      }
      if (!state.open || state.sending || state.conversationId !== convAtClick) { els.hint.textContent = ''; return; }
      if (!ready) {
        els.hint.textContent = '连接未恢复，请稍后再点重试';
        return;
      }
      els.hint.textContent = '';
    }

    if (!state.conversationId) {
      // 原始 user 文本取该 assistant 之前最近的一条 user 消息。
      const at = state.messages.indexOf(message);
      let text = '';
      for (let i = (at < 0 ? state.messages.length : at) - 1; i >= 0; i -= 1) {
        if (state.messages[i].role === 'user') { text = String(state.messages[i].content || '').trim(); break; }
      }
      if (!text || !state.selectedAgent) { els.hint.textContent = '找不到可重试的消息'; return; }
      resetForRetry(message);
      dispatchTurn(text, message);
      return;
    }

    // live 消息是前端对象没有真实 id：回查会话，取后端最后一条 error assistant 的 id。
    let messageId = message.id;
    if (messageId == null) {
      els.hint.textContent = '正在查询可重试的消息…';
      // 回查是「一次往返」请求，加超时：桥接半死时 sendChat 永不 resolve，这个 await
      // 会永久挂起，界面上一点变化都没有（就是「点了没反应」）。null = 超时。
      const data = await Promise.race([
        sendChat('chat_load_conversation', { conversationId: state.conversationId }, () => {}),
        new Promise((r) => setTimeout(() => r(null), RETRY_QUERY_TIMEOUT_MS)),
      ]);
      if (data === null) {
        els.hint.textContent = '会话回查超时（服务未响应），请稍后再点重试';
        return;
      }
      // 回查失败（桥接抖动/服务端 5xx）与「确实没有可重试的消息」要分开：
      // 前者把原因写进 hint，别再误导成「找不到可重试的消息」。
      if (data && data.type === 'error') {
        els.hint.textContent = data.message || '会话回查失败，请稍后重试';
        return;
      }
      const rows = (data && data.messages) || [];
      // 服务端最后一条 assistant 仍是 streaming：本轮其实在跑（上一轮受理回执超时
      // 被本地误判成失败）。别报「找不到可重试的消息」——接管这一轮，把实时事件
      // 接回消息流，用户点重试的意图（继续）得到满足。
      for (let i = rows.length - 1; i >= 0; i -= 1) {
        if (rows[i].role !== 'assistant') continue;
        if (rows[i].status === 'streaming') {
          replaceMessagesFromPayload(data);
          renderMessages();
          const tail = lastStreamingAssistant();
          if (tail) {
            _liveSink = makeStreamHandler(tail);
            setWatchedConversation(state.conversationId);
            setSending(true);
            els.hint.textContent = '服务端正在执行本轮，已接管…';
          } else {
            els.hint.textContent = '本轮已在服务端完成，已刷新';
          }
          return;
        }
        break;
      }
      for (let i = rows.length - 1; i >= 0; i -= 1) {
        if (rows[i].role === 'assistant' && rows[i].status === 'error') { messageId = rows[i].id; break; }
      }
      if (messageId == null) { els.hint.textContent = '找不到可重试的消息'; return; }
      message.id = messageId;
    }
    resetForRetry(message);
    els.hint.textContent = '已发起重试，等待响应…';
    // 看门狗只盯「首个事件」：桥接半死时一个事件都到不了，sendChat 永不 resolve，
    // state.sending 卡死、面板锁住。首个事件一到就撤销，之后是正常长流。
    const handler = makeStreamHandler(message);
    let watchdog = setTimeout(() => {
      watchdog = null;
      if (!state.sending || message.status !== 'streaming') return;
      finalizeAssistant(message, 'error');
      message.status = 'error';
      message.parts.push({ type: 'text', text: '重试请求超时（服务未响应），请稍后再试' });
      setSending(false);
      renderMessages();
      els.hint.textContent = '重试请求超时，请稍后再点重试';
    }, RETRY_FIRST_FRAME_TIMEOUT_MS);
    sendChat('chat_retry', {
      conversationId: state.conversationId,
      messageId,
      // 面板协议版本：chat_send 带、retry 漏了的话，服务端 _proxy_chat_retry 的
      // panel_v 判 0 < 2，点重试永远回「扩展版本过旧」的 chat_error——新版面板
      // 被误判成旧版。与 chat_send 同口径带上。
      panelV: 2,
    }, (ev) => {
      if (watchdog !== null) { clearTimeout(watchdog); watchdog = null; els.hint.textContent = ''; }
      // 409 turn_running：上一轮仍在服务端跑，不是真失败。别把「正在执行中」当
      // 错误渲染，改为回拉对账接管这一轮（与 dispatchTurn 同口径）。
      if (ev && ev.type === 'error' && ev.code === 'turn_running') {
        els.hint.textContent = '上一轮仍在执行，正在接管…';
        void reconcileAfterAckTimeout(message);
        return;
      }
      handler(ev);
    });
  }

  // 手动拉一次当前会话最新状态（重载后接管进行中回复时用）。不动会话/agent 选择，
  // 只替换消息流；仍在 streaming 就重新排一次自动轮询。
  // 发送中不调用：本地事件流还在实时喂，整页替换会和它打架；服务端
  // chat_load_conversation 会对账「无活任务却残留 streaming」的消息
  // （_reconcile_orphaned_streaming），所以正常路径终态一定会回来。
  async function refreshCurrentSession(opts) {
    const convId = state.conversationId;
    // 默认发送中不刷（本地事件流在实时喂，整页替换会和它打架）。但顶部刷新按钮
    // 是用户主动要「拉一次服务端权威状态」——会话卡在 streaming（等审批/卡住）时
    // 更需要能刷，所以 allowWhileSending 时不挡。
    if (!convId || (state.sending && !(opts && opts.allowWhileSending))) return;
    stopStreamWatch();
    _liveSink = null; // 整页替换后旧消息对象不再是消息流尾条，sink 由 ensureLiveSink 重建
    const data = await sendChat('chat_load_conversation', { conversationId: convId }, () => {});
    if (state.conversationId !== convId) return;
    if (data && data.type === 'error') {
      els.hint.textContent = data.message || '会话加载失败，请稍后重试';
      return;
    }
    if (data && data.conversation) {
      replaceMessagesFromPayload(data);
      renderMessages();
    }
    watchStreamingIfNeeded();
  }

  // 缺口恢复：服务端发来 reset 帧或检测到 offset 缺口（ev.offset > cur）时调用。
  // 与 refreshCurrentSession 不同：它专为「流式期间缺帧」设计，带并发护栏避免
  // 高频 delta 下多次重拉互相覆盖；同时允许在 state.sending===true 时执行。
  async function recoverFromGap() {
    if (_gapRecovering) return; // 防风暴
    _gapRecovering = true;
    try {
      const convId = state.conversationId;
      if (!convId) return;
      stopStreamWatch();
      _liveSink = null;
      const data = await sendChat('chat_load_conversation', { conversationId: convId }, () => {});
      if (state.conversationId !== convId) return;
      if (data && data.type === 'error') {
        els.hint.textContent = data.message || '会话加载失败，请稍后重试';
        return;
      }
      if (data && data.conversation) {
        replaceMessagesFromPayload(data);
        renderMessages();
      }
      watchStreamingIfNeeded();
    } finally {
      _gapRecovering = false;
    }
  }

  // 对话页常驻刷新：手动重拉当前会话最新状态。发送中也允许（用户就是要看服务端
  // 到底跑到哪了 / 把卡住的审批刷出来）；没有会话时给一行提示。
  function onRefreshChat() {
    if (!state.conversationId) {
      els.hint.textContent = '当前没有会话';
      return;
    }
    void refreshCurrentSession({ allowWhileSending: true });
  }

  // --- 会话管理：新建 / 列表 / 载入 ---
  // 这一轮是否还在跑：本地正在发送（state.sending），或服务端仍在跑而本标签页只是
  // 「接管查看」（尾条 assistant 还是 streaming——典型是刷新页面/断连重连后续看）。
  // 输入区停止按钮原先只看 state.sending，接管态下按钮仍是「发送」，用户无从停止
  // 服务端那一轮；对话气泡上补的「刷新进度/停止」正是为此，属于重复品，已删除——
  // 统一收敛到「输入区停止 + 顶部刷新」两个入口。
  function turnRunning() {
    return state.sending || !!lastStreamingAssistant();
  }

  function syncComposer() {
    if (!els.send) return;
    const running = turnRunning();
    if (running) {
      // 执行中：发送按钮变身「停止」按钮（红底方块）。模型可能长时间自主跑
      // 工具，必须给用户随时中止的出口——服务端 abort 链路（chat_abort →
      // gateway → agent 任务 cancel）一直都在。
      if (!els.send.classList.contains('stop')) {
        els.send.classList.add('stop');
        els.send.innerHTML = els.ICON.stop;
        els.send.title = '停止执行';
        els.send.setAttribute('aria-label', '停止执行');
      }
      els.send.disabled = false;
    } else {
      if (els.send.classList.contains('stop')) {
        els.send.classList.remove('stop');
        els.send.innerHTML = els.ICON.send;
        els.send.title = '发送';
        els.send.setAttribute('aria-label', '发送');
      }
      els.send.disabled = !state.selectedAgent;
    }
    syncAttachButton();
  }

  // 会话运行状态 → 底栏文案。conversation_state 事件（服务端阶段切换时广播）驱动：
  // 「思考中…」「执行中 web…」「等待审批」「重试中 第2/5次 · 3s后重试」——让用户分得清
  // 在重试、在跑工具、还是已完结，而不是对着一个永久转圈的 spinner 猜。
  // 状态枚举与后端 agent/conversation_state.py 对齐。
  const STATUS_TEXT = {
    sending: '发送中…',
    waiting_upstream: '等待上游输出…',
    thinking: '思考中…',
    processing: '处理中…',
    executing: '执行中',
    retrying: '重试中',
    waiting_approval: '等待审批',
    approval_failed: '审批失败暂停',
    waiting_input: '等待输入',
    error: '异常',
    aborted: '已中止',
  };

  function statusText(st) {
    if (!st || !st.run_status) return null;
    const status = String(st.run_status);
    let text = STATUS_TEXT[status];
    if (text === undefined) return null;
    const info = String(st.run_status_info || '');
    if (status === 'executing') return info ? `执行中 ${info}…` : '执行中…';
    if (status === 'retrying') return info ? `重试中 ${info}…` : '重试中…';
    if (status === 'approval_failed') return info ? `审批失败：${info}` : '审批失败暂停';
    if (status === 'error') return info ? `异常：${info}` : '异常';
    if (status === 'waiting_input') return info || '等待输入';
    return text;
  }

  function showRunState(st) {
    state.lastRunState = st || null;
    if (!els.statusBar) return;
    const status = (st && st.run_status) || '';
    const text = statusText(st);
    els.statusBar.classList.toggle('show', !!text);
    els.statusBar.dataset.status = status;
    if (els.statusBarText) els.statusBarText.textContent = text || '';
    // 异常/中止/审批失败态挂重试按钮：点击走现有 retry 链路（保留已做 tool_calls 续跑）。
    const retryable = status === 'error' || status === 'aborted' || status === 'approval_failed';
    if (els.statusRetry) {
      els.statusRetry.hidden = !retryable;
      els.statusRetry.onclick = retryable ? () => onRetryLastAssistant() : null;
    }
  }

  function setSending(sending) {
    state.sending = sending;
    syncComposer();
  }

  // 从当前消息流的最后一条带 usage 的 assistant 消息恢复用量环。
  // 载入历史/刷新后 live 的 done 事件早已过去，不恢复的话环归 0 ——「token 一直
  // 显示为 0」的根因。usage 形状与 done 事件一致（prompt/completion/total/...）。
  function restoreUsageFromMessages() {
    for (let i = state.messages.length - 1; i >= 0; i--) {
      const m = state.messages[i];
      if (m.role === 'assistant' && m.usage && typeof m.usage === 'object') {
        const g = (k1, k2) => Number(m.usage[k1] ?? m.usage[k2]) || 0;
        const prompt = g('prompt_tokens', 'input_tokens');
        const completion = g('completion_tokens', 'output_tokens');
        state.lastUsage = {
          prompt, completion,
          total: Number(m.usage.total_tokens) || (prompt + completion),
          cached: g('cached_tokens', 'cache_read_input_tokens'),
          cacheCreation: g('cache_creation_tokens', 'cache_creation_input_tokens'),
          reasoning: g('reasoning_tokens', 'reasoning_output_tokens'),
        };
        renderUsage();
        return;
      }
    }
    // 没有任何带 usage 的 assistant 消息（新会话/老数据）：环归 0。
    state.lastUsage = null;
    renderUsage();
  }

  function startFreshSession() {
    // 清空当前对话上下文，回到「空会话」态（下一条消息若无 conversationId 会新建）。
    stopStreamWatch();
    state.conversationId = '';
    state.messages = [];
    state.lastUsage = null;
    // 附件是上一个会话的（已上传到其工作区），切新会话必须一起清（与 web
    // resetKey 清空 composer 同义），否则会带错会话的文件路径。
    attachItems.length = 0;
    renderAttachStrip();
    writePanelState({ conversationId: '' });
    _liveSink = null;
    setWatchedConversation('');
    setSending(false);
    renderMessages();
    renderUsage();
  }

  async function onNewSession() {
    if (state.sending) return;
    if (!state.selectedAgent) { startFreshSession(); return; }
    // 先本地进入空会话态，避免等待网络；服务端建好后回填 conversationId。
    startFreshSession();
    const res = await sendChat('chat_new_conversation', {
      agentId: state.selectedAgent,
      title: '',
    }, () => {});
    const conv = res && res.conversation;
    if (conv && conv.id) {
      state.conversationId = conv.id;
      writePanelState({ conversationId: conv.id, agentId: state.selectedAgent });
    }
    // 抽屉若开着，刷新列表反映新会话。
    if (state.drawerOpen) refreshSessions();
  }

  function toggleDrawer(open) {
    state.drawerOpen = open !== undefined ? open : !state.drawerOpen;
    els.drawer.classList.toggle('open', state.drawerOpen);
    if (state.drawerOpen) refreshSessions();
  }

  async function refreshSessions() {
    if (state.loadingSessions) return;
    state.loadingSessions = true;
    els.drawerList.innerHTML = '<div class="drawer-empty">加载中…</div>';
    const res = await sendChat('chat_list_conversations', {}, () => {});
    state.loadingSessions = false;
    // 桥接断开等失败：明示原因而不是误导性的「还没有会话」；再点刷新即可重试。
    if (res && res.type === 'error') {
      state.sessions = [];
      const err = document.createElement('div');
      err.className = 'drawer-empty';
      err.textContent = res.message || '会话列表加载失败，请稍后重试';
      els.drawerList.innerHTML = '';
      els.drawerList.appendChild(err);
      return;
    }
    state.sessions = (res && Array.isArray(res.conversations)) ? res.conversations : [];
    renderSessions();
  }

  function renderSessions() {
    els.drawerList.innerHTML = '';
    if (!state.sessions.length) {
      const empty = document.createElement('div'); empty.className = 'drawer-empty';
      empty.textContent = '还没有会话，发送消息或点“+”新建';
      els.drawerList.appendChild(empty); return;
    }
    for (const s of state.sessions) {
      const el = document.createElement('div');
      el.className = 'sess' + (s.id === state.conversationId ? ' active' : '');
      el.innerHTML = `<div class="sess-main">` +
        `<div class="sess-title">${escapeHtml(s.title || '未命名会话')}</div>` +
        `<div class="sess-meta">${escapeHtml(formatTime(s.updated_at))}</div></div>`;
      el.addEventListener('click', () => loadSession(s.id));
      els.drawerList.appendChild(el);
    }
  }

  function formatTime(value) {
    if (!value) return '';
    const d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    return d.toLocaleString();
  }

  // 把一条工具调用折进 parts：ask_user 出候选快捷回复（不出工具卡，问题正文已在
  // m.content 里），其余出工具卡；两种情况都登记进 toolCalls 供后续配对。
  function appendCallToParts(call, resultData, parts, toolCalls) {
    if (call.name === 'ask_user') {
      const q = resultData && typeof resultData === 'object' ? resultData : {};
      const candidates = Array.isArray(q.candidates)
        ? q.candidates.filter((c) => typeof c === 'string' && c.trim()) : [];
      if (candidates.length) parts.push({ type: 'candidates', candidates });
      toolCalls.push(call);
      return;
    }
    toolCalls.push(call);
    parts.push({ type: 'tool', call });
  }

  // 把服务端返回的会话历史展平成面板消息流。loadSession 与「重载后续看」轮询共用。
  // 工具卡展开态跨「整体替换消息流」保留：轮询/刷新/载入历史都会用
  // mapLoadedMessages 重建全新消息对象，新对象没有 _open，用户刚展开的卡片会
  // 被折回去。这里按「消息 id + 卡片序号」记展开态，重建时回填。
  const _cardOpenState = new Map(); // msgId -> Set(callIndex)
  function rememberCardOpenState() {
    _cardOpenState.clear();
    for (const m of state.messages) {
      if (m.role !== 'assistant' || !Array.isArray(m.toolCalls)) continue;
      const openSet = new Set();
      m.toolCalls.forEach((c, i) => { if (c._open) openSet.add(i); });
      if (openSet.size) _cardOpenState.set(m.id, openSet);
    }
  }
  function restoreCardOpenState(m) {
    const openSet = _cardOpenState.get(m.id);
    if (!openSet) return;
    (m.toolCalls || []).forEach((c, i) => { if (openSet.has(i)) c._open = true; });
  }

  // 恢复 pending 审批卡：审批是纯事件态、不入库，整页导航/刷新/断连后原卡就丢了，
  // 而那一轮 agent 正 await 着这个裁决——卡片回不来 = 会话永久卡住。载入会话时把
  // 服务端按会话回带的 pending_approvals 挂回最后一条 assistant 气泡（已 dedup）。
  function applyPendingApprovals(data) {
    const pending = Array.isArray(data && data.pending_approvals) ? data.pending_approvals : [];
    if (!pending.length) return;
    let assistant = null;
    for (let i = state.messages.length - 1; i >= 0; i--) {
      if (state.messages[i].role === 'assistant') { assistant = state.messages[i]; break; }
    }
    if (!assistant) {
      assistant = { role: 'assistant', content: '', toolCalls: [], parts: [], status: 'streaming' };
      state.messages.push(assistant);
    }
    if (!Array.isArray(assistant.parts)) assistant.parts = [];
    for (const a of pending) {
      const cid = a.confirmation_id;
      if (!cid) continue;
      if (assistant.parts.some((p) => p.type === 'approval' && p.approval && p.approval.confirmationId === cid)) continue;
      const meta = (a.metadata && typeof a.metadata === 'object') ? a.metadata : {};
      const isFileWrite = a.tool_name === 'file_write' || a.tool_name === 'file_patch';
      assistant.parts.push({ type: 'approval', approval: {
        confirmationId: cid,
        commandHash: a.command_hash || '',
        toolName: a.tool_name || 'code_run',
        runs: Array.isArray(meta.runs) ? meta.runs : [],
        writes: Array.isArray(meta.writes) ? meta.writes : [],
        message: a.message || (isFileWrite
          ? 'file_write 将写入沙箱边界外的文件。请核对路径与内容；本次授权仅写入一次。'
          : 'code_run 将在服务端执行代码。请核对完整代码和哈希；本次授权仅执行一次。'),
        expiresAt: approvalExpiresAtMs(a.expires_at),
        state: 'pending',
      } });
    }
  }

  // 统一的「整页替换消息流」入口：先记展开态，映射历史，恢复审批卡。三处载入
  // （手动刷新、接管轮询、切会话）共用，避免漏调。
  function replaceMessagesFromPayload(data) {
    rememberCardOpenState();
    state.messages = mapLoadedMessages(data);
    applyPendingApprovals(data);
    // 翻页游标：服务端按用户轮次分页，next_cursor 为空表示已到最早。整页替换
    // （刷新/轮询/切会话）都重置为服务端权威值，避免用旧的游标拉到错位的一页。
    const page = (data && data.page) || {};
    state.nextCursor = page.next_cursor != null ? page.next_cursor : null;
    state.hasMore = !!page.has_more;
  }

  // 滚到顶部加载更早的一页：带 cursor 拉更早的 user 轮次，prepend 到消息流头部，
  // 并保持视口位置（记录替换前的 scrollHeight，插入后按增量回补 scrollTop），
  // 避免内容插到上面把用户正在读的位置顶飞。
  async function loadEarlier() {
    if (state.loadingMore || !state.hasMore || !state.conversationId || state.nextCursor == null) return;
    if (state.sending) return; // 实时流期间整页替换会打架
    state.loadingMore = true;
    const convId = state.conversationId;
    const prevHeight = els.body.scrollHeight;
    const prevTop = els.body.scrollTop;
    const data = await sendChat('chat_load_conversation', {
      conversationId: convId,
      cursor: state.nextCursor,
    }, () => {});
    state.loadingMore = false;
    if (state.conversationId !== convId) return;
    if (!data || data.type === 'error' || !data.conversation) {
      if (data && data.message) els.hint.textContent = data.message;
      return;
    }
    const earlier = mapLoadedMessages(data);
    if (!earlier.length) {
      state.hasMore = false;
      return;
    }
    // 头部插入：保留展开态（旧对象仍在 earlier 里），拼在新消息之前。
    state.messages = earlier.concat(state.messages);
    const page = (data && data.page) || {};
    state.nextCursor = page.next_cursor != null ? page.next_cursor : null;
    state.hasMore = !!page.has_more;
    renderMessages();
    // 回补滚动位置：内容变高了，把 scrollTop 加上高度差，用户看到的是同一处。
    els.body.scrollTop = prevTop + (els.body.scrollHeight - prevHeight);
  }

  // 手动压缩上下文：/compact 或用量弹层「压缩」按钮共用。服务端对历史做一次
  // LLM 摘要并落 <history_summary> 标记消息（跨轮生效）；本地立刻弹完成提示，
  // 随后刷新会话（标记消息会出现在历史里，摘要内容用户可读）。
  async function runCompact() {
    if (state.compactPending) return;
    if (!state.conversationId) { els.hint.textContent = '当前没有会话可压缩'; return; }
    state.compactPending = true;
    els.hint.textContent = '正在压缩上下文…';
    const convId = state.conversationId;
    try {
      const res = await sendChat('chat_compact', { conversationId: convId }, () => {});
      if (state.conversationId !== convId) return;
      if (res && res.type !== 'error') {
        state.compactNotified = false; // 压缩后占用骤降，允许下一次超阈值再提醒
        els.hint.textContent = res.summarized
          ? `已压缩上下文（摘要 ${res.summaryChars} 字），历史保留在会话中`
          : '历史较短，无需压缩';
        void refreshCurrentSession();
      } else {
        els.hint.textContent = (res && res.message) || '压缩失败';
      }
    } finally {
      state.compactPending = false;
    }
  }

  function mapLoadedMessages(data) {
    const out = [];
    for (const m of (data.messages || [])) {
      if (m.role !== 'user' && m.role !== 'assistant') continue;
      const toolCalls = [];
      const parts = [];
      if (m.role === 'assistant' && Array.isArray(m.tool_calls)) {
        const entries = m.tool_calls.map((tc, i) => {
          const result = Array.isArray(m.tool_results) ? m.tool_results[i] : undefined;
          const resultData = result && result.data !== undefined ? result.data : result;
          const call = {
            name: tc.name || '',
            args: typeof tc.args === 'string' ? tc.args : JSON.stringify(tc.args || {}),
            status: tc.status || 'done',
            result: resultData,
            index: i,
          };
          return { call, offset: Number(tc.content_offset), reasoningOffset: Number(tc.reasoning_offset), resultData };
        });
        // 有 content_offset（服务端落库时按流式顺序打的时间线锚点）→ 按偏移量交错
        // 渲染正文段与工具卡片，还原真实顺序（文字→工具→文字）。旧消息没有该字段
        // → 回退到「工具在前、正文在后」的历史行为。reasoning_offset 与之同套路，
        // 把思考也按「想一步、做一步」切段交织（agent_loop 每轮先思考再正文再调工具）。
        const hasOffsets = entries.length > 0 && entries.every((e) => Number.isFinite(e.offset));
        const reasoning = String(m.reasoning || '');
        const hasReasoningOffsets = hasOffsets && reasoning.length > 0
          && entries.every((e) => Number.isFinite(e.reasoningOffset));
        if (hasOffsets) {
          const content = String(m.content || '');
          let cursor = 0;
          let rCursor = 0;
          const pushReasoning = (end) => {
            if (!hasReasoningOffsets) return;
            const slice = reasoning.slice(rCursor, end);
            if (slice) parts.push({ type: 'reasoning', text: slice });
            rCursor = end;
          };
          for (const e of entries) {
            // 每轮：思考先于正文吐出。先落这段的思考（到该调用的 reasoning_offset），
            // 再落这段的正文（到 content_offset），再工具卡。
            const rOff = Math.min(Math.max(e.reasoningOffset, rCursor), reasoning.length);
            pushReasoning(rOff);
            const off = Math.max(cursor, Math.min(e.offset, content.length));
            const slice = content.slice(cursor, off);
            if (slice) parts.push({ type: 'text', text: slice });
            cursor = off;
            appendCallToParts(e.call, e.resultData, parts, toolCalls);
          }
          pushReasoning(reasoning.length); // 尾部思考（最后一轮工具之后、done 之前）
          const tail = content.slice(cursor);
          if (tail) parts.push({ type: 'text', text: tail });
        } else {
          for (const e of entries) appendCallToParts(e.call, e.resultData, parts, toolCalls);
          // 正文段放最后（历史回放兼容）。仅 assistant 建 text part：renderMessages
          // 只渲染 assistant 的 parts，用户正文走 m.content 兜底路径。
          if (m.content) parts.push({ type: 'text', text: m.content });
        }
      } else if (m.content && m.role === 'assistant') {
        // 无工具调用的 assistant 消息：整段正文一个 text part。
        parts.push({ type: 'text', text: m.content });
      }
      // ask_user 附件的回放载体：media 列的 attachment part 由服务端现签直链
      // （content_url，相对路径需绝对化），url part 直接透传公网地址。
      if (m.role === 'assistant' && Array.isArray(m.media)) {
        for (const item of m.media) {
          if (!item || typeof item !== 'object') continue;
          const src = item.kind === 'url'
            ? String(item.url || '')
            : absolutizeMediaUrl(String(item.content_url || item.url || ''));
          parts.push({ type: 'media', media: { src, name: String(item.name || ''), mime: String(item.mime_type || '') } });
        }
      }
      // 思考回放兜底：只有当 reasoning 不为空、却没打进 parts（旧消息无 reasoning_offset
      // 或无工具调用）时，才整体前置一段——让顶部「无 parts 时整段」守卫或「有 parts 时
      // 这一段」至少能显示出来。已按偏移交错进 parts 的不再追加。
      if (m.role === 'assistant' && m.reasoning && parts.length
          && !parts.some((p) => p.type === 'reasoning')) {
        parts.unshift({ type: 'reasoning', text: m.reasoning });
      }
      out.push({
        id: m.id,
        role: m.role,
        content: m.content || '',
        toolCalls,
        parts,
        status: m.status || 'done',
        createdAt: m.created_at || m.createdAt || undefined,
        model: m.model || '',
        // 思考过程：历史回放折叠块（服务端 messages 行带 reasoning 字段）。
        reasoning: m.reasoning || '',
        // 错误独立字段（与 web 同口径）：live error 分支写 assistant.error，这里
        // 从落库行回填，刷新后「错误：…」独立行 == 实时（不再把报错粘进正文）。
        error: m.error || '',
        // 子 agent 卡片随消息落库（turn_writer 统一聚合），刷新后恢复。
        subagents: Array.isArray(m.subagents) ? m.subagents : [],
        // 本轮 token 用量：载入历史时用它恢复用量环（否则刷新后归 0）。
        usage: m.usage || undefined,
      });
      restoreCardOpenState(out[out.length - 1]);
    }
    return out;
  }

  // --- 重载后自动续看进行中的回复 ---
  // 整页导航/刷新后本地已无 live 事件监听，但 run_agent 仍在服务端跑到底并在回合
  // 结束时把最终结果落库。这里轮询会话，直到最后一条 assistant 落定（done/error），
  // 把「永久转圈」自动解成最终答复，用户无需再手动刷新。
  // 注意：正文仅在回合结束时落库，故轮询期间只见转圈、结束后一次性见完整答复；
  // 逐字进度需服务端事件重放缓冲才能恢复，不在本次范围内。
  let _streamWatch = null;
  function stopStreamWatch() {
    if (_streamWatch) { clearTimeout(_streamWatch); _streamWatch = null; }
  }
  function lastStreamingAssistant() {
    const m = state.messages[state.messages.length - 1];
    return m && m.role === 'assistant' && m.status === 'streaming' ? m : null;
  }
  function watchStreamingIfNeeded() {
    stopStreamWatch();
    if (state.sending) return; // 本地有活跃流：事件流自己更新，不轮询
    const convId = state.conversationId;
    if (!convId || !lastStreamingAssistant()) return;
    const poll = async () => {
      _streamWatch = null;
      // 期间用户又发消息 / 切了会话：交还给 live 流或新会话，停止本轮轮询。
      if (state.sending || state.conversationId !== convId) return;
      const data = await sendChat('chat_load_conversation', { conversationId: convId }, () => {});
      if (state.sending || state.conversationId !== convId) return;
      if (data && data.conversation) {
        replaceMessagesFromPayload(data);
        renderMessages();
      }
      if (lastStreamingAssistant()) _streamWatch = setTimeout(poll, 3000);
    };
    _streamWatch = setTimeout(poll, 3000);
  }

  async function loadSession(convId) {
    if (state.sending || !convId) return;
    stopStreamWatch();
    const data = await sendChat('chat_load_conversation', { conversationId: convId }, () => {});
    // 失败可见：以前静默 return，断流后用户点了会话什么都没发生还以为面板坏了。
    // 抽屉此刻盖着面板，footer 的 hint 看不见，错误要同时写进抽屉列表。
    if (!data || data.type === 'error' || !data.conversation) {
      const msg = (data && data.message) || '会话载入失败，请稍后重试';
      els.hint.textContent = msg;
      els.drawerList.innerHTML = '';
      const err = document.createElement('div');
      err.className = 'drawer-empty';
      err.textContent = msg;
      els.drawerList.appendChild(err);
      return;
    }
    state.conversationId = data.conversation.id || convId;
    writePanelState({ conversationId: state.conversationId, agentId: state.selectedAgent });
    // 恢复会话级对话参数：mode 是会话状态（切换即生效、重试/后续轮沿用），载入
    // 会话时按服务端权威值复原下拉显示，不与本地旧值混。model 由 refreshModels 的
    // 会话默认值流程覆盖，这里只复原 mode。
    if (data.conversation.mode && data.conversation.mode !== state.mode) {
      state.mode = data.conversation.mode;
      renderModeMenu(); renderModeLabel();
      if (els.goalInputs) els.goalInputs.style.display = state.mode === 'goal' ? 'flex' : 'none';
    }
    // 恢复会话运行状态：刷新/重开面板后，从会话行带回来的 run_state 告诉模型
    // 「上次在重试第几次 / 上次出了什么错」，而不是只有转圈。终态（done）服务端
    // 已清空，这里自然回到默认「流式中…」。
    if (data.conversation.run_state) showRunState(data.conversation.run_state);
    // 切到这条会话 = 换订阅：先摘旧 sink（历史已整体替换，旧对象不再是消息流尾条），
    // 再向服务端登记观察者；若本轮仍在跑，尾条 streaming 消息会被 ensureLiveSink 接上。
    _liveSink = null;
    setWatchedConversation(state.conversationId);
    // Agent 跟随会话，避免历史会话与当前所选 agent 错配；换 agent 要重拉模型列表。
    if (data.conversation.agent_id && state.agents.some(a => a.id === data.conversation.agent_id)) {
      if (state.selectedAgent !== data.conversation.agent_id) {
        state.selectedAgent = data.conversation.agent_id;
        rememberAgentId(state.selectedAgent);
        state.selectedModel = '';
        void refreshModels();
      }
    }
    // 历史消息（含用户/助手/工具）展平进面板消息流。
    replaceMessagesFromPayload(data);
    // 用量环：载入历史后从最后一条 assistant 的 usage 恢复（刷新/切会话不再归 0）。
    restoreUsageFromMessages();
    _stickToBottom = true; // 切会话：落到新会话底部，别沿用上一次的阅读位置
    renderMessages();
    renderSessions();
    toggleDrawer(false);
    // 载入的最后一条 assistant 仍是 streaming（服务端还在跑）→ 自动轮询续看到落定。
    watchStreamingIfNeeded();
  }

  // 徽标点击前从 background 查询实时 WS 状态；未连接时不创建聊天面板。
  document.addEventListener('click', async (e) => {
    const ind = e.target && e.target.closest && e.target.closest('#ljq-ind');
    if (!ind) return;
    if (ind._preventClick) { ind._preventClick = false; return; } // 拖拽后不触发
    e.preventDefault(); e.stopPropagation();
    if (statusQueryPending) return;
    statusQueryPending = true;
    try {
      if (tabId == null) { try { tabId = await getTabId(); } catch (_) { /* ignore */ } }
      const status = await getBridgeStatus();
      updateBridgeIndicator(status);
      if (!status.connected) {
        if (state.open) togglePanel(false);
        showBridgeUnavailable(status);
        return;
      }
      togglePanel();
    } finally {
      statusQueryPending = false;
    }
  }, true);

  getBridgeStatus().then(updateBridgeIndicator);

  // 徽标自愈：状态唯一来源是 SW 的 bridge_status_push，而它是「发完就忘」的
  // （sendMessage 失败只 catch 掉）。SW 休眠、标签页被冻结、扩展重载让页面失联，
  // 任意一次丢失都会把徽标永久钉在旧值上——原先只有刷新页面（重新注入 = 再查一次）
  // 才能恢复，表现为「徽标不动，必须刷新」。这里主动轮询兜底：可见时 5s 一次，
  // 切回本标签页/窗口获焦时立刻补一次（最常见的「切回来发现是旧的」场景）。
  // 后台标签页不轮询——既省电，也避免无意义地唤醒 SW。
  let _indicatorTimer = null;
  function refreshIndicatorSoon() {
    getBridgeStatus().then(updateBridgeIndicator).catch(() => { /* 查询失败：保持现状 */ });
  }
  function syncIndicatorPolling() {
    if (document.hidden) {
      if (_indicatorTimer !== null) { clearInterval(_indicatorTimer); _indicatorTimer = null; }
      return;
    }
    if (_indicatorTimer === null) _indicatorTimer = setInterval(refreshIndicatorSoon, 5000);
    refreshIndicatorSoon();
  }
  document.addEventListener('visibilitychange', syncIndicatorPolling);
  window.addEventListener('focus', syncIndicatorPolling);
  syncIndicatorPolling();

  // 整页导航重注入后恢复面板：本 tab 上次开着面板就自动重开。桥还在重连
  // 时也照开（用空态占位），SW 连上后由 bridge_status_push 的 connected 分支
  // 触发 resumeConversationIfStored 把上次的会话历史拉回来——不再因刷新时 SW
  // 没连上就整段跳过、逼用户手动点徽标。
  (async () => {
    try {
      tabId = await getTabId();
    } catch (_) { tabId = null; }
    if (tabId == null) return;
    const saved = await readPanelState();
    if (!saved || !saved.open) return;
    togglePanel(true);
  })();

  // 暴露给其它脚本/调试用。
  window.__tmwdChatPanel = { toggle: togglePanel };
})();

