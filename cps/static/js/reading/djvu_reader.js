/* This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
 *    Copyright (C) 2021 Ozzieisaacs
 *
 *  This program is free software: you can redistribute it and/or modify
 *  it under the terms of the GNU General Public License as published by
 *  the Free Software Foundation, either version 3 of the License, or
 *  (at your option) any later version.
 *
 *  This program is distributed in the hope that it will be useful,
 *  but WITHOUT ANY WARRANTY; without even the implied warranty of
 *  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *  GNU General Public License for more details.
 *
 *  You should have received a copy of the GNU General Public License
 *  along with this program. If not, see <http://www.gnu.org/licenses/>.
 */

var DJVU_CONTEXT = {
        background: "#666",
        uiHideDelay: 1500,
};

document.addEventListener("DOMContentLoaded", function () {
    "use strict";
    const container = document.getElementById("djvuContainer");
    // The bundled GWT viewer exposes its page selector through the toolbar DOM.
    function selector() {
        const next = container.querySelector(".buttonPageNext");
        return next && next.parentElement.querySelector("select");
    }
    const navigation = window.CwaNavigation.create({
        position: () => { const select = selector(); return select ? select.selectedIndex : 0; },
        bounds: () => { const select = selector(); return [0, select ? select.options.length - 1 : 0]; },
        navigate: page => {
            const select = selector();
            if (!select) throw new Error("DJVU page selector is unavailable");
            select.selectedIndex = page;
            select.dispatchEvent(new Event("change", {bubbles: true}));
        },
        onError: error => console.error("DJVU page navigation failed", error)
    });
    container.addEventListener("click", event => {
        const button = event.target.closest(".buttonPageNext, .buttonPagePrev");
        if (!button) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        navigation.move(button.classList.contains("buttonPageNext") ? 1 : -1);
    }, true);
    container.addEventListener("change", event => {
        if (event.isTrusted && event.target === selector()) navigation.cancel();
    }, true);
    window.CwaNavigation.bindSwipe(container, {
        move: delta => navigation.move(delta),
        enabled: () => {
            const select = selector();
            const horizontal = container.querySelector(".scrollbarHorizontal");
            return !!select && select.options.length > 0 && (!horizontal || horizontal.offsetWidth === 0) &&
                !(window.visualViewport && window.visualViewport.scale > 1);
        }
    });
});
