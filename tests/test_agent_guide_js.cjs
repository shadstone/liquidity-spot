// Dependency-free unit checks; no browser, network or real clipboard access.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../static/js/agent-guide.js'), 'utf8');

function element(properties = {}) {
    return { hidden: false, handlers: {}, textContent: '',
        addEventListener(event, callback) { this.handlers[event] = callback; },
        ...properties };
}

function boot({ stored = null, storageBlocked = false, clipboard = undefined, empty = false, stackHeight = null } = {}) {
    const saved = [];
    const offsets = [];
    const resizeCallbacks = [];
    const elements = empty ? {} : {
        'agent-help-banner': element(),
        'dismiss-agent-help': element({ hidden: true }),
        'agent-setup-prompt': element({ value: 'Public setup brief — no credentials',
            focus() { this.focused = true; }, select() { this.selected = true; } }),
        'copy-agent-prompt': element(),
        'copy-agent-status': element(),
    };
    if (stackHeight !== null) {
        elements['trade-notice-stack'] = element({ height: stackHeight,
            getBoundingClientRect() { return { height: this.height }; } });
    }
    vm.runInNewContext(source, {
        document: { getElementById(id) { return elements[id] || null; },
            documentElement: { style: { setProperty(name, value) { offsets.push([name, value]); } } } },
        window: { addEventListener(event, callback) { if (event === 'resize') resizeCallbacks.push(callback); },
            ResizeObserver: class { constructor(callback) { this.callback = callback; }
                observe() { resizeCallbacks.push(this.callback); } },
            sessionStorage: {
            getItem() { if (storageBlocked) throw new Error('denied'); return stored; },
            setItem(key, value) { if (storageBlocked) throw new Error('denied'); saved.push([key, value]); },
        } },
        navigator: { clipboard },
    });
    return { elements, saved, offsets, resizeCallbacks };
}

test('guide anchor offset follows the actual sticky notice height', () => {
    const { elements, offsets, resizeCallbacks } = boot({ stackHeight: 331.5 });
    assert.deepEqual(offsets[0], ['--liquidity-sticky-offset', '348px']);
    assert.equal(resizeCallbacks.length, 2);
    elements['trade-notice-stack'].height = 76;
    resizeCallbacks.forEach(callback => callback());
    assert.deepEqual(offsets.at(-1), ['--liquidity-sticky-offset', '92px']);
});

test('pages without agent controls do not throw', () => {
    assert.doesNotThrow(() => boot({ empty: true }));
});

test('banner dismissal is tab-local and restored on the next page', () => {
    const { elements, saved } = boot();
    assert.equal(elements['agent-help-banner'].hidden, false);
    assert.equal(elements['dismiss-agent-help'].hidden, false);
    elements['dismiss-agent-help'].handlers.click();
    assert.equal(elements['agent-help-banner'].hidden, true);
    assert.deepEqual(saved, [['liquidity-agent-help-hidden-v1', 'yes']]);
    assert.equal(boot({ stored: 'yes' }).elements['agent-help-banner'].hidden, true);
});

test('storage denial still allows hiding without breaking the page', () => {
    const { elements } = boot({ storageBlocked: true });
    assert.equal(elements['agent-help-banner'].hidden, false);
    assert.doesNotThrow(() => elements['dismiss-agent-help'].handlers.click());
    assert.equal(elements['agent-help-banner'].hidden, true);
});

test('successful copy writes exactly the visible brief and reports success', async () => {
    const writes = [];
    const { elements } = boot({ clipboard: { async writeText(text) { writes.push(text); } } });
    await elements['copy-agent-prompt'].handlers.click();
    assert.deepEqual(writes, [elements['agent-setup-prompt'].value]);
    assert.match(elements['copy-agent-status'].textContent, /Prompt copied/);
    assert.match(elements['copy-agent-status'].textContent, /Do not add API keys/);
});

for (const [name, clipboard] of [
    ['unavailable', undefined],
    ['denied', { async writeText() { throw new Error('denied'); } }],
]) {
    test(`clipboard ${name} selects the text and explains manual copy`, async () => {
        const { elements } = boot({ clipboard });
        await elements['copy-agent-prompt'].handlers.click();
        assert.equal(elements['agent-setup-prompt'].focused, true);
        assert.equal(elements['agent-setup-prompt'].selected, true);
        assert.match(elements['copy-agent-status'].textContent, /Automatic copy is unavailable/);
        assert.doesNotMatch(elements['copy-agent-status'].textContent, /Prompt copied/);
    });
}
