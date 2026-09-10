/* Navigation is presentation-only; device hints never change authentication. */
(function () {
    const views = {
        overview: ['运行概览', '查看运行状态与关键指标，管理各平台的暂停状态。', 'WORKSPACE / OVERVIEW'],
        decisions: ['决策账本', '从研究证据到执行结果，追溯每一次决策的完整过程。', 'WORKSPACE / DECISIONS'],
        models: ['模型服务', '连接 Codex、Claude 或兼容 API，为机器人选择可用的模型。', 'CONFIGURATION / MODELS'],
        plugins: ['插件中心', '按用途选择能力，了解它们如何配合，再配置需要的部分。', 'CONFIGURATION / PLUGINS'],
        settings: ['程序设置', '管理部署参数、数据位置与插件目录。', 'CONFIGURATION / SETTINGS'],
        security: ['安全与会话', '管理管理员 Passkey，以及已登录的设备和会话。', 'ACCOUNT / SECURITY']
    };
    const menu = document.getElementById('menuToggle');
    const sidebar = document.getElementById('sidebar');
    const narrow = matchMedia('(max-width:800px)');
    function closeMenu() { document.body.classList.remove('menu-open'); menu.setAttribute('aria-expanded', 'false'); sidebar.inert = narrow.matches; }
    narrow.addEventListener('change', closeMenu);
    menu.addEventListener('click', () => {
        const open = document.body.classList.toggle('menu-open');
        sidebar.inert = !open && narrow.matches;
        menu.setAttribute('aria-expanded', String(open));
        if (open) document.querySelector('.nav-link[aria-current="page"]').focus();
    });
    document.getElementById('navBackdrop').addEventListener('click', closeMenu);
    for (const link of document.querySelectorAll('.nav-link')) link.addEventListener('click', closeMenu);
    document.addEventListener('keydown', event => { if (event.key === 'Escape') { closeMenu(); menu.focus(); } });
    function showView() {
        let hash;
        try { hash = decodeURIComponent(location.hash.slice(1)); } catch (_) { hash = ''; }
        if(hash==='plugins/decision_provider'){
            history.replaceState(null,'','#models');
            hash='models';
        }
        const oldTarget = hash.startsWith('plugin_') ? document.getElementById(hash) : null;
        const modelName = hash.startsWith('model-config/') ? hash.slice('model-config/'.length) : '';
        const target = modelName ? document.getElementById('plugin_decision_provider_'+modelName) : oldTarget;
        let kind = hash.startsWith('plugins/') ? hash.slice(8) : oldTarget?.closest('[data-kind]')?.dataset.kind;
        if (!Object.hasOwn(CATEGORY_HELP,kind||'')) kind='';
        const name = modelName ? 'models' : hash.startsWith('plugins/')||oldTarget ? 'plugins' : (Object.hasOwn(views,hash)?hash:'overview');
        for (const section of document.querySelectorAll('[data-view]')) section.hidden=section.dataset.view!==name;
        for (const link of document.querySelectorAll('.nav-link')) {
            if(link.hash==='#'+name)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
        }
        const providerGroup=document.getElementById('category_decision_provider');
        document.getElementById('modelConfigurationSection').hidden=name!=='models'||!modelName;
        if(providerGroup)for(const card of providerGroup.querySelectorAll('.configuration-card'))card.hidden=!modelName||card.id!=='plugin_decision_provider_'+modelName;
        for(const group of document.querySelectorAll('.plugin-category'))group.hidden=!(name==='models'&&group.dataset.kind==='decision_provider')&&!(name==='plugins'&&group.dataset.kind===kind);
        document.getElementById('pluginCategoryHome').hidden=Boolean(kind);
        document.getElementById('pluginCategoryNav').hidden=!kind;
        for(const link of document.querySelectorAll('#pluginCategoryNav a')){if(link.hash==='#plugins/'+kind)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current')}
        for(const bar of document.querySelectorAll('.plugin-management-tools'))bar.hidden=!kind;
        const guide=document.getElementById('pluginCategoryGuide'),help=CATEGORY_HELP[kind];
        guide.innerHTML=help?'<h3>'+help.title+'：'+help.role+'</h3><p class="muted">'+help.description+'</p>'+processStrip(help.steps)+'<div class="info-banner"><strong>你需要做什么</strong><p>'+help.next+'</p></div>':'';
        if(kind){document.getElementById('installKind').value=kind;renderInstallTargets()}
        document.getElementById('pageTitle').textContent=help&&name==='plugins'?help.title:views[name][0];
        document.getElementById('pageDescription').textContent=views[name][1];
        document.getElementById('pageEyebrow').textContent=help&&name==='plugins'?'插件中心 / '+help.title:views[name][2];
        document.title=document.getElementById('pageTitle').textContent+' · Prediction Agent';
        closeMenu();
        if (target) {
            const detail = target.querySelector('.plugin-config');
            if (detail) detail.open = true;
            target.scrollIntoView({block: 'start'});
        } else window.scrollTo(0, 0);
        activateChoiceLists();
        if(modelName)loadCurrentModelConfig(modelName);
    }
    window.showDashboardView = showView;
    window.addEventListener('hashchange', showView);
    showView();
    document.getElementById('accessMode').textContent = typeof LOCAL_ACCESS !== 'undefined' && LOCAL_ACCESS ? '本地 / 私网访问' : '加密管理会话';
    document.getElementById('securityAccessNote').hidden=!LOCAL_ACCESS;
    document.getElementById('logoutSession').hidden=LOCAL_ACCESS;
    window.addEventListener('unhandledrejection', event => {
        const feedback = document.getElementById('globalFeedback');
        feedback.textContent = event.reason?.message || '操作失败，请刷新状态后重试。';
        feedback.hidden = false;
    });
    document.getElementById('globalFeedback').addEventListener('click', event => { event.currentTarget.hidden = true; });
})();

function browserNeedsDesktopHelper(nav = navigator) {
    return Boolean(nav.userAgentData?.mobile || /Android|iPhone|iPad|iPod/i.test(nav.userAgent || '') ||
        (nav.platform === 'MacIntel' && nav.maxTouchPoints > 1));
}

function preferredLoginMethod(nav = navigator, host = location.hostname) {
    const name = host.toLowerCase().replace(/^\[|\]$/g, '');
    const loopback = name === 'localhost' || name === '::1' || /^127(?:\.\d{1,3}){3}$/.test(name);
    return browserNeedsDesktopHelper(nav) || !loopback ? 'remote' : 'local';
}
