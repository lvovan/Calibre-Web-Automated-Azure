/* SPDX-License-Identifier: GPL-3.0-or-later */
(function () {
  'use strict';
  var config = JSON.parse(document.getElementById('rt-config').textContent);
  var status = document.getElementById('reader-status');
  var saveStatus = document.getElementById('save-status');
  var book, rendition, closed = false, busy = false, saveTimer, pending, saving = false, lastSaved;
  var image, preloads = [], page = 0, total = 0;
  function failure(error) {
    status.textContent = config.labels.failed;
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
    });
  }
  function save(value) {
    if (closed || !config.saveUrl || value === lastSaved) return;
    pending = value;
    if (saveTimer === undefined) saveTimer = window.setTimeout(flush, 750);
  }
  function closeReader() {
    if (closed) return;
    closed = true;
    window.clearTimeout(saveTimer);
    flush();
    if (book) book.destroy();
    book = rendition = null;
    preloads = [];
    if (image) { image.onload = image.onerror = null; image.removeAttribute('src'); }
    var viewer = document.getElementById('viewer');
    if (viewer) viewer.innerHTML = '';
    var audio = document.getElementById('audio');
    if (audio) { audio.pause(); audio.removeAttribute('src'); audio.load(); }
    document.getElementById('previous') && (document.getElementById('previous').disabled = true);
    document.getElementById('next') && (document.getElementById('next').disabled = true);
    status.textContent = config.labels.closed;
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
    page = Math.max(0, Math.min(total - 1, Math.floor(index)));
    status.textContent = config.labels.loading;
    document.getElementById('page-number').value = page + 1;
    document.getElementById('previous').disabled = page === 0;
    document.getElementById('next').disabled = page === total - 1;
    image.src = pageUrl(page);
  }
  function navigate(delta) {
    if (config.kind === 'epub') operation(function () { return delta < 0 ? rendition.prev() : rendition.next(); });
    else displayPage(page + delta);
  }
  function epub() {
    book = window.ePub(config.opf, {openAs: 'opf', replacements: 'none'});
    rendition = book.renderTo('viewer', {width: '100%', height: '100%', allowScriptedContent: false});
    rendition.on('relocated', function (location) { if (!closed && location.start) save(location.start.cfi); });
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
    var font = 100, dark = false;
    function resizeFont(delta) { if (!closed) { font = Math.max(70, Math.min(200, font + delta)); rendition.themes.fontSize(font + '%'); } }
    document.getElementById('smaller').onclick = function () { resizeFont(-10); };
    document.getElementById('larger').onclick = function () { resizeFont(10); };
    document.getElementById('theme').onclick = function () {
      if (closed) return;
      dark = !dark;
      rendition.themes.default({body: {color: dark ? '#eeeeee' : '#202020', background: dark ? '#202020' : '#ffffff'}});
    };
    operation(function () { return rendition.display(config.bookmark || undefined).then(function () { if (!closed) status.textContent = config.labels.ready; }); });
  }
  function pages() {
    image = document.getElementById('page-image');
    image.onload = function () {
      if (closed) return;
      status.textContent = config.labels.ready + ' ' + (page + 1) + '/' + total;
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
    var startX, startY;
    image.addEventListener('pointerdown', function (event) { startX = event.clientX; startY = event.clientY; });
    image.addEventListener('pointerup', function (event) {
      var dx = event.clientX - startX, dy = event.clientY - startY;
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) navigate(dx > 0 ? -1 : 1);
      startX = startY = undefined;
    });
    image.addEventListener('pointercancel', function () { startX = startY = undefined; });
  }
  function audioReader() {
    var audio = document.getElementById('audio');
    audio.addEventListener('loadedmetadata', function () {
      var position = Number(config.bookmark) / 1000;
      if (isFinite(position) && position > 0 && position < audio.duration) audio.currentTime = position;
      status.textContent = config.labels.ready;
    });
    audio.addEventListener('timeupdate', function () { save(String(Math.floor(audio.currentTime * 1000))); });
    audio.addEventListener('pause', function () { save(String(Math.floor(audio.currentTime * 1000))); flush(); });
    audio.addEventListener('error', function () { status.textContent = config.labels.audioFailed; });
  }
  if (config.kind === 'epub' || config.kind === 'pages') {
    document.getElementById('previous').onclick = function () { navigate(-1); };
    document.getElementById('next').onclick = function () { navigate(1); };
    document.getElementById('close-reader').onclick = closeReader;
    document.addEventListener('keydown', function (event) {
      if (/^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(event.target.tagName)) return;
      if (event.keyCode === 37 || event.keyCode === 39) { event.preventDefault(); navigate(event.keyCode === 37 ? -1 : 1); }
    });
    if (config.kind === 'epub') epub(); else pages();
  } else if (config.kind === 'audio') audioReader();
  else { status.textContent = config.labels.ready; if (config.kind === 'txt') save(String(config.page)); }
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flush(); });
  window.addEventListener('pagehide', closeReader);
  window.addEventListener('beforeunload', flush);
}());
