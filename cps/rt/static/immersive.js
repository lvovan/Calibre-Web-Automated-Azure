/* SPDX-License-Identifier: GPL-3.0-or-later */
(function () {
  'use strict';
  window.RTImmersive = function (config, actions) {
    var stage = document.getElementById('reader-stage'), modal = document.getElementById('reader-modal');
    var dialog = document.getElementById('reader-dialog'), slider = document.getElementById('reading-progress');
    var label = document.getElementById('progress-label'), image = document.getElementById('page-image');
    var listeners = [], destroyed = false, timer, dragging = false, position = 0, count = 0;
    var zoom = 1, offsetX = 0, offsetY = 0, fitWidth = 0, fitHeight = 0, fullscreenAttempted = false;
    function listen(target, name, handler) {
      target.addEventListener(name, handler, false);
      listeners.push(function () { target.removeEventListener(name, handler, false); });
    }
    function open() {
      modal.hidden = false;
      stage.setAttribute('aria-hidden', 'true');
      document.getElementById('dialog-close').focus();
    }
    function dismiss() {
      modal.hidden = true;
      stage.removeAttribute('aria-hidden');
      stage.focus();
    }
    function progressLabel(value) {
      label.textContent = (config.kind === 'epub' ? config.labels.position : config.kind === 'audio' ? config.labels.time : config.labels.page) + ' ' + value + ' / ' + count;
    }
    function progress(current, total) {
      position = current; count = total;
      slider.disabled = total < 1;
      slider.min = config.kind === 'audio' ? 0 : 1;
      slider.max = Math.max(1, total);
      var value = current + (config.kind === 'audio' ? 0 : 1);
      if (!dragging) { slider.value = value; progressLabel(value); }
    }
    function enterFullscreen() {
      var root = document.documentElement;
      var request = root.requestFullscreen || root.msRequestFullscreen;
      fullscreenAttempted = true;
      if (!request) return;
      var result;
      try { result = request.call(root); } catch (error) { actions.error(config.labels.fullscreenFailed, error); return; }
      if (result && result.catch) result.catch(function (error) {
        if (!destroyed) actions.error(config.labels.fullscreenFailed, error);
      });
    }
    function tap(x, y) {
      if (destroyed || !modal.hidden) return;
      if (!fullscreenAttempted) enterFullscreen();
      var bounds = stage.getBoundingClientRect();
      x = (x - bounds.left) / bounds.width; y = (y - bounds.top) / bounds.height;
      if (x < 0 || x > 1 || y < 0 || y > 1) return;
      if (x < 0.2) actions.navigate(-1);
      else if (x >= 0.8) actions.navigate(1);
      else if (y < 0.2) open();
    }
    function paint() {
      if (!image || !image.naturalWidth || destroyed) return;
      var bounds = stage.getBoundingClientRect();
      var fit = Math.min(bounds.width / image.naturalWidth, bounds.height / image.naturalHeight);
      fitWidth = image.naturalWidth * fit; fitHeight = image.naturalHeight * fit;
      var maxX = Math.max(0, (fitWidth * zoom - bounds.width) / 2);
      var maxY = Math.max(0, (fitHeight * zoom - bounds.height) / 2);
      offsetX = Math.max(-maxX, Math.min(maxX, offsetX));
      offsetY = Math.max(-maxY, Math.min(maxY, offsetY));
      image.style.width = fitWidth + 'px'; image.style.height = fitHeight + 'px';
      var transform = 'translate(' + (offsetX - fitWidth * zoom / 2) + 'px,' + (offsetY - fitHeight * zoom / 2) + 'px) scale(' + zoom + ')';
      image.style.transform = image.style.msTransform = transform;
    }
    function resetZoom() { zoom = 1; offsetX = offsetY = 0; paint(); }
    function interactive(target) {
      while (target && target.nodeType === 1) {
        if (/^(A|BUTTON|INPUT|SELECT|TEXTAREA|AUDIO)$/.test(target.tagName)) return true;
        target = target.parentNode;
      }
      return false;
    }
    function gestures(target, coordinates, alive, imageGestures) {
      var points = {}, start, multiple = false, moved = false, pinch;
      function values() { return Object.keys(points).map(function (key) { return points[key]; }); }
      function baseline() {
        var items = values();
        if (items.length === 2) {
          var dx = items[0].x - items[1].x, dy = items[0].y - items[1].y;
          var bounds = stage.getBoundingClientRect();
          pinch = {distance: Math.sqrt(dx * dx + dy * dy), zoom: zoom, x: offsetX, y: offsetY,
            cx: (items[0].x + items[1].x) / 2 - bounds.left - bounds.width / 2,
            cy: (items[0].y + items[1].y) / 2 - bounds.top - bounds.height / 2};
        }
      }
      function down(event) {
        if (destroyed || !alive() || !modal.hidden || interactive(event.target) || (event.button !== undefined && event.button !== 0)) return;
        var point = coordinates(event);
        points[event.pointerId] = point;
        if (values().length === 1) {
          start = {x: point.x, y: point.y, ox: offsetX, oy: offsetY}; moved = multiple = false;
        } else { multiple = true; baseline(); }
        if (imageGestures && target.setPointerCapture) target.setPointerCapture(event.pointerId);
      }
      function move(event) {
        if (destroyed || !alive() || !points[event.pointerId]) return;
        var point = coordinates(event); points[event.pointerId] = point;
        var items = values(), dx = point.x - start.x, dy = point.y - start.y;
        if (Math.abs(dx) + Math.abs(dy) > 12) moved = true;
        if (!imageGestures) return;
        if (items.length === 2 && pinch && pinch.distance > 0) {
          dx = items[0].x - items[1].x; dy = items[0].y - items[1].y;
          zoom = Math.max(1, Math.min(5, pinch.zoom * Math.sqrt(dx * dx + dy * dy) / pinch.distance));
          var bounds = stage.getBoundingClientRect();
          offsetX = (items[0].x + items[1].x) / 2 - bounds.left - bounds.width / 2 - (pinch.cx - pinch.x) * zoom / pinch.zoom;
          offsetY = (items[0].y + items[1].y) / 2 - bounds.top - bounds.height / 2 - (pinch.cy - pinch.y) * zoom / pinch.zoom;
          paint();
        } else if (items.length === 1 && !multiple && zoom > 1) {
          offsetX = start.ox + dx; offsetY = start.oy + dy; paint();
        }
      }
      function up(event) {
        if (destroyed || !alive() || !points[event.pointerId]) return;
        var point = coordinates(event);
        delete points[event.pointerId];
        if (imageGestures && target.hasPointerCapture && target.hasPointerCapture(event.pointerId)) target.releasePointerCapture(event.pointerId);
        if (!values().length && !multiple) {
          var dx = point.x - start.x, dy = point.y - start.y;
          if (!moved && Math.abs(dx) + Math.abs(dy) < 12) tap(point.x, point.y);
          else if (imageGestures && zoom === 1 && Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) actions.navigate(dx > 0 ? -1 : 1);
        }
      }
      function cancel() { points = {}; start = pinch = undefined; multiple = moved = false; }
      target.addEventListener('pointerdown', down, false);
      target.addEventListener('pointermove', move, false);
      target.addEventListener('pointerup', up, false);
      target.addEventListener('pointercancel', cancel, false);
      return function () {
        target.removeEventListener('pointerdown', down, false); target.removeEventListener('pointermove', move, false);
        target.removeEventListener('pointerup', up, false); target.removeEventListener('pointercancel', cancel, false);
        cancel();
      };
    }
    listeners.push(gestures(stage, function (event) { return {x: event.clientX, y: event.clientY}; }, function () { return true; }, !!image));
    if (image) stage.style.touchAction = stage.style.msTouchAction = 'none';
    document.getElementById('reader-menu').onclick = open;
    document.getElementById('dialog-close').onclick = dismiss;
    listen(document.getElementById('fullscreen'), 'click', enterFullscreen);
    listen(modal, 'click', function (event) { if (event.target === modal) dismiss(); });
    listen(slider, 'pointerdown', function () { dragging = true; });
    listen(slider, 'pointerup', function () { dragging = false; });
    listen(slider, 'pointercancel', function () { dragging = false; progress(position, count); });
    listen(slider, 'input', function () { progressLabel(Number(slider.value)); });
    listen(slider, 'change', function () { if (!slider.disabled) actions.seek(Number(slider.value) - (config.kind === 'audio' ? 0 : 1)); });
    if (image) listen(document.getElementById('reset-zoom'), 'click', resetZoom);
    listen(document, 'keydown', function (event) {
      if (modal.hidden) {
        if (event.keyCode === 27 || event.keyCode === 77) { event.preventDefault(); open(); }
        return;
      }
      if (event.keyCode === 27) { event.preventDefault(); dismiss(); return; }
      if (event.keyCode !== 9) return;
      var nodes = dialog.querySelectorAll('button, input, select, a[href], [tabindex]');
      var focusable = Array.prototype.filter.call(nodes, function (node) { return !node.disabled && (node.offsetWidth || node.offsetHeight); });
      var first = focusable[0], last = focusable[focusable.length - 1];
      if (!first) { event.preventDefault(); dialog.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    function resize() {
      window.clearTimeout(timer);
      timer = window.setTimeout(function () { if (!destroyed) { paint(); actions.resize(stage.clientWidth, stage.clientHeight); } }, 100);
    }
    listen(window, 'resize', resize); listen(window, 'orientationchange', resize);
    listen(document, 'fullscreenchange', resize); listen(document, 'MSFullscreenChange', resize);
    listen(document, 'MSFullscreenError', function (error) { actions.error(config.labels.fullscreenFailed, error); });
    return {
      open: open, progress: progress, imageLoaded: paint, resetZoom: resetZoom, isOpen: function () { return !modal.hidden; },
      positionError: function () { label.textContent = config.labels.positionsFailed; },
      bindContents: function (contents) {
        var frame = contents.window.frameElement;
        var remove = gestures(contents.document, function (event) {
          var bounds = frame.getBoundingClientRect(); return {x: bounds.left + event.clientX, y: bounds.top + event.clientY};
        }, function () { return !contents.rtDisposed; }, false);
        var original = contents.removeListeners;
        contents.removeListeners = function () { remove(); return original.apply(this, arguments); };
      },
      destroy: function () {
        destroyed = true; window.clearTimeout(timer); listeners.forEach(function (remove) { remove(); }); listeners = [];
        slider.disabled = true;
        var exit = document.exitFullscreen || document.msExitFullscreen;
        if (exit && (document.fullscreenElement || document.msFullscreenElement)) {
          var result = exit.call(document); if (result && result.catch) result.catch(function (error) { actions.error(config.labels.fullscreenFailed, error); });
        }
        modal.hidden = false; stage.setAttribute('aria-hidden', 'true');
        document.getElementById('reader-return').hidden = false;
      }
    };
  };
}());
