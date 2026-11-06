const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../static/js/navigation.js'), 'utf8');

function boot() {
    const handlers = {}, events = {}, attrs = {};
    const link = {};
    const button = { hidden: true, focus() { this.focused = true; },
        setAttribute(key, value) { attrs[key] = value; },
        addEventListener(event, handler) { handlers[event] = handler; } };
    const menu = { hidden: false, contains(node) { return node === link; } };
    const summary = { focus() { this.focused = true; } };
    const details = { open: true, contains(node) { return node === summary; }, querySelector() { return summary; } };
    const document = { activeElement: button,
        getElementById(id) { return id === 'mobile-menu' ? menu : button; },
        addEventListener(event, handler) { events[event] = handler; },
        querySelectorAll() { return details.open ? [details] : []; } };
    const viewport = {};
    vm.runInNewContext(source, { document, window: { matchMedia() {
        return { addEventListener(event, handler) { viewport[event] = handler; } };
    } } });
    return { button, menu, attrs, handlers, events, document, link, details, summary, viewport };
}

test('mobile menu synchronizes visibility and accessible state', () => {
    const page = boot();
    assert.equal(page.button.hidden, false);
    assert.equal(page.menu.hidden, true);
    assert.equal(page.attrs['aria-expanded'], 'false');
    page.handlers.click();
    assert.equal(page.menu.hidden, false);
    assert.equal(page.attrs['aria-expanded'], 'true');
    assert.equal(page.attrs['aria-label'], 'Close navigation');
    page.handlers.click();
    assert.equal(page.menu.hidden, true);
    assert.equal(page.attrs['aria-label'], 'Open navigation');
});

test('Escape from a mobile link closes navigation and returns focus', () => {
    const page = boot();
    page.handlers.click();
    page.document.activeElement = page.link;
    page.events.keydown({ key: 'Escape' });
    assert.equal(page.menu.hidden, true);
    assert.equal(page.button.focused, true);
});

test('Escape from desktop details returns focus to its summary', () => {
    const page = boot();
    page.document.activeElement = page.summary;
    page.events.keydown({ key: 'Escape' });
    assert.equal(page.details.open, false);
    assert.equal(page.summary.focused, true);
});

test('changing breakpoints closes an open mobile menu', () => {
    const page = boot();
    page.handlers.click();
    page.viewport.change();
    assert.equal(page.menu.hidden, true);
    assert.equal(page.attrs['aria-expanded'], 'false');
});
