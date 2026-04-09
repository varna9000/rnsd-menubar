# Feature Request: OS-wide `rns://` URL Scheme Handler with In-App Nomadnet Page Rendering

## Summary

Extend `rnsd-menubar` to register itself as the macOS handler for a new `rns://` URL scheme, so that clicking or opening any `rns://<destination_hash>/page/<path>` link from anywhere on the system (Safari, Mail, Terminal, Raycast, other apps) opens and renders the corresponding Nomadnet page. Rendering is done client-side in the user's default browser using [micron-parser-js](https://github.com/RFnexus/micron-parser-js), bridged to the running `rnsd` via a tiny loopback HTTP server inside the menubar app.

## Motivation

Today, browsing Nomadnet requires launching the `nomadnet` TUI and pasting destination hashes manually. There is no way to share a Nomadnet page as a clickable link, and no integration with the rest of the OS. Since `rnsd-menubar` already supervises `rnsd`, ships as a real `.app` bundle, and has `RNS` as an in-process dependency, it is uniquely positioned to become the system-wide entry point for `rns://` links — without requiring a separate handler app, Electron runtime, or embedded web view.

## Goals

- Register `rns://` as a system URL scheme owned by `RNSD.app`.
- Handle `rns://<dest_hash>[/page/<path>]` links from any macOS app.
- Fetch Nomadnet pages in-process using the already-running shared `rnsd` instance.
- Render micron markup as HTML using `micron-parser-js`, sanitized with DOMPurify.
- Provide free history, back/forward, and bookmarks by reusing the user's default browser.
- Avoid any new heavyweight dependencies (no Electron, no WKWebView embedding).

## Non-Goals (for v1)

- Writing a native Swift/AppKit browser window.
- Supporting non-page Reticulum endpoints (files, LXMF messaging) — pages only.
- Authoring or editing micron pages.
- Windows/Linux parity (can follow later; the bridge design is portable).

## Architecture Overview

```
 ┌───────────────────────┐       rns://hash/page/x
 │ Safari / Mail / any   │ ───────────────────────────┐
 │ app that opens a URL  │                            │
 └───────────────────────┘                            ▼
                                      ┌───────────────────────────┐
                                      │ RNSD.app (menubar)        │
                                      │                           │
                                      │ ┌───────────────────────┐ │
                                      │ │ NSAppleEventManager   │ │
                                      │ │ kAEGetURL handler     │ │
                                      │ └──────────┬────────────┘ │
                                      │            │              │
                                      │            ▼              │
                                      │ ┌───────────────────────┐ │
                                      │ │ Loopback HTTP bridge  │ │
                                      │ │ 127.0.0.1:<port>      │ │
                                      │ │  GET /  (renderer)    │ │
                                      │ │  GET /fetch?dest&path │ │
                                      │ └──────────┬────────────┘ │
                                      │            │              │
                                      │            ▼              │
                                      │ ┌───────────────────────┐ │
                                      │ │ RNS in-process client │ │
                                      │ │ → shared rnsd         │ │
                                      │ │ → Link + page request │ │
                                      │ └───────────────────────┘ │
                                      └─────────────┬─────────────┘
                                                    │
                                                    ▼
                                      ┌───────────────────────────┐
                                      │ Default browser window    │
                                      │ micron-parser-js +        │
                                      │ DOMPurify render the page │
                                      └───────────────────────────┘
```

The menubar app dispatches the incoming `rns://` URL to `http://127.0.0.1:<port>/?url=<encoded>` in the user's default browser. The static renderer page fetches micron bytes from `/fetch`, converts them to sanitized HTML with `micron-parser-js`, and rewrites in-page links back to `/?url=rns://...` so navigation is transparent.

## Implementation Plan

### 1. Register the URL scheme in `RNSD.spec`

Add an `info_plist` dict to the existing `BUNDLE(...)` call in the PyInstaller spec. No separate `Info.plist` file is required; PyInstaller merges it at build time.

Required keys:

- `CFBundleURLTypes` with one entry containing `CFBundleURLName = "network.reticulum.rns"` and `CFBundleURLSchemes = ["rns"]`.
- `LSUIElement = True` is presumably already set (menubar app) — keep it.

Acceptance: after rebuild and reinstall, `open rns://example` launches or focuses `RNSD.app`.

### 2. Install an Apple Event handler for `kAEGetURL`

At app startup (before or alongside rumps), register a PyObjC handler on `NSAppleEventManager.sharedAppleEventManager()` for the `GetURL` event (`kInternetEventClass` / `kAEGetURL`). The handler extracts the URL string and enqueues it on the page-fetch worker.

Acceptance: logging the incoming URL works for links opened from Safari, Terminal (`open`), and other apps.

### 3. In-process Nomadnet page fetch

Because `rnsd` runs as a shared instance, instantiating `RNS.Reticulum()` a second time inside the menubar process attaches as a client — no second transport, no config conflict. On top of that, implement a `fetch_page(dest_hash, path, timeout) -> bytes` function that:

1. Validates and decodes the destination hash.
2. Requests a path via `RNS.Transport.request_path()` if unknown, waits up to N seconds.
3. Recalls the identity, constructs the `nomadnetwork.node` destination.
4. Establishes an `RNS.Link` and issues the page request for `/page/<path>`.
5. Returns the raw micron bytes, or a structured error.

Reference implementations to crib from: `nomadnet/Browser.py` and `reticulum-meshchat`'s page fetcher.

Acceptance: given a known public Nomadnet node, the function returns identical bytes to what `nomadnet` fetches.

### 4. Loopback HTTP bridge

Start a small `http.server` (stdlib is fine for v1) bound to `127.0.0.1` on an ephemeral port chosen at startup via `socket` with port `0`. Two routes:

- `GET /` — serves a single static HTML page that bundles `micron-parser.js` and DOMPurify (vendored into the app), reads `?url=rns://...` from `window.location`, fetches `/fetch?dest=...&path=...`, passes the response through `MicronParser.convertMicronToHtml()`, sanitizes, and injects into the DOM. In-page links matching `rns://` are rewritten to `/?url=rns://...` so clicks navigate naturally.
- `GET /fetch?dest=...&path=...` — calls `fetch_page()` and returns `text/plain; charset=utf-8` with the micron source, or a JSON error with an appropriate status code.

Security requirements:

- Bind to `127.0.0.1` explicitly, never `0.0.0.0`.
- Reject requests whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>`.
- DOMPurify must be in the rendering pipeline; it is not optional.

Acceptance: opening `http://127.0.0.1:<port>/?url=rns://<known_hash>/page/index.mu` directly in a browser renders the page.

### 5. Wire the Apple Event handler to the bridge

On receiving an `rns://` URL, the handler URL-encodes it and calls `webbrowser.open(f"http://127.0.0.1:{port}/?url={encoded}")`. Requests that arrive during the brief `rnsd` startup window should queue until `RNS.Reticulum()` is ready rather than erroring.

Acceptance: `open rns://<hash>/page/index.mu` from Terminal opens the default browser with a fully rendered page.

### 6. Menubar integration

- Add a **"Open Nomadnet page…"** menu item that prompts for an `rns://` URL and routes it through the same bridge.
- Make entries in the existing **Nodebook** clickable where the discovered node advertises a Nomadnet page destination, reusing the same flow.
- Add a **"Copy rns:// link"** action for nodes.

### 7. Vendoring and licensing

- Vendor `micron-parser-js` and DOMPurify into `assets/web/` and include them in `RNSD.spec`'s `datas`. Both are permissively licensed (Unlicense and Apache-2.0/MPL respectively).
- Add attribution to the README.

## Security Considerations

- **Loopback only.** The HTTP bridge must never bind to a non-loopback interface.
- **Host header check.** Prevents DNS rebinding attacks from a page in the user's browser targeting the bridge.
- **HTML sanitization.** All micron-derived HTML passes through DOMPurify before injection.
- **No remote script execution.** The static renderer page loads only vendored local JS; no CDN fetches, no inline script from fetched content.
- **Rate limiting.** Cap concurrent in-flight page fetches to avoid a malicious link storm exhausting Reticulum links.
- **Scheme scoping.** Only `rns://` URLs are accepted by the Apple Event handler; all other schemes are ignored.

## Open Questions

- Should the renderer page expose a minimal address bar for ad-hoc `rns://` entry, or stay chromeless and rely on the menubar's "Open…" dialog?
- Caching policy: in-memory only, or a small on-disk LRU keyed by `(dest, path)` with a short TTL?
- How should path-resolution progress be surfaced — polling endpoint, Server-Sent Events, or just a spinner with a timeout?
- Should discovered Nomadnet nodes in the Nodebook auto-prefetch their index page in the background?

## Milestones

1. **M1 — Scheme registration.** Info.plist entry, Apple Event handler, logs incoming URLs. *(≈1 evening.)*
2. **M2 — Page fetch.** In-process `fetch_page()` returns raw micron from a real node. *(≈1 evening.)*
3. **M3 — Bridge + renderer.** Loopback HTTP server, static renderer page, end-to-end working render in the default browser. *(≈1 evening.)*
4. **M4 — Menubar integration.** "Open Nomadnet page…" dialog, Nodebook click-through, copy-link action.
5. **M5 — Polish.** Loading states, error pages, caching, README/docs, vendored licenses.

Total realistic effort from current state: a weekend of focused work for M1–M3, another short session for M4–M5.

## Out of Scope / Future Work

- Native WKWebView window instead of the default browser (optional; the bridge design makes this a drop-in replacement).
- LXMF messaging UI.
- File/download endpoints beyond `/page/`.
- Windows and Linux packaging with the same scheme registration mechanism.
- Signed/notarized distribution so Gatekeeper accepts the URL scheme registration without a right-click-open on first launch.
