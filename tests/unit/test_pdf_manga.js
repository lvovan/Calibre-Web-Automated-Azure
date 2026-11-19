/* Run with node --test tests\unit\test_pdf_manga.js */
"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const manga = require("../../cps/static/js/reading/pdf-manga.js");

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
