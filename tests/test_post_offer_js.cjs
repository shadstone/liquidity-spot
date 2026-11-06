const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const template = fs.readFileSync(path.join(__dirname, '../templates/p2p.html'), 'utf8');
const functions = template.slice(template.indexOf('    function selectedPaymentAsset()'), template.indexOf('    const p2pOfferForm ='));

function boot() {
    const fields = {};
    for (const id of ['p2p-payment-asset', 'p2p-amount', 'p2p-price', 'p2p-side', 'p2p-total-btc', 'p2p-you-send', 'p2p-you-receive']) {
        fields[id] = { value: '', textContent: '' };
    }
    fields['p2p-payment-asset'].value = 'eth-optimism';
    fields['p2p-amount'].value = '1000.000001';
    fields['p2p-price'].value = '0.123456789012345678';
    fields['p2p-side'].value = 'sell';
    const context = vm.createContext({ document: { getElementById(id) { return fields[id]; } },
        p2pPaymentAssets: [{ id: 'eth-optimism', symbol: 'ETH', network_label: 'OP Mainnet (Optimism)', decimals: 18 }] });
    vm.runInContext(functions, context);
    return { context, fields };
}

test('preview rounds decimal strings at asset precision without float conversion', () => {
    const { context } = boot();
    const total = (a, p, d) => context.exactTransferTotal(a, p, d);
    assert.equal(total('1000.000001', '0.123456789012345678', 18), '123.456789135802467012');
    assert.equal(total('0.5', '0.00000001', 8), '0.00000001');
    assert.equal(total('1', '0.0000005', 6), '0.000001');
    assert.equal(total('1', '0.0000001', 6), null);
    for (const bad of ['NaN', 'Infinity', '1e3', '-1', '', '0']) assert.equal(total(bad, '1', 18), null);
});

test('live send/receive follows the maker side and names both networks', () => {
    const { context, fields } = boot();
    context.updateP2PTotal();
    assert.equal(fields['p2p-you-send'].textContent, '1000.000001 HNS · Handshake mainnet');
    assert.equal(fields['p2p-you-receive'].textContent, '123.456789135802467012 ETH · OP Mainnet (Optimism)');
    fields['p2p-side'].value = 'buy';
    context.updateP2PTotal();
    assert.equal(fields['p2p-you-receive'].textContent, '1000.000001 HNS · Handshake mainnet');
    assert.equal(fields['p2p-you-send'].textContent, '123.456789135802467012 ETH · OP Mainnet (Optimism)');
    fields['p2p-amount'].value = '';
    context.updateP2PTotal();
    assert.equal(fields['p2p-you-send'].textContent, 'Enter amount and price');
    assert.equal(fields['p2p-you-receive'].textContent, 'Enter amount and price');
});
