/* global reader, calibre */
(function () {
    "use strict";

    const book = reader.book;
    const rendition = reader.rendition;
    const progress = document.getElementById("progress");
    const locationKey = calibre.positionKey;
    const cacheKey = calibre.storageKey + ".locations";
    let indexed = false;
    let navigated = false;
    // Only positions the reader moved to are sent to the server, not restored or re-laid-out ones.
    let userMoved = false;
    let indexing;

    function interacted(event) {
        if (event.type !== "keydown" || ["ArrowLeft", "ArrowRight", "PageUp", "PageDown"].includes(event.key)) {
            navigated = true;
            userMoved = true;
        }
    }
    ["pointerdown", "touchstart", "keydown"].forEach(name => document.addEventListener(name, interacted, {capture: true, passive: true}));
    rendition.on("keydown", interacted);
    rendition.on("touchstart", interacted);
    rendition.on("mousedown", interacted);

    function update(location) {
        if (!location || !location.start || !location.end) return;
        let fraction;
        if (typeof location.end.percentage === "number" && calibre.imagePages) {
            fraction = location.end.percentage;
        } else if (indexed && !calibre.manga) {
            fraction = book.locations.percentageFromCfi(location.end.cfi);
        } else {
            const sections = book.spine.spineItems.filter(section => section.linear);
            const index = sections.findIndex(section => section.index === location.end.index);
            const displayed = location.end.displayed;
            const within = displayed && displayed.total ? displayed.page / displayed.total : 1;
            fraction = (Math.max(0, index) + within) / sections.length;
        }
        const percentage = Math.max(0, Math.min(100, fraction * 100));
        progress.textContent = Math.round(percentage) + "%";
        const xpointer = xpointerFor(location.start);
        const previous = stored();
        const record = {revision: calibre.revision, cfi: location.start.cfi, percentage, xpointer};
        if (calibre.sync && userMoved) {
            record.pending = true;
            record.savedAt = Date.now();
            calibre.sync.save(xpointer, percentage / 100);
        } else if (previous && previous.cfi === record.cfi && previous.pending) {
            Object.assign(record, {pending: true, savedAt: previous.savedAt, timestamp: previous.timestamp});
        } else if (calibre.sync && calibre.sync.state) {
            record.timestamp = calibre.sync.state.timestamp;
        }
        calibre.storage.set(locationKey, JSON.stringify(record));
        calibre.storage.set("calibre.reader.progress." + calibre.bookUrl, String(percentage));
    }
    rendition.on("relocated", update);

    function stored() {
        try { return JSON.parse(calibre.storage.get(locationKey)); }
        catch (error) { return null; }
    }

    /** KOReader XPointer for the start of the visible page. */
    function xpointerFor(start) {
        const sync = window.CwaProgressSync;
        if (!sync) return null;
        if (!calibre.manga && typeof rendition.getRange === "function") {
            try {
                const range = rendition.getRange(start.cfi);
                if (range) {
                    let node = range.startContainer;
                    let offset = range.startOffset;
                    if (node.nodeType === 1 && node.childNodes[offset]) {
                        node = node.childNodes[offset];
                        offset = 0;
                    }
                    return sync.xpointerFromNode(start.index, node, offset);
                }
            } catch (error) {
                console.warn("Could not map the EPUB position for KOReader", error);
            }
        }
        return sync.xpointerFromNode(start.index, null, 0);
    }

    calibre.onProgressSaved = function (state) {
        const record = stored();
        if (record && record.xpointer === state.progress) {
            record.pending = false;
            record.timestamp = state.timestamp;
            calibre.storage.set(locationKey, JSON.stringify(record));
        }
    };

    function targetForPercentage(percent) {
        const fraction = Math.max(0, Math.min(1, percent / 100));
        if (calibre.imagePages) {
            const pages = calibre.epubData.pages;
            return pages[Math.min(pages.length - 1, Math.max(0, Math.ceil(fraction * pages.length) - 1))].index;
        }
        const sections = book.spine.spineItems.filter(section => section.linear);
        return indexed ? book.locations.cfiFromPercentage(fraction) :
            sections[Math.min(sections.length - 1, Math.max(0, Math.ceil(fraction * sections.length) - 1))].index;
    }

    calibre.onRemoteProgress = async function (state) {
        userMoved = false;
        navigated = true;
        try {
            let target = await calibre.resolveProgress(book, state);
            if (target === null || target === undefined) target = targetForPercentage(state.percentage * 100);
            if (target !== null && target !== undefined) await rendition.display(target);
        } catch (error) {
            console.error("Could not open the synchronized reading position", error);
        }
    };

    async function locations() {
        if (indexing) return indexing;
        indexing = (async function () {
            const cached = calibre.storage.get(cacheKey);
            if (cached) {
                try {
                    const parsed = JSON.parse(cached);
                    if (parsed.revision === calibre.revision && Array.isArray(parsed.entries) && parsed.entries.length &&
                        parsed.entries.every(cfi => typeof cfi === "string" && cfi.startsWith("epubcfi("))) {
                        book.locations.load(parsed.entries);
                        indexed = true;
                        return;
                    }
                } catch (error) {
                    console.warn("Discarding invalid EPUB location cache", error);
                }
                calibre.storage.remove(cacheKey);
            }
            // A separate locations object avoids unloading the section currently on screen.
            const scanner = new book.locations.constructor(book.spine, book.load.bind(book), 100);
            const entries = [];
            for (const section of book.spine.spineItems) {
                if (!section.linear) continue;
                const document = await book.load(section.url);
                for (const cfi of scanner.parse(document.documentElement, section.cfiBase)) entries.push(cfi);
                await new Promise(resolve => {
                    if (typeof requestIdleCallback === "function") requestIdleCallback(resolve, {timeout: 1000});
                    else setTimeout(resolve, 100);
                });
            }
            if (entries.length) {
                book.locations.load(entries);
                indexed = true;
                calibre.storage.set(cacheKey, JSON.stringify({revision: calibre.revision, entries}));
            }
        })();
        return indexing;
    }

    /** First unmatched KOReader position (newest first) that points to real content in this book. */
    async function matchingCandidate(candidates) {
        const sync = window.CwaProgressSync;
        const linear = book.spine.spineItems.filter(section => section.linear);
        for (const candidate of candidates) {
            const parsed = sync.parseXPointer(candidate.progress);
            const section = parsed && book.spine.get(parsed.fragment);
            if (!section || !parsed.steps.length) continue;
            // A position deep in another book can resolve here by coincidence; its percentage would not fit.
            const position = Math.max(0, linear.indexOf(section)) / Math.max(1, linear.length);
            if (Math.abs(position - candidate.percentage) > 0.2) continue;
            try {
                const doc = await book.load(section.url);
                const body = doc && (doc.body || doc.getElementsByTagName("body")[0]);
                const found = body && sync.resolveXPointer(body, parsed);
                if (found && found.exact && found.node !== body &&
                    (!parsed.text || found.offset <= found.node.nodeValue.length)) return candidate;
            } catch (error) {
                console.warn("Could not check a KOReader position", error);
            }
        }
        return null;
    }

    async function offerUnmatchedProgress() {
        const candidates = calibre.sync && calibre.sync.candidates;
        if (!candidates || !candidates.length) return;
        const candidate = await matchingCandidate(candidates);
        if (!candidate) return;
        calibre.sync.offer(candidate, async function () {
            try {
                await calibre.sync.link(candidate);
            } catch (error) {
                console.warn("Could not link the KOReader document to this book", error);
            }
            await calibre.onRemoteProgress(candidate);
        });
    }

    reader.displayed.then(offerUnmatchedProgress).catch(error => console.warn("Could not offer KOReader progress", error));
    reader.displayed.then(async function () {
        if (!calibre.manga) await locations();
        const legacy = calibre.legacyPercent;
        if (!calibre.hasExactPosition && !navigated && Number.isFinite(legacy) && legacy > 0) {
            const target = targetForPercentage(legacy);
            if (target !== undefined && target !== null && !navigated) await rendition.display(target);
        }
        update(rendition.currentLocation());
    }).catch(error => {
        console.error("EPUB progress initialization failed", error);
        document.getElementById("reader-status").textContent = calibre.messages.progressError;
    });
})();
