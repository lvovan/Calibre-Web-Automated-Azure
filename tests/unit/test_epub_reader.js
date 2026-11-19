/* Run with node --test tests\unit\test_epub_reader.js */
"use strict";

const assert = require("node:assert/strict");
const {EventEmitter} = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "..", "cps", "static", "js", "reading", "epub-progress.js"), "utf8");
const syncSource = fs.readFileSync(path.join(__dirname, "..", "..", "cps", "static", "js", "reading", "progress-sync.js"), "utf8");

function loadSyncModule() {
    const context = {};
    vm.runInNewContext(syncSource, context);
    return context.CwaProgressSync;
}

function progressHarness({manga = false, exact = false, cached = null, legacy = null, sync = null} = {}) {
    const saved = new Map();
    if (cached) saved.set("book.locations", JSON.stringify(cached));
    const events = new EventEmitter();
    const nodes = {progress: {textContent: ""}, "reader-status": {textContent: ""}};
    const rendition = new EventEmitter();
    let cfi = "epubcfi(/6/2!/4/2/1:0)";
    const location = () => ({start: {cfi, index: 0}, end: {cfi, index: 0, displayed: {page: 1, total: 1}}});
    const displayed = [];
    rendition.currentLocation = location;
    rendition.display = async target => { displayed.push(target); };
    let scanned = 0;
    class Locations {
        parse() { return ["epubcfi(/6/2!/4/2/1:0)", "epubcfi(/6/4!/4/2/1:0)"]; }
        load(entries) { this.entries = entries; }
        percentageFromCfi() { return 0.375; }
        cfiFromPercentage(fraction) { return "restored:" + fraction; }
    }
    const book = {locations: new Locations(), spine: {spineItems: [{linear: true, index: 0, url: "p0", cfiBase: "/6/2"},
        {linear: false, index: 1, url: "notes", cfiBase: "/6/4"}, {linear: true, index: 2, url: "p1", cfiBase: "/6/6"}]},
    load: async () => { scanned++; return {documentElement: {}}; }};
    let resolveDisplay;
    const reader = {book, rendition, displayed: new Promise(resolve => { resolveDisplay = resolve; })};
    const calibre = {storageKey: "book", revision: "v1", positionKey: "position", bookUrl: "original.epub",
        manga, hasExactPosition: exact, legacyPercent: legacy, messages: {progressError: "progress failed"},
        storage: {get: key => saved.get(key), set: (key, value) => saved.set(key, value), remove: key => saved.delete(key)}};
    if (sync) calibre.sync = sync;
    vm.runInNewContext(source, {reader, calibre, console, window: {CwaProgressSync: loadSyncModule()},
        document: {getElementById: id => nodes[id],
        addEventListener: (name, callback) => events.on(name, callback)}, setTimeout: fn => queueMicrotask(fn)});
    return {reader, rendition, saved, displayed, nodes, events, calibre, scanned: () => scanned,
        finish: async () => { resolveDisplay(); await new Promise(resolve => setImmediate(resolve)); },
        relocate: value => { cfi = value; rendition.emit("relocated", location()); }};
}

test("text indexing uses the existing book and persists an exact CFI", async () => {
    const h = progressHarness({exact: true, legacy: 50});
    await h.finish();
    assert.equal(h.scanned(), 2);
    assert.deepEqual(h.displayed, []);
    h.relocate("epubcfi(/6/2!/4/8/1:17)");
    assert.equal(JSON.parse(h.saved.get("position")).cfi, "epubcfi(/6/2!/4/8/1:17)");
    assert.equal(h.nodes.progress.textContent, "38%");
});

test("a matching revision reuses the text index without loading sections", async () => {
    const h = progressHarness({cached: {revision: "v1", entries: ["epubcfi(/6/2!/4)"]}});
    await h.finish();
    assert.equal(h.scanned(), 0);
});

test("a replaced book rebuilds its index", async () => {
    const h = progressHarness({cached: {revision: "old", entries: ["epubcfi(/6/2!/4)"]}});
    await h.finish();
    assert.equal(h.scanned(), 2);
    assert.equal(JSON.parse(h.saved.get("book.locations")).revision, "v1");
});

test("user navigation prevents a delayed legacy percentage jump", async () => {
    const h = progressHarness({legacy: 75});
    h.events.emit("pointerdown", {type: "pointerdown"});
    await h.finish();
    assert.deepEqual(h.displayed, []);
});

test("legacy percentages migrate only when there is no exact position", async () => {
    const h = progressHarness({legacy: 75});
    await h.finish();
    assert.deepEqual(h.displayed, ["restored:0.75"]);
});

test("manga progress does not scan text and excludes non-linear sections", async () => {
    const h = progressHarness({manga: true});
    await h.finish();
    assert.equal(h.scanned(), 0);
    assert.equal(h.nodes.progress.textContent, "50%");
    h.relocate("epubcfi(/6/2!/4)");
    assert.equal(JSON.parse(h.saved.get("position")).revision, "v1");
});

function fakeSync(state = null) {
    const saves = [];
    return {saves, state, save: (progress, percentage) => saves.push([progress, percentage])};
}

test("only positions the reader navigated to are sent to the sync server", async () => {
    const sync = fakeSync({progress: "/body/DocFragment[1]/body", timestamp: 100});
    const h = progressHarness({manga: true, exact: true, sync});
    await h.finish();
    h.relocate("epubcfi(/6/6!/4)");
    assert.deepEqual(sync.saves, []);
    assert.equal(JSON.parse(h.saved.get("position")).timestamp, 100);

    h.events.emit("keydown", {type: "keydown", key: "ArrowRight"});
    h.relocate("epubcfi(/6/2!/4)");
    assert.deepEqual(sync.saves, [["/body/DocFragment[1]/body", 0.5]]);
    const record = JSON.parse(h.saved.get("position"));
    assert.equal(record.pending, true);
    assert.equal(record.xpointer, "/body/DocFragment[1]/body");

    h.calibre.onProgressSaved({progress: "/body/DocFragment[1]/body", timestamp: 200});
    assert.deepEqual([JSON.parse(h.saved.get("position")).pending, JSON.parse(h.saved.get("position")).timestamp], [false, 200]);
});

test("a remote position is displayed without being echoed back", async () => {
    const sync = fakeSync();
    const h = progressHarness({manga: true, exact: true, sync});
    h.calibre.resolveProgress = async (book, state) => state.progress === "/body/DocFragment[3]/body" ? 2 : null;
    await h.finish();
    h.events.emit("pointerdown", {type: "pointerdown"});
    await h.calibre.onRemoteProgress({progress: "/body/DocFragment[3]/body", percentage: 1});
    await h.calibre.onRemoteProgress({progress: "12", percentage: 0.4});
    assert.deepEqual(h.displayed, [2, 0]);
    h.relocate("epubcfi(/6/6!/4)");
    assert.deepEqual(sync.saves, []);
});

test("manga can migrate percentage progress without a text index", async () => {
    const h = progressHarness({manga: true, legacy: 100});
    await h.finish();
    assert.deepEqual(h.displayed, [2]);
    assert.equal(h.scanned(), 0);
});

const mangaSource = fs.readFileSync(path.join(__dirname, "..", "..", "cps", "static", "js", "reading", "epub-manga.js"), "utf8");

function mangaHarness({rtl = false, fail = false, cwa = true} = {}) {
    const stage = {style: {}, replaceChildren(...images) { this.children = images; }, remove() {}};
    const element = {classList: {add() {}}, appendChild() {}, scrollTo() {}};
    class Image {
        set src(value) { this.url = value; queueMicrotask(() => fail ? this.onerror?.() : this.onload?.()); }
        removeAttribute() {}
    }
    const pages = Array.from({length: 30}, (_, i) => ({index: i, image: "images/p" + i + ".png", href: "p" + i + ".xhtml"}));
    const sections = pages.map((page, i) => ({index: i, cfiBase: "/6/" + (2 * i + 2), href: page.href}));
    class Book {
        constructor() {
            this.settings = {cwaReader: cwa};
            this.ready = Promise.resolve();
            this.spine = {get(target) {
                if (typeof target === "number") return sections[target];
                if (target.startsWith("epubcfi(")) return sections[(Number(target.match(/\/6\/(\d+)/)[1]) - 2) / 2];
                return sections.find(section => section.href === target);
            }};
        }
        renderTo() { return "original-renderer"; }
    }
    const calibre = {manga: true, renditionOptions: {spread: "none"},
        epubData: {baseUrl: "/epub/1/v1/", pages, direction: rtl ? "rtl" : "ltr"}};
    vm.runInNewContext(mangaSource, {calibre, ePub: {Book}, Image, DOMException, console, window: {},
        document: {createElement: () => stage, getElementById: () => element},
        RSVP: {EventTarget: {mixin(target) {
            target.on = EventEmitter.prototype.on;
            target.trigger = EventEmitter.prototype.emit;
        }}}});
    const book = new Book();
    const rendition = book.renderTo("viewer", {});
    return {rendition, stage, calibre};
}

test("manga renderer keeps only visible and neighboring images", async () => {
    const {rendition, stage} = mangaHarness();
    await rendition.display();
    for (let i = 0; i < 20; i++) await rendition.next();
    assert.equal(rendition.index, 20);
    assert.equal(stage.children.length, 1);
    assert.ok(rendition.images.size <= 3);
    await rendition.spread(true);
    assert.equal(stage.children.length, 2);
    assert.ok(rendition.images.size <= 4);
    await rendition.display(28);
    await rendition.next();
    assert.equal(rendition.index, 28);
});

test("manga restoration, rapid navigation and RTL order preserve the spine", async () => {
    const {rendition, stage} = mangaHarness({rtl: true});
    await rendition.display("epubcfi(/6/12!/4)");
    assert.equal(rendition.index, 5);
    assert.equal(stage.dir, "rtl");
    await Promise.all(Array.from({length: 8}, () => rendition.next()));
    assert.equal(rendition.currentLocation().start.index, 13);
    assert.ok(rendition.images.size <= 3);
});

test("page failures are surfaced through displayerror", async () => {
    const {rendition} = mangaHarness({fail: true});
    let error;
    rendition.on("displayerror", value => { error = value; });
    await assert.rejects(rendition.display(), /Unable to load EPUB page/);
    assert.ok(error);
});

test("books outside CWA retain their original renderTo behavior", () => {
    assert.equal(mangaHarness({cwa: false}).rendition, "original-renderer");
});
