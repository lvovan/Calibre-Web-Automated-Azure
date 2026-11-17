# kthoom (vendored)

Comic book reader from https://github.com/codedread/kthoom (MIT, see `LICENSE.txt`),
vendored at upstream commit `8534eba` (2025-11-19) from its `code/` directory.

Not vendored: `main.js` (replaced by `cwa-main.js`), the Google Drive / IPFS / Microsoft
integrations, the service worker and analytics.

CWA-specific changes are marked with `CWA:` comments:

- `book.js` / `page.js`: `Book.loadFromPageList()` and `RemoteImagePage`, so pages are
  fetched one at a time from the server (`/comic/<id>/<format>/page/<n>`) instead of
  downloading and unpacking the whole archive in the browser. The full download is
  still used as a fallback when the server cannot list the pages.
- `page.js`: `ImagePage.getURI()` caches its object URL instead of leaking a new one per render.
- `book-viewer.js`: long-strip / wide-strip modes only attach images within a few pages of
  the current page (hidden containers release theirs), track touch scrolling, only scroll
  on explicit navigation (not on resizes such as the iOS toolbar collapsing), fix wide-strip
  page jumps (upstream scrolled vertically), and emit a `kthoom-pagechange` event (bookmarks).
- `pages/page-container.js`: `unloadImage()` / `getRenderedPageNum()` for the strip window.
- `kthoom.js`: `loadSingleBookFromPageList()`.
- `common/helpers.js`: URL rewriting can be disabled through the `SERIALIZE_PARAMS` config key.
- `metadata/metadata-viewer.js`: metadata editing is disabled for page-list books (no archive).
- `cwa-main.js` / `cwa.css`: CWA entry point (bookmarks, prefetching, fallback, UI trimming).

Server side: `cps/comic_pages.py` lists and extracts CBZ/CBT/CBR pages; routes live in `cps/web.py`.
