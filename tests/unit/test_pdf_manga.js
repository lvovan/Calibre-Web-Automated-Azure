/* Run with node --test tests\unit\test_pdf_manga.js */
"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const manga = require("../../cps/static/js/reading/pdf-manga.js");
const fs = require("node:fs");
const vm = require("node:vm");
const {EventEmitter} = require("node:events");
const navigation = require("../../cps/static/js/reading/navigation.js");

test("PDFs whose sampled pages carry no real text are treated as manga", () => {
    assert.equal(manga.looksLikeManga([0, 3, 12]), true);
    assert.equal(manga.looksLikeManga([0, 1800, 0]), false);
    assert.equal(manga.looksLikeManga([]), false);
    assert.deepEqual(manga.samplePages(1), [1]);
    assert.deepEqual(manga.samplePages(200), [2, 100, 199]);
});

test("taps, swipes and arrow keys follow the reading direction", () => {
    assert.equal(manga.tapDirection(10, 300, false), -1);
    assert.equal(manga.tapDirection(150, 300, false), 0);
    assert.equal(manga.tapDirection(290, 300, false), 1);
    assert.equal(manga.tapDirection(10, 300, true), 1);

    assert.equal(manga.swipeDirection(-120, 10, false), 1);
    assert.equal(manga.swipeDirection(-120, 10, true), -1);
    assert.equal(manga.swipeDirection(-30, 0, false), 0);
    assert.equal(manga.swipeDirection(-80, 100, false), 0);

    assert.equal(manga.keyDirection("ArrowLeft", true), 1);
    assert.equal(manga.keyDirection("ArrowRight", false), 1);
    assert.equal(manga.keyDirection("PageDown", false), 0);
});

test("full PDF toolbar turns are coalesced and touch swipes use the real cursor-tool API", async t => {
    t.mock.timers.enable({apis: ["setTimeout"]});
    const listeners = {};
    const elements = new Map();
    function element(id) {
        if (!elements.has(id)) elements.set(id, {classList: {toggle() {}}, setAttribute() {},
            scrollWidth: 800, clientWidth: 800, scrollHeight: 600, clientHeight: 600,
            addEventListener(name, callback) { this[name] = callback; }});
        return elements.get(id);
    }
    let page = 1;
    const changes = [];
    const app = {initializedPromise: Promise.resolve(), pdfDocument: {}, pagesCount: 40,
        eventBus: new EventEmitter(), pdfCursorTools: {activeTool: 0},
        pdfViewer: {annotationEditorMode: 0, isInPresentationMode: false},
        get page() { return page; }, set page(value) { changes.push(value); page = value; }};
    const window = {CwaNavigation: navigation, PDFViewerApplication: app,
        getSelection: () => ({isCollapsed: true}), localStorage: {getItem: () => "off"},
        addEventListener: (name, callback) => { listeners[name] = callback; },
        document: {body: {classList: {toggle() {}}}, getElementById: element,
            addEventListener: (name, callback) => { listeners["doc:" + name] = callback; }}};
    vm.runInNewContext(fs.readFileSync(require.resolve("../../cps/static/js/reading/pdf-manga.js"), "utf8"), {window, console});
    listeners.webviewerloaded();
    await new Promise(resolve => setImmediate(resolve));
    for (let i = 0; i < 10; i++) listeners["doc:click"]({target: {closest: () => ({id: "next"})},
        preventDefault() {}, stopImmediatePropagation() {}});
    t.mock.timers.tick(160);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(changes, [11]);
    const container = element("viewerContainer");
    container.touchstart({touches: [{clientX: 400, clientY: 100}]});
    container.touchend({touches: [], changedTouches: [{clientX: 200, clientY: 100}], cancelable: true, preventDefault() {}});
    t.mock.timers.tick(160);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(changes, [11, 12]);
    app.pdfViewer.annotationEditorMode = 1;
    container.touchstart({touches: [{clientX: 400, clientY: 100}]});
    container.touchend({touches: [], changedTouches: [{clientX: 200, clientY: 100}], cancelable: true, preventDefault() {}});
    t.mock.timers.tick(160);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(changes, [11, 12]);
});
