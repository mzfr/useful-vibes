package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/chromedp/chromedp"
)

const (
	excalidrawEmpty    = `{"type":"excalidraw","version":2,"elements":[],"appState":{},"files":{}}`
	excalidrawRemote   = `{"type":"excalidraw","version":2,"elements":[{"id":"remote","type":"rectangle","x":10,"y":10,"width":300,"height":150,"strokeColor":"#1e1e1e","backgroundColor":"#a5d8ff","fillStyle":"solid","strokeWidth":2,"strokeStyle":"solid","roughness":0,"opacity":100,"angle":0,"groupIds":[],"seed":42,"version":1,"versionNonce":1,"isDeleted":false,"boundElements":null,"updated":1,"link":null,"locked":false}],"appState":{"viewBackgroundColor":"#ffffff"},"files":{}}`
	excalidrawConflict = `{"type":"excalidraw","version":2,"elements":[{"id":"server","type":"ellipse","x":30,"y":20,"width":180,"height":120,"strokeColor":"#e03131","backgroundColor":"#ffc9c9","fillStyle":"solid","strokeWidth":2,"strokeStyle":"solid","roughness":0,"opacity":100,"angle":0,"groupIds":[],"seed":43,"version":1,"versionNonce":2,"isDeleted":false,"boundElements":null,"updated":2,"link":null,"locked":false}],"appState":{"viewBackgroundColor":"#ffffff"},"files":{}}`
	drawioInitial      = `<mxfile><diagram id="p1" name="Page-1"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/></root></mxGraphModel></diagram></mxfile>`
	drawioAutosave     = `<mxfile><diagram id="p1" name="Page-1"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="Autosaved" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;" vertex="1" parent="1"><mxGeometry x="20" y="20" width="200" height="100" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>`
)

// These checks drive the bundled editors in a real Chrome. They are opt-in
// because they require built frontend assets and a local Chrome executable.
func TestBrowserEditorSync(t *testing.T) {
	if os.Getenv("DIAGRAMS_BROWSER_TEST") != "1" {
		t.Skip("set DIAGRAMS_BROWSER_TEST=1 and CHROME=/path/to/chrome")
	}
	chrome := os.Getenv("CHROME")
	if chrome == "" {
		t.Skip("set CHROME to the Chrome/Chromium executable")
	}
	if info, err := os.Stat(chrome); err != nil || info.IsDir() {
		t.Fatalf("CHROME=%q is not an executable file: %v", chrome, err)
	}

	store, err := OpenStore(filepath.Join(t.TempDir(), "diagrams.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })

	excalidraw, err := store.Create(context.Background(), "Browser excalidraw", "excalidraw", []byte(excalidrawEmpty))
	if err != nil {
		t.Fatal(err)
	}
	drawio, err := store.Create(context.Background(), "Browser drawio", "drawio", []byte(drawioInitial))
	if err != nil {
		t.Fatal(err)
	}

	server := newBrowserTestServer(t, store)

	t.Run("dashboard_lists_and_searches", func(t *testing.T) {
		ctx := newBrowserTest(t, chrome).tab(t)
		runBrowser(t, ctx,
			chromedp.Navigate(server.URL),
			chromedp.WaitReady(chromedp.CSS("#search")),
			waitFor("document.querySelector('#list').children.length === 2"),
			waitForStatus("2 diagrams"),
			chromedp.Click(chromedp.CSS("#search")),
			chromedp.SendKeys(chromedp.CSS("#search"), "excal"),
			waitFor("document.querySelector('#list').children.length === 1"),
			waitFor("document.querySelector('#list strong')?.textContent === 'Browser excalidraw'"),
		)
	})

	t.Run("excalidraw_editor_autosaves_remote_updates_and_keeps_conflicts_local", func(t *testing.T) {
		ctx := newBrowserTest(t, chrome).tab(t)
		runBrowser(t, ctx,
			chromedp.Navigate(server.URL+"/d/"+excalidraw.ID),
			chromedp.WaitReady(chromedp.CSS("#canvas canvas")),
			waitForStatus("Saved · watching for changes"),
		)

		drawExcalidrawRectangle(t, ctx, 450, 260, 680, 400)
		waitForStatusContains(t, ctx, "Unsaved changes")
		autosaved := waitForSourceChange(t, store, excalidraw.ID, []byte(excalidrawEmpty))
		if !hasExcalidrawRectangle(autosaved) {
			t.Fatalf("Excalidraw autosave did not contain the rectangle: %s", autosaved)
		}
		waitForStatusExact(t, ctx, "Saved · watching for changes")

		beforeRemote := canvasDigest(t, ctx)
		updateSource(t, store, excalidraw.ID, []byte(excalidrawRemote))
		waitForCanvasChange(t, ctx, beforeRemote)
		waitForStatusExact(t, ctx, "Saved · watching for changes")
		if got := readSource(t, store, excalidraw.ID); !bytes.Equal(got, []byte(excalidrawRemote)) {
			t.Fatalf("clean editor overwrote remote source: %s", got)
		}

		drawExcalidrawRectangle(t, ctx, 720, 260, 900, 390)
		waitForStatusContains(t, ctx, "Unsaved changes")
		updateSource(t, store, excalidraw.ID, []byte(excalidrawConflict))
		waitForStatusContains(t, ctx, "Conflict: your edits are kept")
		if got := readSource(t, store, excalidraw.ID); !bytes.Equal(got, []byte(excalidrawConflict)) {
			t.Fatalf("conflicting local edit replaced server source: %s", got)
		}
		var beforeUnloadBlocked bool
		beforeUnloadBlocked = browserValue[bool](t, ctx, `(() => { const event = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(event); return event.defaultPrevented; })()`)
		if !beforeUnloadBlocked {
			t.Fatal("conflict discarded the pending local edit")
		}
	})

	t.Run("drawio_editor_autosaves_iframe_protocol", func(t *testing.T) {
		ctx := newBrowserTest(t, chrome).tab(t)
		runBrowser(t, ctx,
			chromedp.Navigate(server.URL+"/d/"+drawio.ID),
			chromedp.WaitReady(chromedp.CSS(`iframe[title="Draw.io editor"]`)),
			waitForStatus("Saved · watching for changes"),
		)

		// draw.io is a native app inside this real iframe and exposes no parent-page
		// editor API. Emit its documented autosave protocol from the iframe window,
		// so event.source and origin take the same path as a real native autosave.
		emitDrawioAutosave(t, ctx, server.URL, drawioAutosave)
		waitForStatusContains(t, ctx, "Unsaved changes")
		if got := waitForSourceChange(t, store, drawio.ID, []byte(drawioInitial)); !bytes.Equal(got, []byte(drawioAutosave)) {
			t.Fatalf("draw.io autosave source = %s, want %s", got, drawioAutosave)
		}
		waitForStatusExact(t, ctx, "Saved · watching for changes")
	})
}

type browserTest struct {
	ctx context.Context
}

func newBrowserTestServer(t *testing.T, store *Store) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(&app{
		store:      store,
		web:        "web",
		excalidraw: "frontend/dist",
		drawio:     "third_party/drawio/src/main/webapp",
	})
	t.Cleanup(server.Close)
	return server
}

func newBrowserTest(t *testing.T, chrome string) browserTest {
	t.Helper()
	profile, err := os.MkdirTemp("", "diagrams-browser-")
	if err != nil {
		t.Fatal(err)
	}
	opts := append([]chromedp.ExecAllocatorOption{}, chromedp.DefaultExecAllocatorOptions[:]...)
	opts = append(opts, chromedp.ExecPath(chrome), chromedp.UserDataDir(profile), chromedp.WindowSize(1280, 900), chromedp.Flag("no-sandbox", false))
	allocatorCtx, cancelAllocator := chromedp.NewExecAllocator(context.Background(), opts...)
	browserCtx, _ := chromedp.NewContext(allocatorCtx)
	t.Cleanup(func() {
		done := make(chan error, 1)
		go func() { done <- chromedp.Cancel(browserCtx) }()
		select {
		case err := <-done:
			if err != nil {
				t.Logf("close browser: %v", err)
			}
			cancelAllocator()
		case <-time.After(10 * time.Second):
			t.Log("timed out closing browser; cancelling allocator")
			cancelAllocator()
			<-done
		}
		_ = os.RemoveAll(profile)
	})
	return browserTest{ctx: browserCtx}
}

func (b browserTest) tab(t *testing.T) context.Context {
	t.Helper()
	return b.ctx
}

func runBrowser(t *testing.T, ctx context.Context, actions ...chromedp.Action[chromedp.Void]) {
	t.Helper()
	if err := chromedp.Do(ctx, actions...); err != nil {
		t.Fatal(err)
	}
}

func browserValue[T any](t *testing.T, ctx context.Context, expression string) T {
	t.Helper()
	value, err := chromedp.Run(ctx, chromedp.Evaluate[T](expression))
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func waitFor(expression string) chromedp.Action[chromedp.Void] {
	return chromedp.Poll[chromedp.Void](expression, chromedp.WithPollingInterval(50*time.Millisecond), chromedp.WithPollingTimeout(15*time.Second))
}

func waitForStatus(text string) chromedp.Action[chromedp.Void] {
	return waitFor("document.querySelector('#status')?.textContent === " + javascriptString(text))
}

func waitForStatusExact(t *testing.T, ctx context.Context, text string) {
	t.Helper()
	runBrowser(t, ctx, waitForStatus(text))
}

func waitForStatusContains(t *testing.T, ctx context.Context, text string) {
	t.Helper()
	runBrowser(t, ctx, waitFor("document.querySelector('#status')?.textContent.includes("+javascriptString(text)+")"))
}

func drawExcalidrawRectangle(t *testing.T, ctx context.Context, x1, y1, x2, y2 float64) {
	t.Helper()
	runBrowser(t, ctx,
		chromedp.MouseClickXY(x1, y1),
		chromedp.KeyEvent("r"),
		chromedp.MouseEvent(chromedp.MousePressed, x1, y1, chromedp.ButtonLeft),
		chromedp.MouseEvent(chromedp.MouseMoved, x2, y2, chromedp.ButtonLeft),
		chromedp.MouseEvent(chromedp.MouseReleased, x2, y2, chromedp.ButtonLeft),
	)
}

func emitDrawioAutosave(t *testing.T, ctx context.Context, origin, source string) {
	t.Helper()
	message, err := json.Marshal(map[string]string{"event": "autosave", "xml": source})
	if err != nil {
		t.Fatal(err)
	}
	frameScript := "parent.postMessage(" + javascriptString(string(message)) + ", " + javascriptString(origin) + ")"
	expression := `(() => { const frame = document.querySelector('iframe[title="Draw.io editor"]'); if (!frame?.contentWindow) throw Error('Draw.io frame is unavailable'); frame.contentWindow.eval(` + javascriptString(frameScript) + `); })()`
	runBrowser(t, ctx, chromedp.Evaluate[chromedp.Void](expression))
}

func updateSource(t *testing.T, store *Store, id string, source []byte) {
	t.Helper()
	meta, _, err := store.Source(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Update(context.Background(), id, meta.Revision, source); err != nil {
		t.Fatal(err)
	}
}

func readSource(t *testing.T, store *Store, id string) []byte {
	t.Helper()
	_, source, err := store.Source(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	return source
}

func waitForSourceChange(t *testing.T, store *Store, id string, previous []byte) []byte {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		got := readSource(t, store, id)
		if !bytes.Equal(got, previous) {
			return got
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("source for %s did not change from %s", id, previous)
	return nil
}

func hasExcalidrawRectangle(source []byte) bool {
	var document struct {
		Elements []struct {
			Type string `json:"type"`
		}
	}
	return json.Unmarshal(source, &document) == nil && len(document.Elements) > 0 && document.Elements[len(document.Elements)-1].Type == "rectangle"
}

func canvasDigest(t *testing.T, ctx context.Context) string {
	t.Helper()
	const expression = `(() => Array.from(document.querySelectorAll('#canvas canvas')).map(canvas => {
	const context = canvas.getContext('2d');
	if (!context) return canvas.width + 'x' + canvas.height + ':non-2d';
	const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
	let hash = 2166136261;
	for (let i = 0; i < pixels.length; i += 97) { hash = Math.imul(hash ^ pixels[i], 16777619); }
	return canvas.width + 'x' + canvas.height + ':' + (hash >>> 0);
}).join('|'))()`
	return browserValue[string](t, ctx, expression)
}

func waitForCanvasChange(t *testing.T, ctx context.Context, previous string) {
	t.Helper()
	runBrowser(t, ctx, waitFor(`(() => Array.from(document.querySelectorAll('#canvas canvas')).map(canvas => {
	const context = canvas.getContext('2d');
	if (!context) return canvas.width + 'x' + canvas.height + ':non-2d';
	const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
	let hash = 2166136261;
	for (let i = 0; i < pixels.length; i += 97) { hash = Math.imul(hash ^ pixels[i], 16777619); }
	return canvas.width + 'x' + canvas.height + ':' + (hash >>> 0);
}).join('|'))() !== `+javascriptString(previous)))
}

func javascriptString(value string) string {
	encoded, err := json.Marshal(value)
	if err != nil {
		panic(fmt.Sprintf("encode JavaScript string: %v", err))
	}
	return string(encoded)
}
