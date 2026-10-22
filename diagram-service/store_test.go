package main

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
)

func newTestStore(t *testing.T) *Store {
	t.Helper()
	store, err := OpenStore(filepath.Join(t.TempDir(), "diagrams.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

func TestNativeSourceRoundTrip(t *testing.T) {
	store := newTestStore(t)
	ctx := context.Background()
	source := []byte("{\n  \"type\": \"excalidraw\", \"elements\": [], \"appState\": {}, \"files\": {}\n}\n")
	created, err := store.Create(ctx, "Sketch", "excalidraw", source)
	if err != nil {
		t.Fatal(err)
	}
	got, stored, err := store.Source(ctx, created.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Revision != 1 || string(stored) != string(source) {
		t.Fatalf("got revision=%d source=%q", got.Revision, stored)
	}
	if err := validateSource("drawio", []byte(`<mxfile><diagram>compressed-page-data</diagram></mxfile>`)); err != nil {
		t.Fatalf("compressed mxfile was rejected: %v", err)
	}
}

func TestUpdateConflictDoesNotReplaceSource(t *testing.T) {
	store := newTestStore(t)
	ctx := context.Background()
	first := []byte(`{"type":"excalidraw","elements":[],"appState":{},"files":{}}`)
	created, err := store.Create(ctx, "Sketch", "excalidraw", first)
	if err != nil {
		t.Fatal(err)
	}
	second := []byte(`{"type":"excalidraw","elements":[{"id":"a"}],"appState":{},"files":{}}`)
	updated, err := store.Update(ctx, created.ID, created.Revision, second)
	if err != nil {
		t.Fatal(err)
	}
	if updated.Revision != 2 {
		t.Fatalf("revision = %d, want 2", updated.Revision)
	}
	third := []byte(`{"type":"excalidraw","elements":[{"id":"b"}],"appState":{},"files":{}}`)
	if _, err := store.Update(ctx, created.ID, created.Revision, third); !errors.Is(err, ErrConflict) {
		t.Fatalf("stale update error = %v, want ErrConflict", err)
	}
	_, stored, err := store.Source(ctx, created.ID)
	if err != nil {
		t.Fatal(err)
	}
	if string(stored) != string(second) {
		t.Fatalf("stale update replaced source: %q", stored)
	}
}
