/**
 * API Platform JavaScript - 动态鉴权懒加载版
 * 接口密钥与模型列表按需加载，提升安全与性能
 * 平台协议向导按需注入，在需要时准确加载
 * Version: 202603312000
 */

(function () {
    var _slot, _cur, _exitTm, _hideTm, _pending;
    function show(msg, duration) {
        duration = duration || 3000;
        if (!_slot) {
            _slot = document.createElement('div');
            _slot.id = 'global-toast-slot';
            _slot.className = 'global-toast-slot';
            document.body.appendChild(_slot);
        }
        _pending = msg;
        if (_cur) {
            if (!_cur.classList.contains('global-toast--exit')) {
                if (_hideTm) { clearTimeout(_hideTm); _hideTm = null; }
                _cur.classList.add('global-toast--exit');
                if (_exitTm) clearTimeout(_exitTm);
                _exitTm = setTimeout(function () {
                    _exitTm = null;
                    _cur = null;
                    if (_hideTm) clearTimeout(_hideTm);
                    var m = _pending;
                    _pending = null;
                    _slot.innerHTML = '';
                    var el = document.createElement('div');
                    el.className = 'global-toast';
                    el.textContent = m;
                    _slot.appendChild(el);
                    el.offsetHeight;
                    el.classList.add('global-toast--visible');
                    _cur = el;
                    _hideTm = setTimeout(function () {
                        el.classList.add('global-toast--exit');
                        _hideTm = setTimeout(function () {
                            _slot.innerHTML = '';
                            _cur = null;
                        }, 220);
                    }, duration);
                }, 220);
            }
            return;
        }
        _slot.innerHTML = '';
        var el = document.createElement('div');
        el.className = 'global-toast';
        el.textContent = msg;
        _slot.appendChild(el);
        el.offsetHeight;
        el.classList.add('global-toast--visible');
        _cur = el;
        _hideTm = setTimeout(function () {
            el.classList.add('global-toast--exit');
            _hideTm = setTimeout(function () {
                _slot.innerHTML = '';
                _cur = null;
            }, 220);
        }, duration);
    }
    window.showToast = show;
})();

/** 与 components/notifications.js 配合：首屏统计加载完成后再拉公告，避免弹窗被 loading 遮住 */
window.__deferApiPlatformNotifications = true;

document.addEventListener('DOMContentLoaded', function () {
    // --- Global State ---
    let currentUser = null;
    let firstApiKey = 'YOUR_API_KEY';
    let statsLoading = false;
    let lastStatsData = null;
    let statsLastLoaded = 0;
    // 懒加载标记：仅在实际进入对应页面时加载
    let apiKeysLoaded = false;
    /** @type {{ updated: number, userLevel: number, available: Array<{id:string,name:string,type:string,desc:string,ctx:number}>, unavailableCount: number } | null} */
    let chatModelsSnapshot = null;

    // --- UI Elements ---
    const sidebar = document.querySelector('.sidebar');
    const navItems = document.querySelectorAll('.nav-item:not(.disabled)');
    const navGroups = document.querySelectorAll('.nav-group-title');
    const sections = document.querySelectorAll('.dashboard-section');
    const notificationBtn = document.getElementById('notificationBtn');
    const notificationPopover = document.getElementById('notificationPopover');
    const notificationBackdrop = document.getElementById('notificationBackdrop');
    const notificationCloseBtn = document.getElementById('notificationCloseBtn');
    const avatarBtn = document.getElementById('userAvatarBtn');
    const avatarDropdown = document.getElementById('avatarDropdown');
    const loadingOverlay = document.getElementById('loadingOverlay');
    const mainContent = document.getElementById('apiPlatformContent');

    // --- Initialization ---
    checkAccess();

    // --- Event Listeners ---

    // Sidebar Navigation
    navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            const targetId = item.getAttribute('data-section');
            if (!targetId) {
                return;
            }

            // 有 data-section 的才是内部导航，阻止默认行为
            e.preventDefault();

            // Update Active State
            document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active'));
            item.classList.add('active');

            // Update Parent Group Active State
            updateSidebarActiveState();

            // Show Section
            sections.forEach(section => {
                section.classList.remove('active');
                if (section.id === targetId) {
                    section.classList.add('active');
                }
            });

            // 通知后台模块挂起（中止 AI SSE 流、停掉排行自动刷新等）
            notifySectionSwitch(targetId);

            // 动态鉴权懒加载：进入「接口密钥」时再加载密钥数据（安全+性能）
            if (targetId === 'api-keys' && !apiKeysLoaded && currentUser?.has_api_access) {
                apiKeysLoaded = true;
                loadApiKeys();
            }
            // 账户信息：首次进入时加载邀请数据（原为独立的第二个 nav 监听，已合并至此）
            if (targetId === 'account-info') initInviteSection();
            // 开发文档：首次进入时懒加载文档片段，并更新 API 密钥占位
            if (targetId === 'documentation') { ensureDocumentationLoaded(); updateDocumentationKeys(); }
            // 用量统计：首次进入时按需加载 Chart.js/marked.js 并拉取数据（幂等）
            if (targetId === 'usage' && window.ApiUsage) window.ApiUsage.init();
            // 模型排名：首次进入时按需拉取数据（幂等）
            if (targetId === 'rankings' && window.ApiRankings) window.ApiRankings.init();
            // Agent 开发：首次进入时拉取已发布 Agent 列表（幂等）
            if (targetId === 'agent-dev' && window.AgentDev) window.AgentDev.init();

            // Close mobile sidebar if open（使用统一关闭逻辑）
            if (window.innerWidth <= 768) closeSidebar();
        });
    });

    // Sidebar Group Toggle
    navGroups.forEach(group => {
        group.addEventListener('click', () => {
            group.parentElement.classList.toggle('expanded');
        });
    });

    // 用量统计：鼠标悬停/触摸时预取 Chart.js/marked.js，让点击切换做到零感延迟
    document.querySelectorAll('.nav-item[data-section="usage"]').forEach(el => {
        let hinted = false;
        const warm = () => {
            if (hinted) return;
            hinted = true;
            window.ApiUsage?.prefetch?.();
        };
        el.addEventListener('mouseenter', warm, { passive: true });
        el.addEventListener('touchstart', warm, { passive: true });
    });

    // 开发文档：悬停预取文档片段（29KB），点击切换零等待
    document.querySelectorAll('.nav-item[data-section="documentation"]').forEach(el => {
        let hinted = false;
        const warm = () => {
            if (hinted) return;
            hinted = true;
            ensureDocumentationLoaded();
        };
        el.addEventListener('mouseenter', warm, { passive: true });
        el.addEventListener('touchstart', warm, { passive: true });
    });

    // 模型排名：悬停预取，触发后端缓存生成，让点击切换显示更快
    document.querySelectorAll('.nav-item[data-section="rankings"]').forEach(el => {
        let hinted = false;
        const warm = () => {
            if (hinted) return;
            hinted = true;
            window.ApiRankings?.prefetch?.();
        };
        el.addEventListener('mouseenter', warm, { passive: true });
        el.addEventListener('touchstart', warm, { passive: true });
    });

    // Agent 开发：悬停预取已发布列表，点击切换零等待
    document.querySelectorAll('.nav-item[data-section="agent-dev"]').forEach(el => {
        let hinted = false;
        const warm = () => {
            if (hinted) return;
            hinted = true;
            window.AgentDev?.prefetch?.();
        };
        el.addEventListener('mouseenter', warm, { passive: true });
        el.addEventListener('touchstart', warm, { passive: true });
    });

    // 汉堡菜单（移动端）：打开/关闭侧边栏
    const hamburgerBtn = document.getElementById('hamburgerBtn');
    const sidebarOverlay = document.getElementById('sidebarOverlay');

    function openSidebar() {
        hamburgerBtn?.classList.add('active');
        sidebar?.classList.add('active');
        sidebarOverlay?.classList.add('active');
        document.body.classList.add('sidebar-open');
    }

    function closeSidebar() {
        hamburgerBtn?.classList.remove('active');
        sidebar?.classList.remove('active');
        sidebarOverlay?.classList.remove('active');
        document.body.classList.remove('sidebar-open');
    }

    hamburgerBtn?.addEventListener('click', function (e) {
        e.stopPropagation();
        e.preventDefault();
        if (sidebar?.classList.contains('active')) {
            closeSidebar();
        } else {
            openSidebar();
        }
    });

    sidebarOverlay?.addEventListener('click', closeSidebar);

    window.addEventListener('resize', function () {
        if (window.innerWidth > 768) closeSidebar();
    });

    function isMobileNotificationUi() {
        return window.matchMedia('(max-width: 768px)').matches;
    }

    function openNotificationPopover() {
        if (!notificationPopover) return;
        notificationPopover.classList.add('active');
        notificationBtn?.setAttribute('aria-expanded', 'true');
        if (isMobileNotificationUi()) {
            notificationBackdrop?.classList.add('active');
            notificationBackdrop?.setAttribute('aria-hidden', 'false');
            document.body.classList.add('notification-open');
            closeSidebar();
        }
    }

    function closeNotificationPopover() {
        if (!notificationPopover) return;
        notificationPopover.classList.remove('active');
        notificationPopover.style.transform = '';
        notificationPopover.style.opacity = '';
        notificationBtn?.setAttribute('aria-expanded', 'false');
        notificationBackdrop?.classList.remove('active');
        notificationBackdrop?.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('notification-open');
    }

    window.closeNotificationPopover = closeNotificationPopover;

    // Notification Toggle
    if (notificationBtn) {
        notificationBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpening = !notificationPopover?.classList.contains('active');
            if (isOpening) {
                openNotificationPopover();
            } else {
                closeNotificationPopover();
            }
            avatarDropdown?.classList.remove('active');
            playBellRing(notificationBtn);
        });
    }

    notificationCloseBtn?.addEventListener('click', (e) => {
        e.stopPropagation();
        closeNotificationPopover();
    });

    notificationBackdrop?.addEventListener('click', closeNotificationPopover);

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && notificationPopover?.classList.contains('active')) {
            closeNotificationPopover();
        }
    });

    // 移动端：下拉关闭通知面板
    if (notificationPopover) {
        let swipeStartY = 0;
        let swipeDeltaY = 0;
        let swipeDragging = false;

        notificationPopover.addEventListener('touchstart', (e) => {
            if (!isMobileNotificationUi() || !notificationPopover.classList.contains('active')) return;
            if (!e.target.closest('.popover-header')) return;
            swipeStartY = e.touches[0].clientY;
            swipeDeltaY = 0;
            swipeDragging = true;
        }, { passive: true });

        notificationPopover.addEventListener('touchmove', (e) => {
            if (!swipeDragging) return;
            swipeDeltaY = Math.max(0, e.touches[0].clientY - swipeStartY);
            notificationPopover.style.transform = `translateY(${swipeDeltaY}px)`;
            notificationPopover.style.opacity = String(Math.max(0.35, 1 - swipeDeltaY / 280));
        }, { passive: true });

        const endSwipe = () => {
            if (!swipeDragging) return;
            swipeDragging = false;
            if (swipeDeltaY > 72) {
                closeNotificationPopover();
            } else {
                notificationPopover.style.transform = '';
                notificationPopover.style.opacity = '';
            }
            swipeDeltaY = 0;
        };

        notificationPopover.addEventListener('touchend', endSwipe);
        notificationPopover.addEventListener('touchcancel', endSwipe);
    }

    window.addEventListener('resize', () => {
        if (!isMobileNotificationUi()) closeNotificationPopover();
    });

    // 铃铛点击动效：移除并强制 reflow 后重新添加，确保连续点击仍能触发动画
    function playBellRing(btn) {
        if (!btn) return;
        btn.classList.remove('is-ringing');
        // eslint-disable-next-line no-unused-expressions
        void btn.offsetWidth;
        btn.classList.add('is-ringing');
        setTimeout(() => btn.classList.remove('is-ringing'), 700);
    }

    // Avatar Dropdown Toggle
    if (avatarBtn) {
        avatarBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            avatarDropdown.classList.toggle('active');
            closeNotificationPopover();
        });
    }

    // Close panels when clicking outside
    document.addEventListener('click', () => {
        closeNotificationPopover();
        avatarDropdown?.classList.remove('active');
    });

    // Prevent closing when clicking inside panels（notificationPopover 由 notifications.js 共用）
    if (notificationPopover) notificationPopover.addEventListener('click', (e) => e.stopPropagation());
    if (avatarDropdown) avatarDropdown.addEventListener('click', (e) => e.stopPropagation());

    // Logout
    document.getElementById('logoutBtn')?.addEventListener('click', handleLogout);

    // 模型卡片收纳切换（热门/对话/图像/OCR/向量等）
    document.querySelectorAll('.card-header-collapse[data-collapse]').forEach(btn => {
        btn.addEventListener('click', () => {
            const wrap = document.getElementById(btn.getAttribute('data-collapse'));
            if (wrap) wrap.classList.toggle('collapsed');
        });
    });

    // 定向领取：仅一侧领取成功后（服务端已校验 Turnstile），同会话另一侧可在 promo_human_verified 内免验证码
    const PROMO_TS_SITEKEY = window.__TURNSTILE_SITEKEY || '0x4AAAAAAB4TbrXJmPX8Qu8W';
    const ANTHROPIC_CLAIM_TS_SITEKEY = PROMO_TS_SITEKEY;
    let promoClaimTsWidgetId = null;
    let promoHumanVerifiedClient = false;

    /** 定向领取 API action（须为明文常量；勿放入易被混淆破坏的对象字面量） */
    function promoApiClaimAction(kind) {
        if (kind === 'anthropic') return 'claim_anthropic_promo';
        return 'claim_openclaw_promo';
    }

    function promoModalIds(kind) {
        if (kind === 'anthropic') {
            return {
                modal: 'anthropicClaimModal',
                host: 'anthropicClaimTurnstileHost',
                err: 'anthropicClaimModalError',
                hint: 'anthropicClaimModalHint',
                confirm: 'anthropicClaimConfirmBtn',
                toastOk: '已领取今日 Anthropic 定向额度'
            };
        }
        return {
            modal: 'openclawClaimModal',
            host: 'openclawClaimTurnstileHost',
            err: 'openclawClaimModalError',
            hint: 'openclawClaimModalHint',
            confirm: 'openclawClaimConfirmBtn',
            toastOk: '已领取今日 API 总额度 +500 万 Tokens'
        };
    }

    function setPromoHumanVerifiedUi(kind, verified) {
        const ids = promoModalIds(kind);
        const hint = document.getElementById(ids.hint);
        const host = document.getElementById(ids.host);
        if (verified) {
            if (hint) {
                hint.textContent = '已通过人机验证，可直接确认领取。';
                hint.classList.add('human-ok-hint');
            }
            if (host) { host.style.display = 'none'; host.innerHTML = ''; }
        } else {
            if (hint) {
                hint.textContent = '完成下方人机验证后点击「确认领取」';
                hint.classList.remove('human-ok-hint');
            }
            if (host) host.style.display = '';
        }
    }

    function destroyPromoClaimTs() {
        try { if (promoClaimTsWidgetId != null && window.turnstile) window.turnstile.remove(promoClaimTsWidgetId); } catch (e) { }
        promoClaimTsWidgetId = null;
        ['anthropicClaimTurnstileHost', 'openclawClaimTurnstileHost'].forEach(function (hid) {
            const h = document.getElementById(hid);
            if (h) h.innerHTML = '';
        });
    }

    let promoClaimTsRetryTimer = null;

    function mountPromoClaimTs(kind, retry) {
        retry = retry || 0;
        const ids = promoModalIds(kind);
        const host = document.getElementById(ids.host);
        const confirmBtn = document.getElementById(ids.confirm);
        const errEl = document.getElementById(ids.err);
        if (!host) return;
        // 弹窗已关闭则停止重试链，避免向隐藏弹窗渲染验证组件
        const modalEl = document.getElementById(ids.modal);
        if (!modalEl || modalEl.style.display === 'none') return;
        if (promoHumanVerifiedClient) {
            destroyPromoClaimTs();
            if (errEl) errEl.textContent = '';
            if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = '确认领取'; }
            setPromoHumanVerifiedUi(kind, true);
            return;
        }
        setPromoHumanVerifiedUi(kind, false);
        destroyPromoClaimTs();
        if (errEl) errEl.textContent = '';
        if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = '确认领取'; }
        if (!window.turnstile || typeof window.turnstile.render !== 'function') {
            if (retry < 100) {
                promoClaimTsRetryTimer = setTimeout(() => mountPromoClaimTs(kind, retry + 1), 50);
                return;
            }
            if (errEl) errEl.textContent = '人机验证脚本未加载，请刷新页面';
            return;
        }
        try {
            promoClaimTsWidgetId = window.turnstile.render(host, {
                sitekey: PROMO_TS_SITEKEY,
                theme: 'auto',
                callback: function () {
                    if (errEl) errEl.textContent = '';
                    if (confirmBtn) confirmBtn.disabled = false;
                },
                'expired-callback': () => { if (confirmBtn) confirmBtn.disabled = true; try { if (window.turnstile && promoClaimTsWidgetId != null) window.turnstile.reset(promoClaimTsWidgetId); } catch (e2) { } },
                'error-callback': () => { if (errEl) errEl.textContent = '验证加载失败，请关闭后重试'; if (confirmBtn) confirmBtn.disabled = true; }
            });
        } catch (e) { if (errEl) errEl.textContent = '人机验证初始化失败'; }
        if (promoClaimTsWidgetId == null && errEl && !errEl.textContent) errEl.textContent = '人机验证初始化失败';
    }

    function openPromoClaimModal(kind) {
        const ids = promoModalIds(kind);
        const modal = document.getElementById(ids.modal);
        if (!modal) return;
        if (lastStatsData && lastStatsData.promo_human_verified) promoHumanVerifiedClient = true;
        const errEl = document.getElementById(ids.err);
        if (errEl) errEl.textContent = '';
        modal.style.display = 'flex';
        modal.setAttribute('aria-hidden', 'false');
        window.TurnstileLoader?.load().catch(() => { /* 失败时 mountPromoClaimTs 自带提示 */ });
        requestAnimationFrame(() => mountPromoClaimTs(kind, 0));
    }

    function closePromoClaimModal(kind) {
        const ids = promoModalIds(kind);
        const modal = document.getElementById(ids.modal);
        if (modal) { modal.style.display = 'none'; modal.setAttribute('aria-hidden', 'true'); }
        if (promoClaimTsRetryTimer) { clearTimeout(promoClaimTsRetryTimer); promoClaimTsRetryTimer = null; }
        destroyPromoClaimTs();
        const errEl = document.getElementById(ids.err);
        if (errEl) errEl.textContent = '';
        const btn = document.getElementById(ids.confirm);
        if (btn) { btn.disabled = true; btn.textContent = '确认领取'; }
        setPromoHumanVerifiedUi(kind, false);
    }

    function syncDirectedPromoBars(stats) {
        if (stats && typeof stats.promo_human_verified !== 'undefined') {
            promoHumanVerifiedClient = !!stats.promo_human_verified;
        }
        function one(claimed, btnId, doneId, giftSel, ctaId) {
            const done = document.getElementById(doneId);
            const btn = document.getElementById(btnId);
            if (!done || !btn) return;
            const c = !!claimed;
            done.hidden = !c;
            btn.hidden = c;
            btn.disabled = c;
            btn.style.display = c ? 'none' : '';
            const gift = giftSel ? document.querySelector(giftSel) : null;
            if (gift) gift.style.display = c ? 'none' : '';
            const cta = document.getElementById(ctaId);
            if (cta) {
                cta.style.cursor = c ? 'default' : '';
                cta.style.pointerEvents = c ? 'none' : '';
            }
        }
        one(stats.anthropic_promo_claimed, 'anthropicPromoClaimBtn', 'anthropicPromoDone', '#anthropicPromoCta .directed-promo-gift', 'anthropicPromoCta');
        one(stats.openclaw_promo_claimed, 'openclawPromoClaimBtn', 'openclawPromoDone', '#openclawPromoCta .directed-promo-gift', 'openclawPromoCta');
    }

    async function onPromoClaimConfirm(kind) {
        const ids = promoModalIds(kind);
        const confirmBtn = document.getElementById(ids.confirm);
        const errEl = document.getElementById(ids.err);
        if (!confirmBtn) return;
        let turnstileToken = '';
        if (!promoHumanVerifiedClient) {
            if (!window.turnstile || promoClaimTsWidgetId == null) {
                if (errEl) errEl.textContent = '请先完成人机验证';
                return;
            }
            try { turnstileToken = window.turnstile.getResponse(promoClaimTsWidgetId) || ''; } catch (e) { }
            if (!turnstileToken) {
                if (errEl) errEl.textContent = '请先完成人机验证';
                return;
            }
        }
        if (errEl) errEl.textContent = '';
        if (confirmBtn) {
            confirmBtn.disabled = true;
            confirmBtn.textContent = '领取中...';
        }
        try {
            const resp = await fetch('/api/api_keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: promoApiClaimAction(kind), turnstileToken: turnstileToken })
            });
            const data = await resp.json();
            if (data.success) {
                closePromoClaimModal(kind);
                if (typeof window.showToast === 'function') window.showToast(ids.toastOk, 3200);
                if (typeof window.fireConfetti === 'function') window.fireConfetti();
                lastStatsData = null;
                statsLastLoaded = 0;
                await loadStats(true);
            } else if (data.already_claimed) {
                closePromoClaimModal(kind);
                const alreadyMsg = '今日已领取';
                if (typeof window.showToast === 'function') window.showToast(alreadyMsg, 2600);
                lastStatsData = null;
                statsLastLoaded = 0;
                await loadStats(true);
            } else {
                const failMsg = data.message || '领取失败';
                if (errEl) errEl.textContent = failMsg;
                const modalEl = document.getElementById(ids.modal);
                const modalOpen = modalEl && modalEl.style.display === 'flex';
                if (!modalOpen && typeof window.showToast === 'function') {
                    window.showToast(failMsg, 3200);
                }
                if (confirmBtn) {
                    confirmBtn.disabled = false;
                    confirmBtn.textContent = '确认领取';
                }
                if (!promoHumanVerifiedClient && window.turnstile && promoClaimTsWidgetId != null) {
                    try { window.turnstile.reset(promoClaimTsWidgetId); } catch (e2) { }
                }
            }
        } catch (e) {
            if (errEl) errEl.textContent = '网络错误，请稍后重试';
            const modalEl = document.getElementById(ids.modal);
            const modalOpen = modalEl && modalEl.style.display === 'flex';
            if (!modalOpen && typeof window.showToast === 'function') {
                window.showToast('网络错误，请稍后重试', 3200);
            }
            if (confirmBtn) {
                confirmBtn.disabled = false;
                confirmBtn.textContent = '确认领取';
            }
            if (!promoHumanVerifiedClient && window.turnstile && promoClaimTsWidgetId != null) {
                try { window.turnstile.reset(promoClaimTsWidgetId); } catch (e3) { }
            }
        }
    }

    document.getElementById('anthropicPromoClaimBtn')?.addEventListener('click', (e) => {
        if (e.currentTarget.disabled) return;
        e.preventDefault();
        openPromoClaimModal('anthropic');
    });
    document.getElementById('openclawPromoClaimBtn')?.addEventListener('click', (e) => {
        if (e.currentTarget.disabled) return;
        e.preventDefault();
        openPromoClaimModal('openclaw');
    });
    document.getElementById('closeAnthropicClaimModal')?.addEventListener('click', () => closePromoClaimModal('anthropic'));
    document.getElementById('anthropicClaimCancelBtn')?.addEventListener('click', () => closePromoClaimModal('anthropic'));
    document.getElementById('anthropicClaimModal')?.addEventListener('click', (e) => { if (e.target === e.currentTarget) closePromoClaimModal('anthropic'); });
    document.getElementById('closeOpenclawClaimModal')?.addEventListener('click', () => closePromoClaimModal('openclaw'));
    document.getElementById('openclawClaimCancelBtn')?.addEventListener('click', () => closePromoClaimModal('openclaw'));
    document.getElementById('openclawClaimModal')?.addEventListener('click', (e) => { if (e.target === e.currentTarget) closePromoClaimModal('openclaw'); });
    document.getElementById('anthropicClaimConfirmBtn')?.addEventListener('click', function () { onPromoClaimConfirm('anthropic'); });
    document.getElementById('openclawClaimConfirmBtn')?.addEventListener('click', function () { onPromoClaimConfirm('openclaw'); });

    // Create Key Modal
    const createKeyBtn = document.getElementById('createKeyBtn');
    const createKeyModal = document.getElementById('createKeyModal');
    const cancelCreateBtn = document.getElementById('cancelCreateBtn');
    const confirmCreateBtn = document.getElementById('confirmCreateBtn');

    if (createKeyBtn) {
        createKeyBtn.addEventListener('click', () => {
            createKeyModal.classList.add('active');
            document.getElementById('keyName').value = '';
            document.getElementById('keyName').focus();
        });
    }

    if (cancelCreateBtn) {
        cancelCreateBtn.addEventListener('click', () => {
            createKeyModal.classList.remove('active');
        });
    }

    if (confirmCreateBtn) {
        confirmCreateBtn.addEventListener('click', createApiKey);
    }

    // Close Show Key Modal
    document.getElementById('closeShowKeyBtn')?.addEventListener('click', () => {
        document.getElementById('showKeyModal').classList.remove('active');
    });

    // --- Change Password Modal Logic ---
    const changePasswordBtn = document.getElementById('changePasswordBtn');
    const changePasswordModal = document.getElementById('changePasswordModal');
    const closeChangePasswordModal = document.getElementById('closeChangePasswordModal');
    const tabEmailVerify = document.getElementById('tabEmailVerify');
    const tabPasswordVerify = document.getElementById('tabPasswordVerify');
    const emailPane = document.getElementById('emailVerifyPane');
    const pwdPane = document.getElementById('passwordVerifyPane');

    function setChangePwTabActive(which) {
        const emailActive = which === 'email';
        emailPane.style.display = emailActive ? 'block' : 'none';
        pwdPane.style.display = emailActive ? 'none' : 'block';

        if (emailActive) {
            tabEmailVerify.classList.remove('btn-outline');
            tabEmailVerify.classList.add('btn-primary');
            tabPasswordVerify.classList.remove('btn-primary');
            tabPasswordVerify.classList.add('btn-outline');
        } else {
            tabPasswordVerify.classList.remove('btn-outline');
            tabPasswordVerify.classList.add('btn-primary');
            tabEmailVerify.classList.remove('btn-primary');
            tabEmailVerify.classList.add('btn-outline');
        }
    }

    if (changePasswordBtn) {
        changePasswordBtn.addEventListener('click', () => {
            if (currentUser && currentUser.email) {
                document.getElementById('cpUserEmail').textContent = currentUser.email;
            }
            changePasswordModal.style.display = 'flex';
            setChangePwTabActive('email');
            window.TurnstileLoader?.load().catch(() => { /* 静默：render 失败时 getTurnstileToken 会提示 */ });
        });
    }

    if (closeChangePasswordModal) {
        closeChangePasswordModal.addEventListener('click', () => {
            changePasswordModal.style.display = 'none';
        });
    }

    if (tabEmailVerify) {
        tabEmailVerify.addEventListener('click', () => setChangePwTabActive('email'));
    }

    if (tabPasswordVerify) {
        tabPasswordVerify.addEventListener('click', () => setChangePwTabActive('password'));
    }

    function getTurnstileToken(containerId) {
        try {
            const el = document.getElementById(containerId);
            if (window.turnstile && el) {
                const token = window.turnstile.getResponse(el);
                return token || '';
            }
        } catch (e) {
            console.warn('Turnstile token failed:', e);
        }
        return '';
    }

    // Send Email Verification Code
    document.getElementById('sendEmailCodeBtn')?.addEventListener('click', async function () {
        const btn = this;
        btn.disabled = true;
        btn.textContent = '发送中...';
        try {
            const turnstileToken = getTurnstileToken('cpEmailTurnstile');
            if (!turnstileToken) {
                BLModal.alert({ title: '人机验证', message: '请先完成人机验证后再发送验证码。', type: 'warning' });
                btn.disabled = false;
                btn.textContent = '发送验证码';
                return;
            }
            const resp = await fetch('/api/send_verification', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    email: currentUser.email,
                    type: 'change_password',
                    turnstileToken: turnstileToken
                })
            });
            const result = await resp.json();
            if (result.success) {
                const sessionToken = encodeURIComponent(result.sessionToken || '');
                const email = encodeURIComponent(currentUser.email || '');
                changePasswordModal.style.display = 'none';
                window.location.href = `/auth/verify?flow=reset_email&rid=${sessionToken}&email=${email}&return_url=${encodeURIComponent('/api_platform')}`;
            } else {
                BLModal.alert({ title: '发送失败', message: result.message || '发送失败，请稍后重试。', type: 'danger' });
                btn.disabled = false;
                btn.textContent = '发送验证码';
            }
        } catch (e) {
            console.error(e);
            BLModal.alert({ title: '网络错误', message: '网络连接异常，请稍后重试。', type: 'danger' });
            btn.disabled = false;
            btn.textContent = '发送验证码';
        }
    });

    /** 统一的密码显示/隐藏切换：按钮点击 → 翻转 input.type 与图标 */
    function bindPasswordToggle(btnId, inputId) {
        const btn = document.getElementById(btnId);
        if (!btn) return;
        btn.addEventListener('click', function () {
            const input = document.getElementById(inputId);
            const icon = this.querySelector('i');
            if (!input) return;
            const isHidden = input.type === 'password';
            input.type = isHidden ? 'text' : 'password';
            if (icon) icon.className = isHidden ? 'fas fa-eye-slash' : 'fas fa-eye';
        });
    }

    bindPasswordToggle('toggleOldPassword', 'oldPassword');
    bindPasswordToggle('toggleNewPasswordCP', 'newPasswordCP');
    bindPasswordToggle('toggleConfirmPasswordCP', 'confirmPasswordCP');

    // Change Password Form Submit
    function setFieldError(id, msg) {
        const el = document.getElementById(id);
        if (el) el.textContent = msg || '';
    }

    function validatePassword(password) {
        const hasLetter = /[a-zA-Z]/.test(password);
        const hasNumber = /\d/.test(password);
        return password.length >= 8 && hasLetter && hasNumber;
    }

    document.getElementById('changePasswordForm')?.addEventListener('submit', async function (e) {
        e.preventDefault();
        setFieldError('oldPasswordError', '');
        setFieldError('newPasswordCPError', '');
        setFieldError('confirmPasswordCPError', '');

        const oldPw = document.getElementById('oldPassword').value.trim();
        const newPw = document.getElementById('newPasswordCP').value.trim();
        const cPw = document.getElementById('confirmPasswordCP').value.trim();
        const btn = document.getElementById('confirmChangePasswordBtn');

        if (!oldPw) { setFieldError('oldPasswordError', '请输入旧密码'); return; }
        if (!validatePassword(newPw)) { setFieldError('newPasswordCPError', '至少8位且包含字母与数字'); return; }
        if (oldPw === newPw) { setFieldError('newPasswordCPError', '新密码不能与旧密码相同'); return; }
        if (newPw !== cPw) { setFieldError('confirmPasswordCPError', '两次输入不一致'); return; }

        const turnstileToken = getTurnstileToken('cpTurnstile');
        if (!turnstileToken) { setFieldError('confirmPasswordCPError', '请先完成人机验证'); return; }

        btn.disabled = true;
        btn.textContent = '处理中...';
        try {
            const resp = await fetch('/api/auth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    action: 'change_password_with_old',
                    old_password: oldPw,
                    password: newPw,
                    confirm_password: cPw,
                    turnstileToken: turnstileToken
                })
            });
            const result = await resp.json();
            if (result.success) {
                if (typeof window.showToast === 'function') {
                    window.showToast('密码修改成功，请重新登录');
                } else {
                    BLModal.alert({ title: '修改成功', message: '密码已更新，请重新登录。', type: 'success', confirmText: '好的' });
                }
                try { await fetch('/api/logout', { method: 'POST' }); } catch (e) { }
                setTimeout(function () {
                    window.location.href = '/auth/login?return_url=' + encodeURIComponent('/api_platform/');
                }, 800);
            } else {
                setFieldError('confirmPasswordCPError', result.message || '修改失败');
                btn.disabled = false;
                btn.textContent = '确定';
            }
        } catch (err) {
            console.error(err);
            setFieldError('confirmPasswordCPError', '网络错误');
            btn.disabled = false;
            btn.textContent = '确定';
        }
    });

    // --- 平台协议向导（按需注入）---
    let agreementWizardRoot = null;
    let agreementWizardOnComplete = null;

    async function checkAgreement() {
        try {
            const r = await fetch('/api/api_platform_notifications', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'check_agreement' })
            });
            const d = await r.json();
            return !!(d.success && d.agreed);
        } catch (e) {
            return false;
        }
    }

    function showAgreementWizard(options) {
        const initialStep = options?.initialStep ?? 0;
        agreementWizardOnComplete = options?.onComplete || (function () { });

        if (!agreementWizardRoot) {
            const overlay = document.createElement('div');
            overlay.className = 'agr-overlay';
            overlay.id = 'agreementWizardOverlay';
            overlay.innerHTML = `
                <div class="agr-card">
                    <div class="agr-progress">
                        <span class="agr-dot" data-step="0"></span>
                        <span class="agr-dot" data-step="1"></span>
                        <span class="agr-dot" data-step="2"></span>
                        <span class="agr-dot" data-step="3"></span>
                    </div>
                    <div class="agr-viewport">
                        <div class="agr-track">
                            <!-- Step 0: 欢迎 -->
                            <div class="agr-step" data-step="0">
                                <div class="agr-icon agr-icon--blue"><i class="fas fa-rocket" aria-hidden="true"></i></div>
                                <h2>欢迎使用白鹿 API 平台</h2>
                                <p class="agr-desc">创建密钥、调用模型，几分钟完成接入</p>
                                <div class="agr-features agr-features-welcome">
                                    <div class="agr-feature agr-feature--row">
                                        <i class="fas fa-gift" style="color:#f59e0b;" aria-hidden="true"></i>
                                        <span>新用户赠送<strong>免费额度</strong>，如需更多可联系我们扩容</span>
                                   
                                    </div>
                                    <div class="agr-feature agr-feature--row">
                                        <i class="fas fa-graduation-cap" style="color:#3b82f6;" aria-hidden="true"></i>
                                        <span>高校与科研团队可申请全量接入，用于教学、实验与课题研究</span>
                                    </div>
                                    <div class="agr-tip-card">
                                        <div class="agr-tip-icon"><i class="fas fa-server" aria-hidden="true"></i></div>
                                        <div>
                                            <strong>本地部署推荐</strong>
                                            <p>稳定业务场景下，本地部署可控性更高、长期成本更优。</p>
                                            <a href="https://bailucode.com/blog/model-finetuning-deployment" target="_blank" rel="noopener noreferrer">了解模型部署 →</a>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <!-- Step 1: 数据使用协议 -->
                            <div class="agr-step" data-step="1">
                                <div class="agr-icon agr-icon--blue"><i class="fas fa-shield-alt" aria-hidden="true"></i></div>
                                <h2>数据使用协议</h2>
                                <p class="agr-desc">开始使用前，请确认以下数据条款</p>
                                <div class="agr-features agr-features-data">
                                    <div class="agr-feature">
                                        <div class="agr-feature-header"><i class="fas fa-lock"></i><span class="agr-feature-title">隐私承诺</span></div>
                                        <p class="agr-feature-para">BAILU AI 团队<strong>不会主动读取、存储或向第三方分享</strong>您的聊天历史与个人信息。所有对话内容仅在请求处理期间临时存在于服务器内存中，处理完成后即时释放。</p>
                                    </div>
                                    <div class="agr-feature">
                                        <div class="agr-feature-header"><i class="fas fa-flask"></i><span class="agr-feature-title">训练数据授权</span></div>
                                        <p class="agr-feature-para">平台免费开放，但您允许我们将<strong>部分对话内容</strong>用于模型优化训练。数据将经过以下处理流程：由模型自动完成<strong>端到端加密与去敏</strong>，严格移除所有可识别的个人身份信息，仅保留对模型改进有价值的语言模式与结构化信息。</p>
                                    </div>
                                    <div class="agr-includes">
                                        <span class="agr-includes-label">涉及内容范围：</span>代码结构与代码库片段 / 文本对话中的纠错与改进信息 / 指令理解与响应质量数据
                                    </div>
                                </div>
                                <label class="agr-checkbox" id="agrCheckbox1Label">
                                    <input type="checkbox" id="agrCheckbox1">
                                    <span class="agr-checkmark"></span>
                                    <span>我已阅读并同意上述数据使用条款</span>
                                </label>
                            </div>
                            <!-- Step 2: 平台使用规范 -->
                            <div class="agr-step" data-step="2">
                                <div class="agr-icon agr-icon--amber"><i class="fas fa-balance-scale" aria-hidden="true"></i></div>
                                <h2>平台使用规范</h2>
                                <div class="agr-features agr-features-rule">
                                    <div class="agr-feature">
                                        <div class="agr-feature-header"><i class="fas fa-comments" style="color:#3b82f6;"></i><span class="agr-feature-title">反馈义务</span></div>
                                        <p class="agr-feature-para">白鹿 API 平台免费开放，但您有义务及时反馈各模型的效能表现与不足之处，帮助我们持续改进模型质量、做好下一轮训练迭代。</p>
                                    </div>
                                    <div class="agr-feature">
                                        <div class="agr-feature-header"><i class="fas fa-exclamation-triangle" style="color:#3b82f6;"></i><span class="agr-feature-title">安全红线</span></div>
                                        <p class="agr-feature-para">严禁利用平台从事以下行为：危险品制造指导、提示词注入与身份伪装、生成违法或不当内容、伪造官方文件等。违反者我们有权立即冻结或永久封禁账号，且不予退还剩余额度。</p>
                                    </div>
                                    <div class="agr-feature">
                                        <div class="agr-feature-header"><i class="fas fa-link" style="color:#3b82f6;"></i><span class="agr-feature-title">合理使用</span></div>
                                        <p class="agr-feature-para">请勿将 API Key 公开分享或用于大规模自动化滥用。如检测到异常调用行为，我们有权限制速率或暂停服务，以保障全体用户的使用体验。</p>
                                    </div>
                                </div>
                                <label class="agr-checkbox" id="agrCheckbox2Label">
                                    <input type="checkbox" id="agrCheckbox2">
                                    <span class="agr-checkmark"></span>
                                    <span>我已阅读并同意遵守上述平台规范</span>
                                </label>
                            </div>
                            <!-- Step 3: 完成（绿色图标+礼花） -->
                            <div class="agr-step agr-step-done" data-step="3">
                                <div class="agr-confetti-wrap" id="agrConfettiWrap"></div>
                                <div class="agr-icon agr-icon-done agr-icon--green"><i class="fas fa-check" aria-hidden="true"></i></div>
                                <h2>准备就绪</h2>
                                <p class="agr-desc">协议已确认，您可以开始创建密钥并调用模型了</p>
                                <div class="agr-nav" style="margin-top:20px;border:none;background:transparent;padding:0;">
                                    <button type="button" class="agr-btn-next" id="agrDoneBtn" style="margin:0 auto;">开始使用</button>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="agr-nav" id="agrMainNav">
                        <button type="button" class="agr-btn-prev" id="agrPrev">← 上一步</button>
                        <button type="button" class="agr-btn-next" id="agrNext">下一步 →</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);
            agreementWizardRoot = overlay;

            const track = overlay.querySelector('.agr-track');
            const dots = overlay.querySelectorAll('.agr-dot');
            const mainNav = overlay.querySelector('#agrMainNav');
            const prevBtn = overlay.querySelector('#agrPrev');
            const nextBtn = overlay.querySelector('#agrNext');
            const checkbox1 = overlay.querySelector('#agrCheckbox1');
            const checkbox2 = overlay.querySelector('#agrCheckbox2');
            const checkbox1Label = overlay.querySelector('#agrCheckbox1Label');
            const checkbox2Label = overlay.querySelector('#agrCheckbox2Label');
            const doneBtn = overlay.querySelector('#agrDoneBtn');
            const confettiWrap = overlay.querySelector('#agrConfettiWrap');

            let step = 0;
            const totalSteps = 4;

            function fireConfetti() {
                if (!confettiWrap) return;
                const colors = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6'];
                for (let i = 0; i < 50; i++) {
                    const p = document.createElement('div');
                    p.className = 'agr-confetti';
                    p.style.left = Math.random() * 100 + '%';
                    p.style.animationDelay = Math.random() * 0.5 + 's';
                    p.style.background = colors[Math.floor(Math.random() * colors.length)];
                    p.style.transform = `rotate(${Math.random() * 360}deg)`;
                    confettiWrap.appendChild(p);
                    setTimeout(() => p.remove(), 2500);
                }
            }

            function goTo(s) {
                step = Math.max(0, Math.min(totalSteps - 1, s));
                track.style.transform = `translateX(-${step * 100}%)`;
                dots.forEach((d, i) => {
                    d.classList.toggle('active', i === step);
                    d.classList.toggle('completed', i < step);
                });
                mainNav.style.display = step === 3 ? 'none' : 'flex';
                if (prevBtn) prevBtn.style.display = step === 0 ? 'none' : 'inline-flex';
                if (nextBtn) {
                    nextBtn.textContent = '下一步 →';
                    nextBtn.disabled = false;
                }
                if (step === 3) fireConfetti();
            }

            async function doAccept() {
                const resp = await fetch('/api/api_platform_notifications', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'accept_agreement' })
                });
                const d = await resp.json();
                if (d.success) {
                    overlay.remove();
                    agreementWizardRoot = null;
                    const cb = agreementWizardOnComplete;
                    agreementWizardOnComplete = null;
                    if (cb) cb();
                } else {
                    doneBtn.disabled = false;
                    BLModal.alert({ title: '确认失败', message: d.message || '请稍后重试。', type: 'danger' });
                }
            }

            prevBtn?.addEventListener('click', () => goTo(step - 1));
            nextBtn?.addEventListener('click', () => {
                if (step === 1 && !checkbox1?.checked) { BLModal.alert({ title: '请确认条款', message: '请先勾选同意数据使用条款。', type: 'warning' }); return; }
                if (step === 2 && !checkbox2?.checked) { BLModal.alert({ title: '请确认规范', message: '请先勾选同意平台使用规范。', type: 'warning' }); return; }
                if (step < 3) goTo(step + 1);
            });
            /* 复选框通过透明 input 覆盖整块 label 实现全区域可点，依赖原生 label 行为 */
            doneBtn?.addEventListener('click', async () => {
                doneBtn.disabled = true;
                await doAccept();
            });
        }

        agreementWizardRoot.style.display = 'flex';
        const track = agreementWizardRoot.querySelector('.agr-track');
        const dots = agreementWizardRoot.querySelectorAll('.agr-dot');
        const mainNav = agreementWizardRoot.querySelector('#agrMainNav');
        const prevBtn = agreementWizardRoot.querySelector('#agrPrev');
        const nextBtn = agreementWizardRoot.querySelector('#agrNext');
        const checkbox1 = agreementWizardRoot.querySelector('#agrCheckbox1');
        const checkbox2 = agreementWizardRoot.querySelector('#agrCheckbox2');
        if (track) track.style.transform = `translateX(-${initialStep * 100}%)`;
        dots.forEach((d, i) => {
            d.classList.toggle('active', i === initialStep);
            d.classList.toggle('completed', i < initialStep);
        });
        if (mainNav) mainNav.style.display = initialStep === 3 ? 'none' : 'flex';
        if (prevBtn) prevBtn.style.display = initialStep === 0 ? 'none' : 'inline-flex';
        if (checkbox1) checkbox1.checked = false;
        if (checkbox2) checkbox2.checked = false;
        if (initialStep === 3) {
            const wrap = agreementWizardRoot.querySelector('#agrConfettiWrap');
            if (wrap && wrap.children.length === 0) {
                const colors = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6'];
                for (let i = 0; i < 50; i++) {
                    const p = document.createElement('div');
                    p.className = 'agr-confetti';
                    p.style.left = Math.random() * 100 + '%';
                    p.style.animationDelay = Math.random() * 0.5 + 's';
                    p.style.background = colors[Math.floor(Math.random() * colors.length)];
                    p.style.transform = `rotate(${Math.random() * 360}deg)`;
                    wrap.appendChild(p);
                    setTimeout(() => p.remove(), 2500);
                }
            }
        }
    }

    function hideAgreementWizard() {
        if (agreementWizardRoot) {
            agreementWizardRoot.style.display = 'none';
        }
    }

    // --- Core Functions ---

    function updateSidebarActiveState() {
        // Remove active-group class from all groups
        document.querySelectorAll('.nav-group').forEach(group => {
            group.classList.remove('active-group');
        });

        // Find active item and add active-group to its parent
        const activeItem = document.querySelector('.nav-item.active');
        if (activeItem) {
            const parentGroup = activeItem.closest('.nav-group');
            if (parentGroup) {
                parentGroup.classList.add('active-group');
            }
        }
    }

    async function checkAccess() {
        try {
            const response = await fetch('/api/auth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'check_session' })
            });
            const result = await response.json();

            if (result.success && result.user) {
                currentUser = result.user;
                if (result.user.account_flags) {
                    var _pru = encodeURIComponent(window.location.pathname + window.location.search || '/api_platform/');
                    window.location.replace('/auth/account_gate.php?return_url=' + _pru);
                    return;
                }
                if (result.user.has_api_access) {
                    initPlatform(result.user);
                } else {
                    showNoAccess();
                }
            } else {
                const ret = encodeURIComponent(window.location.pathname + window.location.search || '/api_platform/');
                window.location.href = '/auth/login?return_url=' + ret;
            }
        } catch (error) {
            console.error('Auth check failed:', error);
            const ret = encodeURIComponent(window.location.pathname + window.location.search || '/api_platform/');
            window.location.href = '/auth/login?return_url=' + ret;
        }
    }

    function applyBanBanners(user) {
        const apiBar = document.getElementById('apiBannedWarning');
        if (apiBar) {
            const apiSt = user && (user.api_status != null && user.api_status !== '') ? user.api_status : 'active';
            apiBar.style.display = (user && apiSt === 'banned') ? 'block' : 'none';
        }
        const warnBar = document.getElementById('accountWarningBanner');
        if (warnBar && user) {
            const wc = parseInt(user.warning_count) || 0;
            const wu = user.warning_until || '';
            const wr = user.warning_reason || '';
            if (wc > 0 && wu && new Date(wu) > new Date()) {
                document.getElementById('accountWarningText').textContent = wr;
                const wuDisp = String(wu).replace('T', ' ').substring(0, 16);
                document.getElementById('accountWarningMeta').textContent = 'This warning will be removed on ' + wuDisp + '. For this incident, your API keys have been temporarily disabled. If you violate the Terms of Service again, your account will be banned immediately.';
                warnBar.style.display = 'block';
            } else {
                warnBar.style.display = 'none';
            }
        }
    }

    function loadSystemNotificationsAfterReady() {
        // 通知非首屏关键数据：延到浏览器空闲再拉取，避免与 stats/models 抢占网络与主线程
        var run = function () {
            if (window.ApiPlatformNotifications && typeof window.ApiPlatformNotifications.load === 'function') {
                window.ApiPlatformNotifications.load();
            }
        };
        if (typeof window.requestIdleCallback === 'function') {
            window.requestIdleCallback(run, { timeout: 3000 });
        } else {
            setTimeout(run, 400);
        }
    }

    async function initPlatform(user) {
        applyBanBanners(user);
        if (user.is_admin) {
            var al = document.getElementById('adminLink');
            var apl = document.getElementById('apiAdminLink');
            if (al) al.style.display = 'flex';
            if (apl) apl.style.display = 'flex';
        }
        // Update User Info
        const avatarBtn = document.getElementById('userAvatarBtn');
        if (avatarBtn) {
            // Use last_name first character like dashboard.js does
            if (user.last_name) {
                const initial = user.last_name.charAt(0).toUpperCase();
                avatarBtn.textContent = initial;
            } else {
                const fallback = user.username || user.name || user.email || 'User';
                avatarBtn.textContent = fallback.charAt(0).toUpperCase();
            }
        }

        // Update Dropdown Info
        const fullName = `${user.last_name || ''}${user.first_name || ''}`.trim() || user.username || user.email || 'User';
        setText('dropdownUserName', fullName);
        setText('dropdownUserEmail', user.email || 'No Email');

        // Update Profile Section
        setText('profileName', fullName);
        setText('profileEmail', user.email || '--');
        setText('profileGroupId', user.id || '--');

        // Update Plan Badge
        updatePlanBadge(user);

        // Initial Sidebar State
        updateSidebarActiveState();

        // 首屏三请求并行：协议检查与 stats/models 拉取无数据依赖。
        // 协议门只拦截交互（向导为全屏遮罩），数据在向导背后就绪，同意后即刻可见。
        // 注：骨架屏方案下 mainContent 与 loadingOverlay 默认状态已就绪（mainContent 已显示骨架），无需切换
        const agreementReady = checkAgreement();
        const statsReady = loadStats();
        loadModels(); // 模型在 Overview 中展示，和 stats 并行；不 await 以便更早 paint

        const agreed = await agreementReady;
        if (!agreed) {
            showAgreementWizard({
                initialStep: 0,
                onComplete: async () => {
                    loadApiKeys();
                    loadStats(); // 向导期间数据可能已陈旧，刷新一次（2s 内自动去重）
                    applyHashToSection();
                    loadSystemNotificationsAfterReady();
                }
            });
            return;
        }

        // 数据返回后会替换对应区域的骨架，整体平滑过渡
        await statsReady;

        applyHashToSection();
        loadSystemNotificationsAfterReady();
    }

    /** 切换 section 时通知后台模块挂起：中止用量页 AI SSE 流、停掉排行榜 60s 轮询 */
    function notifySectionSwitch(activeId) {
        if (activeId !== 'usage' && window.ApiUsage && typeof window.ApiUsage.suspend === 'function') {
            window.ApiUsage.suspend();
        }
        if (activeId !== 'rankings' && window.ApiRankings && typeof window.ApiRankings.suspend === 'function') {
            window.ApiRankings.suspend();
        }
    }

    function applyHashToSection() {
        var hash = (window.location.hash || '').replace(/^#/, '');
        if (!hash) return;
        var section = document.getElementById(hash);
        var navItem = document.querySelector('.nav-item[data-section="' + hash + '"]');
        if (section && navItem) {
            document.querySelectorAll('.nav-item').forEach(function (n) { n.classList.remove('active'); });
            navItem.classList.add('active');
            updateSidebarActiveState();
            document.querySelectorAll('.dashboard-section').forEach(function (s) {
                s.classList.remove('active');
                if (s.id === hash) s.classList.add('active');
            });
            notifySectionSwitch(hash);
            if (hash === 'api-keys' && !apiKeysLoaded && currentUser && currentUser.has_api_access) {
                apiKeysLoaded = true;
                loadApiKeys();
            }
            if (hash === 'documentation') { ensureDocumentationLoaded(); updateDocumentationKeys(); }
            if (hash === 'account-info') initInviteSection();
            if (hash === 'usage' && window.ApiUsage) window.ApiUsage.init();
            if (hash === 'rankings' && window.ApiRankings) window.ApiRankings.init();
            if (hash === 'agent-dev' && window.AgentDev) window.AgentDev.init();
        }
    }

    function updatePlanBadge(user) {
        const planBadge = document.getElementById('planBadge');
        if (!planBadge) return;

        // Use API platform's independent plan (api_access_level)
        const apiLevel = user.api_access_level || 1;
        const levelNames = { 1: 'Personal', 2: 'Professional', 3: 'Enterprise' };
        const planText = levelNames[apiLevel] || 'Personal';

        planBadge.textContent = planText;

        // Reset class & re-apply color coding（清除骨架占位的 loading 类）
        planBadge.className = 'plan-badge';
        if (apiLevel === 3) {
            planBadge.classList.add('plan-enterprise');
        } else if (apiLevel === 2) {
            planBadge.classList.add('plan-pro');
        } else {
            planBadge.classList.add('plan-free');
        }
    }

    function showNoAccess() {
        if (loadingOverlay) loadingOverlay.style.display = 'none';
        if (mainContent) mainContent.style.display = 'none';
        const noAccessPage = document.getElementById('noAccessPage');
        if (noAccessPage) noAccessPage.style.display = 'block';
    }

    async function handleLogout() {
        const ok = await BLModal.confirm({
            title: '退出登录',
            message: '确定要退出当前账号吗？退出后需要重新登录。',
            confirmText: '退出登录',
            cancelText: '取消',
            type: 'warning'
        });
        if (!ok) return;
        try {
            const response = await fetch('/api/logout', { method: 'POST' });
            const result = await response.json();
            if (result.success) window.location.href = '/';
        } catch (error) {
            console.error('Logout error:', error);
        }
    }

    // --- Data Loading Functions ---

    let statsGen = 0;

    async function loadStats(force) {
        const now = Date.now();
        if (!force) {
            if (statsLoading) return;
            if (lastStatsData && (now - statsLastLoaded < 2000)) {
                updateStatsDisplay(lastStatsData);
                return;
            }
        }

        // 世代号替代自旋等待：force 请求直接发出，旧响应后到时被丢弃
        const gen = ++statsGen;
        statsLoading = true;
        try {
            const response = await fetch('/api/api_keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'get_stats' })
            });
            const result = await response.json();
            if (gen !== statsGen) return; // 已有更新的请求，丢弃过期响应

            if (result.success && result.stats) {
                lastStatsData = result.stats;
                statsLastLoaded = Date.now();
                updateStatsDisplay(result.stats);
            }
        } catch (error) {
            console.error('Load stats error:', error);
        } finally {
            if (gen === statsGen) statsLoading = false;
        }
    }

    function updateStatsDisplay(stats) {
        setText('activeKeysCount', stats.active_keys || 0);
        setText('requestsCount', (stats.requests_30d || 0).toLocaleString());
        const tokensUsed = Number(stats.tokens_used_effective ?? stats.tokens_used) || 0;
        const tokensQuota = Number(stats.tokens_quota_effective ?? stats.tokens_quota) || 10000000;
        setText('tokensUsed', tokensUsed.toLocaleString());
        setText('tokenUsedDisplay', tokensUsed.toLocaleString());
        setText('tokenQuotaDisplay', tokensQuota.toLocaleString());
        const pct = tokensQuota > 0 ? Math.min(100, Math.round((tokensUsed / tokensQuota) * 100)) : 0;
        setText('tokenUsagePercentage', pct + '%');
        const progressFill = document.getElementById('tokenProgressFill');
        if (progressFill) progressFill.style.width = pct + '%';
        const rpmVal = stats.api_rate_limit;
        setText('rpmLimitValue', (rpmVal != null && rpmVal > 0) ? rpmVal.toLocaleString() : '无限制');
        const tpmVal = stats.api_tpm_limit;
        setText('tpmLimitValue', (tpmVal != null && tpmVal > 0) ? tpmVal.toLocaleString() : '无限制');
        const concVal = stats.api_concurrent_limit;
        setText('concurrentLimitValue', (concVal != null && concVal > 0) ? concVal.toLocaleString() : '无限制');
        renderDirectedPacks(stats);
        syncDirectedPromoBars(stats);
    }

    function renderDirectedPacks(stats) {
        const mount = document.getElementById('directedPacksMount');
        if (!mount) return;
        const packs = stats.directed_packs;
        if (!Array.isArray(packs) || packs.length === 0) { mount.innerHTML = ''; mount.hidden = true; return; }
        mount.hidden = false;
        mount.innerHTML = packs.map(function (p) {
            const theme = /^[a-z0-9_-]+$/i.test(p.theme || '') ? p.theme : 'default';
            const pct = Math.min(100, Math.max(0, Math.round(Number(p.pct) || 0)));
            const label = escapeHtml(String(p.label || p.id || ''));
            return '<div class="directed-pack">' +
                '<div class="directed-pack-top">' +
                '<div class="directed-pack-title-row"><span class="directed-pack-name">' + label + '</span></div>' +
                '<span class="directed-pack-pct">' + pct + '%</span></div>' +
                '<div class="directed-pack-nums"><span class="n-used">' + Number(p.used || 0).toLocaleString() + '</span>' +
                '<span class="n-sep">/</span><span class="n-quota">' + Number(p.quota || 0).toLocaleString() + '</span></div>' +
                '<div class="directed-pack-track"><div class="directed-pack-fill directed-pack-fill--' + theme + '" style="width:' + pct + '%"></div></div></div>';
        }).join('');
    }

    async function loadApiKeys(silent = false) {
        const container = document.getElementById('apiKeysList');
        if (container && !silent) {
            container.innerHTML = renderApiKeysSkeleton(2);
        }
        try {
            const response = await fetch('/api/api_keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'get_keys' })
            });
            const result = await response.json();

            if (result.success) {
                if (result.keys && result.keys.length > 0) {
                    const firstActive = result.keys.find(k => k.is_active !== 0 && k.is_active !== false);
                    if (firstActive && firstActive.api_key_full) {
                        firstApiKey = firstActive.api_key_full;
                    } else if (result.keys[0].api_key_full) {
                        firstApiKey = result.keys[0].api_key_full;
                    }
                }
                updateDocumentationKeys();
                displayApiKeys(result.keys);
            } else {
                // 协议未确认：立即弹出平台协议引导
                if (result.message && String(result.message).indexOf('请先完成平台协议确认') !== -1) {
                    if (container) container.innerHTML = '<div class="empty-placeholder" style="text-align: center; padding: 40px; color: var(--text-secondary);"><i class="fas fa-exclamation-triangle"></i> 请先完成平台协议确认</div>';
                    showAgreementWizard({
                        initialStep: 0,
                        onComplete: () => loadApiKeys(false)
                    });
                    return;
                }
                if (container) {
                    const errMsg = escapeHtml(result.message || '加载失败');
                    container.innerHTML = '<div class="empty-placeholder" style="text-align: center; padding: 40px; color: var(--text-secondary);"><i class="fas fa-exclamation-triangle"></i> ' + errMsg + '</div>';
                }
            }
        } catch (error) {
            console.error('Load keys error:', error);
            if (container) {
                container.innerHTML = '<div class="empty-placeholder" style="text-align: center; padding: 40px; color: var(--text-secondary);"><i class="fas fa-exclamation-triangle"></i> 加载失败，请刷新重试</div>';
            }
        }
    }

    function displayApiKeys(keys) {
        const container = document.getElementById('apiKeysList');
        if (!container) return;

        const keysCountEl = document.getElementById('keysCountText');
        if (keysCountEl) keysCountEl.textContent = keys && keys.length > 0 ? `${keys.length} / 10` : '0 / 10';

        if (!keys || keys.length === 0) {
            container.innerHTML = `
                <div class="empty-placeholder" style="text-align: center; padding: 40px; color: var(--text-secondary);">
                    <i class="fas fa-key" style="font-size: 48px; margin-bottom: 16px; opacity: 0.5;"></i>
                    <p>暂无 API 密钥</p>
                    <p style="font-size: 14px;">创建您的第一个 API 密钥以开始使用</p>
                </div>
            `;
            return;
        }

        // 事件委托（绑定在容器上，见 bindApiKeysListEvents）：
        // 不再使用内联 onclick/onchange，密钥名通过 data-id 反查，避免字符串拼进 JS 上下文
        container.innerHTML = keys.map(key => {
            const isActive = key.is_active !== 0 && key.is_active !== false;
            const keyId = parseInt(key.id, 10) || 0;
            return `
            <div class="api-key-item ${isActive ? '' : 'api-key-item--disabled'}" data-key-id="${keyId}">
                <div class="api-key-info">
                    <div class="api-key-name">${escapeHtml(key.key_name)}${!isActive ? '<span class="api-key-badge-disabled">已禁用</span>' : ''}</div>
                    <div class="api-key-value">
                        <code class="api-key-code">${escapeHtml(key.api_key_full || key.api_key_masked)}</code>
                    </div>
                    <div class="api-key-meta">
                        <span><i class="fas fa-calendar"></i> 创建于：${formatDate(key.created_at)}</span>
                        ${key.last_used_at ? `<span><i class="fas fa-clock"></i> 最后使用：${formatDate(key.last_used_at)}</span>` : ''}
                        ${key.usage ? `<span><i class="fas fa-chart-bar"></i> 30天用量：${fmtNum(key.usage.total_tokens)} tokens · ${fmtNum(key.usage.request_count)} 次</span>` : ''}
                    </div>
                </div>
                <div class="api-key-actions">
                    <label class="toggle-switch" title="${isActive ? '启用状态' : '已禁用'}">
                        <input type="checkbox" ${isActive ? 'checked' : ''} data-key-action="toggle">
                        <span class="toggle-slider"></span>
                    </label>
                    <button class="btn btn-danger btn-sm" data-key-action="delete">
                        <i class="fas fa-trash"></i> 删除
                    </button>
                </div>
            </div>
        `;
        }).join('');

        lastKeysData = keys;
    }

    // 密钥列表数据缓存（委托回调按 id 反查名称等信息）
    let lastKeysData = [];

    function bindApiKeysListEvents() {
        const container = document.getElementById('apiKeysList');
        if (!container) return;

        container.addEventListener('change', function (e) {
            const input = e.target.closest('input[data-key-action="toggle"]');
            if (!input) return;
            const item = input.closest('.api-key-item');
            const keyId = item ? parseInt(item.getAttribute('data-key-id'), 10) : 0;
            if (keyId) window.toggleApiKey(keyId, input);
        });

        container.addEventListener('click', function (e) {
            const btn = e.target.closest('[data-key-action="delete"]');
            if (!btn) return;
            const item = btn.closest('.api-key-item');
            const keyId = item ? parseInt(item.getAttribute('data-key-id'), 10) : 0;
            if (!keyId) return;
            const key = lastKeysData.find(k => parseInt(k.id, 10) === keyId);
            window.deleteApiKey(keyId, key ? String(key.key_name || '') : '');
        });
    }
    bindApiKeysListEvents();

    // 按需加载 pow.js 并求解（仅创建 Key 触警时加载）。
    // Worker 版求解器不阻塞主线程；带 ASSET_VERSION 允许浏览器缓存，重复创建不再反复下载
    async function loadAndSolvePoW(nonce, difficulty) {
        if (typeof window.solvePoW === 'function') {
            return await window.solvePoW(nonce, difficulty);
        }
        return new Promise(function (resolve, reject) {
            var s = document.createElement('script');
            var v = (typeof window.__ASSET_VERSION === 'string' && window.__ASSET_VERSION) ? window.__ASSET_VERSION : '1';
            s.src = 'js/pow.js?v=' + v;  // 相对当前页 /api_platform/
            s.onload = function () {
                if (typeof window.solvePoW === 'function') {
                    window.solvePoW(nonce, difficulty).then(resolve).catch(reject);
                } else {
                    resolve('');
                }
            };
            s.onerror = function () { resolve(''); };
            document.head.appendChild(s);
        });
    }

    async function createApiKey() {
        const keyName = document.getElementById('keyName').value.trim();
        if (!keyName) {
            BLModal.alert({ title: '名称必填', message: '请输入 API 密钥的名称。', type: 'warning' });
            return;
        }

        const btn = document.getElementById('confirmCreateBtn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 创建中...';

        try {
            let response = await doCreateKeyRequest(keyName, {});
            let result = await response.json();

            // PoW 挑战：按需加载 pow.js，求解后重试（用户仅见“创建中...”）
            // 后端用 200 返回挑战（避免 nginx error_page 403 吞掉 JSON）；仅按 code 判断即可
            if (result.code === 'ERR_SAMPLE_NEEDS_POW') {
                const powNonce = result.pow_nonce || '';
                const powDiff = result.pow_difficulty ?? 3;
                if (!powNonce) {
                    BLModal.alert({ title: '验证未通过', message: result.message || '请完成验证后重试。', type: 'warning' });
                    return;
                }
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 验证中...';
                try {
                    var powResult = await loadAndSolvePoW(powNonce, powDiff);
                    if (!powResult) {
                        BLModal.alert({ title: '验证失败', message: '安全验证未通过，请重试。', type: 'danger' });
                        return;
                    }
                    response = await doCreateKeyRequest(keyName, {
                        'X-PoW-Result': powResult,
                        'X-PoW-Nonce': powNonce
                    });
                    result = await response.json();
                } catch (e) {
                    BLModal.alert({ title: '验证失败', message: '安全验证过程异常，请重试。', type: 'danger' });
                    return;
                }
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 创建中...';
            }

            if (result.success) {
                if (firstApiKey === 'YOUR_API_KEY') {
                    firstApiKey = result.api_key;
                    updateDocumentationKeys();
                }

                document.getElementById('createKeyModal').classList.remove('active');
                document.getElementById('newApiKey').textContent = result.api_key;
                document.getElementById('showKeyModal').classList.add('active');

                // 静默刷新列表，避免全屏加载效果
                loadApiKeys(true);
                loadStats();
            } else {
                // 协议未确认：立即弹出平台协议引导，完成后再重试创建
                if (result.message && String(result.message).indexOf('请先完成平台协议确认') !== -1) {
                    btn.disabled = false;
                    btn.innerHTML = '创建密钥';
                    const savedKeyName = keyName;
                    document.getElementById('createKeyModal').classList.remove('active');
                    showAgreementWizard({
                        initialStep: 0,
                        onComplete: () => {
                            document.getElementById('keyName').value = savedKeyName;
                            createKeyModal.classList.add('active');
                            document.getElementById('keyName').focus();
                            createApiKey();
                        }
                    });
                    return;
                }
                BLModal.alert({ title: '创建失败', message: '创建 API 密钥失败：' + (result.message || '未知错误'), type: 'danger' });
            }
        } catch (error) {
            console.error('Create key error:', error);
            BLModal.alert({ title: '网络错误', message: '网络连接异常，请检查网络后重试。', type: 'danger' });
        } finally {
            btn.disabled = false;
            btn.innerHTML = '创建密钥';
        }
    }

    function doCreateKeyRequest(keyName, extraHeaders) {
        const headers = Object.assign({ 'Content-Type': 'application/json' }, extraHeaders);
        return fetch('/api/api_keys', {
            method: 'POST',
            headers: headers,
            credentials: 'include',
            body: JSON.stringify({ action: 'create_key', key_name: keyName })
        });
    }

    window.toggleApiKey = async function (keyId, checkboxEl) {
        const keyItem = document.querySelector(`.api-key-item[data-key-id="${keyId}"]`);
        var prevChecked = !checkboxEl.checked;
        checkboxEl.disabled = true;

        try {
            const response = await fetch('/api/api_keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'toggle_key', key_id: keyId })
            });
            const result = await response.json();

            if (result.success) {
                keyItem?.classList.toggle('api-key-item--disabled', !result.is_active);
                const badge = keyItem?.querySelector('.api-key-badge-disabled');
                const labelEl = keyItem?.querySelector('.toggle-switch');
                if (result.is_active) {
                    badge?.remove();
                    if (labelEl) labelEl.title = '启用状态';
                } else {
                    if (!badge) {
                        const nameEl = keyItem?.querySelector('.api-key-name');
                        if (nameEl) {
                            const span = document.createElement('span');
                            span.className = 'api-key-badge-disabled';
                            span.textContent = '已禁用';
                            nameEl.appendChild(span);
                        }
                    }
                    if (labelEl) labelEl.title = '已禁用';
                }
                loadStats();
            } else {
                checkboxEl.checked = prevChecked;
                if (result.code === 'TOGGLE_COOLDOWN') {
                    showToast(result.message || '操作频繁，请稍后重试');
                } else {
                    BLModal.alert({ title: '操作失败', message: result.message || '请稍后重试。', type: 'danger' });
                }
            }
        } catch (error) {
            console.error('Toggle key error:', error);
            checkboxEl.checked = prevChecked;
            BLModal.alert({ title: '网络错误', message: '网络连接异常，请检查网络后重试。', type: 'danger' });
        } finally {
            checkboxEl.disabled = false;
        }
    };

    window.deleteApiKey = async function (keyId, keyName) {
        const ok = await BLModal.confirm({
            title: '删除 API 密钥',
            messageHTML: '即将永久删除密钥 <strong>"' + (keyName || '').replace(/[&<>"']/g, function (c) {
                return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
            }) + '"</strong>，使用该密钥的应用将立刻失去访问权限。此操作无法撤销，请谨慎确认。',
            confirmText: '永久删除',
            cancelText: '取消',
            type: 'danger',
            danger: true
        });
        if (!ok) return;

        const keyItem = document.querySelector(`.api-key-item[data-key-id="${keyId}"]`);
        const keysCountEl = document.getElementById('keysCountText');
        const prevCountText = keysCountEl?.textContent;

        if (keyItem) {
            keyItem.style.opacity = '0.5';
            keyItem.style.pointerEvents = 'none';
        }

        try {
            const response = await fetch('/api/api_keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'delete_key', key_id: keyId })
            });
            const result = await response.json();

            if (result.success) {
                keyItem?.remove();
                const match = prevCountText?.match(/(\d+)\s*\/\s*10/);
                if (keysCountEl && match) {
                    const n = Math.max(0, parseInt(match[1], 10) - 1);
                    keysCountEl.textContent = `${n} / 10`;
                }
                loadStats();
            } else {
                keyItem && (keyItem.style.opacity = '', keyItem.style.pointerEvents = '');
                BLModal.alert({ title: '删除失败', message: '删除 API 密钥失败：' + (result.message || '未知错误'), type: 'danger' });
            }
        } catch (error) {
            console.error('Delete key error:', error);
            keyItem && (keyItem.style.opacity = '', keyItem.style.pointerEvents = '');
            BLModal.alert({ title: '网络错误', message: '网络连接异常，请检查网络后重试。', type: 'danger' });
        }
    };

    async function loadModels() {
        try {
            const response = await fetch('/api/getmodels');
            const result = await response.json();

            if (result.success) {
                const userLevel = result.user_api_level || 1;
                const available = [];
                const unavailable = [];

                if (result.models) {
                    result.models.forEach(model => {
                        const level = model.access_level || 1;
                        if (userLevel >= level) available.push(model);
                        else unavailable.push({ ...model, required_level: level });
                    });
                }

                chatModelsSnapshot = {
                    updated: Date.now(),
                    userLevel,
                    available: available.map(m => {
                        var raw = String(m.description || '').replace(/\s+/g, ' ').trim();
                        var desc = raw.length > 220 ? raw.slice(0, 217) + '...' : raw;
                        var ctx = m.context_length != null ? Number(m.context_length) : (m.max_tokens != null ? Number(m.max_tokens) : 0);
                        return {
                            id: String(m.id || m.model_id || '').trim(),
                            name: String(m.display_name || m.name || m.modelname || '').trim(),
                            type: String(m.model_type || ''),
                            desc,
                            ctx: ctx > 0 ? ctx : 0
                        };
                    }).filter(m => m.id),
                    unavailableCount: unavailable.length
                };

                const hotModels = available.filter(m => m.model_type === 'hot');
                const dialogueAvailable = available.filter(m => m.model_type !== 'hot');
                const hotCard = document.getElementById('hotModelsCard');
                if (hotCard) {
                    if (hotModels.length) {
                        hotCard.style.display = '';
                        displayModels(hotModels, 'hotModelsList');
                    } else {
                        hotCard.style.display = 'none';
                    }
                }

                const allTextModels = [
                    ...dialogueAvailable.filter(m => m.model_type !== 'cc'),
                    ...unavailable.map(m => ({ ...m, _planUnavailable: true }))
                ];
                displayModels(allTextModels, 'modelsList');
                if (result.claude_code_models) displayModels(result.claude_code_models, 'claudeCodeModelsList', true);
                if (result.embedding_models) displayModels(result.embedding_models, 'embeddingModelsList', false, true);
                displayImageModels(result.image_models || [], 'imageModelsList');
                displayVideoModels(result.video_models || [], 'videoModelsList');
                displayModels(result.ocr_models || [], 'ocrModelsList');

                // 隐藏独立的不可用模型卡片
                const card = document.getElementById('unavailableModelsCard');
                if (card) card.style.display = 'none';
            }
        } catch (error) {
            console.error('Load models error:', error);
        }
    }

    function displayImageModels(models, containerId) {
        const container = document.getElementById(containerId);
        if (!container) return;

        if (!models || models.length === 0) {
            container.innerHTML = '<div class="empty-placeholder">No models available</div>';
            return;
        }

        // 不可用模型（enabled:false）排在最后
        const sorted = [...models].sort((a, b) => (a.enabled === false ? 1 : 0) - (b.enabled === false ? 1 : 0));

        container.innerHTML = sorted.map(model => {
            const isDisabled = model.enabled === false;
            const extraClass = 'model-item image-model-item' + (isDisabled ? ' disabled model-unavailable' : '');
            const statusText = isDisabled ? '当前计划不适用' : 'Active';
            const copyAttr = escapeHtmlAttr(String(model.id || '').trim());

            return `
                <div class="${extraClass}">
                    <div class="model-header">
                        <div class="model-title">
                            <h3 class="model-name">
                                ${escapeHtml(model.modelname)}
                                ${isDisabled ? '<span class="model-badge model-badge-unavailable">停用</span>' : ''}
                            </h3>
                        </div>
                        <div class="model-status">
                            <span class="model-status-dot"></span>
                            <span>${statusText}</span>
                        </div>
                    </div>
                    <div class="model-id">
                        <code>${escapeHtml(model.id)}</code>
                        <button type="button" class="model-copy-btn" data-copy="${copyAttr}" title="复制 Model ID">
                            <i class="far fa-copy"></i>
                        </button>
                    </div>
                    <div class="model-description">${escapeHtml(model.description)}</div>
                    ${renderModalities('image', model)}
                    <div class="model-meta">
                        <div class="model-meta-item">
                            <i class="fas fa-building"></i>
                            <span>${escapeHtml(model.provider)}</span>
                        </div>
                        <div class="model-meta-item">
                            <i class="fas fa-coins"></i>
                            <span>${model.tokens_per_generation || 0} tokens/次</span>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
    }

    function displayVideoModels(models, containerId) {
        const container = document.getElementById(containerId);
        if (!container) return;

        if (!models || models.length === 0) {
            container.innerHTML = '<div class="empty-placeholder">No models available</div>';
            return;
        }

        const sorted = [...models].sort((a, b) => (a.enabled === false ? 1 : 0) - (b.enabled === false ? 1 : 0));

        container.innerHTML = sorted.map(model => {
            const isDisabled = model.enabled === false;
            const extraClass = 'model-item video-model-item' + (isDisabled ? ' disabled model-unavailable' : '');
            const statusText = isDisabled ? '当前计划不适用' : 'Active';
            const copyAttr = escapeHtmlAttr(String(model.id || '').trim());
            const tps = model.tokens_per_second || 10000;
            const defSec = model.default_seconds || 5;
            const billingHint = `${tps.toLocaleString()} tokens/秒 · 默认约 ${defSec}s`;

            return `
                <div class="${extraClass}">
                    <div class="model-header">
                        <div class="model-title">
                            <h3 class="model-name">
                                ${escapeHtml(model.modelname)}
                                ${isDisabled ? '<span class="model-badge model-badge-unavailable">停用</span>' : ''}
                            </h3>
                        </div>
                        <div class="model-status">
                            <span class="model-status-dot"></span>
                            <span>${statusText}</span>
                        </div>
                    </div>
                    <div class="model-id">
                        <code>${escapeHtml(model.id)}</code>
                        <button type="button" class="model-copy-btn" data-copy="${copyAttr}" title="复制 Model ID">
                            <i class="far fa-copy"></i>
                        </button>
                    </div>
                    <div class="model-description">${escapeHtml(model.description)}</div>
                    <div class="model-capabilities">
                        <span class="vm-cap-chip vm-cap-chip--t2v"><i class="fas fa-film"></i>文生视频</span>
                        <span class="vm-cap-chip vm-cap-chip--i2v"><i class="fas fa-image"></i>图生视频</span>
                        <span class="vm-cap-chip vm-cap-chip--m2v"><i class="fas fa-images"></i>多图视频</span>
                    </div>
                    <div class="model-meta model-meta--video">
                        <div class="model-meta-item">
                            <i class="fas fa-building"></i>
                            <span>${escapeHtml(model.provider)}</span>
                        </div>
                        <div class="model-meta-item">
                            <i class="fas fa-coins"></i>
                            <span>${billingHint}</span>
                        </div>
                        <div class="model-meta-item">
                            <i class="fas fa-clock"></i>
                            <span>异步任务 · 轮询查询</span>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
    }

    function renderModelIdBlock(model) {
        const primary = String(model.id || '').trim();
        const primaryEsc = escapeHtml(primary);
        const aliases = Array.isArray(model.aliases)
            ? model.aliases.map((a) => String(a).trim()).filter((a) => a && a !== primary)
            : [];
        const copyAttr = escapeHtmlAttr(primary);
        if (aliases.length === 0) {
            return `
                    <div class="model-id">
                        <code>${primaryEsc}</code>
                        <button type="button" class="model-copy-btn" data-copy="${copyAttr}" title="复制 Model ID">
                            <i class="far fa-copy"></i>
                        </button>
                    </div>`;
        }
        const mergedHtml = [primary, ...aliases]
            .map((part, i) => {
                const esc = escapeHtml(part);
                return i === 0 ? esc : `<span class="model-id-merge-sep"> / </span>${esc}`;
            })
            .join('');
        return `
                    <div class="model-id model-id-merged">
                        <code>${mergedHtml}</code>
                        <button type="button" class="model-copy-btn" data-copy="${copyAttr}" title="复制 Model ID（主名称）">
                            <i class="far fa-copy"></i>
                        </button>
                    </div>`;
    }

    // 模态能力图标：极简线性内联 SVG（stroke-width 1.5，扁平、统一节奏）
    const MODALITY_ICONS = {
        text:   '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M5 6h10M10 6v9M8 15h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
        vision: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="2.75" y="4.75" width="14.5" height="10.5" rx="1.6" stroke="currentColor" stroke-width="1.4"/><circle cx="7.25" cy="8.75" r="1.15" fill="currentColor"/><path d="M3.25 13.5l3.75-3.4 2.75 2.5 2.6-2.3 4.4 3.9" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        audio:  '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M10 3.5v13M7 6.5v7M13 6.5v7M4.25 9v2M15.75 9v2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
        video:  '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="2.75" y="5.5" width="10.5" height="9" rx="1.5" stroke="currentColor" stroke-width="1.4"/><path d="M13.25 8.5l4.2-2.25v7.5l-4.2-2.25v-3z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
        doc:    '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M5.5 3h6L15.75 7v9.5a1.25 1.25 0 0 1-1.25 1.25H5.5A1.25 1.25 0 0 1 4.25 16.25V4.25A1.25 1.25 0 0 1 5.5 3z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M11.5 3.25v3.5h4" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M7 11h6M7 14h4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
        vector: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="4.25" cy="10" r="1.85" fill="currentColor"/><circle cx="10" cy="10" r="1.85" fill="currentColor"/><circle cx="15.75" cy="10" r="1.85" fill="currentColor"/></svg>',
        arrow:  '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M4 10h11M11.5 6.25L15.25 10l-3.75 3.75" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    };

    // 推理强度（reasoning effort）图标
    const REASONING_GAUGE_ICON = '<svg class="rs-gauge" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M3.6 14.6a7 7 0 1 1 12.8 0" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M10 11.4l3.1-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="10" cy="11.7" r="1.15" fill="currentColor"/></svg>';

    // 解析 multimodal 字段（兼容 string / array / object 三种形态）
    function parseMultimodalCaps(mm) {
        const caps = { vision: false, audio: false, video: false };
        if (mm == null) return caps;
        if (typeof mm === 'string') {
            const k = mm.trim().toLowerCase();
            if (k === 'vision' || k === 'audio' || k === 'video') caps[k] = true;
        } else if (Array.isArray(mm)) {
            mm.forEach((cap) => {
                if (typeof cap !== 'string') return;
                const key = cap.trim().toLowerCase();
                if (key === 'vision' || key === 'audio' || key === 'video') caps[key] = true;
            });
        } else if (typeof mm === 'object') {
            caps.vision = !!mm.vision;
            caps.audio = !!mm.audio;
            caps.video = !!mm.video;
        }
        return caps;
    }

    /**
     * 渲染模态能力区块（输入 → 输出）
     * @param {string} kind  'chat' | 'embedding' | 'image' | 'ocr'
     * @param {object} model
     */
    function renderModalities(kind, model) {
        const caps = parseMultimodalCaps(model && model.multimodal);
        let inputs;
        let outKey;
        let outLabel;

        if (kind === 'image') {
            inputs = [
                { key: 'text', label: '文本', active: true },
                { key: 'vision', label: '图像', active: true }
            ];
            outKey = 'vision'; outLabel = '图像';
        } else if (kind === 'ocr') {
            inputs = [
                { key: 'vision', label: '图像', active: true },
                { key: 'doc', label: '文档', active: true }
            ];
            outKey = 'text'; outLabel = '文本';
        } else if (kind === 'embedding') {
            inputs = [
                { key: 'text', label: '文本', active: true },
                { key: 'vision', label: '图像', active: caps.vision }
            ];
            outKey = 'vector'; outLabel = '向量';
        } else {
            inputs = [
                { key: 'text', label: '文本', active: true },
                { key: 'vision', label: '图像', active: caps.vision },
                { key: 'audio', label: '音频', active: caps.audio },
                { key: 'video', label: '视频', active: caps.video }
            ];
            outKey = 'text'; outLabel = '文本';
        }

        const chips = inputs
            .filter((it) => it.active)
            .map((it) => `<span class="mc-chip mc-chip--${it.key}" title="${it.label}">${MODALITY_ICONS[it.key]}</span>`)
            .join('');

        return `
                    <div class="model-modalities" aria-label="模态能力">
                        <span class="mc-group">${chips}</span>
                        <span class="mc-arrow" aria-hidden="true">${MODALITY_ICONS.arrow}</span>
                        <span class="mc-group mc-group--out">
                            <span class="mc-chip mc-chip--${outKey} mc-chip--out" title="${outLabel}">${MODALITY_ICONS[outKey]}</span>
                        </span>
                    </div>`;
    }

    // 渲染"推理强度"：仅当模型声明 reasoning_modes 时展示，纯文字罗列支持的档位（不可交互、非选择控件）
    function renderReasoning(model) {
        const modes = (model && Array.isArray(model.reasoning_modes))
            ? model.reasoning_modes.filter(m => m && typeof m.value === 'string' && m.value !== '')
            : [];
        if (modes.length === 0) return '';
        const text = modes.map(m => escapeHtml(m.label || m.value)).join(' / ');
        return `
                    <div class="model-reasoning">
                        <span class="reasoning-label" title="推理强度 (reasoning effort)">
                            ${REASONING_GAUGE_ICON}
                            <span class="reasoning-label-text">推理强度</span>
                        </span>
                        <span class="reasoning-values">${text}</span>
                    </div>`;
    }

    function displayModels(models, containerId, isClaude = false, isEmbedding = false) {
        const container = document.getElementById(containerId);
        if (!container) return;

        if (!models || models.length === 0) {
            container.innerHTML = '<div class="empty-placeholder">No models available</div>';
            return;
        }

        container.innerHTML = models.map(model => {
            const isUnavailable = model._planUnavailable === true;
            const billingBadge = getBillingBadge(model.billing_coefficient);
            const claudeLogo = (isEmbedding && model.model_type === 'cc') ?
                '<img src="https://registry.npmmirror.com/@lobehub/icons-static-png/1.74.0/files/dark/claude-color.png" alt="Claude Code" class="model-badge" style="height: 16px; width: auto; margin-left: 8px;">' : '';

            const aLvl = model.access_level || 1;
            const lvlClass = aLvl >= 3 ? ' model-tier-enterprise' : aLvl >= 2 ? ' model-tier-pro' : '';
            const lvlBadge = aLvl >= 3 ? '<span class="model-badge model-badge-enterprise">Enterprise</span>' : aLvl >= 2 ? '<span class="model-badge model-badge-pro">Pro</span>' : '';
            const itemClass =
                'model-item' +
                lvlClass +
                (isUnavailable ? ' model-unavailable' : '');
            const statusText = isUnavailable ? '当前计划不适用' : 'Active';
            const usage7d = model.usage_7d;
            const usage7dHtml = (usage7d && (usage7d.calls > 0 || usage7d.total_tokens > 0))
                ? `<div class="model-usage" title="全站近 7 天调用统计">
                            <span class="model-usage-label"><i class="fas fa-chart-line" aria-hidden="true"></i> 近 7 天</span>
                            <span class="model-usage-metric"><strong>${fmtNum(usage7d.calls)}</strong> 次调用</span>
                            <span class="model-usage-metric"><strong>${fmtNum(usage7d.total_tokens)}</strong> tokens</span>
                        </div>`
                : '';

            // 推断模态种类：embedding > ocr > chat
            const kind = isEmbedding
                ? 'embedding'
                : (model.model_type === 'ocr' ? 'ocr' : (model.model_type === 'embedding' ? 'embedding' : 'chat'));

            return `
                <div class="${itemClass}">
                    <div class="model-header">
                        <div class="model-title">
                            <h3 class="model-name">
                                ${escapeHtml(model.modelname)}
                                ${billingBadge}
                                ${lvlBadge}
                                ${claudeLogo}
                                ${isEmbedding ? '<span class="model-badge model-badge-embedding">Embedding</span>' : ''}
                            </h3>
                        </div>
                        <div class="model-status">
                            <span class="model-status-dot"></span>
                            <span>${statusText}</span>
                        </div>
                    </div>
                    ${renderModelIdBlock(model)}
                    <div class="model-description">${escapeHtml(model.description)}</div>
                    ${renderModalities(kind, model)}
                    ${renderReasoning(model)}
                    <div class="model-footer">
                        <div class="model-meta model-meta--specs">
                            <div class="model-meta-item model-meta-provider" title="${escapeHtmlAttr(model.provider)}">
                                <span class="model-meta-label"><i class="fas fa-building" aria-hidden="true"></i> 提供方</span>
                                <span class="model-meta-value">${escapeHtml(model.provider)}</span>
                            </div>
                            <div class="model-meta-item" title="上下文窗口长度">
                                <span class="model-meta-label"><i class="fas fa-layer-group" aria-hidden="true"></i> 上下文</span>
                                <span class="model-meta-value">${formatNumber(model.context_length || model.max_tokens)}</span>
                            </div>
                            <div class="model-meta-item" title="最大生成令牌数">
                                <span class="model-meta-label"><i class="fas fa-arrow-right" aria-hidden="true"></i> 最大输出</span>
                                <span class="model-meta-value">${formatNumber(model.max_tokens)}</span>
                            </div>
                        </div>
                        ${usage7dHtml}
                    </div>
                </div>
            `;
        }).join('');
    }

    function displayUnavailableModels(models) {
        const container = document.getElementById('unavailableModelsList');
        const card = document.getElementById('unavailableModelsCard');
        if (!container || !card) return;

        card.style.display = 'block';
        const levelNames = { 1: 'Personal', 2: 'Professional', 3: 'Enterprise' };

        container.innerHTML = models.map(model => {
            const billingBadge = getBillingBadge(model.billing_coefficient);

            return `
                <div class="model-item unavailable-model-item" style="opacity: 0.7;">
                    <div class="model-header">
                        <div class="model-title">
                            <h3 class="model-name">
                                ${escapeHtml(model.modelname)}
                                ${billingBadge}
                            </h3>
                        </div>
                        <div class="model-status" style="color: var(--text-secondary); background: #f1f5f9;">
                            <i class="fas fa-lock"></i> Locked
                        </div>
                    </div>
                    <div class="model-id">
                        <code>${escapeHtml(model.id)}</code>
                    </div>
                    <div class="model-required-level" style="margin-top: 12px; color: var(--danger-color); font-size: 0.85rem;">
                        <i class="fas fa-key"></i> Requires ${levelNames[model.required_level] || 'Higher'} Plan
                    </div>
                </div>
            `;
        }).join('');
    }

    // --- Helper Functions ---

    /**
     * 渲染 API Key 列表骨架（与 .api-key-item 形状一致）
     * @param {number} count 骨架行数（默认 2）
     */
    function renderApiKeysSkeleton(count) {
        count = count || 2;
        var rows = '';
        for (var i = 0; i < count; i++) {
            rows +=
                '<div class="skel-key-row">' +
                '<div class="skr-info">' +
                '<span class="skel skel-line skel-line--md skel-w-30"></span>' +
                '<span class="skel skel-line skel-line--lg skel-w-90"></span>' +
                '<span class="skel skel-line skel-line--sm skel-w-50"></span>' +
                '</div>' +
                '<div class="skr-actions">' +
                '<span class="skel" style="width:36px;height:20px;border-radius:999px;"></span>' +
                '<span class="skel skel-btn" style="width:72px;"></span>' +
                '</div>' +
                '</div>';
        }
        return '<div class="skel-keys" aria-hidden="true">' + rows + '</div>';
    }

    window.__apiPlatformSkeleton = { keys: renderApiKeysSkeleton };

    function setText(id, text) {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    }

    function escapeHtml(text) {
        if (!text) return '';
        return text.replace(/[&<>"']/g, m => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
        })[m]);
    }

    function escapeHtmlAttr(text) {
        if (!text) return '';
        return String(text).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#039;').replace(/</g, '&lt;');
    }

    function formatDate(dateString) {
        if (!dateString) return '-';
        return new Date(dateString).toLocaleString('zh-CN', {
            year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
        });
    }

    function formatNumber(num) {
        return num ? num.toLocaleString() : '0';
    }

    function fmtNum(n) {
        n = Number(n) || 0;
        if (n >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
        if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
        if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
        return String(n);
    }

    function getBillingBadge(coeff) {
        if (coeff === undefined || coeff === 1) return '';
        if (coeff === 0) return '<span class="model-badge model-badge-free">Free</span>';
        if (coeff < 1) return `<span class="model-badge model-badge-discount">-${Math.round((1 - coeff) * 100)}%</span>`;
        return `<span class="model-badge model-badge-premium">${Math.round(coeff * 100)}%</span>`;
    }

    function updateDocumentationKeys() {
        document.querySelectorAll('.api-key-highlight').forEach(el => el.textContent = firstApiKey);
    }

    // === 文档懒加载：首次进入 documentation section 时才拉取 29KB 文档片段 ===
    let docLoadPromise = null;

    function ensureDocumentationLoaded() {
        const mount = document.getElementById('documentationLazyMount');
        if (!mount) return Promise.resolve(); // 已加载或服务端内嵌模式
        if (docLoadPromise) return docLoadPromise;
        const src = mount.getAttribute('data-doc-src') || 'components/documentation';
        docLoadPromise = fetch(src, { credentials: 'same-origin' })
            .then(function (r) {
                if (!r.ok) throw new Error('doc fetch failed: ' + r.status);
                return r.text();
            })
            .then(function (html) {
                const section = document.getElementById('documentation');
                if (section) {
                    section.innerHTML = html;
                    updateDocumentationKeys(); // 注入后重放 API 密钥占位
                }
            })
            .catch(function (e) {
                console.error('文档加载失败:', e);
                docLoadPromise = null; // 允许下次进入时重试
                const m = document.getElementById('documentationLazyMount');
                if (m) m.innerHTML = '<div style="padding:48px 0;text-align:center;color:#94a3b8;">文档加载失败，请刷新页面重试</div>';
            });
        return docLoadPromise;
    }

    // 文档片段为懒加载；内联按钮复用此函数，无需逐元素绑定或测量高度。
    window.toggleDocumentationExamples = function (button) {
        const panel = document.getElementById(button.getAttribute('aria-controls'));
        if (!panel) return;
        const expanded = button.getAttribute('aria-expanded') !== 'true';
        button.setAttribute('aria-expanded', String(expanded));
        panel.setAttribute('aria-hidden', String(!expanded));
        panel.toggleAttribute('inert', !expanded);
    };

    // Copy Functions
    window.copyText = function (text, btn) {
        var targetBtn = btn && btn.nodeType === 1 ? btn : (typeof event !== 'undefined' && event.target && event.target.closest('button'));
        copyTextToClipboard(text, targetBtn);
    };

    window.copyEndpoint = function (url) {
        copyTextToClipboard(url || 'https://bailucode.com/openapi/v1/chat/completions', event.target.closest('.copy-btn'));
    };

    window.copyNewKey = function () {
        copyTextToClipboard(document.getElementById('newApiKey').textContent, event.target.closest('.copy-btn'));
    };

    window.copyCurlExample = function (type) {
        const idMap = {
            'models': 'curlModels', 'stream': 'curlStream', 'nonstream': 'curlNonStream',
            'systemprompt': 'curlSystemPrompt', 'multiturn': 'curlMultiTurn',
            'agentcall': 'curlAgentCall', 'agentlist': 'curlAgentList',
            'embeddings': 'curlEmbeddings', 'codeembeddings': 'curlCodeEmbeddings',
            'apexembeddings': 'curlApexEmbeddings',
            'imagegeneration': 'curlImageGeneration', 'imageedit': 'curlImageEdit',
            'imageremove': 'curlImageRemove', 'ocrurl': 'curlOcrUrl', 'ocrbase64': 'curlOcrBase64',
            'claudecodesettings': 'curlClaudeCodeSettings', 'codexsettings': 'curlCodexSettings', 'anthropic': 'curlAnthropic'
        };
        const el = document.getElementById(idMap[type]);
        if (el) copyTextToClipboard(el.textContent.trim(), event.target.closest('.curl-copy-btn'));
    };

    function copyTextToClipboard(text, btn) {
        const originalHTML = btn ? btn.innerHTML : null;
        const success = () => {
            if (!btn) return;
            btn.innerHTML = '<i class="fas fa-check"></i>';
            btn.style.color = 'var(--success-color)';
            setTimeout(() => {
                btn.innerHTML = originalHTML;
                btn.style.color = '';
            }, 2000);
        };

        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(success).catch(() => fallbackCopy(text, btn, success));
        } else {
            fallbackCopy(text, btn, success);
        }
    }

    function fallbackCopy(text, btn, successCallback) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        try {
            if (document.execCommand('copy')) successCallback();
        } catch (e) {
            prompt('Copy failed, please copy manually:', text);
        }
        document.body.removeChild(ta);
    }

    function openDocGuideLightbox(src) {
        if (!src) return;
        const lb = document.createElement('div');
        lb.className = 'notif-img-lightbox';
        const im = document.createElement('img');
        im.alt = '';
        im.src = src;
        lb.appendChild(im);
        function close() {
            lb.classList.remove('active');
            setTimeout(() => { lb.remove(); document.removeEventListener('keydown', onEsc, true); }, 200);
        }
        function onEsc(e) {
            if (e.key === 'Escape') { e.stopImmediatePropagation(); e.preventDefault(); close(); }
        }
        document.body.appendChild(lb);
        requestAnimationFrame(() => { lb.classList.add('active'); });
        lb.addEventListener('click', close);
        document.addEventListener('keydown', onEsc, true);
    }

    document.addEventListener('click', function (ev) {
        const btn = ev.target.closest('.model-copy-btn');
        if (btn && btn.hasAttribute('data-copy')) {
            ev.preventDefault();
            copyTextToClipboard(btn.getAttribute('data-copy'), btn);
            return;
        }
        const zimg = ev.target.closest('.doc-guide-zoomimg');
        if (zimg && document.getElementById('documentation')?.contains(zimg)) {
            ev.preventDefault();
            openDocGuideLightbox(zimg.currentSrc || zimg.src);
        }
    });

    // === 邀请注册系统 ===
    let inviteDataLoaded = false;
    let inviteCreateTsWidgetId = null;
    let _inviteRewards = [];
    let _inviteActiveCodes = 0;

    function initInviteSection() {
        if (inviteDataLoaded) return;
        inviteDataLoaded = true;
        loadInviteData();
    }

    async function loadInviteData() {
        const container = document.getElementById('inviteContent');
        if (!container) return;
        try {
            const [codesRes, rewardsRes] = await Promise.all([
                fetch('/api/invite.php', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'list_codes' }) }),
                fetch('/api/invite.php', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'list_rewards' }) })
            ]);
            const codesData = await codesRes.json();
            const rewardsData = await rewardsRes.json();
            _inviteRewards = rewardsData.rewards || [];
            const codes = codesData.codes || [];
            _inviteActiveCodes = codes.length;
            renderInviteContent(container, codes, _inviteRewards, rewardsData.pending_count || 0);
        } catch (e) {
            container.innerHTML = '<p style="color:var(--text-secondary);font-size:0.9rem;">加载失败，请刷新重试</p>';
        }
    }

    function renderInviteContent(container, codes, rewards, pendingCount) {
        let html = '<div class="invite-section-inner">';
        html += '<div class="invite-actions-row">';
        html += '<button class="btn btn-primary btn-sm" id="createInviteBtn"><i class="fas fa-plus"></i> 创建邀请码</button>';
        html += '<button class="btn btn-outline btn-sm" id="inviteRedeemTrigger"><i class="fas fa-gift"></i> 兑换奖励';
        if (pendingCount > 0) html += ' <span class="invite-badge">' + pendingCount + '</span>';
        html += '</button>';
        if (codes.length > 0) {
            html += '<button class="btn btn-outline btn-sm" id="inviteGenCopyBtn" style="margin-left:auto;"><i class="fas fa-copy"></i> 复制文案模板</button>';
        }
        html += '</div>';
        html += '<p class="invite-friend-reward-hint">好友注册后也将获得 1,000 Credits + 1000 万 API Tokens</p>';

        if (codes.length > 0) {
            html += '<div class="invite-codes-list">';
            codes.forEach(function (c) {
                html += '<div class="invite-code-row invite-code-active">';
                html += '<div class="invite-code-main">';
                html += '<div class="invite-code-link-text">' + escapeHtml(c.link) + '</div>';
                html += '<span class="invite-code-status status-active">已邀请 ' + (c.use_count || 0) + ' 人</span>';
                html += '</div>';
                html += '<button class="btn btn-outline btn-sm invite-copy-link" data-link="' + escapeHtml(c.link) + '"><i class="fas fa-copy"></i> 复制</button>';
                html += '</div>';
            });
            html += '</div>';
        } else {
            html += '<p class="invite-empty-hint">还没有邀请码，点击上方按钮创建</p>';
        }

        if (rewards.length > 0) {
            html += '<div class="invite-rewards-summary">';
            html += '<span class="invite-rewards-label"><i class="fas fa-ticket-alt"></i> 兑换券：</span>';
            const pendingR = rewards.filter(r => r.status === 'pending').length;
            const redeemedR = rewards.filter(r => r.status === 'redeemed').length;
            html += '<span>' + pendingR + ' 待兑换</span>';
            if (redeemedR > 0) html += '<span style="margin-left:12px;color:var(--text-secondary);">' + redeemedR + ' 已兑换</span>';
            html += '</div>';
        }

        html += '</div>';
        container.innerHTML = html;

        document.getElementById('createInviteBtn')?.addEventListener('click', function () {
            if (_inviteActiveCodes >= 2) { showToast('邀请码数量已达上限（2个）'); return; }
            openInviteCreateModal();
        });
        document.getElementById('inviteRedeemTrigger')?.addEventListener('click', function () {
            openInviteRedeemModal((_inviteRewards || []).filter(r => r.status === 'pending'));
        });
        container.querySelectorAll('.invite-copy-link').forEach(function (btn) {
            btn.addEventListener('click', function () {
                copyTextToClipboard(this.getAttribute('data-link'), this);
            });
        });
        document.getElementById('inviteGenCopyBtn')?.addEventListener('click', function () {
            var link = codes.length > 0 ? codes[0].link : '';
            var prompt = '帮我写 3 种邀请好友注册白鹿 AI 平台的短文案（朋友圈/群聊用）。\n要求：①自然真诚不夸张，禁用「领先/最强/颠覆」；②不提被邀请者奖励，可以带点薅羊毛的语气（比如"白嫖""真香""免费API"）；③重点说平台价值：免费 AI API、支持 Cursor/Claude Code 等 IDE 接入；④每段末尾必须原样附上完整链接文本（不要用 markdown 链接格式，直接写链接 URL）。\n链接：' + link;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(prompt).then(function () { showToast('邀请文案要点已复制到剪贴板'); }).catch(function () { showToast('复制失败，请手动复制'); });
            } else {
                showToast('当前浏览器不支持一键复制');
            }
        });
    }

    // 创建邀请码弹窗（Turnstile）
    function destroyInviteCreateTs() {
        try { if (inviteCreateTsWidgetId != null && window.turnstile) window.turnstile.remove(inviteCreateTsWidgetId); } catch (e) { }
        inviteCreateTsWidgetId = null;
        const host = document.getElementById('inviteCreateTurnstileHost');
        if (host) host.innerHTML = '';
    }

    function mountInviteCreateTs(retry) {
        retry = retry || 0;
        const host = document.getElementById('inviteCreateTurnstileHost');
        const confirmBtn = document.getElementById('inviteCreateConfirmBtn');
        const errEl = document.getElementById('inviteCreateError');
        if (!host) return;
        destroyInviteCreateTs();
        if (errEl) errEl.textContent = '';
        if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = '创建邀请码'; }
        if (!window.turnstile || typeof window.turnstile.render !== 'function') {
            if (retry < 100) { setTimeout(() => mountInviteCreateTs(retry + 1), 50); return; }
            if (errEl) errEl.textContent = '人机验证脚本未加载，请刷新页面';
            return;
        }
        try {
            inviteCreateTsWidgetId = window.turnstile.render(host, {
                sitekey: ANTHROPIC_CLAIM_TS_SITEKEY, theme: 'auto',
                callback: () => { if (errEl) errEl.textContent = ''; if (confirmBtn) confirmBtn.disabled = false; },
                'expired-callback': () => { if (confirmBtn) confirmBtn.disabled = true; try { if (window.turnstile && inviteCreateTsWidgetId != null) window.turnstile.reset(inviteCreateTsWidgetId); } catch (e2) { } },
                'error-callback': () => { if (errEl) errEl.textContent = '验证加载失败，请关闭后重试'; if (confirmBtn) confirmBtn.disabled = true; }
            });
        } catch (e) { if (errEl) errEl.textContent = '人机验证初始化失败'; }
    }

    function openInviteCreateModal() {
        const modal = document.getElementById('inviteCreateModal');
        if (!modal) return;
        document.getElementById('inviteCreatePhase').style.display = 'block';
        document.getElementById('inviteCreatedPhase').style.display = 'none';
        document.getElementById('inviteCreateTitle').textContent = '创建邀请码';
        document.getElementById('inviteCreateDesc').textContent = '完成人机验证后生成邀请链接';
        document.getElementById('inviteCreateError').textContent = '';
        modal.style.display = 'flex';
        window.TurnstileLoader?.load().catch(() => { /* 失败时 mountInviteCreateTs 自带提示 */ });
        requestAnimationFrame(() => mountInviteCreateTs(0));
    }

    function closeInviteCreateModal() {
        const modal = document.getElementById('inviteCreateModal');
        if (modal) modal.style.display = 'none';
        destroyInviteCreateTs();
    }

    document.getElementById('closeInviteCreateModal')?.addEventListener('click', closeInviteCreateModal);
    document.getElementById('inviteCreateCancelBtn')?.addEventListener('click', closeInviteCreateModal);
    document.getElementById('inviteCreateModal')?.addEventListener('click', (e) => { if (e.target === e.currentTarget) closeInviteCreateModal(); });
    document.getElementById('inviteCodeModalDone')?.addEventListener('click', closeInviteCreateModal);
    document.getElementById('copyInviteLinkBtn')?.addEventListener('click', function () {
        copyTextToClipboard(document.getElementById('inviteModalLink').textContent, this);
    });

    document.getElementById('inviteCreateConfirmBtn')?.addEventListener('click', async function () {
        const btn = this;
        const errEl = document.getElementById('inviteCreateError');
        if (!window.turnstile || inviteCreateTsWidgetId == null) { if (errEl) errEl.textContent = '请先完成人机验证'; return; }
        let tsToken = '';
        try { tsToken = window.turnstile.getResponse(inviteCreateTsWidgetId) || ''; } catch (e) { }
        if (!tsToken) { if (errEl) errEl.textContent = '请先完成人机验证'; return; }
        btn.disabled = true;
        btn.textContent = '创建中...';
        if (errEl) errEl.textContent = '';
        try {
            const resp = await fetch('/api/invite.php', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'create_code', turnstileToken: tsToken })
            });
            const data = await resp.json();
            if (data.success) {
                document.getElementById('inviteCreatePhase').style.display = 'none';
                document.getElementById('inviteCreatedPhase').style.display = 'block';
                document.getElementById('inviteCreateTitle').textContent = '邀请链接已生成';
                document.getElementById('inviteCreateDesc').textContent = '分享链接给好友，好友注册后您将获得一张奖励兑换券（API 额度每张可兑 2,500 万 Tokens）';
                document.getElementById('inviteModalLink').textContent = data.link;
                destroyInviteCreateTs();
                inviteDataLoaded = false;
                initInviteSection();
            } else {
                if (errEl) errEl.textContent = data.message || '创建失败';
                btn.disabled = false;
                btn.textContent = '创建邀请码';
                try { if (window.turnstile && inviteCreateTsWidgetId != null) window.turnstile.reset(inviteCreateTsWidgetId); } catch (e2) { }
            }
        } catch (e) {
            if (errEl) errEl.textContent = '网络错误';
            btn.disabled = false;
            btn.textContent = '创建邀请码';
        }
    });

    // 兑换弹窗
    let _inviteRedeemMax = 0;
    let _inviteRedeemCount = 1;

    function inviteRedeemSyncCounter() {
        setText('inviteCountVal', _inviteRedeemCount);
        const minus = document.getElementById('inviteCountMinus');
        const plus = document.getElementById('inviteCountPlus');
        if (minus) minus.disabled = _inviteRedeemCount <= 1 || _inviteRedeemMax === 0;
        if (plus) plus.disabled = _inviteRedeemCount >= _inviteRedeemMax || _inviteRedeemMax === 0;
        const hint = document.getElementById('inviteCountHint');
        if (hint) hint.textContent = _inviteRedeemMax > 0 ? '共 ' + _inviteRedeemMax + ' 张可用' : '';
    }

    function openInviteRedeemModal(pendingRewards) {
        _inviteRedeemMax = pendingRewards ? pendingRewards.length : 0;
        _inviteRedeemCount = _inviteRedeemMax > 0 ? 1 : 0;
        const errEl = document.getElementById('inviteRedeemError');
        if (errEl) { errEl.textContent = _inviteRedeemMax > 0 ? '' : '暂无待兑换的奖励，邀请好友注册即可获得'; errEl.style.color = _inviteRedeemMax > 0 ? '#dc2626' : '#64748b'; }
        const confirmBtn = document.getElementById('inviteRedeemConfirmBtn');
        if (confirmBtn) { confirmBtn.disabled = _inviteRedeemMax === 0; confirmBtn.textContent = '确认兑换'; }
        const counterWrap = document.getElementById('inviteRedeemCounterWrap');
        if (counterWrap) counterWrap.style.display = 'flex';
        inviteRedeemSyncCounter();
        document.getElementById('inviteRedeemModal').style.display = 'flex';
    }

    document.getElementById('inviteCountMinus')?.addEventListener('click', () => { if (_inviteRedeemCount > 1) { _inviteRedeemCount--; inviteRedeemSyncCounter(); } });
    document.getElementById('inviteCountPlus')?.addEventListener('click', () => { if (_inviteRedeemCount < _inviteRedeemMax) { _inviteRedeemCount++; inviteRedeemSyncCounter(); } });

    document.getElementById('closeInviteRedeemModal')?.addEventListener('click', () => { document.getElementById('inviteRedeemModal').style.display = 'none'; });
    document.getElementById('inviteRedeemCancelBtn')?.addEventListener('click', () => { document.getElementById('inviteRedeemModal').style.display = 'none'; });
    document.getElementById('inviteRedeemModal')?.addEventListener('click', (e) => { if (e.target === e.currentTarget) e.currentTarget.style.display = 'none'; });

    document.getElementById('inviteRedeemConfirmBtn')?.addEventListener('click', async function () {
        const btn = this;
        const errEl = document.getElementById('inviteRedeemError');
        const rewardType = document.querySelector('input[name="inviteRewardType"]:checked')?.value;
        if (!rewardType) { if (errEl) { errEl.textContent = '请选择奖励类型'; errEl.style.color = '#dc2626'; } return; }
        if (_inviteRedeemCount <= 0) { if (errEl) { errEl.textContent = '请调整兑换数量'; errEl.style.color = '#dc2626'; } return; }
        btn.disabled = true;
        btn.textContent = '兑换中...';
        if (errEl) errEl.textContent = '';
        try {
            const resp = await fetch('/api/invite.php', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'redeem_reward', reward_type: rewardType, count: _inviteRedeemCount })
            });
            const data = await resp.json();
            if (data.success) {
                document.getElementById('inviteRedeemModal').style.display = 'none';
                showToast(data.message || '兑换成功');
                fireInviteConfetti();
                inviteDataLoaded = false;
                initInviteSection();
                loadStats(true);
            } else {
                if (errEl) { errEl.textContent = data.message || '兑换失败'; errEl.style.color = '#dc2626'; }
                btn.disabled = false;
                btn.textContent = '确认兑换';
            }
        } catch (e) {
            if (errEl) { errEl.textContent = '网络错误'; errEl.style.color = '#dc2626'; }
            btn.disabled = false;
            btn.textContent = '确认兑换';
        }
    });

    /** 兑换成功礼花：统一复用 effects.js 的 canvas 版实现 */
    function fireInviteConfetti() {
        if (typeof window.fireConfetti === 'function') window.fireConfetti();
    }

    if ((window.location.hash || '').replace('#', '') === 'invite' || (window.location.hash || '').replace('#', '') === 'account-info') {
        setTimeout(initInviteSection, 100);
    }

    function doPageTransition(href) {
        var main = document.getElementById('apiPlatformContent') || document.querySelector('.main-content');
        var overlay = document.createElement('div');
        overlay.className = 'page-transition-overlay';
        document.body.appendChild(overlay);
        if (main) main.classList.add('page-exit-active');
        requestAnimationFrame(function () { overlay.classList.add('active'); });
        setTimeout(function () { window.location.href = href; }, 120);
    }

    document.querySelectorAll('[data-page-transition]').forEach(function (a) {
        var prefetched = false;
        a.addEventListener('mouseenter', function () {
            if (prefetched) return;
            prefetched = true;
            var link = document.createElement('link');
            link.rel = 'prefetch';
            link.href = a.href;
            document.head.appendChild(link);
        });
        a.addEventListener('click', function (e) {
            e.preventDefault();
            doPageTransition(a.href);
        });
    });

    function getApiPlatformChatSupportContext() {
        var L = ['[页面快照·勿复述·禁输出密钥密码]'];
        if (currentUser) {
            var disp = String((currentUser.last_name || '') + (currentUser.first_name || '')).trim();
            if (!disp) disp = String(currentUser.username || '').trim();
            var planLv = currentUser.api_access_level || 1;
            var plans = { 1: 'Personal', 2: 'Professional', 3: 'Enterprise' };
            L.push('## 用户');
            L.push('昵称:' + (disp || '—') + ' 邮箱:' + (currentUser.email || '—') + ' 方案:' + (plans[planLv] || 'L' + planLv));
        } else {
            L.push('用户信息未就绪');
        }
        if (lastStatsData) {
            var tu = Number(lastStatsData.tokens_used_effective ?? lastStatsData.tokens_used) || 0;
            var tq = Number(lastStatsData.tokens_quota_effective ?? lastStatsData.tokens_quota) || 0;
            var rpm = lastStatsData.api_rate_limit;
            var tpm = lastStatsData.api_tpm_limit;
            var conc = lastStatsData.api_concurrent_limit;
            L.push('## 用量');
            L.push('Tokens:' + tu.toLocaleString() + '/' + tq.toLocaleString() +
                ' 30d请求:' + (lastStatsData.requests_30d != null ? Number(lastStatsData.requests_30d).toLocaleString() : '—') +
                ' 密钥:' + (lastStatsData.active_keys || '—') + '/10');
            L.push('限速 RPM:' + (rpm > 0 ? rpm : '默认') +
                ' TPM:' + (tpm > 0 ? tpm : '默认') +
                ' 并发:' + (conc > 0 ? conc : '默认'));
            if (Array.isArray(lastStatsData.directed_packs) && lastStatsData.directed_packs.length) {
                L.push('定向额度:');
                lastStatsData.directed_packs.forEach(function (p) {
                    L.push('- ' + (p.label || p.id || '?') + ' ' +
                        Number(p.used || 0).toLocaleString() + '/' + Number(p.quota || 0).toLocaleString() +
                        ' ' + Math.round(Number(p.pct) || 0) + '%');
                });
            }
        } else {
            L.push('用量数据未加载，建议刷新页面');
        }
        if (chatModelsSnapshot && chatModelsSnapshot.available && chatModelsSnapshot.available.length) {
            var avail = chatModelsSnapshot.available;
            L.push('## 可用模型(' + avail.length + '个, level=' + chatModelsSnapshot.userLevel +
                ', 升级可解锁+' + chatModelsSnapshot.unavailableCount + ')');
            var cap = Math.min(avail.length, 40);
            for (var i = 0; i < cap; i++) {
                var m = avail[i];
                L.push('- ' + m.id + (m.type ? '[' + m.type + ']' : '') + (m.ctx ? ' ctx=' + m.ctx : ''));
            }
            if (avail.length > cap) L.push('…余' + (avail.length - cap) + '个见模型列表');
        } else {
            L.push('模型列表未就绪');
        }
        L.push('T:' + new Date().toISOString());
        return L.join('\n');
    }
    window.getApiPlatformChatSupportContext = getApiPlatformChatSupportContext;
});
