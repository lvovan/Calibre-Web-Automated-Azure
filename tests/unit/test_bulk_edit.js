/* SPDX-License-Identifier: GPL-3.0-or-later */
"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {model, common, effective, setTitle, numberAt, sortBooks, moveBook, payload} = require("../../cps/static/js/bulk-edit.js");
function selection() {
    return model({fields: [], books: [
        {id: 20, version: "v20", values: {title: "Zebra", authors: "Common author", series_index: "4", tags: "A"}},
        {id: 10, version: "v10", values: {title: "Apple", authors: "Common author", series_index: "2", tags: "B"}}
    ]});
}
test("common values prefill, mixed fields remain blank and untouched", () => {
    const state = selection();
    assert.equal(common(state, "authors"), "Common author");
    assert.equal(common(state, "tags"), "");
    assert.deepEqual(payload(state).changes, {});
    state.changes.tags = "";
    assert.equal(effective(state, state.books[0], "tags"), "");
    delete state.changes.tags;
    assert.equal(effective(state, state.books[0], "tags"), "A");
});
test("individual titles override the shared title and survive reordering", () => {
    const state = selection();
    state.changes.title = "Shared title";
    setTitle(state, 20, "My individual title");
    moveBook(state, 20, 1);
    assert.equal(effective(state, state.books[1], "title"), "My individual title");
    assert.equal(effective(state, state.books[0], "title"), "Shared title");
    assert.deepEqual(payload(state).titles, {20: "My individual title"});
    delete state.titles[20];
    assert.equal(effective(state, state.books[1], "title"), "Shared title");
    delete state.changes.title;
    assert.equal(effective(state, state.books[1], "title"), "Zebra");
});
test("series numbering follows order and keeps original values when disabled", () => {
    const state = selection();
    sortBooks(state, "series");
    state.numbering = true;
    state.start = "0.5";
    state.increment = "1.25";
    assert.equal(numberAt(state, state.books[0], 0), "0.5");
    assert.equal(numberAt(state, state.books[1], 1), "1.75");
    assert.deepEqual(payload(state).book_ids, [10, 20]);
    assert.deepEqual(payload(state).versions, {10: "v10", 20: "v20"});
    sortBooks(state, "selection");
    assert.deepEqual(payload(state).book_ids, [20, 10]);
    state.numbering = false;
    assert.equal(numberAt(state, state.books[0], 0), "4");
    assert.equal(payload(state).numbering, null);
});
test("title sorting uses individual edits without losing identity or allowing out-of-range moves", () => {
    const state = selection();
    setTitle(state, 20, "Aardvark");
    sortBooks(state, "title");
    assert.deepEqual(state.books.map(book => book.id), [20, 10]);
    moveBook(state, 20, -1);
    assert.deepEqual(state.books.map(book => book.id), [20, 10]);
    setTitle(state, 20, "");
    assert.equal(payload(state).titles[20], "");
    setTitle(state, 20, "Zebra");
    assert.deepEqual(payload(state).titles, {});
});

test("selection controls track mode and selected count through clearing and exiting", () => {
    function element() {
        const classes = new Set();
        return {dataset: {}, children: [], events: {}, attributes: {},
            classList: {toggle(name, active) { if (active) classes.add(name); else classes.delete(name); }, contains: name => classes.has(name)},
            addEventListener(name, callback) { this.events[name] = callback; },
            setAttribute(name, value) { this.attributes[name] = value; },
            appendChild(child) { this.children.push(child); }};
    }
    const ids = Object.fromEntries(["bulk-book-config", "bulkBookModal", "bulk-book-select", "bulk-book-actions",
        "bulk-book-count", "bulk-book-edit", "bulk-book-all", "bulk-book-notice", "bulk-book-clear"].map(id => [id, element()]));
    ids["bulk-book-config"].textContent = JSON.stringify({text: {
        select: "Select", selectCount: "Select (%(count)s)", selected: "%(count)s selected", selectBook: "Select book %(id)s"
    }});
    const cards = [1, 2, 3].map(id => {
        const card = element(), cover = element(), link = {dataset: {bookId: String(id)}};
        card.offsetParent = {};
        card.querySelector = selector => selector === ".cover" ? cover : selector === ".book-cover-link[data-book-id]" ? link :
            selector === ".bulk-cover-check" ? cover.children[0] : cover.children[0]?.children[0];
        return card;
    });
    const grid = element();
    grid.querySelectorAll = () => cards;
    const document = {readyState: "complete", getElementById: id => ids[id], querySelector: () => grid, createElement: element};
    vm.runInNewContext(fs.readFileSync(require.resolve("../../cps/static/js/bulk-edit.js"), "utf8"), {
        document, window: {jQuery: () => ({on() { return this; }})}, MutationObserver: class { observe() {} }
    });
    const button = ids["bulk-book-select"];
    assert.equal(button.textContent, "Select");
    assert.equal(button.attributes["aria-pressed"], "false");
    assert.equal(ids["bulk-book-actions"].hidden, true);
    assert.equal(grid.classList.contains("bulk-selecting"), false);

    button.events.click();
    assert.equal(grid.classList.contains("bulk-selecting"), true);
    assert.equal(ids["bulk-book-actions"].hidden, false);
    grid.events.change({target: {dataset: {bulkId: "1"}}});
    assert.equal(button.textContent, "Select (1)");
    ids["bulk-book-all"].events.change({target: {checked: true}});
    assert.equal(button.textContent, "Select (3)");
    ids["bulk-book-clear"].events.click();
    assert.equal(button.textContent, "Select");
    assert.equal(grid.classList.contains("bulk-selecting"), true);
    grid.events.change({target: {dataset: {bulkId: "2"}}});
    button.events.click();
    assert.equal(button.textContent, "Select");
    assert.equal(grid.classList.contains("bulk-selecting"), false);
    assert.equal(ids["bulk-book-actions"].hidden, true);
    assert.equal(ids["bulk-book-edit"].disabled, true);
    cards.forEach(card => assert.equal(card.querySelector("[data-bulk-id]").checked, false));
});
