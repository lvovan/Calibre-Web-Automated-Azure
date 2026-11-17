/*
 * cwa-main.js
 *
 * Calibre-Web Automated entry point for kthoom (replaces upstream main.js).
 *
 * Books are read page by page: the server lists the pages of the archive and serves each image on
 * demand, so the browser never downloads or unpacks the whole archive. If the page endpoints are
 * unavailable (e.g. Google Drive libraries), the whole book is fetched and unpacked by kthoom.
 *
 * Licensed under the MIT License
 */

import { config } from './config.js';
import { Params } from './common/helpers.js';
import { KthoomApp } from './kthoom.js';
import { RemoteImagePage } from './page.js';

const cfg = window.cwaComic;

const PREFETCH_PAGES = 2;
const BOOKMARK_DEBOUNCE_MS = 1000;

config
  .set('PATH_TO_BITJS', cfg.staticBase + 'bitjs/')
  .set('SERIALIZE_PARAMS', false)
  .lock();

// The book is chosen by the server; the reader must not open other books or rewrite the URL.
for (const key of Object.keys(Params)) {
  delete Params[key];
}
Params.preventUserOpeningBooks = 'true';
Params.doNotPromptOnClose = 'true';

const app = new KthoomApp();
window.kthoom.getApp = () => app;

/** @returns {number} The saved 0-based page, or -1. */
function savedPage() {
  if (!cfg.useBookmarks) return -1;
  const n = parseInt(cfg.bookmark, 10);
  return Number.isInteger(n) && n >= 0 ? n : -1;
}

let bookmarksEnabled = false;
let bookmarkTimer = null;
function saveBookmark(pageNum) {
  if (!bookmarksEnabled || !cfg.useBookmarks) return;
  clearTimeout(bookmarkTimer);
  bookmarkTimer = setTimeout(() => {
    const body = new URLSearchParams({ csrf_token: cfg.csrfToken, bookmark: String(pageNum) });
    fetch(cfg.bookmarkUrl, { method: 'POST', body, credentials: 'same-origin' })
      .catch(err => console.error('Could not save bookmark', err));
  }, BOOKMARK_DEBOUNCE_MS);
}

/** @type {string[]} */
let pageUrls = [];
/** Keeps prefetched images referenced until they are loaded into the HTTP cache. */
const prefetched = [];
function prefetch(pageNum, numPagesInViewer) {
  if (!pageUrls.length) return;
  const ahead = PREFETCH_PAGES * Math.min(Math.max(numPagesInViewer, 1), 2);
  for (let i = pageNum + 1; i <= pageNum + ahead && i < pageUrls.length; ++i) {
    if (prefetched.some(img => img.dataset.page === String(i))) continue;
    const img = new Image();
    img.dataset.page = String(i);
    img.decoding = 'async';
    img.src = pageUrls[i];
    prefetched.push(img);
    while (prefetched.length > 2 * ahead) prefetched.shift();
  }
}

window.addEventListener('kthoom-pagechange', (evt) => {
  const { pageNum, numPagesInViewer } = evt.detail;
  saveBookmark(pageNum);
  prefetch(pageNum, numPagesInViewer);
});

/** Restores the bookmark once the page exists, then starts saving bookmarks. */
function restoreBookmark(book) {
  const target = savedPage();
  const done = () => { bookmarksEnabled = true; };
  if (target <= 0) { done(); return; }
  const tryShow = () => {
    if (book.getNumberOfPagesReady() > target) {
      app.bookViewer_.showPage(target);
      done();
      return true;
    }
    return false;
  };
  if (tryShow()) return;
  // Fallback (whole-archive) mode: pages appear while the archive unpacks.
  const timer = setInterval(() => {
    if (tryShow() || (book.getNumberOfPages() > 0 && book.getNumberOfPages() <= target &&
        book.getNumberOfPagesReady() >= book.getNumberOfPages())) {
      clearInterval(timer);
      done();
    }
  }, 250);
}

async function loadPaged() {
  const resp = await fetch(cfg.manifestUrl, { credentials: 'same-origin' });
  if (!resp.ok) return null;
  const manifest = await resp.json();
  if (!manifest || !Array.isArray(manifest.pages) || manifest.pages.length === 0) return null;
  pageUrls = manifest.pages.map((_, i) => cfg.pageUrlBase + i);
  const pages = manifest.pages.map((p, i) =>
    new RemoteImagePage(p.name, p.mime, pageUrls[i], p.w, p.h));
  return app.loadSingleBookFromPageList(cfg.title, pages);
}

async function start() {
  await app.initializedPromise_;
  let book = null;
  try {
    book = await loadPaged();
  } catch (err) {
    console.warn('Page-by-page loading unavailable, loading the whole book', err);
  }
  if (book) {
    restoreBookmark(book);
    return;
  }
  app.loadSingleBookFromFetch(cfg.title, cfg.serveUrl, { credentials: 'same-origin' });
  const waitForBook = setInterval(() => {
    const current = app.readingStack_.getCurrentBook();
    if (current) {
      clearInterval(waitForBook);
      restoreBookmark(current);
    }
  }, 100);
}

start();
