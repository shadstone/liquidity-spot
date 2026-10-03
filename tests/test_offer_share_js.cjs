// No browser, network, real clipboard or outbound sharing is used by these tests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../static/js/offer-share.js'), 'utf8');

const paragraph = 'HNS offer — test text, not a live listing.\nRead the terms: https://liquidity.spot/p2p/offers/123';
const offerUrl = 'https://liquidity.spot/p2p/offers/123';

function element(properties = {}) {
    return { hidden: false, handlers: {}, textContent: '', value: '', style: {}, scrollHeight: 180,
        addEventListener(event, callback) { this.handlers[event] = callback; },
        focus() { this.focused = true; },
        select() { this.selected = true; },
        setSelectionRange(start, end) { this.selection = [start, end]; },
        ...properties };
}

function boot({ clipboard, share, empty = false, linkOnly = false, omit = [] } = {}) {
    const elements = empty ? {} : {
        'offer-share-url': element({ value: offerUrl }),
        'copy-offer-link': element(),
        'share-offer-status': element(),
        ...(linkOnly ? {} : {
            'offer-share-text': element({ value: paragraph }),
            'copy-offer-text': element(),
            'share-offer-text': element({ hidden: true }),
        }),
    };
    omit.forEach(id => delete elements[id]);
    const navigator = { clipboard, share };
    const network = [];
    const windowHandlers = {};
    const rejectNetwork = (...args) => { network.push(args); throw new Error('Network is prohibited in offer-sharing controls'); };
    vm.runInNewContext(source, {
        document: { getElementById(id) { return elements[id] || null; } },
        navigator,
        window: { navigator, isSecureContext: true,
            addEventListener(event, callback) { windowHandlers[event] = callback; } },
        fetch: rejectNetwork,
        XMLHttpRequest: rejectNetwork,
    });
    return { elements, network, windowHandlers };
}

test('the full paragraph is fitted on initialization and again after viewport resize', () => {
    const { elements, network, windowHandlers } = boot();
    const message = elements['offer-share-text'];
    assert.equal(message.style.height, '182px');
    assert.equal(typeof windowHandlers.resize, 'function');
    message.scrollHeight = 320;
    windowHandlers.resize();
    assert.equal(message.style.height, '322px');
    assert.deepEqual(network, []);
    assert.equal(elements['share-offer-status'].textContent, '');
    assert.equal(message.focused, undefined);
    assert.equal(message.selected, undefined);
});

test('initialization does not copy, share or make an HTTP request', () => {
    const copies = [], shares = [];
    const { elements, network } = boot({
        clipboard: { async writeText(text) { copies.push(text); } },
        async share(payload) { shares.push(payload); },
    });
    assert.deepEqual(copies, []);
    assert.deepEqual(shares, []);
    assert.deepEqual(network, []);
    assert.equal(elements['share-offer-text'].hidden, false);
    assert.equal(elements['share-offer-status'].textContent, '');
});

test('copy text copies exactly the currently visible full paragraph, including its URL', async () => {
    const copies = [];
    const { elements } = boot({ clipboard: { async writeText(text) { copies.push(text); } } });
    elements['offer-share-text'].value = paragraph + '\nCurrent displayed terms.';
    await elements['copy-offer-text'].handlers.click();
    assert.deepEqual(copies, [elements['offer-share-text'].value]);
    assert.match(elements['share-offer-status'].textContent, /copied/i);
    assert.equal(elements['offer-share-url'].selected, undefined);
});

test('copy link copies only the current URL, not the paragraph', async () => {
    const copies = [];
    const { elements } = boot({ clipboard: { async writeText(text) { copies.push(text); } } });
    elements['offer-share-url'].value = offerUrl + '?public=1';
    await elements['copy-offer-link'].handlers.click();
    assert.deepEqual(copies, [elements['offer-share-url'].value]);
    assert.match(elements['share-offer-status'].textContent, /copied/i);
    assert.equal(elements['offer-share-text'].selected, undefined);
});

for (const [condition, clipboard] of [
    ['missing', undefined],
    ['denied', { async writeText() { throw new Error('Clipboard permission denied'); } }],
]) {
    for (const [kind, button, field, otherField] of [
        ['paragraph', 'copy-offer-text', 'offer-share-text', 'offer-share-url'],
        ['link', 'copy-offer-link', 'offer-share-url', 'offer-share-text'],
    ]) {
        test(`clipboard ${condition}: ${kind} falls back to selecting only the intended field`, async () => {
            const { elements } = boot({ clipboard });
            await elements[button].handlers.click();
            assert.equal(elements[field].focused, true);
            assert.equal(elements[field].selected, true);
            assert.equal(elements[otherField].selected, undefined);
            assert.match(elements['share-offer-status'].textContent, /copy|select/i);
            assert.doesNotMatch(elements['share-offer-status'].textContent, /copied|sent|published/i);
        });
    }
}

test('native share sends the paragraph once without a duplicate URL field', async () => {
    const shares = [];
    const { elements, network } = boot({ async share(payload) { shares.push(payload); } });
    elements['offer-share-text'].value = paragraph + '\nRead before accepting.';
    await elements['share-offer-text'].handlers.click();
    assert.equal(shares.length, 1);
    assert.equal(shares[0].text, elements['offer-share-text'].value);
    assert.equal(Object.hasOwn(shares[0], 'url'), false);
    assert.ok(Object.keys(shares[0]).every(key => ['text', 'title'].includes(key)));
    assert.match(elements['share-offer-status'].textContent, /share|complet/i);
    assert.doesNotMatch(elements['share-offer-status'].textContent, /published|posted to|delivered to/i);
    assert.deepEqual(network, []);
});

test('canceling the native share sheet is quiet and does not trigger clipboard fallback', async () => {
    const copies = [];
    const { elements } = boot({
        clipboard: { async writeText(text) { copies.push(text); } },
        async share() { const error = new Error('Canceled'); error.name = 'AbortError'; throw error; },
    });
    await elements['share-offer-text'].handlers.click();
    assert.equal(elements['share-offer-status'].textContent, '');
    assert.equal(elements['offer-share-text'].selected, undefined);
    assert.deepEqual(copies, []);
});

test('native share failure offers manual copying without claiming delivery', async () => {
    const { elements } = boot({ async share() { throw new Error('Share is unavailable'); } });
    await elements['share-offer-text'].handlers.click();
    assert.match(elements['share-offer-status'].textContent, /copy|select/i);
    assert.doesNotMatch(elements['share-offer-status'].textContent, /completed|copied|sent|published/i);
});

test('native share stays hidden when the browser does not support it', () => {
    const { elements } = boot();
    assert.equal(elements['share-offer-text'].hidden, true);
});

test('pages without share controls and partially present controls do not throw', () => {
    assert.doesNotThrow(() => boot({ empty: true }));
    for (const id of ['offer-share-text', 'copy-offer-text', 'offer-share-url',
                      'copy-offer-link', 'share-offer-text', 'share-offer-status']) {
        assert.doesNotThrow(() => boot({ omit: [id] }));
    }
});

test('closed/link-only page still copies its URL without offering a paragraph share', async () => {
    const copies = [], shares = [];
    const { elements } = boot({ linkOnly: true,
        clipboard: { async writeText(text) { copies.push(text); } },
        async share(payload) { shares.push(payload); },
    });
    await elements['copy-offer-link'].handlers.click();
    assert.deepEqual(copies, [offerUrl]);
    assert.deepEqual(shares, []);
    assert.match(elements['share-offer-status'].textContent, /copied/i);
});
