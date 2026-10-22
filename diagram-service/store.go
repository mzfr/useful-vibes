package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

var (
	ErrNotFound = errors.New("diagram not found")
	ErrConflict = errors.New("diagram revision conflict")
	ErrInvalid  = errors.New("invalid diagram")
)

type DiagramMeta struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	Type      string    `json:"type"`
	Revision  int64     `json:"revision"`
	UpdatedAt time.Time `json:"updatedAt"`
}

type Store struct {
	db *sql.DB
}

func OpenStore(path string) (*Store, error) {
	separator := "?"
	if strings.Contains(path, "?") {
		separator = "&"
	}
	// modernc applies these _pragma values to every pooled SQLite connection.
	dsn := path + separator + "_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	for _, pragma := range []string{
		"PRAGMA foreign_keys=ON",
	} {
		if _, err := db.Exec(pragma); err != nil {
			db.Close()
			return nil, err
		}
	}
	_, err = db.Exec(`CREATE TABLE IF NOT EXISTS diagrams (
		id TEXT PRIMARY KEY,
		name TEXT NOT NULL,
		type TEXT NOT NULL,
		source BLOB NOT NULL,
		revision INTEGER NOT NULL,
		updated_at TEXT NOT NULL
	)`)
	if err != nil {
		db.Close()
		return nil, err
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }

func (s *Store) List(ctx context.Context) ([]DiagramMeta, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id, name, type, revision, updated_at FROM diagrams ORDER BY updated_at DESC, id DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var diagrams []DiagramMeta
	for rows.Next() {
		meta, err := scanMeta(rows)
		if err != nil {
			return nil, err
		}
		diagrams = append(diagrams, meta)
	}
	return diagrams, rows.Err()
}

func (s *Store) Create(ctx context.Context, name, typ string, source []byte) (DiagramMeta, error) {
	if err := validateDiagram(name, typ, source); err != nil {
		return DiagramMeta{}, err
	}
	id, err := newID()
	if err != nil {
		return DiagramMeta{}, err
	}
	meta := DiagramMeta{ID: id, Name: name, Type: typ, Revision: 1, UpdatedAt: time.Now().UTC()}
	_, err = s.db.ExecContext(ctx, `INSERT INTO diagrams (id, name, type, source, revision, updated_at) VALUES (?, ?, ?, ?, ?, ?)`,
		meta.ID, meta.Name, meta.Type, source, meta.Revision, meta.UpdatedAt.Format(time.RFC3339Nano))
	return meta, err
}

func (s *Store) Get(ctx context.Context, id string) (DiagramMeta, error) {
	meta, err := scanMeta(s.db.QueryRowContext(ctx, `SELECT id, name, type, revision, updated_at FROM diagrams WHERE id = ?`, id))
	if errors.Is(err, sql.ErrNoRows) {
		return DiagramMeta{}, ErrNotFound
	}
	return meta, err
}

func (s *Store) Source(ctx context.Context, id string) (DiagramMeta, []byte, error) {
	var meta DiagramMeta
	var source []byte
	var updated string
	err := s.db.QueryRowContext(ctx, `SELECT id, name, type, revision, updated_at, source FROM diagrams WHERE id = ?`, id).
		Scan(&meta.ID, &meta.Name, &meta.Type, &meta.Revision, &updated, &source)
	if errors.Is(err, sql.ErrNoRows) {
		return DiagramMeta{}, nil, ErrNotFound
	}
	if err != nil {
		return DiagramMeta{}, nil, err
	}
	meta.UpdatedAt, err = time.Parse(time.RFC3339Nano, updated)
	if err != nil {
		return DiagramMeta{}, nil, fmt.Errorf("parse updated_at: %w", err)
	}
	return meta, source, nil
}

// Update performs the revision check and write in one SQL statement.
func (s *Store) Update(ctx context.Context, id string, expectedRevision int64, source []byte) (DiagramMeta, error) {
	if expectedRevision < 1 {
		return DiagramMeta{}, ErrConflict
	}
	current, err := s.Get(ctx, id)
	if err != nil {
		return DiagramMeta{}, err
	}
	if err := validateSource(current.Type, source); err != nil {
		return DiagramMeta{}, err
	}
	updated := time.Now().UTC()
	result, err := s.db.ExecContext(ctx, `UPDATE diagrams SET source = ?, revision = revision + 1, updated_at = ? WHERE id = ? AND revision = ?`,
		source, updated.Format(time.RFC3339Nano), id, expectedRevision)
	if err != nil {
		return DiagramMeta{}, err
	}
	n, err := result.RowsAffected()
	if err != nil {
		return DiagramMeta{}, err
	}
	if n != 1 {
		return DiagramMeta{}, ErrConflict
	}
	current.Revision = expectedRevision + 1
	current.UpdatedAt = updated
	return current, nil
}

type rowScanner interface {
	Scan(dest ...any) error
}

func scanMeta(row rowScanner) (DiagramMeta, error) {
	var meta DiagramMeta
	var updated string
	if err := row.Scan(&meta.ID, &meta.Name, &meta.Type, &meta.Revision, &updated); err != nil {
		return DiagramMeta{}, err
	}
	parsed, err := time.Parse(time.RFC3339Nano, updated)
	if err != nil {
		return DiagramMeta{}, fmt.Errorf("parse updated_at: %w", err)
	}
	meta.UpdatedAt = parsed
	return meta, nil
}

func newID() (string, error) {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(value[:]), nil
}

func validateDiagram(name, typ string, source []byte) error {
	if strings.TrimSpace(name) == "" || len(name) > 512 {
		return fmt.Errorf("%w: name is required and must be at most 512 bytes", ErrInvalid)
	}
	return validateSource(typ, source)
}

func validateSource(typ string, source []byte) error {
	switch typ {
	case "excalidraw":
		var document map[string]json.RawMessage
		if err := json.Unmarshal(source, &document); err != nil {
			return fmt.Errorf("%w: excalidraw source must be JSON: %v", ErrInvalid, err)
		}
		var kind string
		if raw, ok := document["type"]; !ok || json.Unmarshal(raw, &kind) != nil || kind != "excalidraw" {
			return fmt.Errorf("%w: excalidraw source requires type %q", ErrInvalid, "excalidraw")
		}
		if err := requireJSONShape(document, "elements", '['); err != nil {
			return err
		}
		if err := requireJSONShape(document, "appState", '{'); err != nil {
			return err
		}
		return requireJSONShape(document, "files", '{')
	case "drawio":
		decoder := xml.NewDecoder(strings.NewReader(string(source)))
		var root string
		depth := 0
		for {
			token, err := decoder.Token()
			if err != nil {
				if errors.Is(err, io.EOF) {
					break
				}
				return fmt.Errorf("%w: drawio source must be XML: %v", ErrInvalid, err)
			}
			switch value := token.(type) {
			case xml.StartElement:
				if depth == 0 {
					if root != "" {
						return fmt.Errorf("%w: drawio source must contain exactly one root", ErrInvalid)
					}
					root = value.Name.Local
					if root != "mxfile" && root != "mxGraphModel" {
						return fmt.Errorf("%w: drawio root must be mxfile or mxGraphModel", ErrInvalid)
					}
				}
				depth++
			case xml.EndElement:
				depth--
			case xml.CharData:
				if depth == 0 && strings.TrimSpace(string(value)) != "" {
					return fmt.Errorf("%w: drawio source has content outside its root", ErrInvalid)
				}
			}
		}
		if root == "" {
			return fmt.Errorf("%w: drawio source is empty", ErrInvalid)
		}
		return nil
	default:
		return fmt.Errorf("%w: unsupported type %q", ErrInvalid, typ)
	}
}

func requireJSONShape(document map[string]json.RawMessage, field string, leading byte) error {
	raw, ok := document[field]
	raw = []byte(strings.TrimSpace(string(raw)))
	if !ok || len(raw) == 0 || raw[0] != leading {
		return fmt.Errorf("%w: excalidraw source requires %s", ErrInvalid, field)
	}
	return nil
}
