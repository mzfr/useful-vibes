---
name: diagram
description: Create, edit, share, and export Excalidraw canvases and draw.io diagrams through the shared diagram service. Use for diagrams, whiteboards, architecture drawings, flowcharts, native .excalidraw or .drawio files, and light/dark PNG exports.
---

# Diagram service

Use the centrally hosted HTTP service. Agents need only HTTP access, `curl`, and `jq`; editors, storage, and PNG rendering run on the service host. Do not start a local server or install an editor/Chromium for ordinary diagram work.

## Service address

Use the user's supplied service URL, or `DIAGRAM_SERVICE_URL` from the environment. If neither is available, ask for the address. Never guess a hostname or silently use localhost: the agent may be running on another machine.

```bash
SERVICE="${DIAGRAM_SERVICE_URL%/}"
```

Set this only after confirming the variable is populated, or assign the user-provided URL explicitly. Use existing proxy authentication if required; do not put credentials in shared files or URLs.

The dashboard is `$SERVICE/`. It lists/searches stored diagrams; clicking one opens its native editor. **Local files do not appear there until POSTed/uploaded.**

## Native documents, not a new format

- `excalidraw`: native JSON with `type: "excalidraw"`, `version: 2`, `elements`, `appState`, and `files`.
- `drawio`: native XML with an `mxfile` or `mxGraphModel` root. Preserve all pages; `mxfile` can contain compressed pages.
- The API's `type` selects the editor; it is not inferred from the filename alone.
- Preserve stable element/cell IDs, unrelated content, page IDs, background colors, and embedded image data. The service blocks external image/font requests; embed assets for portable rendering.
- Keep working files under `diagrams/` in the current project unless the user specifies another location. Author native files using `write` for creation and `read`/`edit` for changes. Do not put documents in the skill directory.

## Create and publish

Author the native file, then upload it. The JSON envelope is transport only; the stored/downloaded source stays native.

```bash
TYPE=excalidraw
FILE=diagrams/architecture.excalidraw
NAME='Architecture'

jq -n --arg name "$NAME" --arg type "$TYPE" --rawfile source "$FILE" \
  '{name:$name,type:$type,source:$source}' |
  curl --fail-with-body --silent --show-error --connect-timeout 10 --max-time 30 \
    -H 'Content-Type: application/json' --data-binary @- "$SERVICE/api/diagrams"
```

For draw.io set `TYPE=drawio` and use a `.drawio` file. A successful POST returns **201** and `{id,name,type,revision,updatedAt}`. Record the returned ID and give the user **`$SERVICE/d/$ID`**. Do not claim it is listed/published until the request succeeds. Duplicate names are allowed; each POST creates a separate document. To edit the existing one, use PUT instead.

## Find, fetch, and update

```bash
curl --fail-with-body --silent --show-error "$SERVICE/api/diagrams"
curl --fail-with-body --silent --show-error "$SERVICE/api/diagrams/$ID"
```

Use the list to identify an existing document, not its filename alone. Before **every edit**, GET the latest raw source and its ETag in the same response. Browser edits may have changed it since the previous operation.

```bash
curl --fail-with-body --silent --show-error \
  -D diagrams/source.headers \
  -o diagrams/current.excalidraw \
  "$SERVICE/api/diagrams/$ID/source"
```

Read the fetched source and headers. Capture the quoted `ETag` value (e.g. `"4"`). Edit that fetched file, then submit with **that same ETag**:

```bash
ETAG='"4"' # Replace with the ETag from the source GET, not this example.
curl --fail-with-body --silent --show-error \
  -X PUT -H "If-Match: $ETAG" -H 'Content-Type: application/json' \
  --data-binary @diagrams/current.excalidraw \
  "$SERVICE/api/diagrams/$ID/source"
```

For draw.io use `.drawio` and `Content-Type: application/xml`. Successful PUT returns updated metadata. Request bodies are capped at 16 MiB.

- **409:** another writer saved first. Keep your intended edits, fetch the latest source, and reconcile. Never take a newer ETag and resend stale content unchanged just to force the write.
- **428:** missing/malformed `If-Match`; use the quoted ETag from the source GET.
- **400:** malformed/unsupported source or request; read the error and correct it.
- On an uncertain POST outcome, check the listing before retrying and creating a duplicate.

Browser edits autosave; clean editors reload server changes in about one second. Simultaneous edits to the same diagram can conflict. The browser retains unsaved work and offers source download before **Load server version** discards it. Do not discard the user's changes automatically. Reloading resets editor undo history. Revisions are conflict checks, not recoverable version history.

## Export PNG and download source

```bash
curl --fail-with-body --silent --show-error --connect-timeout 10 --max-time 60 \
  -D diagrams/export.headers \
  "$SERVICE/api/diagrams/$ID/export.png?theme=dark&page=0" \
  -o diagrams/architecture-dark.png
```

- `theme=light` or `theme=dark`; default is light. Use the requested theme. Theme is an export option: do not recolor or rewrite the source just to get a dark PNG.
- `page` is **zero-based** for draw.io; the browser's page input is one-based. Excalidraw uses page 0 only.
- Export runs without a user browser open. The response's `X-Diagram-Revision` identifies the source snapshot rendered. An HTTP export reflects saved service content, not unsaved browser/local edits.
- Exports are serialized with a 45-second service deadline including queue time; **504** means timeout/cancellation. Avoid launching many simultaneous exports. Report other render errors instead of presenting the error body as an image.
- `GET /api/diagrams/$ID/source` downloads the native file for sharing/import elsewhere. It contains no service-specific wrapper. Choose a `.excalidraw` or `.drawio` output filename matching the recorded type.

The editor supports light/dark separately from the PNG export theme. Give links and file paths; browser automation is unnecessary unless a visual/browser check is requested.

## Draw visually

Choose a useful structure: sequence, timeline, branching decision, spatial map, or layers. Use position, grouping, scale, color, and concise labels to explain relationships and changes. Do not turn prose paragraphs into giant boxes. Keep technical details accurate and put long explanations in the chat.
