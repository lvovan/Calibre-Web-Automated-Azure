# Isolated RT / IE11 interface

This package is always registered at `/rt/`, without a feature flag. Requests to
the main catalog, book, reader, shelf, search and login pages redirect here when
the user agent contains `Trident/`. Other clients keep the existing UI and assets.
Only GET/HEAD page requests are selected; API calls and POSTs are not intercepted.

Opening `/rt/` explicitly selects this UI with a one-year `cwa_ui=rt` cookie.
**Full site** selects `cwa_ui=full`, overriding detection. Cookies are HttpOnly,
SameSite=Lax, Secure on HTTPS, and scoped to the application's URL prefix.
Selection responses vary by Cookie/User-Agent and are not publicly cacheable.

## Scope

- Paginated recent books, the existing library-search syntax, ordinary and magic
  shelves, book details and JPEG covers. Shelf books are sorted by title. Magic
  shelves reuse the rule builder and library visibility filters, without the main
  page's templates or cached unfiltered book retrieval. Advanced main-site
  list filters are not reproduced; those main list links land on recent books.
- EPUB/KEPUB with TOC, font size, theme, exact CFI bookmarks and explicit cleanup.
- CBZ/CBR/CBT and all PDF pages through server-rendered, bounded JPEGs. Default
  long edge 1600; lower/higher options 800/1200/2400. At most two adjacent images
  are preloaded. Swipes, arrow keys and numbered page navigation are available.
- Server-paged TXT (20,000 characters, 8 MiB input limit), native audio controls
  (actual codec support depends on IE), and the existing GWT DjVu engine.
- Anonymous browsing and viewer/download roles use existing application rules.
  Bookmarks are saved only for authenticated users, using the existing CSRF-
  protected endpoint and per-format records. Audio preserves the main player's
  millisecond positions; TXT uses its RT page index. KOReader/xpointer synchronization
  and cross-reader TXT position parity are not included. EPUB CFIs and comic/PDF
  page bookmarks are reused.
- Local/mounted libraries only. Google Drive-backed reading, CB7, admin, editing,
  upload, magic-shelf editing and full-site feature parity are outside this module.
  Downloads remain available when the user's role permits them.

Only blueprint registration changes application Python outside this package.
`_deps.py` adapts existing server helpers; it does not patch them. Templates and
assets are separate from the main theme. No Node tooling is needed in production.
The main login POST, OAuth routes, downloads, media and bookmark endpoints remain
authoritative; failed legacy login renders bounce back with flashes preserved.

## Immersive reading

The reader fills the browser viewport without site navigation. The first content
tap requests true browser fullscreen when supported; the dialog's **Full screen**
button can retry. Browsers which disallow that API still use the full-window layout.
Resize, rotation and fullscreen changes refit images and resize EPUB pagination,
retaining the reading position.

Tap the leftmost 20% for Previous, the rightmost 20% for Next, and the top 20% of
the middle 60% to open book controls. EPUB events are attached inside sandboxed
Contents documents and removed when each Contents is destroyed. In-book links,
form controls, drags and multi-touch gestures do not become page taps.

The dialog shows title, authors, format and reading progress, plus existing chapter,
font/theme or image-resolution controls. **X**, Escape or the backdrop dismisses it;
Tab focus stays inside the dialog. Its X remains outside the scrollable content on
short displays. **Close reader** clears owned frames/images and waits for the last
bookmark write before returning to book details. A failed save remains visible.

Comic/PDF image pages have an exact page slider. TXT uses its server page index.
EPUBs use generated CFI reading positions (1,200-character intervals), explicitly
labelled **Reading position**, not invented printed page numbers: pagination changes
with viewport and font size, so jumping aligns to the rendered page containing the
requested CFI. Generation is serial and its queue stops on book teardown. Audio
progress uses elapsed seconds; the legacy DjVu engine retains its own page controls.

Comic/PDF images support two-pointer pinch zoom (1x-5x), single-pointer panning,
bounded edges and **Reset zoom**. Pinches/pans never trigger page turns; a new page
resets zoom. Changing image resolution keeps zoom, and rotation refits the image.
This is custom image zoom, not browser-wide page zoom.

## Publication isolation and bounds

Publication resources require both a filtered book and the viewer role. The
version and manifest allowlist are checked before conditional responses. Standard
CSP and IE's legacy `X-Content-Security-Policy` sandbox header apply even to 304s;
epub.js uses `allowScriptedContent: false` and an iframe sandbox without scripts.
The final RT header hook runs after the main CSP writer. Publication content must
never be served through a script-enabled RT frame.

The RT shell uses external scripts only. DjVu alone retains the main GWT engine's
inline/eval requirement; that exception does not apply to EPUBs or other readers.
RT requests negotiate gzip or identity instead of Flask-Compress's preferred
deflate. Native Trident loaded zero stylesheet rules with deflate, reproducing the
unstyled deployed Surface pages; the compatible negotiation restored the actual
font, spacing and book layout. Streaming assets may remain uncompressed. Main-site
compression is unchanged. Render validation must enable application compression,
not just exercise the bare Flask fixture.
Image bytes are bounded by the existing archive helpers and Pillow's decompression
bomb limit; every page, including JPEG passthrough from a PDF or comic, is resized
to the requested bound. The server still has to decode the source image, so JPEG
output bounds are not a server-memory guarantee. Failure is explicit, never a
successful-looking placeholder. Covers without a file use a neutral JPEG.

## Rebuilding committed assets

From this directory, install the pinned developer dependencies with
`npm ci --prefix tools`, then `npm run build --prefix tools`. The lockfile uses
public registry URLs. The build verifies the original vendored epub.js SHA256,
transpiles for IE11, applies an exactly matched Trident DOMParser-to-XMLSerializer
correction, and parses every shipped RT script as ES5. If the upstream bundle
changes, revalidate on Trident and Surface RT rather than bypassing that guard.

`static/epub-lifecycle.js` separately cancels queued resize frames/polling and
unregisters visibility handlers when Contents is destroyed. It also guards an
already-dispatched callback. Do not remove this shim: native Trident reproduced
permission errors against destroyed iframe documents without it. Vendor notices
are next to the bundles in `static/vendor/`.

## Validation and device acceptance

Run `pytest tests/unit/test_rt_routes.py` and
`node --test tests/unit/test_rt_reader.cjs` from the repository root. Shared EPUB,
comic and PDF helper tests cover the underlying resource paths. The route suite
uses the real blueprint and existing authorization/path helpers with isolated
application dependencies, not a production database. It covers redirects,
cookies, HTML rendering, CSRF, permission failures, private conditionals, resource
allowlists and response-hook ordering. Browser tests are still required.

Initial synthetic-device spikes passed on first-generation Surface RT, Windows
RT 8.1, touch IE11, document mode 11. Two corrected workloads completed 200 total
navigations and 20 switches without errors. A subsequent reader-closed check
settled at 46.6-46.7 in the reported Task Manager units from minutes two through
ten. This supports feasibility, not unrestricted memory or security certification.

After deploying, test on the Surface over the real HTTPS hostname: automatic
selection, successful/failed login, Full site and `/rt/` opt-in, catalog/search,
EPUB chapters/illustrations/font/theme, CBZ/PDF page quality and swipes, position
resume, TXT/audio and any actual DjVu files. Check permission-denied users and
anonymous behavior in a non-production test environment. Repeat lifecycle and
memory checks with real books; do not send mutating verification requests to
production. Admin and configuration remain full-site-only.
