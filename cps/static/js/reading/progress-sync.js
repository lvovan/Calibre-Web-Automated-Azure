/* Calibre-Web Automated – shared reading progress sync for the web readers.
 *
 * Progress is stored per user on the server alongside KOReader sync progress, using KOReader's
 * position formats: an XPointer for reflowable books and a 1-based page number for paged ones.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
(function (root) {
    "use strict";

    const DEVICE_KEY = "cwa.reader.deviceId";
    const SAVE_DELAY_MS = 3000;
    const CHECK_INTERVAL_MS = 60000;

    function storageGet(key) {
        try { return root.localStorage.getItem(key); } catch (error) { return null; }
    }

    function storageSet(key, value) {
        try { root.localStorage.setItem(key, value); } catch (error) { /* private browsing */ }
    }

    function deviceId() {
        let id = storageGet(DEVICE_KEY);
        if (!id || id.length > 100) {
            id = root.crypto && typeof root.crypto.randomUUID === "function" ? root.crypto.randomUUID() :
                Date.now().toString(36) + Math.random().toString(36).slice(2);
            storageSet(DEVICE_KEY, id);
        }
        return id;
    }

    function percentText(fraction) {
        return Math.round(Math.max(0, Math.min(1, fraction)) * 100) + "%";
    }

    // ---------------------------------------------------------------- KOReader XPointers (EPUB)

    function isText(node) {
        return node.nodeType === 3 && /\S/.test(node.nodeValue);
    }

    function nodeName(node) {
        return (node.localName || node.nodeName || "").toLowerCase();
    }

    function childNodes(node) {
        return Array.prototype.slice.call(node.childNodes || []);
    }

    // crengine collapses whitespace runs, so offsets are counted on condensed text.
    function condensedOffset(text, offset) {
        return text.slice(0, offset).replace(/\s+/g, " ").length;
    }

    function rawOffset(text, offset) {
        let count = 0;
        for (let i = 0; i < text.length; i++) {
            if (count >= offset) return i;
            if (/\s/.test(text[i])) {
                while (i + 1 < text.length && /\s/.test(text[i + 1])) i++;
            }
            count++;
        }
        return text.length;
    }

    /** XPointer for a DOM position inside spine item `fragment` (0-based). */
    function xpointerFromNode(fragment, node, offset) {
        let suffix = "";
        if (node && node.nodeType === 3) {
            if (isText(node)) {
                const texts = childNodes(node.parentNode).filter(isText);
                const index = texts.indexOf(node);
                suffix = "/text()" + (texts.length > 1 ? "[" + (index + 1) + "]" : "") + "." +
                    condensedOffset(node.nodeValue, offset || 0);
            }
            node = node.parentNode;
        }
        let path = "";
        while (node && node.nodeType === 1 && nodeName(node) !== "body") {
            const name = nodeName(node);
            const same = childNodes(node.parentNode).filter(n => n.nodeType === 1 && nodeName(n) === name);
            path = "/" + name + (same.length > 1 ? "[" + (same.indexOf(node) + 1) + "]" : "") + path;
            node = node.parentNode;
        }
        if (!node || nodeName(node) !== "body") path = suffix = "";
        return "/body/DocFragment[" + (fragment + 1) + "]/body" + path + suffix;
    }

    /** Parses a KOReader EPUB XPointer, or returns null for other progress formats. */
    function parseXPointer(value) {
        const match = /^\/body\/DocFragment(?:\[(\d+)\])?(\/body(?:\/.*)?)?$/.exec(value || "");
        if (!match) return null;
        const fragment = (match[1] ? parseInt(match[1], 10) : 1) - 1;
        const steps = [];
        let text = null;
        const parts = (match[2] || "").split("/").filter(Boolean).slice(1);
        for (const part of parts) {
            const textStep = /^text\(\)(?:\[(\d+)\])?(?:\.(\d+))?$/.exec(part);
            if (textStep) {
                text = {index: textStep[1] ? parseInt(textStep[1], 10) : 1,
                    offset: textStep[2] ? parseInt(textStep[2], 10) : 0};
                break;
            }
            const step = /^([^[\].]+)(?:\[(\d+)\])?(?:\.\d+)?$/.exec(part);
            if (!step) break;
            steps.push({name: step[1].toLowerCase(), index: step[2] ? parseInt(step[2], 10) : 1});
        }
        return {fragment, steps, text};
    }

    /** Resolves a parsed XPointer in a document body to the closest {node, offset}. */
    function resolveXPointer(body, parsed) {
        let node = body;
        for (const step of parsed.steps) {
            const same = childNodes(node).filter(n => n.nodeType === 1 && nodeName(n) === step.name);
            const next = same[step.index - 1];
            if (!next) return {node, offset: 0, exact: false};
            node = next;
        }
        if (parsed.text) {
            const texts = childNodes(node).filter(isText);
            const text = texts[parsed.text.index - 1];
            if (text) return {node: text, offset: rawOffset(text.nodeValue, parsed.text.offset), exact: true};
        }
        return {node, offset: 0, exact: !parsed.text};
    }

    /** 1-based page from a KOReader paged-document progress string, or null. */
    function parsePage(value) {
        return /^\d+$/.test(String(value || "")) ? parseInt(value, 10) : null;
    }

    // ---------------------------------------------------------------- Sync session

    let styled = false;
    function addStyles() {
        if (styled || !root.document) return;
        styled = true;
        const style = root.document.createElement("style");
        style.textContent = ".cwa-progress-prompt{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);" +
            "z-index:2147483000;display:flex;gap:8px;align-items:center;max-width:calc(100% - 32px);" +
            "padding:8px 12px;border-radius:6px;background:rgba(32,32,32,.95);color:#fff;" +
            "font:14px/1.4 sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.4)}" +
            ".cwa-progress-prompt button{font:inherit;padding:4px 10px;border:0;border-radius:4px;cursor:pointer}" +
            ".cwa-progress-prompt button.cwa-primary{background:#45b29d;color:#fff}";
        root.document.head.appendChild(style);
    }

    /**
     * @param {object|null} config Server config: {url, state, device, messages}.
     * @param {object} options {csrfToken, onRemote(state)}
     * @returns {object|null} Sync session, or null when sync is unavailable.
     */
    function create(config, options) {
        if (!config || !config.url) return null;
        options = options || {};
        const messages = config.messages || {};
        const id = deviceId();
        let known = config.state && typeof config.state.timestamp === "number" ? config.state : null;
        let pending = null;
        let timer = null;
        let inflight = null;
        let errorShown = false;
        let prompt = null;

        function hidePrompt() {
            if (prompt) prompt.remove();
            prompt = null;
        }

        function show(text, actions) {
            if (!root.document || !root.document.body) return;
            addStyles();
            hidePrompt();
            prompt = root.document.createElement("div");
            prompt.className = "cwa-progress-prompt";
            prompt.setAttribute("role", actions.length ? "alertdialog" : "status");
            const label = root.document.createElement("span");
            label.textContent = text;
            prompt.appendChild(label);
            actions.forEach(action => {
                const button = root.document.createElement("button");
                button.type = "button";
                button.textContent = action.label;
                if (action.primary) button.className = "cwa-primary";
                button.addEventListener("click", () => { hidePrompt(); if (action.run) action.run(); });
                prompt.appendChild(button);
            });
            root.document.body.appendChild(prompt);
            if (!actions.length) setTimeout(hidePrompt, 5000);
        }

        function failed(error) {
            console.warn("Reading progress sync failed", error);
            if (!errorShown && messages.error) {
                errorShown = true;
                show(messages.error, []);
            }
        }

        async function request(method, body, keepalive, url) {
            const init = {method, credentials: "same-origin", headers: {Accept: "application/json"}, keepalive};
            if (body) {
                init.headers["Content-Type"] = "application/json";
                init.headers["X-CSRFToken"] = options.csrfToken || "";
                init.body = JSON.stringify(body);
            }
            const response = await root.fetch(url || config.url, init);
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.json();
        }

        function remoteText(state) {
            return (messages.remote || "{percent} {device}")
                .replace("{percent}", percentText(state.percentage))
                .replace("{device}", state.device || "");
        }

        function isNewerRemote(state) {
            return (!known || state.timestamp > known.timestamp) && state.device_id !== id &&
                state.progress !== (known && known.progress);
        }

        function send(keepalive) {
            clearTimeout(timer);
            timer = null;
            if (!pending) return inflight || Promise.resolve(known);
            const body = pending;
            pending = null;
            inflight = request("PUT", body, keepalive).then(state => {
                known = state;
                errorShown = false;
                if (options.onSaved) options.onSaved(state);
                return state;
            }).catch(error => {
                if (!pending) pending = body;
                failed(error);
            }).finally(() => { inflight = null; });
            return inflight;
        }

        const session = {
            deviceId: id,
            get state() { return known; },
            /** Queues a position; identical positions are not resent. */
            save(progress, percentage) {
                progress = String(progress);
                percentage = Math.max(0, Math.min(1, Number(percentage) || 0));
                if (!pending && known && known.progress === progress) return;
                pending = {progress, percentage, device: config.device, device_id: id};
                clearTimeout(timer);
                timer = setTimeout(() => send(false), SAVE_DELAY_MS);
            },
            flush() { return send(true); },
            /** Fetches the server state and offers to jump if another device is further along in time. */
            async check() {
                if (pending || inflight) return null;
                try {
                    const state = await request("GET");
                    if (!state || typeof state.timestamp !== "number") return null;
                    if (isNewerRemote(state)) {
                        known = state;
                        if (options.onRemote) {
                            session.offer(state, () => options.onRemote(state));
                        }
                        return state;
                    }
                    known = state;
                } catch (error) {
                    console.warn("Could not check reading progress", error);
                }
                return null;
            },
            /** Saves the current position now, then opens a newer position from another device if there is one. */
            async syncNow() {
                show(messages.syncing || "…", []);
                try {
                    if (pending || inflight) await send(false);
                    if (pending) throw new Error("save failed");
                    const state = await request("GET");
                    if (state && typeof state.timestamp === "number") {
                        const remote = isNewerRemote(state);
                        known = state;
                        if (remote && options.onRemote) await options.onRemote(state);
                    }
                    show((messages.synced || "{percent}").replace("{percent}", percentText(known ? known.percentage : 0)), []);
                    return known;
                } catch (error) {
                    hidePrompt();
                    errorShown = false;
                    failed(error);
                    return null;
                }
            },
            /** Prompts to continue from `state`, calling `run` if the reader accepts. */
            offer(state, run) {
                show(remoteText(state), [{label: messages.jump || "OK", primary: true, run},
                    {label: messages.dismiss || "×"}]);
            },
            /** Links an unmatched KOReader document hash to this book; resolves to the new server state. */
            async link(candidate) {
                const state = await request("POST", {document: candidate.document, format: config.format}, false,
                    config.linkUrl);
                if (state && typeof state.timestamp === "number") known = state;
                return known;
            },
            candidates: Array.isArray(config.candidates) ? config.candidates : [],
            isOwn(state) { return Boolean(state && state.device_id === id); }
        };

        if (root.document) {
            root.document.addEventListener("visibilitychange", () => {
                if (root.document.visibilityState === "hidden") session.flush();
                else session.check();
            });
            root.addEventListener("pagehide", () => session.flush());
            setInterval(() => {
                if (root.document.visibilityState !== "hidden") session.check();
            }, CHECK_INTERVAL_MS);
        }
        return session;
    }

    root.CwaProgressSync = {create, xpointerFromNode, parseXPointer, resolveXPointer, parsePage, percentText};
})(typeof window !== "undefined" ? window : globalThis);
