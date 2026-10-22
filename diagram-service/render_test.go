package main

import (
	"bytes"
	"context"
	"image/png"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestRenderCancellationCleanup(t *testing.T) {
	if os.Getenv("DIAGRAMS_BROWSER_TEST") != "1" {
		t.Skip("requires Chrome")
	}
	temp := t.TempDir()
	t.Setenv("TMPDIR", temp)
	for _, mode := range []string{"disconnect", "shutdown", "error"} {
		t.Run(mode, func(t *testing.T) {
			started := make(chan struct{}, 1)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/started" {
					select {
					case started <- struct{}{}:
					default:
					}
					return
				}
				w.Header().Set("Content-Type", "text/html")
				end := "return new Promise(()=>{})"
				if mode == "error" {
					end = "throw Error('intentional render failure')"
				}
				_, _ = w.Write([]byte(`<body><script>window.renderDiagram=async()=>{await fetch('/started');` + end + `}</script>`))
			}))
			defer server.Close()
			renderer := newRenderer(context.Background(), server.URL, os.Getenv("CHROME"))
			defer renderer.Wait()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan error, 1)
			go func() { _, err := renderer.Render(ctx, "test", []byte("{}"), "excalidraw", "light", 0); done <- err }()
			select {
			case <-started:
			case <-time.After(20 * time.Second):
				t.Fatal("browser did not start")
			}
			if mode == "disconnect" {
				cancel()
			}
			if mode == "shutdown" {
				renderer.Stop()
			}
			select {
			case err := <-done:
				if err == nil {
					t.Fatal("expected render failure")
				}
			case <-time.After(15 * time.Second):
				t.Fatal("browser cleanup hung")
			}
			renderer.Wait()
			profiles, err := filepath.Glob(filepath.Join(temp, "diagrams-chrome-*"))
			if err != nil || len(profiles) > 0 {
				t.Fatalf("profiles left after cleanup: %v, %v", profiles, err)
			}
		})
	}
}

// Opt-in because these checks launch a real, sandboxed Chrome and require built assets.
func TestNativePNGExports(t *testing.T) {
	if os.Getenv("DIAGRAMS_BROWSER_TEST") != "1" {
		t.Skip("set DIAGRAMS_BROWSER_TEST=1 after building frontend")
	}
	s, err := OpenStore(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	a := &app{store: s, web: "web", excalidraw: "frontend/dist", drawio: "third_party/drawio/src/main/webapp"}
	server := httptest.NewServer(a)
	defer server.Close()
	r := newRenderer(context.Background(), server.URL, os.Getenv("CHROME"))
	a.renderer = r
	defer func() { r.Stop(); r.Wait() }()
	exc := `{"type":"excalidraw","version":2,"elements":[{"id":"box","type":"rectangle","x":10,"y":10,"width":200,"height":100,"strokeColor":"#1e1e1e","backgroundColor":"#a5d8ff","fillStyle":"solid","strokeWidth":2,"strokeStyle":"solid","roughness":0,"opacity":100,"angle":0,"groupIds":[],"seed":42,"version":1,"versionNonce":1,"isDeleted":false,"boundElements":null,"updated":1,"link":null,"locked":false}],"appState":{"viewBackgroundColor":"#ffffff"},"files":{}}`
	draw := `<mxfile><diagram id="p1" name="Page-1"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="Hello" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;" vertex="1" parent="1"><mxGeometry x="20" y="20" width="200" height="100" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>`
	for typ, source := range map[string]string{"excalidraw": exc, "drawio": draw} {
		t.Run(typ, func(t *testing.T) {
			meta, err := s.Create(context.Background(), typ, typ, []byte(source))
			if err != nil {
				t.Fatal(err)
			}
			var previous []byte
			for _, theme := range []string{"light", "dark"} {
				data, err := r.Render(context.Background(), meta.ID, []byte(source), typ, theme, 0)
				if err != nil {
					t.Fatal(theme, err)
				}
				image, err := png.Decode(bytes.NewReader(data))
				if err != nil {
					t.Fatal(err)
				}
				if image.Bounds().Dx() < 100 || image.Bounds().Dy() < 50 {
					t.Fatalf("unexpected size %v", image.Bounds())
				}
				if bytes.Equal(data, previous) {
					t.Fatal("light/dark PNGs identical")
				}
				previous = data
				t.Logf("%s %s: %v, %d bytes", typ, theme, image.Bounds(), len(data))
			}
		})
	}
}
