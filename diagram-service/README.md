# Diagrams

Web service for creating and editing Excalidraw and draw.io diagrams, with SQLite storage, an HTTP API, and PNG export.

## Upstream projects and attribution

This service integrates existing editors; it does not implement or claim authorship of Excalidraw or draw.io. The dashboard, storage/API, synchronization, and editor adapters connect those projects into this service.

- [Excalidraw](https://github.com/excalidraw/excalidraw): the sketch-style editor and its native serialization/PNG export functions, used through `@excalidraw/excalidraw` 0.18.1. Its bundled fonts and assets come from that package.
- [draw.io / diagrams.net](https://github.com/jgraph/drawio): the diagram editor, shapes, assets, and native export functionality, loaded from `third_party/drawio` at the pinned commit below. Upstream code is licensed under [Apache-2.0](https://github.com/jgraph/drawio/blob/f3abfe0f082c18f7b4fee8a34c2d07b1987687fd/LICENSE).

## Build and run

Requires Go 1.25+, Node 20.19+ or 22.12+ for the frontend build, and Chrome/Chromium for HTTP PNG export. Run Chromium as a non-root user with its sandbox available; the service does not disable it.

Run commands from this project directory. Build the frontend before starting the service:

```sh
(cd frontend && npm ci --ignore-scripts --no-audit --no-fund && node build.mjs)
go build -o diagrams .
```

Node is needed only for the frontend build.

After fetching the draw.io assets below, run `./diagrams` and open http://127.0.0.1:8780. The dashboard lists/searches diagrams, creates blank documents, and uploads native files. Click a card to edit. Browser changes autosave; changes from agents appear within about one second. Both editors support light/dark, and PNG export has its own independent light/dark selection. draw.io page numbers in the UI start at 1.

draw.io assets are not tracked in Git. Fetch the pinned version if `third_party/drawio` is absent:

```sh
mkdir -p third_party
git clone --no-checkout https://github.com/jgraph/drawio.git third_party/drawio
git -C third_party/drawio checkout --detach f3abfe0f082c18f7b4fee8a34c2d07b1987687fd
```

Use `./diagrams -help` for database, asset, listener, and Chromium path options. The database parent directory must exist and be writable.

**HTTPS is provided by your reverse proxy**, not built into this service. Preserve the Host header. There is no application login: restrict the service to your trusted private network/VPN or put authentication at the proxy. Do not expose it directly to the public internet. `-addr 0.0.0.0:8780` enables direct IPv4 LAN access. The renderer reaches this same listener internally without going through proxy authentication.

## Skill distribution

Copy only `skills/diagram/` into the agent's skills directory. For Pi on the current machine:

```sh
mkdir -p ~/.pi/agent/skills/diagram
rsync -a skills/diagram/ ~/.pi/agent/skills/diagram/
```

Run `/reload` in existing Pi sessions after copying it.

Set `DIAGRAM_SERVICE_URL` in the agent's environment to the actual reachable service origin (for example, your HTTPS reverse-proxy URL). No deployment hostname is hardcoded: if it is unset, the skill asks for the URL rather than assuming localhost. Syncing the skill does not deploy or start the backend.

## Agent API

Each document has a random ID independent of filename; duplicate names are allowed. Type is explicitly `excalidraw` or `drawio` and content is validated. Source remains native JSON/XML, not a custom wrapper. Downloaded embedded images remain part of the source. External network images/fonts are blocked by the service CSP: embed images in the native file for portable rendering.

```sh
SERVICE=http://127.0.0.1:8780

# Create: source is the complete native file encoded as a JSON string.
jq -n --rawfile source example.excalidraw \
  '{name:"Architecture",type:"excalidraw",source:$source}' |
  curl -f -H 'Content-Type: application/json' --data-binary @- "$SERVICE/api/diagrams"

# List / metadata (creation response also contains id and revision).
curl -f "$SERVICE/api/diagrams"
curl -f "$SERVICE/api/diagrams/$ID"

# Fetch before editing; ETag is the quoted revision, e.g. "1".
curl -f -D headers.txt "$SERVICE/api/diagrams/$ID/source" -o diagram.excalidraw

# After editing the native file, send the ETag from that GET, not a fresh metadata fetch.
curl -f -X PUT -H 'If-Match: "1"' -H 'Content-Type: application/json' \
  --data-binary @diagram.excalidraw "$SERVICE/api/diagrams/$ID/source"

# PNG: light or dark; draw.io page index starts at 0.
curl -f "$SERVICE/api/diagrams/$ID/export.png?theme=dark&page=0" -o diagram.png
```

`POST /api/diagrams` returns `{id,name,type,revision,updatedAt}`. Open `/d/{id}` in the browser. For draw.io use `type:"drawio"`, the raw XML, and `Content-Type: application/xml` on PUT. Request bodies are capped at 16 MiB.

Updates require `If-Match`. A stale update returns **409** and must be reconciled against a fresh GET, not blindly retried. Missing/malformed `If-Match` returns **428**. The browser retains its unsaved work on conflict; download it before choosing **Load server version**. Server reloads reset editor undo history. This is revision-based synchronization, not simultaneous collaborative merging. SQLite stores the latest source and revision counter, not historical versions. Back up SQLite using its backup facilities or stop the service before copying the database (WAL files matter while running).

PNG requests snapshot the source when accepted and return that revision in `X-Diagram-Revision`. They use the native export functions, not screenshots. A fresh browser/profile is used for each request; one render runs at a time, with a 45-second deadline including queue time. Cleanup allows up to 5 additional seconds for graceful browser exit before allocator cancellation kills/waits for its process, then removes the profile. Request disconnects, render errors, SIGINT and SIGTERM trigger cleanup too. Run the binary directly under your service supervisor, not `go run`, so signals reach it. For containers use an init (`--init`) to reap orphaned descendants; SIGKILL/power loss cannot run application cleanup.

Browser PNG exports run locally in the user's browser. Excalidraw exports are scaled to a maximum 4000px edge; draw.io caps output to 8192px edges/16MP. API export requires no user browser to be open. Blank/unsupported documents may fail native export; the source remains saved.

## Checks

```sh
go test ./...
go vet ./...
DIAGRAMS_BROWSER_TEST=1 CHROME='/path/to/chrome' go test -v ./...
```

The opt-in checks use temporary databases/browser profiles and real built editors. They cover native PNG exports in both themes, editor synchronization, and cleanup. No server is left running by the checks.
