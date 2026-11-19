/* global $, calibre, EPUBJS, ePubReader */

var reader;

(function() {
    "use strict";

    EPUBJS.filePath = calibre.filePath;
    EPUBJS.cssPath = calibre.cssPath;

    calibre.storage = {
        get(key) {
            try { return localStorage.getItem(key); }
            catch (error) { console.warn("EPUB settings storage is unavailable", error); return null; }
        },
        set(key, value) {
            try { localStorage.setItem(key, value); }
            catch (error) { console.warn("Unable to save EPUB settings", error);
                document.getElementById("reader-status").textContent = calibre.messages.storageError; }
        },
        remove(key) {
            try { localStorage.removeItem(key); }
            catch (error) { console.warn("Unable to remove EPUB settings", error); }
        }
    };
    calibre.storageKey = "calibre.reader." + calibre.userId + "." + calibre.bookUrl;
    calibre.revision = calibre.epubData ? calibre.epubData.revision : "archive";
    calibre.positionKey = calibre.storageKey + ".position";
    const storedMode = calibre.storage.get(calibre.storageKey + ".mode");
    const mode = ["auto", "text", "manga"].includes(storedMode) ? storedMode : "auto";
    calibre.manga = mode === "manga" || (mode === "auto" && calibre.epubData && calibre.epubData.manga);
    document.body.classList.toggle("epub-manga", Boolean(calibre.manga));
    const modeControl = document.getElementById("reader-mode");
    modeControl.value = mode;
    modeControl.addEventListener("change", function () {
        calibre.storage.set(calibre.storageKey + ".mode", this.value);
        window.location.reload();
    });
    const spreadKey = calibre.manga ? calibre.storageKey + ".spread" : "calibre.reader.spread";
    const savedSpread = calibre.storage.get(spreadKey);
    calibre.renditionOptions = {spread: savedSpread === "spread" ? "auto" :
        (savedSpread === "nonespread" || calibre.manga ? "none" : "auto")};
    document.getElementById("spread").querySelector("span").textContent = calibre.renditionOptions.spread === "none" ? "" : "\u2713";
    document.getElementById("nonespread").querySelector("span").textContent = calibre.renditionOptions.spread === "none" ? "\u2713" : "";
    let position;
    let localRecord = null;
    const storedPosition = calibre.storage.get(calibre.positionKey);
    if (storedPosition) {
        try {
            const parsed = JSON.parse(storedPosition);
            if (parsed.revision === calibre.revision && typeof parsed.cfi === "string" && parsed.cfi.startsWith("epubcfi(")) {
                position = parsed.cfi;
                localRecord = parsed;
            }
        } catch (error) { console.warn("Invalid EPUB reading position", error); }
    }
    if (!position && !storedPosition) {
        const oldKey = "epubjsreader:" + EPUBJS.VERSION + ":" + window.location.host + ":" + calibre.bookUrl;
        const oldSettings = calibre.storage.get(oldKey);
        if (oldSettings) {
            try { position = JSON.parse(oldSettings).previousLocationCfi; }
            catch (error) { console.warn("Invalid legacy EPUB settings", error); }
        }
    }
    if (!position && calibre.bookmark.startsWith("epubcfi(")) position = calibre.bookmark;
    const legacy = calibre.storage.get("calibre.reader.progress." + calibre.bookUrl);
    calibre.legacyPercent = legacy !== null ? Number(legacy) : Number(calibre.kosyncPercent);

    // Server progress (shared with KOReader devices) wins unless this browser holds the same or a newer position.
    calibre.sync = window.CwaProgressSync.create(calibre.progressSync, {
        csrfToken: calibre.csrfToken,
        onRemote: state => calibre.onRemoteProgress && calibre.onRemoteProgress(state),
        onSaved: state => calibre.onProgressSaved && calibre.onProgressSaved(state)
    });
    const remote = calibre.sync && calibre.sync.state;
    const syncButton = document.getElementById("sync-now");
    if (syncButton && calibre.sync) {
        syncButton.addEventListener("click", function () {
            syncButton.disabled = true;
            calibre.sync.syncNow().finally(() => { syncButton.disabled = false; });
        });
    }
    const localIsCurrent = localRecord && remote && (
        (typeof localRecord.timestamp === "number" && localRecord.timestamp >= remote.timestamp) ||
        (localRecord.pending && localRecord.savedAt > remote.timestamp * 1000));
    calibre.remoteStart = null;
    if (remote && !localIsCurrent) {
        calibre.remoteStart = remote;
        position = undefined;
        calibre.legacyPercent = remote.percentage * 100;
        if (window.CwaProgressSync.parseXPointer(remote.progress)) {
            calibre.initialTarget = book => calibre.resolveProgress(book, remote);
        }
    } else if (localRecord && localRecord.pending && localRecord.xpointer && calibre.sync) {
        calibre.sync.save(localRecord.xpointer, localRecord.percentage / 100);
    }
    if (window.location.hash.startsWith("#epubcfi(")) {
        position = window.location.hash.slice(1);
        calibre.initialTarget = undefined;
    }
    if (typeof position !== "string" || !position.startsWith("epubcfi(")) position = undefined;
    calibre.hasExactPosition = Boolean(position || calibre.initialTarget);

    /** CFI (or spine index) for a KOReader XPointer, or null when it cannot be resolved. */
    calibre.resolveProgress = async function (book, state) {
        const parsed = window.CwaProgressSync.parseXPointer(state.progress);
        if (!parsed) return null;
        await book.ready;
        const section = book.spine.get(parsed.fragment);
        if (!section) return null;
        if (calibre.manga || !parsed.steps.length) return section.index;
        const doc = await book.load(section.url);
        const body = doc && (doc.body || doc.getElementsByTagName("body")[0]);
        if (!body) return section.index;
        const found = window.CwaProgressSync.resolveXPointer(body, parsed);
        if (found.node === body) return section.index;
        if (found.node.nodeType === 3) {
            const range = doc.createRange();
            range.setStart(found.node, found.offset);
            range.collapse(true);
            return new ePub.CFI(range, section.cfiBase).toString();
        }
        return new ePub.CFI(found.node, section.cfiBase).toString();
    };

    const source = calibre.epubData ? window.epubResourceUrl(calibre.epubData.opf) : calibre.bookUrl;
    window.reader = reader = ePubReader(source, {
        cwaReader: true,
        openAs: calibre.epubData ? "opf" : undefined,
        replacements: calibre.epubData ? "none" : undefined,
        restore: false,
        previousLocationCfi: position,
        bookmarks: calibre.bookmark ? [calibre.bookmark] : []
    });

    reader.displayed.catch(error => showReaderError(calibre.messages.openError, error));
    reader.book.ready.then(function () {
        reader.rendition.on("relocated", function (location) {
            const rtl = reader.book.package.metadata.direction === "rtl";
            document.getElementById(rtl ? "next" : "prev").classList.toggle("disabled", Boolean(location.atStart));
            document.getElementById(rtl ? "prev" : "next").classList.toggle("disabled", Boolean(location.atEnd));
        });
    });
    const zoomControl = document.getElementById("manga-zoom");
    const imageMode = typeof reader.rendition.setZoom === "function";
    calibre.imagePages = imageMode;
    document.getElementById("manga-controls").hidden = !imageMode;
    if (imageMode) {
        const savedZoom = Number(calibre.storage.get(calibre.storageKey + ".zoom")) || 1;
        zoomControl.value = String(Math.max(1, Math.min(3, savedZoom)));
        reader.rendition.setZoom(Number(zoomControl.value));
        zoomControl.addEventListener("input", function () {
            reader.rendition.setZoom(Number(this.value));
            calibre.storage.set(calibre.storageKey + ".zoom", this.value);
        });
        ["touchstart", "touchend"].forEach(name => document.getElementById("viewer").addEventListener(name,
            event => reader.rendition.emit(name, event), {passive: true}));
        // The sidebar lists every page; chapter titles from the book's TOC label the pages they start on.
        reader.book.loaded.navigation.then(function (navigation) {
            const chapters = new Map();
            const flatten = items => items.forEach(item => {
                const section = reader.book.spine.get(item.href);
                if (section && !chapters.has(section.index)) chapters.set(section.index, item.label.trim());
                if (item.subitems) flatten(item.subitems);
            });
            flatten((navigation && navigation.toc) || []);
            const list = document.createElement("ul");
            list.id = "manga-pages";
            calibre.epubData.pages.forEach(function (page, index) {
                const item = document.createElement("li");
                item.className = "list_item manga-page";
                item.dataset.index = String(page.index);
                const link = document.createElement("a");
                link.className = "toc_link";
                link.href = "#";
                const chapter = chapters.get(page.index);
                link.textContent = calibre.messages.page.replace("%(page)s", index + 1) + (chapter ? " \u2013 " + chapter : "");
                link.addEventListener("click", function (event) {
                    event.preventDefault();
                    reader.rendition.display(page.index);
                });
                item.appendChild(link);
                list.appendChild(item);
            });
            document.getElementById("tocView").replaceChildren(list);
            const mark = location => {
                if (!location || !location.start) return;
                list.querySelectorAll(".currentChapter").forEach(item => item.classList.remove("currentChapter"));
                const current = list.querySelector('[data-index="' + location.start.index + '"]');
                if (current) {
                    current.classList.add("currentChapter");
                    current.scrollIntoView({block: "nearest"});
                }
            };
            reader.rendition.on("relocated", mark);
            mark(reader.rendition.currentLocation());
        }).catch(error => console.warn("Could not build the page list", error));
    }

    function showReaderError(message, error) {
        try {
            console.error(message, error || "");
        } catch (e) {}
        var loader = document.getElementById("loader");
        if (loader) {
            loader.style.display = "none";
        }
        var viewer = document.getElementById("viewer");
        if (viewer) {
            const notice = document.createElement("div");
            notice.className = "reader-error";
            notice.textContent = message;
            viewer.replaceChildren(notice);
        }
    }

    if (reader && reader.book && typeof reader.book.on === 'function') {
        reader.book.on("openFailed", function(error) {
            showReaderError(calibre.messages.openError, error);
        });
        reader.book.on("error", function(error) {
            showReaderError(calibre.messages.openError, error);
        });
    }

    if (reader && reader.rendition && typeof reader.rendition.on === 'function') {
        reader.rendition.on("displayerror", function(error) {
            showReaderError(calibre.messages.displayError, error);
        });
        reader.rendition.on("loaderror", function(error) {
            showReaderError(calibre.messages.displayError, error);
        });
    }

    Object.keys(themes).forEach(function (theme) {
        reader.rendition.themes.register(theme, themes[theme].css_path);
    });

    if (calibre.useBookmarks) {
        reader.on("reader:bookmarked", updateBookmark.bind(reader, "add"));
        reader.on("reader:unbookmarked", updateBookmark.bind(reader, "remove"));
    } else {
        $("#bookmark, #show-Bookmarks").remove();
    }

    // Enable swipe support
    // I have no idea why swiperRight/swiperLeft from plugins is not working, events just don't get fired
    var touchStart = 0;
    var touchEnd = 0;
    var touchY = 0;

    if (reader && reader.rendition) {
        reader.rendition.on('touchstart', function(event) {
            if (event.touches.length !== 1) { touchStart = null; return; }
            touchStart = event.changedTouches[0].screenX;
            touchY = event.changedTouches[0].screenY;
        });
        reader.rendition.on('touchend', function(event) {
            if (touchStart === null || event.touches.length || (reader.rendition.zoom && reader.rendition.zoom > 1)) return;
            touchEnd = event.changedTouches[0].screenX;
            const distance = Math.abs(touchEnd - touchStart);
            if (distance < 40 || distance < Math.abs(event.changedTouches[0].screenY - touchY)) return;
            if (touchStart < touchEnd) {
                if(reader.book.package.metadata.direction === "rtl") {
					reader.rendition.next();
				} else {
					reader.rendition.prev();
				}
                // Swiped Right
            }
            if (touchStart > touchEnd) {
                if(reader.book.package.metadata.direction === "rtl") {
					reader.rendition.prev();
				} else {
                    reader.rendition.next();
				}
                // Swiped Left
            }
        });
    }

    /**
     * @param {string} action - Add or remove bookmark
     * @param {string|int} location - Location or zero
     */
    function updateBookmark(action, location) {
        // Remove other bookmarks (there can only be one)
        if (action === "add") {
            this.settings.bookmarks.filter(function (bookmark) {
                return bookmark && bookmark !== location;
            }).map(function (bookmark) {
                this.removeBookmark(bookmark);
            }.bind(this));
        }
        
        var csrftoken = $("input[name='csrf_token']").val();

        // Save to database
        $.ajax(calibre.bookmarkUrl, {
            method: "post",
            data: { bookmark: location || "" },
            headers: { "X-CSRFToken": csrftoken }
        }).fail(function (xhr, status, error) {
            alert(error);
        });
    }
    
    // Restore all settings after DOM and reader are ready
    document.addEventListener("DOMContentLoaded", function() {
        // Declare reflowBox once and reuse
        var reflowBox = document.getElementById('sidebarReflow');
        if (reflowBox && reader && reader.settings) {
            reader.settings.sidebarReflow = reflowBox.checked;
            // Trigger resize after first render to avoid calling resize too early
            if (reader.rendition && typeof reader.rendition.on === 'function') {
                let resizedOnce = false;
                reader.rendition.on('rendered', function() {
                    if (resizedOnce) {
                        return;
                    }
                    resizedOnce = true;
                    if (reader && reader.rendition && typeof reader.rendition.resize === 'function') {
                        try {
                            reader.rendition.resize();
                        } catch (e) {
                            // Avoid breaking the reader when resize isn't available
                        }
                    }
                });
            }
        }
        // Ensure reflow logic is always applied if the class is present
        setTimeout(function() {
            if (reflowBox && document.body.classList.contains('reflow-enabled')) {
                // Trigger change event to re-apply reflow logic
                reflowBox.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }, 0);
        // Theme
        const theme = calibre.storage.get("calibre.reader.theme") ?? "lightTheme";
        if (typeof selectTheme === 'function') selectTheme(theme);

        // Font size
        let savedFontSize = !calibre.manga && calibre.storage.get("calibre.reader.fontSize");
        let fontSizeFader = document.getElementById('fontSizeFader');
        if (savedFontSize && fontSizeFader && reader && reader.rendition && reader.rendition.themes) {
            fontSizeFader.value = savedFontSize;
            reader.rendition.themes.fontSize(`${savedFontSize}%`);
        }

        // Font
        let fontMap = {
            'default': '',
            'Yahei': '"Microsoft YaHei", sans-serif',
            'SimSun': 'SimSun, serif',
            'KaiTi': 'KaiTi, serif',
            'Arial': 'Arial, Helvetica, sans-serif'
        };
        let savedFont = !calibre.manga && calibre.storage.get("calibre.reader.font");
        if (savedFont && typeof selectFont === 'function') {
            selectFont(savedFont);
            let fontValue = fontMap[savedFont] || '';
            if (savedFont !== 'default' && fontValue && reader && reader.rendition && reader.rendition.themes) {
                reader.rendition.themes.font(fontValue);
            }
        }

        // Spread
        let savedSpread = calibre.storage.get(spreadKey);
        if (savedSpread && typeof spread === 'function') {
            spread(savedSpread);
        }

        // Reflow
        // Use the reflowBox declared earlier in this handler
        var savedReflow = calibre.storage.get("calibre.reader.reflow");
        function applyReflow(enabled) {
            if (reader && reader.rendition && typeof reader.rendition.reflow === 'function') {
                reader.rendition.reflow(enabled);
            } else {
                document.body.classList.toggle('reflow-enabled', enabled);
            }
        }
        if (reflowBox && !calibre.manga) {
            if (savedReflow !== null) {
                reflowBox.checked = savedReflow === 'true';
                applyReflow(reflowBox.checked);
            }
            reflowBox.addEventListener("change", function() {
                calibre.storage.set("calibre.reader.reflow", this.checked);
                applyReflow(this.checked);
            });
        }
    });
})();
