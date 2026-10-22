package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/chromedp/chromedp"
)

const maxRequestBody = 16 << 20

type app struct {
	store      *Store
	renderer   *renderer
	web        string
	excalidraw string
	drawio     string
}

type createRequest struct {
	Name   string `json:"name"`
	Type   string `json:"type"`
	Source string `json:"source"`
}

func (a *app) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Security-Policy", "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob:; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data: blob:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if strings.HasPrefix(r.URL.Path, "/api/diagrams") {
		a.handleAPI(w, r)
		return
	}
	if r.URL.Path == "/" {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			methodNotAllowed(w)
			return
		}
		http.ServeFile(w, r, filepath.Join(a.web, "index.html"))
		return
	}
	if strings.HasPrefix(r.URL.Path, "/d/") {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			methodNotAllowed(w)
			return
		}
		id := strings.TrimPrefix(r.URL.Path, "/d/")
		if id == "" || strings.Contains(id, "/") {
			http.NotFound(w, r)
			return
		}
		http.ServeFile(w, r, filepath.Join(a.web, "editor.html"))
		return
	}
	if strings.HasPrefix(r.URL.Path, "/web/") {
		a.serveStatic(w, r, "/web/", a.web)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/excalidraw/") {
		a.serveStatic(w, r, "/excalidraw/", a.excalidraw)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/drawio/") {
		a.serveStatic(w, r, "/drawio/", a.drawio)
		return
	}
	http.NotFound(w, r)
}

func (a *app) serveStatic(w http.ResponseWriter, r *http.Request, prefix, directory string) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		methodNotAllowed(w)
		return
	}
	http.StripPrefix(prefix, http.FileServer(http.Dir(directory))).ServeHTTP(w, r)
}

func (a *app) handleAPI(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	const collection = "/api/diagrams"
	if r.URL.Path == collection || r.URL.Path == collection+"/" {
		switch r.Method {
		case http.MethodGet:
			diagrams, err := a.store.List(r.Context())
			if err != nil {
				writeStoreError(w, err)
				return
			}
			if diagrams == nil {
				diagrams = []DiagramMeta{}
			}
			writeJSON(w, http.StatusOK, diagrams)
		case http.MethodPost:
			if !sameOrigin(r) {
				writeError(w, http.StatusForbidden, "cross-origin mutation denied")
				return
			}
			var input createRequest
			if err := decodeJSONBody(w, r, &input); err != nil {
				writeError(w, http.StatusBadRequest, err.Error())
				return
			}
			meta, err := a.store.Create(r.Context(), input.Name, input.Type, []byte(input.Source))
			if err != nil {
				writeStoreError(w, err)
				return
			}
			writeJSON(w, http.StatusCreated, meta)
		default:
			methodNotAllowed(w)
		}
		return
	}

	rest := strings.TrimPrefix(r.URL.Path, collection+"/")
	parts := strings.Split(rest, "/")
	if len(parts) == 0 || parts[0] == "" || len(parts) > 2 {
		http.NotFound(w, r)
		return
	}
	id := parts[0]
	if len(parts) == 1 {
		if r.Method != http.MethodGet {
			methodNotAllowed(w)
			return
		}
		meta, err := a.store.Get(r.Context(), id)
		if err != nil {
			writeStoreError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, meta)
		return
	}

	switch parts[1] {
	case "source":
		a.handleSource(w, r, id)
	case "export.png":
		a.handleExport(w, r, id)
	default:
		http.NotFound(w, r)
	}
}

func (a *app) handleSource(w http.ResponseWriter, r *http.Request, id string) {
	switch r.Method {
	case http.MethodGet:
		meta, source, err := a.store.Source(r.Context(), id)
		if err != nil {
			writeStoreError(w, err)
			return
		}
		w.Header().Set("ETag", quoteRevision(meta.Revision))
		w.Header().Set("Content-Type", sourceContentType(meta.Type))
		_, _ = w.Write(source)
	case http.MethodPut:
		if !sameOrigin(r) {
			writeError(w, http.StatusForbidden, "cross-origin mutation denied")
			return
		}
		revision, ok := parseIfMatch(r.Header.Get("If-Match"))
		if !ok {
			writeError(w, http.StatusPreconditionRequired, "If-Match must be a quoted revision")
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxRequestBody)
		source, err := io.ReadAll(r.Body)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid request body")
			return
		}
		meta, err := a.store.Update(r.Context(), id, revision, source)
		if err != nil {
			writeStoreError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, meta)
	default:
		methodNotAllowed(w)
	}
}

func (a *app) handleExport(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}
	theme := r.URL.Query().Get("theme")
	if theme == "" {
		theme = "light"
	}
	if theme != "light" && theme != "dark" {
		writeError(w, http.StatusBadRequest, "theme must be light or dark")
		return
	}
	page := 0
	if raw := r.URL.Query().Get("page"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 0 {
			writeError(w, http.StatusBadRequest, "page must be a non-negative integer")
			return
		}
		page = value
	}

	// This source snapshot is deliberately passed to Chromium. The page never reads it back from the API.
	meta, source, err := a.store.Source(r.Context(), id)
	if err != nil {
		writeStoreError(w, err)
		return
	}
	w.Header().Set("X-Diagram-Revision", strconv.FormatInt(meta.Revision, 10))
	png, err := a.renderer.Render(r.Context(), id, source, meta.Type, theme, page)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			writeError(w, http.StatusGatewayTimeout, "render timed out or was cancelled")
			return
		}
		log.Printf("render %s: %v", id, err)
		writeError(w, http.StatusInternalServerError, "render failed")
		return
	}
	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(png)
}

func decodeJSONBody(w http.ResponseWriter, r *http.Request, target any) error {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBody)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return fmt.Errorf("invalid JSON body")
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return fmt.Errorf("request body must contain one JSON value")
	}
	return nil
}

func sameOrigin(r *http.Request) bool {
	origin := r.Header.Get("Origin")
	if origin == "" { // command-line agents commonly omit Origin.
		return true
	}
	parsed, err := url.Parse(origin)
	return err == nil && parsed.Scheme != "" && parsed.Host != "" && parsed.User == nil && parsed.Path == "" && parsed.RawQuery == "" && parsed.Fragment == "" && strings.EqualFold(parsed.Host, r.Host)
}

func parseIfMatch(value string) (int64, bool) {
	if len(value) < 3 || value[0] != '"' || value[len(value)-1] != '"' {
		return 0, false
	}
	revision, err := strconv.ParseInt(value[1:len(value)-1], 10, 64)
	return revision, err == nil && revision > 0
}

func quoteRevision(revision int64) string { return strconv.Quote(strconv.FormatInt(revision, 10)) }

func sourceContentType(typ string) string {
	if typ == "drawio" {
		return "application/xml; charset=utf-8"
	}
	return "application/json; charset=utf-8"
}

func methodNotAllowed(w http.ResponseWriter) {
	writeError(w, http.StatusMethodNotAllowed, "method not allowed")
}

func writeStoreError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, ErrNotFound):
		writeError(w, http.StatusNotFound, "diagram not found")
	case errors.Is(err, ErrConflict):
		writeError(w, http.StatusConflict, "diagram revision conflict")
	case errors.Is(err, ErrInvalid):
		writeError(w, http.StatusBadRequest, err.Error())
	default:
		log.Printf("store: %v", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
	}
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

type renderer struct {
	ctx     context.Context
	cancel  context.CancelFunc
	url     string
	chrome  string
	sem     chan struct{}
	workers sync.WaitGroup
	mu      sync.Mutex
	stopped bool
}

func newRenderer(parent context.Context, pageURL, chrome string) *renderer {
	ctx, cancel := context.WithCancel(parent)
	return &renderer{ctx: ctx, cancel: cancel, url: pageURL, chrome: chrome, sem: make(chan struct{}, 1)}
}

// Stop closes admission before cancelling work, so Wait cannot race a later Add.
func (r *renderer) Stop() {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.stopped {
		r.stopped = true
		r.cancel()
	}
}

func (r *renderer) Wait() {
	r.Stop()
	r.workers.Wait()
}

func (r *renderer) begin() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.stopped {
		return false
	}
	r.workers.Add(1)
	return true
}

func (r *renderer) Render(request context.Context, id string, source []byte, typ, theme string, page int) ([]byte, error) {
	if !r.begin() {
		return nil, context.Canceled
	}
	defer r.workers.Done()

	// This deadline covers time queued behind the single Chromium slot as well as browser work.
	totalCtx, cancelTotal := context.WithTimeout(request, 45*time.Second)
	stopOnShutdown := context.AfterFunc(r.ctx, cancelTotal)
	defer func() {
		stopOnShutdown()
		cancelTotal()
	}()

	select {
	case r.sem <- struct{}{}:
		defer func() { <-r.sem }()
	case <-totalCtx.Done():
		return nil, totalCtx.Err()
	}
	if err := totalCtx.Err(); err != nil {
		return nil, err
	}
	return r.render(totalCtx, id, source, typ, theme, page)
}

func (r *renderer) render(totalCtx context.Context, id string, source []byte, typ, theme string, page int) ([]byte, error) {
	profile, err := os.MkdirTemp("", "diagrams-chrome-")
	if err != nil {
		return nil, err
	}

	opts := append([]chromedp.ExecAllocatorOption{}, chromedp.DefaultExecAllocatorOptions[:]...)
	// Prevent ExecAllocator from adding --no-sandbox when this runs as root.
	opts = append(opts, chromedp.Flag("no-sandbox", false), chromedp.UserDataDir(profile))
	if r.chrome != "" {
		opts = append(opts, chromedp.ExecPath(r.chrome))
	}
	// The allocator is deliberately independent of the render deadline so Cancel can wait for Chrome to exit.
	allocatorCtx, cancelAllocator := chromedp.NewExecAllocator(context.Background(), opts...)
	browserCtx, cancelBrowser := chromedp.NewContext(allocatorCtx)
	defer func() {
		// Browser.close can hang. Give it a small graceful window, then cancel the
		// allocator; its cancel function waits for cmd.Wait before profile removal.
		gracefulCtx, cancelGraceful := context.WithTimeout(browserCtx, 5*time.Second)
		gracefulDone := make(chan struct{})
		go func() {
			_ = chromedp.Cancel(gracefulCtx)
			close(gracefulDone)
		}()
		select {
		case <-gracefulDone:
		case <-gracefulCtx.Done():
		}
		cancelGraceful()
		browserCancelled := make(chan struct{})
		go func() {
			cancelBrowser()
			close(browserCancelled)
		}()
		cancelAllocator()
		<-browserCancelled
		<-gracefulDone
		_ = os.RemoveAll(profile)
	}()

	renderCtx, cancelRender := context.WithCancel(browserCtx)
	stopOnDeadline := context.AfterFunc(totalCtx, cancelRender)
	defer func() {
		stopOnDeadline()
		cancelRender()
	}()

	sourceArg, _ := json.Marshal(string(source))
	typeArg, _ := json.Marshal(typ)
	themeArg, _ := json.Marshal(theme)
	expression := fmt.Sprintf(`(async () => await window.renderDiagram(%s, %s, %s, %d))()`, sourceArg, typeArg, themeArg, page)
	pageURL := r.url + "/d/" + url.PathEscape(id) + "?render=1&theme=" + url.QueryEscape(theme) + "&page=" + strconv.Itoa(page)
	if err := chromedp.Do(renderCtx,
		chromedp.Navigate(pageURL),
		chromedp.WaitReady(chromedp.CSS("body")),
		chromedp.Poll[chromedp.Void](`typeof window.renderDiagram === "function"`, chromedp.WithPollingTimeout(0)),
	); err != nil {
		return nil, err
	}
	dataURL, err := chromedp.Run(renderCtx, chromedp.Evaluate[string](expression, chromedp.EvalAwaitPromise))
	if err != nil {
		return nil, err
	}
	const prefix = "data:image/png;base64,"
	if !strings.HasPrefix(dataURL, prefix) {
		return nil, errors.New("renderer did not return a PNG data URL")
	}
	png, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(dataURL, prefix))
	if err != nil || len(png) == 0 {
		return nil, errors.New("renderer returned invalid PNG data")
	}
	return png, nil
}

func main() {
	executable, err := os.Executable()
	if err != nil {
		log.Fatalf("locate executable: %v", err)
	}
	base := filepath.Dir(executable)
	addr := flag.String("addr", "127.0.0.1:8780", "listen address")
	dbPath := flag.String("db", "diagrams.db", "SQLite database path")
	web := flag.String("web", filepath.Join(base, "web"), "web directory")
	excalidraw := flag.String("excalidraw", filepath.Join(base, "frontend/dist"), "Excalidraw frontend directory")
	drawio := flag.String("drawio", filepath.Join(base, "third_party/drawio/src/main/webapp"), "draw.io webapp directory")
	chrome := flag.String("chrome", "", "Chrome/Chromium executable (default: chromedp discovery)")
	flag.Parse()

	for _, asset := range []string{filepath.Join(*web, "index.html"), filepath.Join(*web, "editor.html"), filepath.Join(*excalidraw, "adapter.js"), filepath.Join(*drawio, "index.html")} {
		if info, statErr := os.Stat(asset); statErr != nil || !info.Mode().IsRegular() {
			log.Fatalf("missing editor asset %s: build/copy the service assets or set -web, -excalidraw and -drawio", asset)
		}
	}

	store, err := OpenStore(*dbPath)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer store.Close()

	listener, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Fatalf("listen: %v", err)
	}
	address := listener.Addr().(*net.TCPAddr)
	host := address.IP.String()
	if address.IP.IsUnspecified() {
		if address.IP.To4() != nil {
			host = "127.0.0.1"
		} else {
			host = "::1"
		}
	}
	pageURL := "http://" + net.JoinHostPort(host, strconv.Itoa(address.Port))
	root, cancelRoot := context.WithCancel(context.Background())
	renderer := newRenderer(root, pageURL, *chrome)
	application := &app{store: store, renderer: renderer, web: *web, excalidraw: *excalidraw, drawio: *drawio}
	server := &http.Server{Handler: application, ReadHeaderTimeout: 10 * time.Second}

	serveDone := make(chan error, 1)
	go func() { serveDone <- server.Serve(listener) }()
	log.Printf("listening on http://%s", listener.Addr())

	shutdown := func() {
		cancelRoot()
		renderer.Stop()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 50*time.Second)
		if err := server.Shutdown(shutdownCtx); err != nil {
			log.Printf("shutdown server: %v; forcing close", err)
			if closeErr := server.Close(); closeErr != nil && !errors.Is(closeErr, http.ErrServerClosed) {
				log.Printf("close server: %v", closeErr)
			}
		}
		cancel()
		renderer.Wait()
	}

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(signals)
	select {
	case sig := <-signals:
		log.Printf("received %s; shutting down", sig)
		shutdown()
	case err := <-serveDone:
		if !errors.Is(err, http.ErrServerClosed) {
			log.Printf("server: %v", err)
		}
		shutdown()
	}
}
