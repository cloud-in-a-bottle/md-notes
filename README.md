# md-notes

## Design

This is a document/notes editor, most similar to Obsidian. Some important core design principles:
- it is markdown based. plain markdown files are the source of truth
- the backend runs in openhost, a platform for easily self-hosting open source and personal software in a persistent, web-routeable space. every user of this app will host their own instance in their own openhost space.
- there's a client-server model because we want native support for multi-device use and real-time collaboration.
- the backend is designed to be used with any compatible client - web or native. the openhost app comes with the server and a default web frontend, but other clients can connect to the same backend.
- realtime collaboration is enabled by a CRDT layer sitting on top of the markdown file archive - this is intended to be ephemeral, serving to deconflict simultaneous edits but otherwise do as little as possible.
- some features/metadata might likely won't fit cleanly in markdown, like comments, history, sharing status, etc, and we'll deal with those as necessary, probably persisting to a sqlite db.


## Implementation details

### sync protocol

- i first tried to build this where markdown is really the source of truth, and the CRDT is ephemeral - created when the first client connects, and deleted shortly after the last client disconnects
    - and the client is supposed to pull fresh state any time it reconnects
    - but "disconnects" can mean "closed laptop and will reopen in a week", and their client will still have the old state
    - and when the client reconnects, part of the handshake is that the client and server sync state. if the server is a fresh room, with the doc freshly loaded, and the client already has the doc with separate history, they'll merge and you'll end up with duplication.
    - seems like this could be addressed with a change to the handshake logic, but the CRDT libraries expect that the server persists CRDT state even if the room is shutdown, so hooks into this handshake aren't really exposed. and i don't really wanna get to rewriting the core libraries.
    - with a native app that has persistent local state, this gets even harder, and a persistent remote CRDT state probably makes even more sense?

- the intended pattern is to persist room state to disk also, so that there's only one CRDT history, and resyncs of different devices will always be consistent.
    - i don't love this though because it weakens the .md files being the single source of truth - if the CRDT is persistent, it's really the source of truth, and i guess we just write out the body to the .md files periodically, but the .md files never actually get read back. does this matter? idk
    - this also helps avoid a full document copy on page load (if client state was stored in indexdb or similar)
    - what if the .md files get edited while the CRDT is active? that makes a mess of things.
    - this will also let me store comments etc in the CRDT state and not need to persist to an external DB.

i ended up switching to the second pattern; just seemed like that's closer to standard practice.

### backend

- litestar API
- CRDT implemented with y.js and served over a websocket
- API is documented in server/openapi/openapi.json. this is generated via a git hook; **clients should build against this spec**

### default web editor (built for zack specifically :) )

- **CodeMirror 6 editor** with markdown language support
- **Markdown styling** via `markdownStylePlugin` — headings render at proper sizes, bold/italic/code/links get CSS styling.
- **Live preview** via `livePreviewPlugin`/`linkPlugin` — formatting marks are hidden unless the cursor is nearby: `#`/`>` show while the cursor is on their line, `**`/`*`/`~~`/`` ` `` while the selection touches the styled span. Links render as just the link text; the URL shows when the cursor is inside. Cmd/Ctrl-click opens a link (plain click places the cursor to edit).
- **Editor preference** (Settings) — "Live preview" (standard keybindings, the default) or "Live preview (vim keybindings)". Both share the same editor core; the long-term vision is that the whole editor is swappable and other frontends can target the same backend.
- **Vim mode** (opt-in) via `@replit/codemirror-vim` with status bar showing current mode
- **Vimrc parser** — supports `map`/`noremap` (with mode prefixes `nmap`, `imap`, `vmap`, etc.) and `set` commands (`number`, `relativenumber`, `tabstop`, `shiftwidth`, `expandtab`, `wrap`, `scrolloff`)
- **Header-based folding** — Obsidian-style inline chevrons (no gutter column): hover a heading to reveal the chevron, click to collapse; folded headings keep the chevron and show a muted `...` that unfolds on click
- **File tree sidebar** with create (+ button), rename/delete (right-click context menu)
- **Pane collapse** — with split panes, the `«`/`»` button in a pane's tab bar collapses it to a thin strip so the other pane gets the full width; click the strip (or focus the pane) to restore. `Cmd/Ctrl+Shift+\` toggles the active pane.
- **Header share links** — hover a heading to get a link button that copies a read-only or editable share link to that section (`/share/<uuid>#<slug>`, GitHub-style slugs). Opening such a link scrolls to the first matching header, cursor on it, unfolded; unknown slugs just load normally.
- **Comments** — Google-Docs-style: selecting text reveals a comment icon at the editor's right edge (debounced; also Cmd/Ctrl-Alt-M). Threads render as floating cards inside the editor scroller, each vertically anchored beside its commented text (crowded cards stack downward; the active/composing card pins exactly to its anchor and pushes neighbors aside), with Linear-style linear threads (avatar + name rows, no indent), replies and resolve. Comments are stored in a `comments` Y.Map on the doc's Y.Doc (so they persist in the CRDT sidecar, never in the .md) and are anchored with Yjs relative positions, which track concurrent edits; when a whole commented span is deleted the comment is hidden immediately and GC'd on the server's next save. All comment mutations go through REST (`/api/docs/.../comments`, `/api/share/<uuid>/comments`) and the *server* writes into the live Y.Doc — necessary because share links below "write" get a read-only CRDT channel. Share links now have three tiers: view / comment / edit. Share visitors are prompted for a display name on first comment (stored in localStorage with a per-browser UUID); the owner's name comes from `OPENHOST_OWNER_USERNAME`. Mobile shows comment highlights only for now (no panel).

### mobile

- **Mobile shell** (phones, Obsidian-mobile-style): one document at a time, a slide-in drawer for the file tree with separate "Open note" (file search) and "Search text" (full-text) entries, a floating drawer toggle over the text (no top bar), and a floating formatting-toolbar pill (heading-level picker incl. remove, bold, checkbox cycle, indent/outdent) that sits above the virtual keyboard via `visualViewport` tracking.
- **Mobile editor variant** (`live-preview-mobile`): shares the live-preview core, but fold chevrons reveal on the cursor line instead of hover (and stay on folded headings), and hover-only features like header share-link buttons are skipped.
- **Shell selection**: autodetected (coarse pointer + small screen → mobile; tablets currently get desktop), overridable via Settings → "App layout".
- File rename/delete on touch via a per-row `⋯` menu (iOS has no long-press path to right-click menus).
- **PWA**: `manifest.webmanifest` + icons, installable to the home screen (standalone display). No service worker on purpose — the app is online-only by design, so offline caching would only add staleness risk.

### federation

Whole vaults can be shared with other md-notes instances (view-only, comment-only, or full
access): see `docs/federation.md`. There's a single vault model — host URL + optional secret +
permission — so owned and connected vaults go through the same client code path, talking
directly from the browser to the vault's host. Sharing is per-vault ("Share this vault with
another md-notes user..." in the sidebar's vault menu), named per recipient, and revocable. The
share produces an invite link to this instance; the recipient pastes it into their own md-notes
("Manage vaults… → Connect a shared vault"), whose server merely records the connection.

### cross-app service

Other apps in the same OpenHost space can read notes through the `notes` service
(`github.com/imbue-openhost/md-notes/services/notes`): list files, list a file's headers, read one
section, read a whole file — raw markdown, read out of the live CRDT. Access is per-vault and
optionally per-file, and md-notes serves its own consent page (`/service/grant`) where the owner
shapes the grant, so a consumer app can't ask for more than the owner picks. Published spec in
`services/notes/`; implementation notes in `docs/notes_service.md`.

### tauri native editor

- intended to share most code with the web editor, just packaged as a native app
- unfinished and on hold for now, to focus on the web editor.
- supporting a nice offline mode adds complexity vs web.


## Development

Running locally without a container (faster iteration than the openhost harness):

- **Server**: config comes from the OpenHost env vars and fails loudly without them. Vaults are plain directories of .md files under `$OPENHOST_APP_DATA_DIR/vault/<vault-name>/`. The identity/router vars only matter for links and cross-app calls, so any placeholder works locally.
  ```bash
  OPENHOST_APP_DATA_DIR=/tmp/mdnotes OPENHOST_SQLITE_MAIN=/tmp/mdnotes/main.db \
  OPENHOST_APP_NAME=md-notes OPENHOST_ZONE_DOMAIN=localhost:5173 \
  OPENHOST_APP_ID=dev OPENHOST_APP_TOKEN=dev OPENHOST_ROUTER_URL=http://localhost:9999 \
  uv run python -m server
  ```
- **Frontend**: `cd frontend && npx vite` proxies `/api` (including websockets) to `localhost:8000`. Authed routes only check for an `x-openhost-is-owner: true` header (normally set by the OpenHost router). `npx vite --config vite.config.lan.mts` injects that header at the proxy (skipping the login flow) and listens on all interfaces — use it to test from a phone on the same network (`http://<laptop-ip>:5173`). Local testing only; don't expose beyond a trusted network. Share pages (`/share/<uuid>`) are public and need no header.
- **Driving it headlessly**: `@playwright/test` is a frontend devDependency. Docs load empty and fill via CRDT sync, so after `.cm-editor` appears allow ~1s before asserting on content. The default editor is non-vim (typing inserts directly); set `localStorage['mdnotes-editor-kind'] = 'live-preview-vim'` before load for vim keys, or emulate a phone viewport for the mobile shell.
- **Integration tests**: `just test-integration` builds the container and deploys it on a real local OpenHost router (needs podman). The same `OpenhostStack` harness can be held open to click around a real deployment — it snapshots the working tree (tracked + untracked files), so uncommitted changes are included.

## TODO

### server

- make openapi doc for api contract
- organize files better
- split core vs route code better
- cleanup config.py into a typed config object
- do something with the db
