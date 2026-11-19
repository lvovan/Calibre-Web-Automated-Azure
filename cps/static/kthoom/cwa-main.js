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
import { MenuEventType } from './menu.js';
import { RemoteImagePage } from './page.js';
import { FitMode } from './book-viewer-types.js';

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

// Per-user progress shared with KOReader devices (1-based page numbers), when sync is enabled.
const sync = window.CwaProgressSync ? window.CwaProgressSync.create(cfg.progressSync, {
  csrfToken: cfg.csrfToken,
  onRemote: (state) => {
    const book = app.readingStack_.getCurrentBook();
    const page = book ? pageFromProgress(state, book.getNumberOfPages()) : -1;
    if (page >= 0 && book.getNumberOfPagesReady() > page) showSyncedPage(page);
  },
}) : null;

/** 0-based page shown by a restore or synchronized jump; its page change is not saved as new progress. */
let jumpPage = -1;
let navigation = null;
function showSyncedPage(page) {
  if (navigation) navigation.cancel();
  if (page !== app.bookViewer_.lastNotifiedPageNum_) jumpPage = page;
  app.bookViewer_.showPage(page);
}

/**
 * @param {object} state Server progress.
 * @param {number} total Number of pages in the book.
 * @returns {number} The 0-based page for a synchronized position, or -1.
 */
function pageFromProgress(state, total) {
  if (!state || !total) return -1;
  const page = window.CwaProgressSync.parsePage(state.progress);
  if (page !== null && page >= 1 && page <= total) return page - 1;
  if (typeof state.percentage === 'number' && state.percentage > 0) {
    return Math.min(total - 1, Math.max(0, Math.round(state.percentage * total) - 1));
  }
  return -1;
}

/** @returns {number} The saved 0-based page, or -1. */
function savedPage(book) {
  if (sync && sync.state) {
    const page = pageFromProgress(sync.state, book.getNumberOfPages());
    if (page >= 0) return page;
  }
  if (!cfg.useBookmarks) return -1;
  const n = parseInt(cfg.bookmark, 10);
  return Number.isInteger(n) && n >= 0 ? n : -1;
}

let bookmarksEnabled = false;
let bookmarkTimer = null;
function saveBookmark(pageNum) {
  if (!bookmarksEnabled) return;
  const book = app.readingStack_.getCurrentBook();
  const total = book ? book.getNumberOfPages() : 0;
  if (sync && total > 0) sync.save(String(pageNum + 1), (pageNum + 1) / total);
  if (!cfg.useBookmarks) return;
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
  const programmatic = jumpPage >= 0 && pageNum === jumpPage;
  jumpPage = -1;
  if (!programmatic) saveBookmark(pageNum);
  prefetch(pageNum, numPagesInViewer);
});

/** Restores the bookmark once the page exists, then starts saving bookmarks. */
function restoreBookmark(book) {
  const done = () => { bookmarksEnabled = true; };
  let target = -1;
  const tryShow = () => {
    // The page count is only known once the archive has been listed or unpacked.
    if (target < 0) target = book.getNumberOfPages() > 0 || !sync ? savedPage(book) : -1;
    if (target === 0 || (target < 0 && book.getNumberOfPages() > 0)) {
      done();
      return true;
    }
    if (target > 0 && book.getNumberOfPagesReady() > target) {
      showSyncedPage(target);
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

/**
 * @returns {string} Query string asking the server for page images matched to this screen (PDF pages
 *     are rendered at a requested size; image archives ignore it).
 */
function pageSizeQuery() {
  if (!cfg.sizedPages) return '';
  const longSide = Math.max(window.screen.width || 0, window.screen.height || 0, 800) *
      (window.devicePixelRatio || 1);
  return '?size=' + Math.min(3200, Math.ceil(longSide / 400) * 400);
}

async function loadPaged() {
  const resp = await fetch(cfg.manifestUrl, { credentials: 'same-origin' });
  if (!resp.ok) return null;
  const manifest = await resp.json();
  if (!manifest || !Array.isArray(manifest.pages) || manifest.pages.length === 0) return null;
  const query = pageSizeQuery();
  pageUrls = manifest.pages.map((_, i) => cfg.pageUrlBase + i + query);
  const pages = manifest.pages.map((p, i) =>
    new RemoteImagePage(p.name, p.mime, pageUrls[i], p.w, p.h));
  return app.loadSingleBookFromPageList(cfg.title, pages);
}

async function start() {
  await app.initializedPromise_;
  const viewer = app.bookViewer_;
  navigation = window.CwaNavigation.create({
    position: () => viewer.getCurrentPageNum(),
    bounds: () => [-Infinity, Infinity],
    navigate: page => {
      const total = app.readingStack_.getCurrentBook()?.getNumberOfPages() || 0;
      if (!total) return;
      viewer.showPage(((page % total) + total) % total);
      if (viewer.getFitMode() === FitMode.Width && viewer.getNumPagesInViewer() < 3) window.scrollTo(0, 0);
    },
    onError: error => console.error('Comic page navigation failed', error),
  });
  // All page-turn controls (including page taps and keyboard arrows) share the same intent.
  app.showNextPage = () => navigation.move(1);
  app.showPrevPage = () => navigation.move(-1);
  window.CwaNavigation.bindSwipe(document.getElementById('bookViewer'), {
    move: delta => navigation.move(delta),
    axis: () => viewer.getRotateTimes(),
    enabled: () => !(window.visualViewport && window.visualViewport.scale > 1) &&
      document.documentElement.scrollWidth <= window.innerWidth + 2 &&
      !app.readingStack_.isOpen(),
  });
  if (app.mainMenu_) {
    app.mainMenu_.addEventListener(MenuEventType.ITEM_SELECTED, (evt) => {
      if (evt.item.id === 'menu-sync-now' && sync) sync.syncNow();
      else if (evt.item.id === 'menu-open-pdfjs' && cfg.fallbackUrl) window.location.href = cfg.fallbackUrl;
    });
  }
  let book = null;
  try {
    book = await loadPaged();
  } catch (err) {
    console.warn('Page-by-page loading unavailable', err);
  }
  if (book) {
    restoreBookmark(book);
    return;
  }
  if (cfg.fallbackUrl) {
    // Formats kthoom cannot unpack itself (PDF) fall back to their own reader.
    window.location.replace(cfg.fallbackUrl);
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
