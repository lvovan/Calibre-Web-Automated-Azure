/* SPDX-License-Identifier: GPL-3.0-or-later */
(function (root) {
    "use strict";

    function attach(reader, calibre) {
        const rendition = reader.rendition;
        let turns = 0;
        const navigation = root.CwaNavigation.create({
            position: () => calibre.imagePages ? rendition.index : turns,
            bounds: () => calibre.imagePages ? [0, rendition.pages.length - 1] : [-Infinity, Infinity],
            navigate: async target => {
                if (calibre.imagePages) await rendition.display(rendition.pages[target].index);
                else {
                    await turnText(target - turns);
                    turns = target;
                }
            },
            onError: error => {
                console.error("EPUB page navigation failed", error);
                document.getElementById("reader-status").textContent = calibre.messages.displayError;
            }
        });
        rendition.next = () => navigation.move(calibre.imagePages ? rendition.count() : 1);
        rendition.prev = () => navigation.move(calibre.imagePages ? -rendition.count() : -1);

        // Reflowable books have viewport-dependent pages, not server page numbers. Measure only
        // sections crossed by the jump and display the final CFI once, never each skipped page.
        async function turnText(delta) {
            const location = rendition.currentLocation();
            if (!location || !location.start) return;
            const manager = rendition.manager;
            const layout = manager.layout;
            const spread = layout.divisor || 1;
            let section = reader.book.spine.get(location.start.index);
            let page = Math.floor((location.start.displayed.page - 1) / spread) + delta;
            let fromEnd = false;
            while (section) {
                let view = manager.views.find(section);
                let temporary = false;
                let host;
                try {
                    if (!view) {
                        temporary = true;
                        view = manager.createView(section);
                        host = document.createElement("div");
                        host.style.cssText = "position:absolute;visibility:hidden;pointer-events:none;overflow:hidden;left:0;top:0;width:" +
                            layout.width + "px;height:" + layout.height + "px";
                        manager.container.appendChild(host);
                        host.appendChild(view.element);
                        await view.display(reader.book.load.bind(reader.book));
                        await rendition.hooks.content.trigger(view.contents, rendition);
                        view.expand();
                    }
                    const vertical = manager.settings.axis === "vertical";
                    const size = vertical ? layout.height : layout.pageWidth;
                    const total = Math.max(1, Math.ceil((vertical ? view.height() : view.width()) / (size * spread)));
                    if (fromEnd) { page += total; fromEnd = false; }
                    if (page >= 0 && page < total) {
                        let offset = page * size * spread;
                        if (!vertical && manager.settings.direction === "rtl") offset = view.width() - (page + 1) * size * spread;
                        const mapped = manager.mapping.page(view.contents, section.cfiBase, Math.max(0, offset),
                            Math.max(0, offset) + size * spread);
                        if (!mapped || !mapped.start) throw new Error("Unable to map the EPUB destination page");
                        await rendition.display(mapped.start);
                        return;
                    }
                    if (page < 0) {
                        const previous = section.prev();
                        if (!previous) { await rendition.display(section.index); return; }
                        // The previous section's length is known only after it has been laid out.
                        section = previous;
                        fromEnd = true;
                    } else {
                        const next = section.next();
                        if (!next) {
                            const lastOffset = !vertical && manager.settings.direction === "rtl" ? 0 : (total - 1) * size * spread;
                            const mapped = manager.mapping.page(view.contents, section.cfiBase,
                                lastOffset, lastOffset + size * spread);
                            await rendition.display(mapped.start);
                            return;
                        }
                        page -= total;
                        section = next;
                    }
                } finally {
                    if (temporary) view.destroy();
                    if (host) host.remove();
                }
            }
        }

        const swipe = {
            move: direction => direction > 0 ? rendition.next() : rendition.prev(),
            rtl: () => reader.book.package.metadata.direction === "rtl",
            enabled: () => !(rendition.zoom > 1) && !(root.visualViewport && root.visualViewport.scale > 1)
        };
        root.CwaNavigation.bindSwipe(document.getElementById("viewer"), swipe);
        if (rendition.hooks) rendition.hooks.content.register(contents => root.CwaNavigation.bindSwipe(contents.document, swipe));
        // Image-mode document pages are sandboxed frames rather than epub.js contents.
        document.getElementById("viewer").addEventListener("load", event => {
            if (calibre.imagePages && event.target.tagName === "IFRAME" && event.target.contentDocument) {
                root.CwaNavigation.bindSwipe(event.target.contentDocument, swipe);
            }
        }, true);
        rendition.on("resized", () => navigation.cancel());
        document.getElementById("sidebar").addEventListener("click", () => navigation.cancel(), true);
        calibre.navigation = navigation;
    }

    root.CwaEpubNavigation = {attach};
})(window);
