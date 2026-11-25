/* SPDX-License-Identifier: GPL-3.0-or-later */
(function () {
  'use strict';
  var config = JSON.parse(document.getElementById('rt-config').textContent);
  var status = document.getElementById('reader-status');
  var saveStatus = document.getElementById('save-status');
  var book, rendition, closed = false, busy = false, saveTimer, pending, saving = false, lastSaved;
  var image, preloads = [], page = 0, total = 0;
  var currentCfi, positionsReady = false, returnRequested = false;
  var font = config.fontSize, fontSaved = font, fontTimer, fontPending, fontSaving = false;
  var immersive = window.RTImmersive(config, {navigate: navigate, seek: seek,
    resize: function (width, height) { if (!closed && rendition) rendition.resize(width, height); },
    error: function (message, error) { status.textContent = message; if (window.console) window.console.error(message, error); }});
  function returnToBook() {
    if (returnRequested && config.returnUrl && !saving && pending === undefined && !fontSaving && fontPending === undefined) window.location.assign(config.returnUrl);
  }
  function failure(error) {
    status.textContent = config.labels.failed;
    if (!closed) immersive.open();
    if (window.console && window.console.error) window.console.error('RT reader:', error);
  }
  function xhr(method, url, body, done) {
    var request = new XMLHttpRequest();
    request.open(method, url, true);
    request.timeout = 30000;
    if (method === 'POST') {
      request.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
      request.setRequestHeader('X-CSRFToken', document.querySelector('meta[name="csrf-token"]').content);
    }
    request.onload = function () {
      var ok = method === 'POST' ? request.status === 201 || request.status === 204 : request.status >= 200 && request.status < 300;
      done(ok ? null : new Error('HTTP ' + request.status), request.responseText);
    };
    request.onerror = request.ontimeout = function () { done(new Error('Network request failed')); };
    request.send(body || null);
  }
  function flush() {
    window.clearTimeout(saveTimer);
    saveTimer = undefined;
    if (!config.saveUrl || pending === undefined || saving) return;
    var value = pending;
    pending = undefined;
    saving = true;
    xhr('POST', config.saveUrl, 'bookmark=' + encodeURIComponent(value), function (error) {
      saving = false;
      if (error) {
        saveStatus.textContent = config.labels.saveFailed;
        if (window.console && window.console.error) window.console.error('RT bookmark:', error);
      } else {
        lastSaved = value;
        saveStatus.textContent = config.labels.saved;
      }
      if (pending !== undefined) flush();
      else if (!error) returnToBook();
    });
  }
  function save(value) {
    if (closed || !config.saveUrl || value === lastSaved) return;
    pending = value;
    if (saveTimer === undefined) saveTimer = window.setTimeout(flush, 750);
  }
  function flushFont() {
    if (!config.fontSaveUrl || fontPending === undefined || fontSaving) return;
    var value = fontPending;
    fontPending = undefined; fontSaving = true;
    xhr('POST', config.fontSaveUrl, 'font_size=' + encodeURIComponent(value), function (error) {
      fontSaving = false;
      var message = document.getElementById('font-save-status');
      if (error) {
        message.textContent = config.labels.fontSaveFailed;
        if (window.console && window.console.error) window.console.error('RT font size:', error);
        if (fontPending === undefined) fontPending = value;
        return;
      }
      fontSaved = value; message.textContent = config.labels.fontSaved;
      if (fontPending === fontSaved) fontPending = undefined;
      if (fontPending !== undefined) flushFont(); else returnToBook();
    });
  }
  function saveFont() {
    if (!config.fontSaveUrl || (font === fontSaved && !fontSaving && fontPending === undefined)) return;
    fontPending = font; flushFont();
  }
  function closeReader(leave) {
    if (closed) return;
    returnRequested = leave === true;
    closed = true;
    window.clearTimeout(fontTimer);
    saveFont();
    window.clearTimeout(saveTimer);
    flush();
    if (book) book.destroy();
    book = rendition = null;
    immersive.destroy();
    preloads = [];
    if (image) { image.onload = image.onerror = null; image.removeAttribute('src'); }
    var viewer = document.getElementById('viewer');
    if (viewer) viewer.innerHTML = '';
    var audio = document.getElementById('audio');
    if (audio) { audio.pause(); audio.removeAttribute('src'); audio.load(); }
    document.getElementById('previous') && (document.getElementById('previous').disabled = true);
    document.getElementById('next') && (document.getElementById('next').disabled = true);
    status.textContent = config.labels.closed;
    returnToBook();
  }
  function operation(work) {
    if (busy || closed) return;
    busy = true;
    try {
      work().then(function () { busy = false; }, function (error) { busy = false; if (!closed) failure(error); });
    } catch (error) { busy = false; failure(error); }
  }
  function pageUrl(index) {
    return config.pageUrl.replace(/\/0$/, '/' + index) + '?size=' + document.getElementById('resolution').value;
  }
  function displayPage(index) {
    if (closed || !total || !isFinite(index)) return;
    index = Math.max(0, Math.min(total - 1, Math.floor(index)));
    if (index !== page) immersive.resetZoom();
    page = index;
    immersive.progress(page, total);
    status.textContent = config.labels.loading;
    document.getElementById('page-number').value = page + 1;
    document.getElementById('previous').disabled = page === 0;
    document.getElementById('next').disabled = page === total - 1;
    image.src = pageUrl(page);
  }
  function navigate(delta) {
    if (closed) return;
    if (config.kind === 'epub') operation(function () { return delta < 0 ? rendition.prev() : rendition.next(); });
    else if (config.kind === 'txt') seek(config.page + delta);
    else displayPage(page + delta);
  }
  function seek(index) {
    if (closed || !isFinite(index)) return;
    if (config.kind === 'epub' && positionsReady) {
      index = Math.max(0, Math.min(book.locations.length() - 1, Math.floor(index)));
      operation(function () { return rendition.display(book.locations.cfiFromLocation(index)); });
    } else if (config.kind === 'pages') displayPage(index);
    else if (config.kind === 'txt' && index >= 0 && index < config.total) window.location.assign(config.textUrl + '?page=' + (index + 1));
    else if (config.kind === 'audio') document.getElementById('audio').currentTime = index;
  }
  function epub() {
    book = window.ePub(config.opf, {openAs: 'opf', replacements: 'none'});
    rendition = book.renderTo('viewer', {width: '100%', height: '100%', allowScriptedContent: false});
    rendition.hooks.content.register(function (contents) { if (!closed) immersive.bindContents(contents); });
    rendition.on('relocated', function (location) {
      if (closed || !location.start) return;
      currentCfi = location.start.cfi; save(currentCfi);
      if (positionsReady) immersive.progress(book.locations.locationFromCfi(currentCfi), book.locations.length());
    });
    rendition.on('displayerror', failure);
    book.loaded.navigation.then(function (navigation) {
      if (closed) return;
      var select = document.getElementById('toc');
      function add(items, prefix) {
        items.forEach(function (item) {
          var option = document.createElement('option');
          option.value = item.href;
          option.textContent = prefix + item.label;
          select.appendChild(option);
          if (item.subitems) add(item.subitems, prefix + '- ');
        });
      }
      add(navigation.toc, '');
    }, failure);
    document.getElementById('toc').onchange = function () {
      var href = this.value;
      if (href) operation(function () { return rendition.display(href); });
    };
    var dark = true, fontSlider = document.getElementById('font-size');
    document.getElementById('night-mode').checked = dark;
    function applyTheme() {
      rendition.themes.default({html: {background: dark ? '#000000' : '#ffffff'},
        body: {color: dark ? '#eeeeee' : '#202020', background: dark ? '#000000' : '#ffffff'},
        a: {color: dark ? '#8acbff' : '#124e88'}});
    }
    applyTheme();
    function applyFont() { if (!closed) rendition.themes.fontSize(font + '%'); }
    function selectFont(value) {
      font = Math.max(70, Math.min(200, Math.round(value)));
      fontSlider.value = font; document.getElementById('font-size-value').textContent = font + '%';
    }
    function commitFont() { window.clearTimeout(fontTimer); applyFont(); saveFont(); }
    applyFont();
    fontSlider.oninput = function () { if (!closed) { selectFont(Number(this.value)); window.clearTimeout(fontTimer); fontTimer = window.setTimeout(applyFont, 100); } };
    fontSlider.onchange = function () { if (!closed) { selectFont(Number(this.value)); commitFont(); } };
    function resizeFont(delta) { if (!closed) { selectFont(font + delta); commitFont(); } }
    document.getElementById('smaller').onclick = function () { resizeFont(-10); };
    document.getElementById('larger').onclick = function () { resizeFont(10); };
    document.getElementById('night-mode').onchange = function () {
      if (closed) return;
      dark = this.checked;
      applyTheme();
    };
    operation(function () { return rendition.display(config.bookmark || undefined).then(function () { if (!closed) status.textContent = config.labels.ready; }); });
    book.ready.then(function () { if (!closed) return book.locations.generate(1200); }).then(function () {
      if (closed) return;
      if (!book.locations.length()) { status.textContent = config.labels.positionsFailed; immersive.positionError(); return; }
      positionsReady = true;
      immersive.progress(currentCfi ? book.locations.locationFromCfi(currentCfi) : 0, book.locations.length());
    }, function (error) {
      if (!closed) { status.textContent = config.labels.positionsFailed; immersive.positionError(); if (window.console) window.console.error(config.labels.positionsFailed, error); }
    });
  }
  function pages() {
    image = document.getElementById('page-image');
    image.onload = function () {
      if (closed) return;
      status.textContent = config.labels.ready + ' ' + (page + 1) + '/' + total;
      immersive.imageLoaded();
      save(String(page));
      preloads = [];
      [page - 1, page + 1].forEach(function (index) {
        if (index >= 0 && index < total) { var next = new Image(); next.src = pageUrl(index); preloads.push(next); }
      });
    };
    image.onerror = function () { if (!closed) failure(new Error('Page image failed')); };
    xhr('GET', config.listUrl, null, function (error, text) {
      if (closed) return;
      if (error) { failure(error); return; }
      try {
        total = JSON.parse(text).pages.length;
        if (!total) throw new Error('No pages');
        document.getElementById('page-count').textContent = '/ ' + total;
        document.getElementById('page-number').max = total;
        displayPage(Number(config.bookmark) || 0);
      } catch (parseError) { failure(parseError); }
    });
    document.getElementById('go').onclick = function () { displayPage(Number(document.getElementById('page-number').value) - 1); };
    document.getElementById('resolution').onchange = function () { displayPage(page); };
  }
  function audioReader() {
    var audio = document.getElementById('audio');
    audio.addEventListener('loadedmetadata', function () {
      var position = Number(config.bookmark) / 1000;
      if (isFinite(position) && position > 0 && position < audio.duration) audio.currentTime = position;
      status.textContent = config.labels.ready;
      if (isFinite(audio.duration)) immersive.progress(Math.floor(audio.currentTime), Math.ceil(audio.duration));
    });
    audio.addEventListener('timeupdate', function () { save(String(Math.floor(audio.currentTime * 1000))); if (isFinite(audio.duration)) immersive.progress(Math.floor(audio.currentTime), Math.ceil(audio.duration)); });
    audio.addEventListener('pause', function () { save(String(Math.floor(audio.currentTime * 1000))); flush(); });
    audio.addEventListener('error', function () { status.textContent = config.labels.audioFailed; });
  }
  if (config.kind === 'epub' || config.kind === 'pages') {
    document.getElementById('previous').onclick = function () { navigate(-1); };
    document.getElementById('next').onclick = function () { navigate(1); };
    document.addEventListener('keydown', function (event) {
      if (/^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(event.target.tagName)) return;
      if (immersive.isOpen()) return;
      if (event.keyCode === 37 || event.keyCode === 39) { event.preventDefault(); navigate(event.keyCode === 37 ? -1 : 1); }
    });
    if (config.kind === 'epub') epub(); else pages();
  } else if (config.kind === 'audio') audioReader();
  else { status.textContent = config.labels.ready; if (config.kind === 'txt') { save(String(config.page)); immersive.progress(config.page, config.total); } }
  if (config.kind === 'djvu') document.getElementById('progress-label').textContent = config.labels.externalPages;
  document.getElementById('close-reader').onclick = function () { closeReader(true); };
  document.getElementById('reader-details').onclick = function (event) { event.preventDefault(); closeReader(true); };
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flush(); });
  window.addEventListener('pagehide', closeReader);
  window.addEventListener('beforeunload', flush);
}());
