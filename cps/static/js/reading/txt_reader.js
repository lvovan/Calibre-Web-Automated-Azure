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

$(document).ready(function() {
    //to int
    $("#area").width($("#area").width());
    $("#content").width($("#content").width());
    //bind text
    $("#content").load($("#readmain").data('load'), function(textStr) {
        $(this).height($(this).parent().height()*0.95);
        $(this).text(textStr);
        restoreProgress();
    });
    //keybind
    $(document).keydown(function(event){
        if(event.keyCode == 37){
            prevPage();
        }
        if(event.keyCode == 39){
            nextPage();
        }
    });
    //click
    $( "#left" ).click(function() {
        prevPage();
    });
    $( "#right" ).click(function() {
        nextPage();
    });
    window.CwaNavigation.bindSwipe($("#readmain")[0], {move: delta => navigation.move(delta)});

    //bind mouse
    $(window).bind('DOMMouseScroll mousewheel', function(event) {
        var delta = 0;
        if (event.originalEvent.wheelDelta) {
            delta = event.originalEvent.wheelDelta;
        } else if (event.originalEvent.detail) {
            delta = event.originalEvent.detail*-1;
        }
        if (delta >= 0) {
            prevPage();
        } else {
            nextPage();
        }
    });

    //page animate
    var origwidth = $("#content")[0].getBoundingClientRect().width;
    var gap = 20;
    var baseLeft = $("#content").offset().left;
    var navigation = window.CwaNavigation.create({
        position: currentPage,
        bounds: () => [0, pageCount() - 1],
        navigate: page => {
            $("#content").offset({left: baseLeft - page * (origwidth + gap)});
            saveProgress();
        },
        onError: error => console.error("TXT page navigation failed", error)
    });
    function prevPage() {
        navigation.move(-1);
    }
    function nextPage() {
        navigation.move(1);
    }

    // Per-user progress shared across devices. KOReader cannot map web TXT pages, so only the
    // percentage is meaningful to other devices; "cwa-txt:<fraction>" keeps the exact web position.
    var settings = window.cwaTxt || {};
    var sync = window.CwaProgressSync ? window.CwaProgressSync.create(settings.progressSync, {
        csrfToken: settings.csrfToken,
        onRemote: function(state) { goToFraction(fractionFor(state)); }
    }) : null;
    function pageCount() {
        return Math.max(1, Math.ceil($("#content")[0].scrollWidth / (origwidth + gap)));
    }
    function currentPage() {
        return Math.max(0, Math.round((baseLeft - $("#content").offset().left) / (origwidth + gap)));
    }
    function goToFraction(fraction) {
        if (fraction === null) return;
        navigation.cancel();
        var page = Math.min(pageCount() - 1, Math.max(0, Math.floor(fraction * pageCount())));
        $("#content").offset({left: baseLeft - page * (origwidth + gap)});
    }
    function fractionFor(state) {
        if (!state) return null;
        var match = /^cwa-txt:([0-9.]+)$/.exec(state.progress || "");
        var fraction = match ? parseFloat(match[1]) : state.percentage;
        return typeof fraction === "number" && isFinite(fraction) && fraction > 0 ? Math.min(1, fraction) : null;
    }
    function restoreProgress() {
        if (sync) goToFraction(fractionFor(sync.state));
    }
    $("#sync-now").on("click", function() {
        if (sync) sync.syncNow();
    });
    function saveProgress() {
        if (!sync) return;
        var fraction = currentPage() / pageCount();
        var end = (currentPage() + 1) / pageCount();
        sync.save("cwa-txt:" + fraction.toFixed(6), end);
    }
});
