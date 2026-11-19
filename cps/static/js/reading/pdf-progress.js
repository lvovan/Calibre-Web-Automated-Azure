/* Calibre-Web Automated – per-user PDF reading progress, shared with KOReader devices.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
/* global PDFViewerApplication, CwaProgressSync */
(function () {
    "use strict";

    const config = window.cwaPdf || {};
    if (!config.progressSync || !window.CwaProgressSync) return;

    function pageFor(state, total) {
        if (!state || !total) return null;
        const page = CwaProgressSync.parsePage(state.progress);
        if (page !== null && page >= 1 && page <= total) return page;
        if (typeof state.percentage === "number" && state.percentage > 0) {
            return Math.min(total, Math.max(1, Math.round(state.percentage * total)));
        }
        return null;
    }

    function attach(app) {
        let restored = false;
        // Page set by a restore or a synchronized jump; its page change is not saved as new progress.
        let jumpPage = null;
        function jumpTo(page) {
            if (!page || page === app.page) return;
            jumpPage = page;
            app.page = page;
        }
        const sync = CwaProgressSync.create(config.progressSync, {
            csrfToken: config.csrfToken,
            onRemote: state => jumpTo(pageFor(state, app.pagesCount))
        });
        if (!sync) return;

        app.eventBus.on("documentinit", () => {
            const state = sync.state;
            // An explicit #page= link and this browser's own last position (kept by pdf.js) win.
            if (state && !sync.isOwn(state) && !/[#&]page=/.test(window.location.hash)) {
                jumpTo(pageFor(state, app.pagesCount));
            }
            restored = true;
        });
        app.eventBus.on("pagechanging", event => {
            const programmatic = jumpPage !== null && event.pageNumber === jumpPage;
            jumpPage = null;
            const total = app.pagesCount;
            if (restored && !programmatic && total > 0 && event.pageNumber >= 1) {
                sync.save(String(event.pageNumber), event.pageNumber / total);
            }
        });
    }

    window.addEventListener("webviewerloaded", () => {
        const app = window.PDFViewerApplication;
        if (!app) return;
        Promise.resolve(app.initializedPromise).then(() => attach(app))
            .catch(error => console.error("PDF progress sync unavailable", error));
    });
})();
