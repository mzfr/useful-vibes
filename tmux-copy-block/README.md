# tmux-copy-block

Pick a complete Markdown fenced code block from the visible tmux pane and copy it, either cleaned up or raw.

## Setup

Requires Bash, awk, and a tmux version supporting `display-popup -e` (popup environment variables). Clipboard integration depends on your terminal's OSC 52 support and tmux clipboard settings.

Add this to `~/.tmux.conf`, replacing the script path with your checkout location:

```tmux
set -g set-clipboard external
bind-key y run-shell 'bash "/absolute/path/to/useful-vibes/tmux-copy-block/copy-block.sh" "#{pane_id}" "#{client_name}"'
```

Reload the configuration from inside tmux:

```sh
tmux source-file ~/.tmux.conf
```

Press your tmux prefix followed by `y` to open the picker. This replaces any existing prefix-`y` binding. The script is used directly; no plugin manager is required.

## Usage

- `j` / `k` or arrow keys: select a block.
- `Enter`: copy after heuristic cleanup of prompts, borders, and wrapped commands.
- `r`: copy without the cleanup transformation.
- `Esc` / `q`: cancel.

Only complete backtick or tilde fenced blocks on the visible screen are selected, not the entire scrollback. The preview shows the extracted block before cleanup. Extraction removes common indentation and trailing whitespace, even in raw mode.

Cleanup can change text; use `r` when you need to avoid its transformations. The script copies text and does not execute it. Review commands before pasting them into a shell.

The copied block is stored in a tmux buffer; copying to the system clipboard also requires terminal support. Temporary capture files are removed when the picker exits normally.
