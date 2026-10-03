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

function boot({ stored = null, storageBlocked = false, clipboard = undefined, empty = false } = {}) {
    const saved = [];
    const elements = empty ? {} : {
        'agent-help-banner': element(),
        'dismiss-agent-help': element({ hidden: true }),
        'agent-setup-prompt': element({ value: 'Public setup brief — no credentials',
            focus() { this.focused = true; }, select() { this.selected = true; } }),
        'copy-agent-prompt': element(),
        'copy-agent-status': element(),
    };
    vm.runInNewContext(source, {
        document: { getElementById(id) { return elements[id] || null; } },
        window: { sessionStorage: {
            getItem() { if (storageBlocked) throw new Error('denied'); return stored; },
            setItem(key, value) { if (storageBlocked) throw new Error('denied'); saved.push([key, value]); },
        } },
        navigator: { clipboard },
    });
    return { elements, saved };
}

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
