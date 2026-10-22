use crate::config::{atomic_write, Config, InsertPosition};
use crate::error::{Result, TlError};
use crate::lock::FileLock;
use crate::parser::{self, find_last_section, find_first_section, find_section_end, today_str};
use crate::router::{self, RouteResult};
use crate::state::State;
use std::path::Path;

/// Ensure today's section exists in the log. Returns the full content after modification.
/// When `insert_pos` is `Top`, the new section is prepended at line 0.
/// When `Bottom` (default), it's appended at the end.
fn ensure_today_section(content: &str, insert_pos: &InsertPosition) -> String {
    let today = today_str();

    // Check if today's section already exists anywhere in the file
    let lines: Vec<&str> = content.lines().collect();
    for line in &lines {
        if let Some(date) = parser::is_section_header(line) {
            if date == today {
                return content.to_string();
            }
        }
    }

    match insert_pos {
        InsertPosition::Bottom => {
            let mut result = content.to_string();
            if !result.is_empty() && !result.ends_with('\n') {
                result.push('\n');
            }
            result.push('\n');
            result.push_str(&format!("### {}\n", today));
            result
        }
        InsertPosition::Top => {
            let section = format!("### {}\n\n", today);
            format!("{}{}", section, content)
        }
    }
}

/// Ensure a single log file exists and has today's section.
fn init_log_file(path: &Path, insert_pos: &InsertPosition) -> Result<()> {
    if !path.exists() {
        if let Some(parent) = path.parent() {
            if !parent.exists() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let today = today_str();
        let content = format!("### {}\n", today);
        atomic_write(path, content.as_bytes())?;
    } else {
        let content = std::fs::read_to_string(path)?;
        if content.trim().is_empty() {
            let today = today_str();
            let content = format!("### {}\n", today);
            atomic_write(path, content.as_bytes())?;
        } else {
            let updated = ensure_today_section(&content, insert_pos);
            if updated != content {
                atomic_write(path, updated.as_bytes())?;
            }
        }
    }
    Ok(())
}

/// Initialize the tool: create config dir, files, and today's section.
/// If `log_path` is provided, store it in config. Otherwise use default.
pub fn init(log_path: Option<&str>) -> Result<()> {
    Config::ensure_dir()?;

    let config_path = Config::config_path();
    if !config_path.exists() {
        let config = match log_path {
            Some(p) => Config::with_log_path(p),
            None => Config::default(),
        };
        config.save()?;
    } else if let Some(p) = log_path {
        // Config exists but user is updating the log path
        let mut config = Config::load()?;
        config.log_path = p.to_string();
        config.save()?;
    }

    let state_path = Config::state_path();
    if !state_path.exists() {
        State::default().save()?;
    }

    let config = Config::load()?;

    // Initialize all registered files
    let files = config.effective_files();
    for entry in &files {
        init_log_file(&entry.resolved_path(), &entry.insert)?;
    }

    Ok(())
}

/// Add a new task with the given tag and title.
/// Returns the assigned task ID string.
pub fn add_task(tag: &str, title: &str) -> Result<String> {
    add_task_with_priority(tag, title, false)
}

/// Add a new task with the given tag, title, and priority.
/// Routes to the correct file automatically. For ambiguous routes (multiple
/// variable files), defaults to the first variable file. Use
/// `add_task_to_file` for explicit file targeting (TUI picker).
pub fn add_task_with_priority(tag: &str, title: &str, priority: bool) -> Result<String> {
    let config = Config::load()?;
    let log_path = match router::resolve_file_for_tag(&config, tag)? {
        RouteResult::Resolved(p) => p,
        RouteResult::Ambiguous(files) => files[0].resolved_path(),
    };
    add_task_to_file(tag, title, priority, &log_path)
}

/// Look up the InsertPosition for a given file path from config.
fn insert_position_for_path(config: &Config, path: &Path) -> InsertPosition {
    for entry in config.effective_files() {
        if entry.resolved_path() == path {
            return entry.insert.clone();
        }
    }
    InsertPosition::default()
}

/// Add a new task to a specific file. Called by the TUI after the user picks
/// a file from the picker, or by the CLI auto-route.
pub fn add_task_to_file(tag: &str, title: &str, priority: bool, log_path: &Path) -> Result<String> {
    if !tag.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit()) || tag.is_empty() {
        return Err(TlError::Parse(
            "tag must be lowercase alphanumeric".to_string(),
        ));
    }

    let _lock = FileLock::acquire()?;
    let config = Config::load()?;
    let mut state = State::load()?;
    let insert_pos = insert_position_for_path(&config, log_path);

    let content = std::fs::read_to_string(log_path)?;
    let content = ensure_today_section(&content, &insert_pos);

    // Scan ALL files for the max ID of this tag (IDs are globally unique)
    let mut max_in_all: u64 = 0;
    for path in config.all_file_paths() {
        if !path.exists() {
            continue;
        }
        let c = std::fs::read_to_string(&path)?;
        let secs = parser::parse_log(&c, config.scan_window_lines);
        let max = secs
            .iter()
            .flat_map(|s| &s.tasks)
            .filter(|t| t.tag == tag)
            .map(|t| t.number)
            .max()
            .unwrap_or(0);
        if max > max_in_all {
            max_in_all = max;
        }
    }

    // Ensure state counter is at least as high as what's across all files
    state.sync_min(tag, max_in_all);
    let number = state.next_id(tag);
    let id = format!("{}-{}", tag, number);

    // For "top" files, use the first section; for "bottom", use the last.
    let (section_line, _) = match insert_pos {
        InsertPosition::Top => find_first_section(&content),
        InsertPosition::Bottom => find_last_section(&content),
    }
    .ok_or_else(|| TlError::Other("no section found in log".to_string()))?;
    let section_end = find_section_end(&content, section_line);

    let priority_marker = if priority { "!" } else { "" };
    let task_line = format!("- [ ] {}{} {}", id, priority_marker, title);

    let mut lines: Vec<&str> = content.lines().collect();

    if section_end >= lines.len() {
        lines.push(&task_line);
    } else {
        lines.insert(section_end, &task_line);
    }

    let mut new_content = lines.join("\n");
    if !new_content.ends_with('\n') {
        new_content.push('\n');
    }

    atomic_write(log_path, new_content.as_bytes())?;
    state.save()?;

    Ok(id)
}

/// Mark a task as done by its ID.
pub fn complete_task(id: &str) -> Result<()> {
    let _lock = FileLock::acquire()?;
    let config = Config::load()?;

    let log_path = router::find_file_for_task(&config, id)?;
    let content = std::fs::read_to_string(&log_path)?;

    let sections = parser::parse_log(&content, config.scan_window_lines);
    let task = parser::find_task(&sections, id)?;

    if task.done {
        return Err(TlError::Other(format!("task {} is already done", id)));
    }

    let stamp = chrono::Local::now().format("%d/%m/%Y %I:%M%p").to_string();

    let mut lines: Vec<String> = content.lines().map(|l| l.to_string()).collect();
    let line = &mut lines[task.line_number];
    *line = format!("{} ({})", line.replacen("[ ]", "[x]", 1), stamp);

    let mut new_content = lines.join("\n");
    if !new_content.ends_with('\n') {
        new_content.push('\n');
    }

    atomic_write(&log_path, new_content.as_bytes())?;
    Ok(())
}

/// Undo a completed task: move it (with notes) to today's section as open.
pub fn undo_task(id: &str) -> Result<()> {
    let _lock = FileLock::acquire()?;
    let config = Config::load()?;

    let log_path = router::find_file_for_task(&config, id)?;
    let insert_pos = insert_position_for_path(&config, &log_path);
    let content = std::fs::read_to_string(&log_path)?;
    let content = ensure_today_section(&content, &insert_pos);

    let sections = parser::parse_log(&content, config.scan_window_lines);
    let task = parser::find_task(&sections, id)?;

    if !task.done {
        return Err(TlError::Other(format!("task {} is not done", id)));
    }

    // Collect line numbers to remove (task line + all note lines)
    let mut lines_to_remove: Vec<usize> = Vec::new();
    lines_to_remove.push(task.line_number);
    for note in &task.notes {
        lines_to_remove.push(note.line_number);
    }
    lines_to_remove.sort();

    // Build the reopened task line (strip completion timestamp, flip to [ ])
    let priority_marker = if task.priority { "!" } else { "" };
    let task_line = format!("- [ ] {}{} {}", task.id(), priority_marker, task.title);

    // Build note lines to carry over, plus a reopened note
    let stamp = chrono::Local::now().format("%d/%m/%Y %I:%M%p").to_string();
    let mut new_note_lines: Vec<String> = Vec::new();
    new_note_lines.push(format!("\t- [{}] reopened (was completed on {})", stamp, task.date));
    for note in &task.notes {
        new_note_lines.push(format!("\t- {}", note.text));
    }

    // Remove old lines (reverse order to keep indices valid)
    let mut lines: Vec<String> = content.lines().map(|l| l.to_string()).collect();
    for &ln in lines_to_remove.iter().rev() {
        if ln < lines.len() {
            lines.remove(ln);
        }
    }

    // Re-join to find today's section in the modified content
    let modified = lines.join("\n");
    let find_section = match insert_pos {
        InsertPosition::Top => find_first_section(&modified),
        InsertPosition::Bottom => find_last_section(&modified),
    };
    let (section_line, _) = find_section
        .ok_or_else(|| TlError::Other("no section found in log".to_string()))?;
    let section_end = find_section_end(&modified, section_line);

    // Re-split for insertion
    let mut lines: Vec<String> = modified.lines().map(|l| l.to_string()).collect();

    // Insert task + notes at end of today's section
    let insert_at = if section_end >= lines.len() {
        lines.len()
    } else {
        section_end
    };

    // Insert in reverse so indices stay correct
    let mut to_insert = vec![task_line];
    to_insert.extend(new_note_lines);

    for (i, line) in to_insert.into_iter().enumerate() {
        if insert_at + i >= lines.len() {
            lines.push(line);
        } else {
            lines.insert(insert_at + i, line);
        }
    }

    let mut new_content = lines.join("\n");
    if !new_content.ends_with('\n') {
        new_content.push('\n');
    }

    atomic_write(&log_path, new_content.as_bytes())?;
    Ok(())
}

/// Add a note under a task by its ID.
pub fn add_note(id: &str, text: &str) -> Result<()> {
    let _lock = FileLock::acquire()?;
    let config = Config::load()?;

    let log_path = router::find_file_for_task(&config, id)?;
    let content = std::fs::read_to_string(&log_path)?;

    let sections = parser::parse_log(&content, config.scan_window_lines);
    let task = parser::find_task(&sections, id)?;

    let insert_after = if task.notes.is_empty() {
        task.line_number
    } else {
        task.notes.last().unwrap().line_number
    };

    let indent = "\t";
    let stamp = chrono::Local::now().format("%d/%m/%Y %I:%M%p").to_string();
    let note_line = format!("{}- [{}] {}", indent, stamp, text);

    let mut lines: Vec<String> = content.lines().map(|l| l.to_string()).collect();
    lines.insert(insert_after + 1, note_line);

    let mut new_content = lines.join("\n");
    if !new_content.ends_with('\n') {
        new_content.push('\n');
    }

    atomic_write(&log_path, new_content.as_bytes())?;
    Ok(())
}

/// Delete a note from a task by task ID and note index (0-based).
pub fn delete_note(id: &str, note_index: usize) -> Result<()> {
    let _lock = FileLock::acquire()?;
    let config = Config::load()?;

    let log_path = router::find_file_for_task(&config, id)?;
    let content = std::fs::read_to_string(&log_path)?;

    let sections = parser::parse_log(&content, config.scan_window_lines);
    let task = parser::find_task(&sections, id)?;

    if note_index >= task.notes.len() {
        return Err(TlError::Other(format!(
            "note index {} out of range (task has {} notes)",
            note_index,
            task.notes.len()
        )));
    }

    let line_to_remove = task.notes[note_index].line_number;

    let mut lines: Vec<String> = content.lines().map(|l| l.to_string()).collect();
    if line_to_remove < lines.len() {
        lines.remove(line_to_remove);
    }

    let mut new_content = lines.join("\n");
    if !new_content.ends_with('\n') {
        new_content.push('\n');
    }

    atomic_write(&log_path, new_content.as_bytes())?;
    Ok(())
}

/// Edit a task's title by its ID.
pub fn edit_task(id: &str, new_title: &str) -> Result<()> {
    if new_title.is_empty() {
        return Err(TlError::Other("title cannot be empty".to_string()));
    }

    let _lock = FileLock::acquire()?;
    let config = Config::load()?;

    let log_path = router::find_file_for_task(&config, id)?;
    let content = std::fs::read_to_string(&log_path)?;

    let sections = parser::parse_log(&content, config.scan_window_lines);
    let task = parser::find_task(&sections, id)?;

    let status = if task.done { "x" } else { " " };
    let priority_marker = if task.priority { "!" } else { "" };

    // For done tasks, preserve the completion timestamp at the end
    let mut lines: Vec<String> = content.lines().map(|l| l.to_string()).collect();
    let old_line = &lines[task.line_number];

    // Check if there's a trailing " (timestamp)" on done tasks
    let trailing = if task.done {
        // The timestamp is appended after the title as " (DD/MM/YYYY HH:MMAM/PM)"
        if let Some(paren_pos) = old_line.rfind(" (") {
            &old_line[paren_pos..]
        } else {
            ""
        }
    } else {
        ""
    };

    let new_line = format!(
        "{}- [{}] {}{} {}{}",
        task.indent, status, task.id(), priority_marker, new_title, trailing
    );

    lines[task.line_number] = new_line;

    let mut new_content = lines.join("\n");
    if !new_content.ends_with('\n') {
        new_content.push('\n');
    }

    atomic_write(&log_path, new_content.as_bytes())?;
    Ok(())
}

/// Delete a task and all its notes by its ID.
pub fn delete_task(id: &str) -> Result<()> {
    let _lock = FileLock::acquire()?;
    let config = Config::load()?;

    let log_path = router::find_file_for_task(&config, id)?;
    let content = std::fs::read_to_string(&log_path)?;

    let sections = parser::parse_log(&content, config.scan_window_lines);
    let task = parser::find_task(&sections, id)?;

    // Collect all line numbers to remove: task line + all note lines
    let mut lines_to_remove: Vec<usize> = Vec::new();
    lines_to_remove.push(task.line_number);
    for note in &task.notes {
        lines_to_remove.push(note.line_number);
    }
    lines_to_remove.sort();

    let mut lines: Vec<String> = content.lines().map(|l| l.to_string()).collect();
    // Remove in reverse order to keep indices valid
    for &ln in lines_to_remove.iter().rev() {
        if ln < lines.len() {
            lines.remove(ln);
        }
    }

    let mut new_content = lines.join("\n");
    if !new_content.ends_with('\n') {
        new_content.push('\n');
    }

    atomic_write(&log_path, new_content.as_bytes())?;
    Ok(())
}

/// Rename a tag across ALL log files and update state.
pub fn rename_tag(old_tag: &str, new_tag: &str) -> Result<()> {
    if !new_tag
        .chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        || new_tag.is_empty()
    {
        return Err(TlError::Parse(
            "tag must be lowercase alphanumeric".to_string(),
        ));
    }

    let _lock = FileLock::acquire()?;
    let config = Config::load()?;
    let mut state = State::load()?;

    let task_re = regex::Regex::new(&format!(
        r"^(\s*- \[[ x]\] ){}-(\d+)",
        regex::escape(old_tag)
    ))
    .map_err(|e| TlError::Other(e.to_string()))?;

    let mut found_any = false;

    for log_path in config.all_file_paths() {
        if !log_path.exists() {
            continue;
        }
        let content = std::fs::read_to_string(&log_path)?;
        let sections = parser::parse_log(&content, config.scan_window_lines);
        let has_old = sections
            .iter()
            .flat_map(|s| &s.tasks)
            .any(|t| t.tag == old_tag);

        if !has_old {
            continue;
        }
        found_any = true;

        let mut lines: Vec<String> = content.lines().map(|l| l.to_string()).collect();
        for line in &mut lines {
            if let Some(caps) = task_re.captures(&line.clone()) {
                let prefix = &caps[1];
                let number = &caps[2];
                let rest = &line[caps[0].len()..];
                *line = format!("{}{}-{}{}", prefix, new_tag, number, rest);
            }
        }

        let mut new_content = lines.join("\n");
        if !new_content.ends_with('\n') {
            new_content.push('\n');
        }

        atomic_write(&log_path, new_content.as_bytes())?;
    }

    if !found_any {
        return Err(TlError::Other(format!("tag '{}' not found in any log file", old_tag)));
    }

    // Update state: move counter from old tag to new tag
    let old_counter = state.tags.remove(old_tag).unwrap_or(0);
    let new_counter = state.tags.entry(new_tag.to_string()).or_insert(0);
    if old_counter > *new_counter {
        *new_counter = old_counter;
    }
    state.save()?;

    Ok(())
}

/// Toggle priority on a task by its ID.
pub fn toggle_priority(id: &str) -> Result<bool> {
    let _lock = FileLock::acquire()?;
    let config = Config::load()?;

    let log_path = router::find_file_for_task(&config, id)?;
    let content = std::fs::read_to_string(&log_path)?;

    let sections = parser::parse_log(&content, config.scan_window_lines);
    let task = parser::find_task(&sections, id)?;

    let mut lines: Vec<String> = content.lines().map(|l| l.to_string()).collect();
    let line = &lines[task.line_number];

    let new_priority = !task.priority;

    // Rebuild the task line with or without priority marker
    let status = if task.done { "x" } else { " " };
    let priority_marker = if new_priority { "!" } else { "" };

    // Preserve any trailing content like completion timestamps
    // The original line after the title might have " (timestamp)" appended
    let original_title_and_rest = if task.done {
        // For done tasks, the line might be: "- [x] tag-N! title (timestamp)"
        // We need to preserve the timestamp part
        let task_id_with_priority = if task.priority {
            format!("{}!", task.id())
        } else {
            task.id()
        };
        let after_id = line
            .find(&task_id_with_priority)
            .map(|pos| &line[pos + task_id_with_priority.len()..])
            .unwrap_or("");
        // after_id starts with " title (timestamp)" or " title"
        after_id.trim_start().to_string()
    } else {
        task.title.clone()
    };

    let new_line = format!(
        "{}- [{}] {}{} {}",
        task.indent, status, task.id(), priority_marker, original_title_and_rest
    );

    lines[task.line_number] = new_line;

    let mut new_content = lines.join("\n");
    if !new_content.ends_with('\n') {
        new_content.push('\n');
    }

    atomic_write(&log_path, new_content.as_bytes())?;
    Ok(new_priority)
}

/// Get today's section text from all files.
pub fn get_today() -> Result<String> {
    let config = Config::load()?;
    let files = config.effective_files();
    let paths = config.all_file_paths();
    let mut parts: Vec<String> = Vec::new();
    let multi = paths.len() > 1;

    for (i, path) in paths.iter().enumerate() {
        if !path.exists() {
            continue;
        }
        let content = std::fs::read_to_string(path)?;
        if let Some(section) = parser::get_today_section_text(&content) {
            if multi {
                let label = files.get(i).map(|f| f.label.as_str()).unwrap_or("?");
                parts.push(format!("[{}]\n{}", label, section));
            } else {
                parts.push(section);
            }
        }
    }

    if parts.is_empty() {
        return Err(TlError::Other("no section for today found".to_string()));
    }
    Ok(parts.join("\n\n"))
}

/// Search tasks within the scan window across all files.
pub fn search(query: &str) -> Result<Vec<parser::Task>> {
    let config = Config::load()?;
    let mut results = Vec::new();

    for path in config.all_file_paths() {
        if !path.exists() {
            continue;
        }
        let content = std::fs::read_to_string(&path)?;
        let sections = parser::parse_log(&content, config.scan_window_lines);
        results.extend(parser::search_tasks(&sections, query));
    }

    Ok(results)
}

/// Parse all tasks from all files. Used by the TUI.
pub fn all_tasks() -> Result<Vec<parser::Task>> {
    let config = Config::load()?;
    let mut tasks = Vec::new();

    for path in config.all_file_paths() {
        if !path.exists() {
            continue;
        }
        let content = std::fs::read_to_string(&path)?;
        let sections = parser::parse_log(&content, config.scan_window_lines);
        for sec in sections {
            tasks.extend(sec.tasks);
        }
    }

    Ok(tasks)
}
