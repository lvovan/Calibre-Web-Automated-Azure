/* global ePub, RSVP, calibre */
(function () {
    "use strict";

    function resourceUrl(name) {
        return calibre.epubData.baseUrl + name.split("/").map(encodeURIComponent).join("/");
    }

    class MangaRendition {
        constructor(book, target, settings) {
            this.book = book;
            this.element = typeof target === "string" ? document.getElementById(target) : target;
            this.pages = calibre.epubData.pages;
            this.index = 0;
            this.sequence = 0;
            this.zoom = 1;
            this.settings = settings;
            this.images = new Map();
            this.started = book.ready;
            this.themes = {register() {}, select() {}, font() {}, fontSize() {}};
            this.stage = document.createElement("div");
            this.stage.className = "manga-stage";
            this.stage.dir = calibre.epubData.direction === "rtl" ? "rtl" : "ltr";
            this.element.classList.add("manga-viewer");
            this.element.appendChild(this.stage);
        }

        count() {
            return this.settings.spread === "none" ? 1 : 2;
        }

        load(index) {
            if (!this.images.has(index)) {
                const image = new Image();
                image.alt = "";
                image.draggable = false;
                const ready = new Promise((resolve, reject) => {
                    image.onload = () => resolve(image);
                    image.onerror = () => reject(new Error("Unable to load EPUB page " + (index + 1)));
                    image.cancelLoad = () => reject(new DOMException("EPUB page request cancelled", "AbortError"));
                });
                image.src = resourceUrl(this.pages[index].image);
                this.images.set(index, {image, ready});
            }
            return this.images.get(index).ready;
        }

        async display(target) {
            await this.book.ready;
            if (target !== undefined) {
                const section = this.book.spine.get(target);
                const index = section ? this.pages.findIndex(page => page.index === section.index) : -1;
                if (index < 0) {
                    const error = new Error("EPUB reading position is not in the reading order");
                    this.emit("displayerror", error);
                    throw error;
                }
                this.index = index;
            }
            const sequence = ++this.sequence;
            const first = this.index;
            const last = Math.min(first + this.count() - 1, this.pages.length - 1);
            const keep = new Set();
            for (let i = Math.max(0, first - 1); i <= Math.min(this.pages.length - 1, last + 1); i++) keep.add(i);
            this.images.forEach((value, index) => {
                if (!keep.has(index)) {
                    value.image.cancelLoad();
                    value.image.removeAttribute("src");
                    this.images.delete(index);
                }
            });
            try {
                const visible = await Promise.all(Array.from({length: last - first + 1}, (_, i) => this.load(first + i)));
                if (sequence !== this.sequence) return;
                this.stage.replaceChildren(...visible);
                this.element.scrollTo(this.stage.dir === "rtl" ? this.element.scrollWidth : 0, 0);
                const position = index => {
                    const page = this.pages[index];
                    const section = this.book.spine.get(page.index);
                    return {index: page.index, href: section.href,
                        cfi: "epubcfi(" + section.cfiBase + "!/4)",
                        displayed: {page: 1, total: 1}, percentage: (index + 1) / this.pages.length};
                };
                this.location = {start: position(first), end: position(last),
                    atStart: first === 0, atEnd: last === this.pages.length - 1};
                this.emit("rendered", this.book.spine.get(this.pages[first].index));
                this.emit("relocated", this.location);
                keep.forEach(index => {
                    if (index < first || index > last) {
                        this.load(index).catch(error => {
                            if (error.name === "AbortError") return;
                            this.images.delete(index);
                            console.warn("EPUB adjacent-page prefetch failed", error);
                        });
                    }
                });
            } catch (error) {
                if (sequence !== this.sequence) return;
                this.emit("displayerror", error);
                throw error;
            }
        }

        next() {
            if (this.index + this.count() >= this.pages.length) return Promise.resolve();
            this.index += this.count();
            return this.display();
        }

        prev() {
            this.index = Math.max(0, this.index - this.count());
            return this.display();
        }

        spread(value) {
            this.settings.spread = value;
            return this.display();
        }

        setZoom(value) {
            this.zoom = value;
            this.stage.style.width = (value * 100) + "%";
            this.stage.style.height = (value * 100) + "%";
        }

        currentLocation() { return this.location; }
        getContents() { return []; }
        resize() {}
        destroy() {
            this.sequence++;
            this.images.forEach(value => {
                value.image.cancelLoad();
                value.image.removeAttribute("src");
            });
            this.images.clear();
            this.stage.remove();
        }
    }
    RSVP.EventTarget.mixin(MangaRendition.prototype);
    MangaRendition.prototype.emit = MangaRendition.prototype.trigger;

    // The first display waits for an asynchronously resolved start position (e.g. a KOReader XPointer).
    function withInitialTarget(book, rendition) {
        const resolve = calibre.initialTarget;
        if (typeof resolve !== "function") return rendition;
        rendition.display = function (target) {
            delete rendition.display;
            return Promise.resolve().then(() => resolve(book)).catch(error => {
                console.warn("Could not resolve the synchronized reading position", error);
                return null;
            }).then(resolved => {
                if (resolved === null || resolved === undefined) calibre.hasExactPosition = false;
                return rendition.display(resolved ?? target);
            });
        };
        return rendition;
    }

    // The bundled reader hardcodes renderTo options; scope this extension to CWA books.
    const renderTo = ePub.Book.prototype.renderTo;
    ePub.Book.prototype.renderTo = function (target, options) {
        if (!this.settings.cwaReader) return renderTo.call(this, target, options);
        options = Object.assign({}, options, calibre.renditionOptions);
        if (calibre.manga && calibre.epubData && calibre.epubData.pages.length) {
            this.rendition = new MangaRendition(this, target, options);
            return withInitialTarget(this, this.rendition);
        }
        return withInitialTarget(this, renderTo.call(this, target, options));
    };
    window.epubResourceUrl = resourceUrl;
})();
