/* SPDX-License-Identifier: GPL-3.0-or-later */
(function (root) {
    "use strict";

    // Keep an absolute intent while a render is in flight; later input must not use a stale page.
    function create({position, bounds, navigate, onError, delay = 160}) {
        let target = null;
        let timer = null;
        let busy = false;
        let generation = 0;
        async function flush() {
            timer = null;
            if (busy || target === null) return;
            const destination = target;
            const version = generation;
            busy = true;
            try {
                if (destination !== position()) await navigate(destination);
            } catch (error) {
                if (version === generation) target = null;
                onError(error);
            } finally {
                busy = false;
                if (version === generation && target === destination) target = null;
                if (target !== null && timer === null) timer = setTimeout(flush, delay);
            }
        }
        function jump(value) {
            const [min, max] = bounds();
            target = Math.max(min, Math.min(max, value));
            clearTimeout(timer);
            timer = setTimeout(flush, delay);
        }
        return {
            move(delta) { jump((target === null ? position() : target) + delta); },
            jump,
            cancel() { generation++; target = null; clearTimeout(timer); timer = null; }
        };
    }

    function bindSwipe(element, {move, rtl = () => false, enabled = () => true, axis = () => 0}) {
        let start = null;
        let suppressClickUntil = 0;
        const interactive = target => target && target.closest && target.closest(
            "a, button, input, select, textarea, [contenteditable], .annotationLayer section, .toolbar, .scrollbar");
        element.addEventListener("touchstart", event => {
            suppressClickUntil = 0;
            start = event.touches.length === 1 && enabled() && !interactive(event.target) ?
                {x: event.touches[0].clientX, y: event.touches[0].clientY} : null;
        }, {passive: true, capture: true});
        element.addEventListener("touchmove", event => {
            if (event.touches.length !== 1) start = null;
        }, {passive: true, capture: true});
        element.addEventListener("touchcancel", () => { start = null; }, {passive: true, capture: true});
        element.addEventListener("touchend", event => {
            const origin = start;
            start = null;
            if (!origin || !enabled() || event.touches.length || event.changedTouches.length !== 1) return;
            const touch = event.changedTouches[0];
            const rotation = axis();
            const dx = rotation % 2 ? touch.clientY - origin.y : touch.clientX - origin.x;
            const dy = rotation % 2 ? touch.clientX - origin.x : touch.clientY - origin.y;
            const selection = element.getSelection ? element.getSelection() : root.getSelection && root.getSelection();
            if (selection && !selection.isCollapsed) return;
            if (Math.abs(dx) < 50 || Math.abs(dx) < 1.5 * Math.abs(dy)) return;
            if (event.cancelable) event.preventDefault();
            suppressClickUntil = Date.now() + 700;
            move((dx < 0 ? 1 : -1) * (rtl() ? -1 : 1) * (rotation >= 2 ? -1 : 1));
        }, {passive: false, capture: true});
        element.addEventListener("click", event => {
            if (Date.now() >= suppressClickUntil) return;
            event.preventDefault();
            event.stopImmediatePropagation();
        }, true);
    }

    const api = {create, bindSwipe};
    if (typeof module !== "undefined" && module.exports) module.exports = api;
    else root.CwaNavigation = api;
})(typeof window !== "undefined" ? window : globalThis);
