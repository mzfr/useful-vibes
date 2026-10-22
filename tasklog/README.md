# tl

A minimal global markdown task log with a CLI, interactive TUI, and MCP server.

Tracks structured tasks across Markdown files while preserving surrounding notes and prose.

## How it works

`tl` treats your log file as the source of truth. It recognizes structured tasks that follow a strict pattern:

```
- [ ] tag-1 some task title
	- a note on this task
- [x] tag-2 a completed task
- [ ] tag-3! a high priority task
```

Everything else in the file (freeform bullets, prose, headers, links) is left untouched. Sections are separated by date headers:

```
### 12/02/2026
- [ ] dev-1! review the scan results
	- check the false positives
	- escalate critical findings

### 11/02/2026
- [x] infra-3 rotate production keys
```

Tags act as project identifiers. Each tag gets its own auto-incrementing counter, so `dev-1`, `dev-2`, `infra-1`, etc. are all independent.

__Priority__ is marked with a `!` after the task ID (e.g. `dev-1!`). Everything is low priority by default and can be toggled at any time.

__Task links__ are detected automatically. If a note contains something like `continuing bb-5 with a modification`, the reference to `bb-5` is recognized and can be followed in the TUI.

## Installation

Requires Rust 1.88+.

```bash
cargo install --path .
```

This installs the `tl` binary to `~/.cargo/bin/`.

## Getting started

```bash
# Initialize with the default log location (~/.config/tasklog/log.md)
tl init

# Or point to your existing log file
tl init --log ~/notes/log.md
```

This creates:

- `~/.config/tasklog/config.toml` -- configuration
- `~/.config/tasklog/state.json` -- tag counters for ID allocation
- Your log file (created if it does not exist, left alone if it does)

## CLI

```bash
# Add a task
tl add dev implement the login flow
# => created dev-1

# Add a high priority task
tl add -p dev fix the auth bypass
# => created dev-2

# Mark a task as done
tl done dev-1
# => completed dev-1

# Reopen a completed task (moves it to today's section)
tl undo dev-1
# => reopened dev-1

# Edit a task's title
tl edit dev-1 implement the login flow v2
# => edited dev-1

# Delete a task and all its notes
tl delete dev-1
# => deleted dev-1

# Toggle priority
tl priority dev-2
# => dev-2 marked as high priority

# Add a note to a task
tl note infra-1 blocked on access request
# => noted on infra-1

# Rename a tag across the entire log
tl rename infra infrastructure
# => renamed infra -> infrastructure

# Search across tasks and notes
tl search rotate
# => [x] infra-1 rotate production credentials

# Show today's raw section
tl today

# Manage multiple log files (see Multi-file support)
tl file add --path ~/wishlist.md --label wishlist --mode fixed --tags wish --insert top
tl file list
tl file remove wishlist
```

## TUI

```bash
tl tui
```

The TUI has three panels: __Projects__ (left) lists all tags with open/total counts, __Open__ (center) shows open tasks for the selected project, and __Completed__ (right) shows done tasks.

Priority tasks sort to the top and render in red. Labels truncate with `…` when the terminal is too narrow.

### Keybindings

| Key | Action |
|---|---|
| `j` / `k` | Navigate up/down |
| `h` / `l` | Switch panels left/right |
| `Tab` / `Shift+Tab` | Cycle panel focus |
| `Enter` | Open task detail popup |
| `a` | Add task (auto-selects tag if on task panel) |
| `e` | Edit selected task title |
| `x` | Delete selected task (or note in detail popup) |
| `d` | Mark selected task as done |
| `u` | Undo a completed task (from Completed panel) |
| `n` | Add a note to selected task |
| `p` | Toggle priority |
| `R` | Rename tag (from Projects panel) |
| `/` | Search |
| `c` | Clear search filter |
| `.` | Toggle hiding projects with no open tasks |
| `g` / `G` | Jump to top/bottom |
| `r` | Refresh from disk |
| `b` | Go back after following a task link |
| `?` | Show help |
| `q` / `Esc` | Close popup or quit |

### Detail popup

Press `Enter` on any task to open the detail popup. Inside it:

- `j` / `k` selects individual notes
- `x` deletes the selected note (or the task itself if no note is selected)
- `e` edits the task title
- `p` toggles priority
- Task ID references in notes (like `bb-5`) are highlighted. Press `n` to cycle through detected links and `f` to follow/jump to the linked task. `b` goes back.

## MCP server

```bash
tl mcp
```

The server communicates over `stdio` and exposes these tools:

| Tool | Description |
|---|---|
| `init_log` | Initialize the task log environment |
| `create_task` | Create a new task with a tag and title |
| `complete_task` | Mark a task as completed by ID |
| `add_note` | Add a note to an existing task |
| `search_tasks` | Search tasks and notes, optionally filtered by tag |
| `get_today_section` | Get the raw text of today's section |

Most MCP-compatible tools accept a server definition like:

```json
{
  "tasklog": {
    "type": "local",
    "command": ["tl", "mcp"]
  }
}
```

The only requirement is that `tl` is on your `PATH`. You can test interactively with the MCP inspector:

```bash
npx @modelcontextprotocol/inspector tl mcp
```

## Multi-file support

By default `tl` operates on a single log file. You can register additional files with different modes and behaviors using `tl file`:

```bash
# Add a wishlist file that only accepts the "wish" tag, with new tasks at the top
tl file add \
  --path ~/notes/wishlist.md \
  --label wishlist \
  --mode fixed \
  --tags wish \
  --insert top

# Add a second general-purpose log
tl file add \
  --path ~/notes/work.md \
  --label work \
  --mode variable

# List configured files
tl file list
# => [main] ~/.config/tasklog/log.md (variable)
# => [wishlist] ~/notes/wishlist.md (fixed(wish), insert=top)
# => [work] ~/notes/work.md (variable)

# Remove a file
tl file remove work
```

When you add the first file, `tl` automatically migrates your existing `log_path` as the `main` variable file so nothing breaks.

### File modes

__Variable__ files accept any tag. This is the default. If you have multiple variable files and add a task with a new tag, the TUI will show a file picker so you can choose where it goes.

__Fixed__ files only accept specific tags. A fixed file with `tags = ["wish"]` will never receive tasks for any other tag, and the `wish` tag will always route to that file. This prevents things from getting mixed up.

### Insert position

Each file has an insert position that controls where new date sections appear:

- `bottom` (default) -- new `### date` sections are appended at the end. This is the normal chronological log behavior.
- `top` -- new sections are prepended at the top of the file. Useful when you have an existing file with freeform content that you want to keep below, like a wishlist with notes and links that should stay at the bottom while new tracked tasks appear at the top.

### How routing works

When you add a task, the router decides which file it goes to:

1. If any fixed file claims the tag, the task goes there.
2. Otherwise, all variable files are eligible. If there's only one, it's automatic. If there are multiple, the TUI shows a picker and the CLI defaults to the first one.

For operations on existing tasks (done, undo, note, edit, delete), the router scans all files to find the task by ID. Tag rename also operates across all files.

Search, today, and the TUI aggregate tasks from every registered file. In multi-file mode, the TUI panels show the file label in their title (e.g. `Open — wish — wishlist`).

### Config format

The `[[files]]` array in `config.toml` drives multi-file. If absent, `log_path` is used as a single variable file:

```toml
log_path = "~/.config/tasklog/log.md"
date_format = "DD/MM/YYYY"
note_indent = 6
scan_window_lines = 5000

[[files]]
path = "~/.config/tasklog/log.md"
label = "main"
mode = "variable"

[[files]]
path = "~/notes/wishlist.md"
label = "wishlist"
mode = "fixed"
tags = ["wish"]
insert = "top"
```

## Configuration

Config lives at `~/.config/tasklog/config.toml`:

```toml
log_path = "~/.config/tasklog/log.md"
date_format = "DD/MM/YYYY"
note_indent = 6
scan_window_lines = 5000
```

| Field | Description | Default |
|---|---|---|
| `log_path` | Path to your log file (supports `~`) | `~/.config/tasklog/log.md` |
| `date_format` | Date format for section headers | `DD/MM/YYYY` |
| `note_indent` | Number of spaces to indent notes | `6` |
| `scan_window_lines` | Only parse the last N lines of the log for performance | `5000` |
| `files` | Multi-file configuration (see [Multi-file support](#multi-file-support)) | not set |
| `hide_empty_projects` | TUI starts with projects that have no open tasks hidden (toggle with `.`) | `false` |

The key thing about `log_path` is that you can point it at an existing markdown file you already use. `tl` will add structured tasks alongside your freeform content without disturbing it. When you start using multi-file, `log_path` still serves as the fallback if no `[[files]]` are configured.

