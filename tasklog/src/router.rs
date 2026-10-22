use crate::config::{Config, FileEntry, FileMode};
use crate::error::{Result, TlError};
use crate::parser;
use std::path::PathBuf;

/// Result of resolving which file a tag should go to.
#[derive(Debug)]
pub enum RouteResult {
    /// Exactly one file matched.
    Resolved(PathBuf),
    /// Multiple variable files are eligible; caller must pick.
    Ambiguous(Vec<FileEntry>),
}

/// Resolve which file a tag belongs to for writing.
///
/// Rules:
/// 1. If any fixed file claims this tag, return that file.
/// 2. Otherwise, collect all variable files.
///    - If exactly one, return it.
///    - If zero, fall back to config.log_path.
///    - If multiple, return Ambiguous.
pub fn resolve_file_for_tag(config: &Config, tag: &str) -> Result<RouteResult> {
    let files = config.effective_files();

    // Check fixed files first
    let mut fixed_matches: Vec<&FileEntry> = Vec::new();
    for f in &files {
        if f.mode == FileMode::Fixed && f.tags.contains(&tag.to_string()) {
            fixed_matches.push(f);
        }
    }

    if fixed_matches.len() > 1 {
        return Err(TlError::Config(format!(
            "tag '{}' is claimed by multiple fixed files: {}",
            tag,
            fixed_matches
                .iter()
                .map(|f| f.label.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        )));
    }

    if let Some(f) = fixed_matches.first() {
        return Ok(RouteResult::Resolved(f.resolved_path()));
    }

    // Not fixed: check that no fixed file would be violated (tag not allowed in fixed files)
    // Collect variable files
    let variable: Vec<FileEntry> = files
        .into_iter()
        .filter(|f| f.mode == FileMode::Variable)
        .collect();

    match variable.len() {
        0 => Ok(RouteResult::Resolved(config.resolved_log_path())),
        1 => Ok(RouteResult::Resolved(variable[0].resolved_path())),
        _ => Ok(RouteResult::Ambiguous(variable)),
    }
}

/// Find which file contains a given task ID by scanning all files.
/// Used for operations on existing tasks (done, undo, note, edit, delete).
pub fn find_file_for_task(config: &Config, task_id: &str) -> Result<PathBuf> {
    let paths = config.all_file_paths();

    for path in &paths {
        if !path.exists() {
            continue;
        }
        let content = std::fs::read_to_string(path)?;
        let sections = parser::parse_log(&content, config.scan_window_lines);
        if parser::find_task(&sections, task_id).is_ok() {
            return Ok(path.clone());
        }
    }

    Err(TlError::TaskNotFound(task_id.to_string()))
}

/// Get eligible files for adding a new tag. Returns the variable files plus
/// any fixed file that claims this tag. Used by the TUI to build the file
/// picker list.
#[allow(dead_code)]
pub fn eligible_files_for_tag(config: &Config, tag: &str) -> Vec<FileEntry> {
    let files = config.effective_files();
    let mut eligible = Vec::new();

    for f in &files {
        match f.mode {
            FileMode::Fixed => {
                if f.tags.contains(&tag.to_string()) {
                    eligible.push(f.clone());
                }
            }
            FileMode::Variable => {
                eligible.push(f.clone());
            }
        }
    }

    eligible
}
