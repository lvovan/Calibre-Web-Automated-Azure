/* Calibre-Web Automated – manga mode for the PDF reader: one fitted page at a time, tap/swipe
 * page turns, optional right-to-left reading and look-ahead loading of the next pages.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
(function (root) {
    "use strict";

    const SCROLL_VERTICAL = 0;
    const SCROLL_PAGE = 3;
    const SPREAD_NONE = 0;
    const SAMPLE_PAGES = 3;
    const MAX_TEXT_PER_PAGE = 40;
    const PREFETCH_AHEAD = 3;
    const SWIPE_DISTANCE = 50;

    // Image-only pages (scans, comics) carry little or no extractable text.
    function looksLikeManga(textLengths) {
        if (!textLengths.length) return false;
        return textLengths.every(length => length <= MAX_TEXT_PER_PAGE);
    }

    function samplePages(total) {
        const pages = new Set([Math.min(total, 2), Math.ceil(total / 2), Math.max(1, total - 1)]);
        return Array.from(pages).filter(page => page >= 1).slice(0, SAMPLE_PAGES);
    }

    // Returns +1 (next page), -1 (previous page) or 0 for a tap at x in a view of the given width.
    function tapDirection(x, width, rtl) {
        if (!(width > 0)) return 0;
        const direction = x < width / 3 ? -1 : (x > (2 * width) / 3 ? 1 : 0);
        return rtl ? -direction : direction;
    }

    function swipeDirection(dx, dy, rtl) {
        if (Math.abs(dx) < SWIPE_DISTANCE || Math.abs(dx) < 1.5 * Math.abs(dy)) return 0;
        const direction = dx < 0 ? 1 : -1;
        return rtl ? -direction : direction;
    }

    function keyDirection(key, rtl) {
        const direction = key === "ArrowRight" ? 1 : (key === "ArrowLeft" ? -1 : 0);
        return rtl ? -direction : direction;
    }

    const api = {looksLikeManga, samplePages, tapDirection, swipeDirection, keyDirection};
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
        return;
    }

    const config = root.cwaPdf || {};
    const prefix = "cwa.pdf." + (config.userId || "") + "." + (config.bookId || "") + ".";

    function stored(name) {
        try { return root.localStorage.getItem(prefix + name); } catch (error) { return null; }
    }

    function store(name, value) {
        try { root.localStorage.setItem(prefix + name, value); } catch (error) { /* private mode */ }
    }

    root.addEventListener("webviewerloaded", () => {
        const options = root.PDFViewerApplicationOptions;
        if (options && stored("manga") === "on") {
            options.set("scrollModeOnLoad", SCROLL_PAGE);
            options.set("spreadModeOnLoad", SPREAD_NONE);
            options.set("defaultZoomValue", "page-fit");
        }
        const app = root.PDFViewerApplication;
        if (!app) return;
        Promise.resolve(app.initializedPromise).then(() => attach(app))
            .catch(error => console.error("PDF manga mode unavailable", error));
    });

    function attach(app) {
        const doc = root.document;
        const container = doc.getElementById("viewerContainer");
        const mangaButton = doc.getElementById("cwaMangaMode");
        const rtlButton = doc.getElementById("cwaMangaRtl");
        let manga = stored("manga") === "on";
        let rtl = stored("rtl") === "on";
        const prefetched = new Set();

        function render() {
            doc.body.classList.toggle("cwa-manga", manga);
            for (const [button, on] of [[mangaButton, manga], [rtlButton, rtl]]) {
                if (!button) continue;
                button.classList.toggle("toggled", on);
                button.setAttribute("aria-checked", String(on));
            }
            if (rtlButton) rtlButton.hidden = !manga;
        }

        function apply(on) {
            manga = on;
            const viewer = app.pdfViewer;
            if (viewer && app.pdfDocument) {
                viewer.spreadMode = SPREAD_NONE;
                viewer.scrollMode = on ? SCROLL_PAGE : SCROLL_VERTICAL;
                viewer.currentScaleValue = on ? "page-fit" : "auto";
            }
            render();
            if (on) prefetch(app.page);
        }

        function prefetch(current) {
            const pdf = app.pdfDocument;
            if (!manga || !pdf) return;
            for (let page = current + 1; page <= Math.min(app.pagesCount, current + PREFETCH_AHEAD); page++) {
                if (prefetched.has(page)) continue;
                prefetched.add(page);
                // Parsing the page pulls its byte ranges and images from the server ahead of time.
                pdf.getPage(page).then(proxy => proxy.getOperatorList())
                    .catch(() => prefetched.delete(page));
            }
        }

        function zoomed() {
            return !!container && (container.scrollWidth > container.clientWidth + 2
                || container.scrollHeight > container.clientHeight + 2);
        }

        function turn(direction) {
            if (!direction) return false;
            return direction > 0 ? app.pdfViewer.nextPage() : app.pdfViewer.previousPage();
        }

        function detect() {
            const pdf = app.pdfDocument;
            const pages = samplePages(app.pagesCount);
            return Promise.all(pages.map(number => pdf.getPage(number)
                .then(page => page.getTextContent())
                .then(content => content.items.reduce((sum, item) => sum + (item.str || "").trim().length, 0))))
                .then(looksLikeManga);
        }

        if (mangaButton) {
            mangaButton.addEventListener("click", () => {
                store("manga", manga ? "off" : "on");
                apply(!manga);
                app.secondaryToolbar.close();
            });
        }
        if (rtlButton) {
            rtlButton.addEventListener("click", () => {
                rtl = !rtl;
                store("rtl", rtl ? "on" : "off");
                render();
            });
        }

        app.eventBus.on("documentinit", () => {
            prefetched.clear();
            render();
            if (manga) {
                prefetch(app.page);
            } else if (stored("manga") === null) {
                detect().then(isManga => { if (isManga && !manga) apply(true); })
                    .catch(error => console.debug("PDF manga detection skipped", error));
            }
        });
        app.eventBus.on("pagechanging", event => prefetch(event.pageNumber));

        if (container) {
            container.addEventListener("click", event => {
                if (!manga || event.defaultPrevented || event.button !== 0) return;
                if (event.target.closest("a, button, input, select, textarea, .annotationLayer section")) return;
                const selection = root.getSelection && root.getSelection();
                if (selection && !selection.isCollapsed) return;
                const box = container.getBoundingClientRect();
                turn(tapDirection(event.clientX - box.left, box.width, rtl));
            });
            let start = null;
            container.addEventListener("touchstart", event => {
                start = event.touches.length === 1 ? {x: event.touches[0].clientX, y: event.touches[0].clientY} : null;
            }, {passive: true});
            container.addEventListener("touchend", event => {
                if (!manga || !start || event.changedTouches.length !== 1 || zoomed()) { start = null; return; }
                const touch = event.changedTouches[0];
                const direction = swipeDirection(touch.clientX - start.x, touch.clientY - start.y, rtl);
                start = null;
                if (turn(direction)) event.preventDefault();
            });
        }

        root.addEventListener("keydown", event => {
            if (!manga || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || zoomed()) return;
            const target = event.target;
            if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
            const direction = keyDirection(event.key, rtl);
            if (!direction) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            turn(direction);
        }, true);

        render();
    }
})(typeof window !== "undefined" ? window : globalThis);
