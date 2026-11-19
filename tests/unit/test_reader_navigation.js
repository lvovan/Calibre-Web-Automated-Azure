/* Run with node --test tests\unit\test_reader_navigation.js */
"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const navigation = require("../../cps/static/js/reading/navigation.js");

const settle = () => new Promise(resolve => setImmediate(resolve));

function harness(t, {initial = 1, total = 100, navigate} = {}) {
    t.mock.timers.enable({apis: ["setTimeout"]});
    let page = initial;
    const calls = [];
    const errors = [];
    const control = navigation.create({
        position: () => page,
        bounds: () => [1, total],
        navigate: async target => { calls.push(target); if (navigate) await navigate(target); page = target; },
        onError: error => errors.push(error)
    });
    return {control, calls, errors, page: () => page,
        flush: async () => { t.mock.timers.tick(160); await settle(); }};
}

test("ten rapid page turns produce one navigation directly to page eleven", async t => {
    const h = harness(t);
    for (let i = 0; i < 10; i++) h.control.move(1);
    assert.deepEqual(h.calls, []);
    await h.flush();
    assert.deepEqual(h.calls, [11]);
});

test("mixed directions cancel and page limits are applied to each intent", async t => {
    const h = harness(t, {initial: 9, total: 10});
    h.control.move(1);
    h.control.move(1);
    h.control.move(-1);
    await h.flush();
    assert.deepEqual(h.calls, []);
    for (let i = 0; i < 20; i++) h.control.move(-1);
    await h.flush();
    assert.deepEqual(h.calls, [1]);
});

test("input during an asynchronous render is serialized without losing turns", async t => {
    let release;
    const loading = new Promise(resolve => { release = resolve; });
    const h = harness(t, {navigate: () => loading});
    h.control.move(1);
    await h.flush();
    for (let i = 0; i < 10; i++) h.control.move(1);
    await h.flush();
    assert.deepEqual(h.calls, [2]);
    release();
    await settle();
    await h.flush();
    assert.deepEqual(h.calls, [2, 12]);
});

test("a jump replaces pending turns and cancel discards delayed navigation", async t => {
    const h = harness(t);
    h.control.move(10);
    h.control.jump(50);
    await h.flush();
    assert.deepEqual(h.calls, [50]);
    h.control.move(10);
    h.control.cancel();
    await h.flush();
    assert.deepEqual(h.calls, [50]);
});

test("navigation errors are reported and subsequent input can recover", async t => {
    let fail = true;
    const error = new Error("page request failed");
    const h = harness(t, {navigate: () => { if (fail) throw error; }});
    h.control.move(1);
    await h.flush();
    assert.deepEqual(h.errors, [error]);
    fail = false;
    h.control.move(1);
    await h.flush();
    assert.equal(h.page(), 2);
});

function swipeHarness(options = {}) {
    const handlers = {};
    const moves = [];
    const element = {addEventListener: (name, callback) => { handlers[name] = callback; }};
    navigation.bindSwipe(element, {move: delta => moves.push(delta), ...options});
    const touch = (x, y = 0) => ({clientX: x, clientY: y});
    function start(x, y = 0) { handlers.touchstart({touches: [touch(x, y)]}); }
    function end(x, y = 0) {
        handlers.touchend({touches: [], changedTouches: [touch(x, y)], cancelable: true, preventDefault() {}});
    }
    return {handlers, moves, start, end, touch};
}

test("touch swipes follow LTR/RTL and suppress the synthetic click", () => {
    const h = swipeHarness();
    h.start(200); h.end(80);
    h.start(80); h.end(200);
    assert.deepEqual(h.moves, [1, -1]);
    let prevented = false;
    h.handlers.click({preventDefault() { prevented = true; }, stopImmediatePropagation() {}});
    assert.equal(prevented, true);
    prevented = false;
    h.start(80); h.end(80);
    h.handlers.click({preventDefault() { prevented = true; }, stopImmediatePropagation() {}});
    assert.equal(prevented, false);
    const rtl = swipeHarness({rtl: () => true});
    rtl.start(200); rtl.end(80);
    assert.deepEqual(rtl.moves, [-1]);
});

test("vertical scrolling, small drags, pinch and cancelled gestures never turn pages", () => {
    const h = swipeHarness();
    h.start(100); h.end(90, 150);
    h.start(100); h.end(80);
    h.start(200);
    h.handlers.touchmove({touches: [h.touch(180), h.touch(220)]});
    h.end(80);
    h.start(200); h.handlers.touchcancel(); h.end(80);
    assert.deepEqual(h.moves, []);
    const zoomed = swipeHarness({enabled: () => false});
    zoomed.start(200); zoomed.end(80);
    assert.deepEqual(zoomed.moves, []);
});

test("swipes respect rotated comics and ignore touches starting on controls", () => {
    const h = swipeHarness({axis: () => 1});
    h.start(10, 200); h.end(10, 80);
    h.handlers.touchstart({touches: [h.touch(200)], target: {closest: () => ({})}});
    h.end(80);
    assert.deepEqual(h.moves, [1]);
});

test("DJVU toolbar clicks jump once through the bundled viewer's page selector", async t => {
    const h = harness(t);
    const handlers = {};
    let changes = 0;
    const select = {selectedIndex: 0, options: new Array(30), dispatchEvent: () => { changes++; }};
    const button = {parentElement: {querySelector: () => select}, classList: {contains: () => true}};
    const container = {querySelector: selector => selector === ".buttonPageNext" ? button : null,
        addEventListener: (name, callback) => { handlers[name] = callback; }};
    const source = fs.readFileSync(path.join(__dirname, "../../cps/static/js/reading/djvu_reader.js"), "utf8");
    vm.runInNewContext(source, {window: {CwaNavigation: {...navigation, bindSwipe() {}}}, console, Event,
        document: {getElementById: () => container, addEventListener: (name, callback) => callback()}});
    for (let i = 0; i < 10; i++) handlers.click({target: {closest: () => button}, preventDefault() {}, stopImmediatePropagation() {}});
    await h.flush();
    assert.equal(select.selectedIndex, 10);
    assert.equal(changes, 1);
});

function epubHarness(t, {rtl = false, initial = [0, 1], spread = 1} = {}) {
    t.mock.timers.enable({apis: ["setTimeout"]});
    const displayed = [];
    const measured = [];
    const sections = [4, 8, 2].map((total, index) => ({total, index, cfiBase: String(index)}));
    sections.forEach((section, index) => {
        section.prev = () => sections[index - 1];
        section.next = () => sections[index + 1];
    });
    let location = {start: {index: initial[0], displayed: {page: initial[1]}}};
    const node = () => ({style: {}, appendChild() {}, remove() {}, addEventListener() {}});
    const viewFor = section => ({element: node(), contents: {section}, width: () => section.total * 100,
        height: () => 100, display: async () => { measured.push(section.index); }, expand() {}, destroy() {}});
    const rendition = {
        manager: {layout: {divisor: spread, pageWidth: 100, width: 100 * spread, height: 100},
            settings: {axis: "horizontal", direction: rtl ? "rtl" : "ltr"},
            container: node(), views: {find: section => section.index === location.start.index ? viewFor(section) : null},
            createView: viewFor, mapping: {page: (contents, base, start) => ({start: base + ":" + start})}},
        hooks: {content: {register() {}, trigger: async () => {}}},
        on() {}, currentLocation: () => location,
        display: async target => {
            displayed.push(target);
            if (typeof target === "number") location = {start: {index: target, displayed: {page: 1}}};
            else {
                const [index, offset] = target.split(":").map(Number);
                location = {start: {index, displayed: {page: offset / 100 + 1}}};
            }
        }
    };
    const reader = {rendition, book: {load() {}, spine: {get: index => sections[index]}, package: {metadata: {}}}};
    const calibre = {imagePages: false, messages: {displayError: "failed"}};
    const window = {CwaNavigation: navigation};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../../cps/static/js/reading/epub-navigation.js"), "utf8"),
        {window, console, document: {createElement: node, getElementById: node}});
    window.CwaEpubNavigation.attach(reader, calibre);
    return {rendition, displayed, measured, flush: async () => { t.mock.timers.tick(160); await settle(); }};
}

test("reflowable EPUB batches exact page turns across chapters without displaying intermediate pages", async t => {
    const h = epubHarness(t);
    for (let i = 0; i < 10; i++) h.rendition.next();
    await h.flush();
    assert.deepEqual(h.displayed, ["1:600"]);
    assert.deepEqual(h.measured, [1]);
});

test("backward EPUB jumps measure each crossed chapter once and clamp at the start", async t => {
    const h = epubHarness(t, {initial: [2, 1]});
    for (let i = 0; i < 10; i++) h.rendition.prev();
    await h.flush();
    assert.deepEqual(h.displayed, ["0:200"]);
    assert.deepEqual(h.measured, [1, 0]);
    for (let i = 0; i < 10; i++) h.rendition.prev();
    await h.flush();
    assert.deepEqual(h.displayed, ["0:200", 0]);
});

test("EPUB page turns account for spreads and RTL layout coordinates", async t => {
    const h = epubHarness(t, {rtl: true, spread: 2});
    h.rendition.next();
    await h.flush();
    assert.deepEqual(h.displayed, ["0:0"]);
});
