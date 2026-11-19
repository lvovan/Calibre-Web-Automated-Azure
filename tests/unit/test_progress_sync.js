/* Run with node --test tests\unit\test_progress_sync.js */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "..", "cps", "static", "js", "reading", "progress-sync.js"), "utf8");

function load(globals = {}) {
    const context = {console, ...globals};
    vm.runInNewContext(source, context);
    return context.CwaProgressSync;
}

// Minimal DOM nodes: enough structure for XPointer generation and resolution.
function el(name, ...children) {
    const node = {nodeType: 1, nodeName: name.toUpperCase(), localName: name, childNodes: children, parentNode: null};
    children.forEach(child => { child.parentNode = node; });
    return node;
}
function text(value) {
    return {nodeType: 3, nodeName: "#text", nodeValue: value, parentNode: null};
}

test("XPointers follow crengine indexing and resolve back to the same position", () => {
    const sync = load();
    const target = text("Hello   wide world");
    const body = el("body", text("\n  "), el("div", el("p", text("one")), el("p", el("b", text("x")), target)), el("p"));
    el("html", body);
    const xp = sync.xpointerFromNode(2, target, 10);
    assert.equal(xp, "/body/DocFragment[3]/body/div/p[2]/text().8");

    const parsed = sync.parseXPointer(xp);
    assert.equal(parsed.fragment, 2);
    const found = sync.resolveXPointer(body, parsed);
    assert.equal(found.node, target);
    assert.equal(found.offset, 10);
    assert.equal(found.exact, true);
});

test("XPointer parsing accepts KOReader variants and rejects other progress formats", () => {
    const sync = load();
    assert.deepEqual(JSON.parse(JSON.stringify(sync.parseXPointer("/body/DocFragment[12]/body/div[1]/p[3]/text()[2].7"))),
        {fragment: 11, steps: [{name: "div", index: 1}, {name: "p", index: 3}], text: {index: 2, offset: 7}});
    assert.equal(sync.parseXPointer("/body/DocFragment/body/p.0").fragment, 0);
    assert.equal(sync.parseXPointer("/body/DocFragment[4]").steps.length, 0);
    assert.equal(sync.parseXPointer("12"), null);
    assert.equal(sync.parseXPointer("cwa-txt:0.5"), null);
    assert.equal(sync.parsePage("12"), 12);
    assert.equal(sync.parsePage("/body/DocFragment[1]"), null);
});

test("unresolvable XPointer steps fall back to the deepest existing element", () => {
    const sync = load();
    const div = el("div", el("p"));
    const body = el("body", div);
    const found = sync.resolveXPointer(body, sync.parseXPointer("/body/DocFragment[1]/body/div/section[2]/p.0"));
    assert.equal(found.node, div);
    assert.equal(found.exact, false);
    assert.equal(sync.xpointerFromNode(0, null, 0), "/body/DocFragment[1]/body");
});

function sessionHarness(state = null) {
    const requests = [];
    const timers = [];
    const appended = [];
    const storage = new Map();
    const listeners = {};
    const document = {
        visibilityState: "visible",
        head: {appendChild() {}},
        body: {appendChild: node => appended.push(node)},
        addEventListener: (name, fn) => { listeners[name] = fn; },
        createElement: () => {
            const node = {children: [], className: "", textContent: "", attrs: {},
                setAttribute(key, value) { this.attrs[key] = value; },
                appendChild(child) { this.children.push(child); },
                addEventListener(name, fn) { this.click = fn; },
                remove() { this.removed = true; }};
            return node;
        }
    };
    let response = {};
    const sync = load({
        document,
        localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)},
        crypto: {randomUUID: () => "device-1"},
        addEventListener: () => {},
        setInterval: () => 0,
        setTimeout: (fn) => { timers.push(fn); return timers.length; },
        clearTimeout: () => {},
        fetch: async (url, init) => {
            requests.push({url, init, body: init.body && JSON.parse(init.body)});
            return {ok: true, json: async () => typeof response === "function" ? response(init) : response};
        }
    });
    const remote = [];
    const session = sync.create({url: "/kosync/reader/1", device: "CWA Web Reader", state,
        messages: {remote: "Continue from {percent} on {device}?", jump: "Continue", dismiss: "Dismiss"}},
    {csrfToken: "token", onRemote: s => remote.push(s)});
    return {session, requests, timers, appended, remote, listeners, setResponse: r => { response = r; },
        runTimers: async () => { timers.splice(0).forEach(fn => fn()); await new Promise(r => setImmediate(r)); }};
}

test("saves are debounced, deduplicated and sent with the CSRF token and device id", async () => {
    const h = sessionHarness({progress: "3", percentage: 0.1, timestamp: 10, device_id: "other"});
    h.session.save("3", 0.1);
    assert.equal(h.timers.length, 0);
    h.session.save("4", 0.2);
    h.session.save("5", 0.25);
    h.setResponse(init => ({...JSON.parse(init.body), timestamp: 20}));
    await h.runTimers();
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].init.method, "PUT");
    assert.equal(h.requests[0].init.headers["X-CSRFToken"], "token");
    assert.deepEqual(h.requests[0].body, {progress: "5", percentage: 0.25, device: "CWA Web Reader", device_id: "device-1"});
    assert.equal(h.session.state.timestamp, 20);
});

test("newer progress from another device prompts to jump; own progress does not", async () => {
    const h = sessionHarness({progress: "3", percentage: 0.1, timestamp: 10, device_id: "device-1"});
    h.setResponse({progress: "3", percentage: 0.1, timestamp: 10, device_id: "device-1"});
    assert.equal(await h.session.check(), null);
    assert.equal(h.appended.length, 0);

    h.setResponse({progress: "40", percentage: 0.4, timestamp: 30, device: "Kobo", device_id: "kobo"});
    assert.equal((await h.session.check()).progress, "40");
    const prompt = h.appended[0];
    assert.equal(prompt.children[0].textContent, "Continue from 40% on Kobo?");
    prompt.children[1].click();
    assert.deepEqual(h.remote.map(s => s.progress), ["40"]);

    assert.equal(await h.session.check(), null);  // already known
});

test("sync is unavailable without server configuration", () => {
    assert.equal(load().create(null, {}), null);
});

function pdfHarness(state) {
    const pdfSource = fs.readFileSync(path.join(__dirname, "..", "..", "cps", "static", "js", "reading", "pdf-progress.js"), "utf8");
    const saves = [];
    const handlers = {};
    let options;
    const app = {
        pagesCount: 10,
        _page: 1,
        initializedPromise: Promise.resolve(),
        eventBus: {on: (name, fn) => { handlers[name] = fn; }},
        get page() { return this._page; },
        set page(value) {
            if (value === this._page) return;
            this._page = value;
            handlers.pagechanging({pageNumber: value});
        }
    };
    const listeners = {};
    const context = {
        console,
        location: {hash: ""},
        addEventListener: (name, fn) => { listeners[name] = fn; },
        cwaPdf: {progressSync: {}, csrfToken: "token"},
        PDFViewerApplication: app,
        CwaProgressSync: {
            parsePage: value => (/^\d+$/.test(value || "") ? parseInt(value, 10) : null),
            create: (config, opts) => {
                options = opts;
                return {state, isOwn: s => s.device_id === "me", save: (progress, pct) => saves.push(progress)};
            }
        }
    };
    context.window = context;
    vm.runInNewContext(pdfSource, context);
    return {app, saves, handlers, remote: s => options.onRemote(s),
        ready: async () => { listeners.webviewerloaded(); await new Promise(r => setImmediate(r)); }};
}

test("PDF restores and synchronized jumps do not save progress; user page turns do", async () => {
    const h = pdfHarness({progress: "4", percentage: 0.4, device_id: "kobo"});
    await h.ready();
    h.handlers.documentinit();
    assert.equal(h.app.page, 4);
    h.remote({progress: "7", percentage: 0.7, device_id: "kobo"});
    assert.equal(h.app.page, 7);
    h.remote({progress: "7", percentage: 0.7, device_id: "kobo"});
    assert.deepEqual(h.saves, []);
    h.app.page = 8;
    assert.deepEqual(h.saves, ["8"]);
});
