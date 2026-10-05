/* Japanese display messages only; API responses and user data are never rewritten. */
(() => {
    'use strict';
    const messages = window.OUTLOOK_JAPANESE_MESSAGES;
    const escapePattern = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pattern = new RegExp(Object.keys(messages).sort((a, b) => b.length - a.length).map(escapePattern).join('|'), 'g');
    const translate = text => String(text).replace(pattern, match => messages[match]);
    window.OutlookJapaneseUI = Object.freeze({ translate });

    function builtInGroupLabel(group) {
        if (!group) return null;
        if (Number(group.id) === 1 && group.name === '默认分组') return '既定グループ';
        if (Number(group.is_system) === 1 && group.name === '临时邮箱') return '一時メール';
        return null;
    }
    // Only the labels of the built-in groups change; model names and edit inputs stay intact.
    function localizeBuiltInGroups() {
        if (typeof groups === 'undefined' || !Array.isArray(groups)) return;
        for (const item of document.querySelectorAll('.group-item[data-group-id]')) {
            const group = groups.find(group => Number(group.id) === Number(item.dataset.groupId));
            const label = builtInGroupLabel(group);
            const node = item.querySelector('.group-name');
            if (label && node) node.textContent = label + (Number(group.is_system) === 1 ? ' ⚡' : '');
        }
        if (typeof currentGroupId !== 'undefined') {
            const label = builtInGroupLabel(groups.find(group => group.id === currentGroupId));
            if (label) {
                const node = document.getElementById('mobileCurrentGroup');
                if (node) node.textContent = label;
            }
        }
    }
    for (const name of ['renderGroupList', 'updateMobileContext']) {
        if (typeof window[name] !== 'function') continue;
        const original = window[name];
        window[name] = function(...args) {
            const result = original.apply(this, args);
            localizeBuiltInGroups();
            return result;
        };
    }
    if (typeof window.getGroupOptionLabel === 'function') {
        const original = window.getGroupOptionLabel;
        window.getGroupOptionLabel = function(group, ...args) {
            return builtInGroupLabel(group) || original.call(this, group, ...args);
        };
    }

    // Translate server-supplied display errors without altering fetched JSON or persisted values.
    for (const name of ['showToast', 'appendGraphAuthLog', 'showConfirmModal', 'updateRefreshLogSummary']) {
        if (typeof window[name] !== 'function') continue;
        const original = window[name];
        window[name] = function(message, ...args) {
            return original.call(this, typeof message === 'string' ? translate(message) : message, ...args);
        };
    }
    for (const name of ['alert', 'confirm']) {
        const original = window[name].bind(window);
        window[name] = message => original(translate(message));
    }
    if (typeof window.appendRefreshRuntimeLog === 'function') {
        const original = window.appendRefreshRuntimeLog;
        window.appendRefreshRuntimeLog = function(level, title, detail = '') {
            return original.call(this, level, translate(title), translate(detail));
        };
    }

    const errorTargets = '#errorMessage, #errorModalUserMessage, #errorModalSuggestion, #shareInvalidTitle, #toast';
    function localizeErrors() {
        for (const root of document.querySelectorAll(errorTargets)) {
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                const next = translate(node.data);
                if (next !== node.data) node.data = next;
            }
        }
    }
    let scheduled = false;
    const observer = new MutationObserver(() => {
        if (scheduled) return;
        scheduled = true;
        queueMicrotask(() => { scheduled = false; localizeErrors(); });
    });
    for (const root of document.querySelectorAll(errorTargets)) {
        observer.observe(root, { childList: true, characterData: true, subtree: true });
    }
    localizeErrors();
})();
