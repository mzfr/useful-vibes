use crate::error::{Result, TlError};
use regex::Regex;
use std::sync::LazyLock;

static TASK_RE: LazyLock<Regex> = LazyLock::new(|| {
    // Captures: indent, done marker, tag, number, optional priority (!), title
    Regex::new(r"^(\s*)- \[([ x])\] ([a-z][a-z0-9]*)-(\d+)(!)? (.+)$").unwrap()
});

static SECTION_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^### (.+)$").unwrap()
});

static NOTE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(\s+)- (.+)$").unwrap()
});

static LINK_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"([a-z][a-z0-9]*-\d+)").unwrap()
});

#[derive(Debug, Clone)]
pub struct Task {
    pub line_number: usize,
    pub indent: String,
    pub done: bool,
    pub tag: String,
    pub number: u64,
    pub title: String,
    pub notes: Vec<Note>,
    pub date: String,
    pub priority: bool,
}

impl Task {
    pub fn id(&self) -> String {
        format!("{}-{}", self.tag, self.number)
    }
}

#[derive(Debug, Clone)]
pub struct Note {
    pub line_number: usize,
    pub text: String,
}

#[derive(Debug, Clone)]
pub struct Section {
    pub tasks: Vec<Task>,
}

pub fn parse_task_line(line: &str) -> Option<(String, bool, String, u64, bool, String)> {
    let caps = TASK_RE.captures(line)?;
    let indent = caps[1].to_string();
    let done = &caps[2] == "x";
    let tag = caps[3].to_string();
    let number: u64 = caps[4].parse().ok()?;
    let priority = caps.get(5).is_some();
    let title = caps[6].to_string();
    Some((indent, done, tag, number, priority, title))
}

pub fn is_section_header(line: &str) -> Option<String> {
    SECTION_RE.captures(line).map(|caps| caps[1].trim().to_string())
}

pub fn is_note_line(line: &str) -> Option<(String, String)> {
    let caps = NOTE_RE.captures(line)?;
    Some((caps[1].to_string(), caps[2].to_string()))
}

/// Extract task ID references (e.g. "bb-5", "osv-12") from text.
pub fn extract_links(text: &str) -> Vec<String> {
    LINK_RE
        .captures_iter(text)
        .map(|c| c[1].to_string())
        .collect()
}

/// Parse the last `scan_window` lines of the log file.
/// Returns all sections found with their tasks and notes.
pub fn parse_log(content: &str, scan_window: usize) -> Vec<Section> {
    let all_lines: Vec<&str> = content.lines().collect();
    let total = all_lines.len();
    let start = if total > scan_window {
        total - scan_window
    } else {
        0
    };
    let lines = &all_lines[start..];
    let offset = start;

    let mut sections: Vec<Section> = Vec::new();
    let mut current_task: Option<Task> = None;
    let mut current_date = String::new();

    for (i, line) in lines.iter().enumerate() {
        let abs_line = offset + i;

        if let Some(date) = is_section_header(line) {
            // Flush current task
            if let Some(task) = current_task.take() {
                if let Some(sec) = sections.last_mut() {
                    sec.tasks.push(task);
                }
            }
            current_date = date;
            sections.push(Section {
                tasks: Vec::new(),
            });
            continue;
        }

        if let Some((indent, done, tag, number, priority, title)) = parse_task_line(line) {
            // Flush previous task
            if let Some(task) = current_task.take() {
                if let Some(sec) = sections.last_mut() {
                    sec.tasks.push(task);
                }
            }
            current_task = Some(Task {
                line_number: abs_line,
                indent,
                done,
                tag,
                number,
                title,
                notes: Vec::new(),
                date: current_date.clone(),
                priority,
            });
            continue;
        }

        if let Some((indent, text)) = is_note_line(line) {
            if let Some(ref mut task) = current_task {
                // Only count as note if indented deeper than the task
                if indent.len() > task.indent.len() {
                    task.notes.push(Note {
                        line_number: abs_line,
                        text,
                    });
                    continue;
                }
            }
            // Not a note belonging to a task — could be freeform indented bullet.
            // Flush current task since indentation broke.
            if let Some(task) = current_task.take() {
                if let Some(sec) = sections.last_mut() {
                    sec.tasks.push(task);
                }
            }
            continue;
        }

        // Any other line: if it's not blank and not indented more, flush current task
        if !line.trim().is_empty() {
            if let Some(task) = current_task.take() {
                if let Some(sec) = sections.last_mut() {
                    sec.tasks.push(task);
                }
            }
        }
    }

    // Flush last task
    if let Some(task) = current_task.take() {
        if let Some(sec) = sections.last_mut() {
            sec.tasks.push(task);
        }
    }

    sections
}

/// Find a task by ID within parsed sections.
/// Returns error if not found or if duplicate.
pub fn find_task<'a>(sections: &'a [Section], id: &str) -> Result<&'a Task> {
    let mut found: Vec<&Task> = Vec::new();
    for sec in sections {
        for task in &sec.tasks {
            if task.id() == id {
                found.push(task);
            }
        }
    }
    match found.len() {
        0 => Err(TlError::TaskNotFound(id.to_string())),
        1 => Ok(found[0]),
        _ => Err(TlError::DuplicateId(id.to_string())),
    }
}

/// Get today's date string in DD/MM/YYYY format.
pub fn today_str() -> String {
    chrono::Local::now().format("%d/%m/%Y").to_string()
}

/// Find the first section header line number and date.
pub fn find_first_section(content: &str) -> Option<(usize, String)> {
    let lines: Vec<&str> = content.lines().collect();
    for (i, line) in lines.iter().enumerate() {
        if let Some(date) = is_section_header(line) {
            return Some((i, date));
        }
    }
    None
}

/// Find the last section header line number and date.
pub fn find_last_section(content: &str) -> Option<(usize, String)> {
    let lines: Vec<&str> = content.lines().collect();
    for (i, line) in lines.iter().enumerate().rev() {
        if let Some(date) = is_section_header(line) {
            return Some((i, date));
        }
    }
    None
}

/// Find the end of today's section (line number of next section or EOF).
pub fn find_section_end(content: &str, section_line: usize) -> usize {
    let lines: Vec<&str> = content.lines().collect();
    for i in (section_line + 1)..lines.len() {
        if is_section_header(lines[i]).is_some() {
            return i;
        }
    }
    lines.len()
}

/// Get the raw text of today's section.
pub fn get_today_section_text(content: &str) -> Option<String> {
    let today = today_str();
    let lines: Vec<&str> = content.lines().collect();

    // Find today's section header
    let mut section_start = None;
    for (i, line) in lines.iter().enumerate() {
        if let Some(date) = is_section_header(line) {
            if date == today {
                section_start = Some(i);
            }
        }
    }

    let start = section_start?;
    let end = find_section_end(content, start);
    Some(lines[start..end].join("\n"))
}

/// Search tasks and notes for matching text.
pub fn search_tasks(sections: &[Section], query: &str) -> Vec<Task> {
    let query_lower = query.to_lowercase();
    let mut results = Vec::new();

    for sec in sections {
        for task in &sec.tasks {
            let title_match = task.title.to_lowercase().contains(&query_lower);
            let note_match = task
                .notes
                .iter()
                .any(|n| n.text.to_lowercase().contains(&query_lower));
            let tag_match = task.tag.to_lowercase().contains(&query_lower);
            let id_match = task.id().to_lowercase().contains(&query_lower);

            if title_match || note_match || tag_match || id_match {
                results.push(task.clone());
            }
        }
    }

    results
}
