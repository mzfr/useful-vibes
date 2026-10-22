use crate::config::{Config, FileEntry};
use crate::error::{Result, TlError};
use crate::parser::{self, Task};
use crate::router;
use crate::writer;

use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use crossterm::ExecutableCommand;
use ratatui::prelude::*;
use ratatui::widgets::*;

use std::collections::BTreeSet;
use std::io::stdout;
use std::time::Duration;

#[derive(Debug, Clone, Copy, PartialEq)]
enum Focus {
    Projects,
    Tasks,
    Completed,
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum Mode {
    Normal,
    AddTag,
    AddTitle,
    PickFile,
    NoteInput,
    Search,
    RenameTag,
    EditTitle,
    ConfirmDeleteNote,
    ConfirmDeleteTask,
}

/// Navigation stack entry for task link jumping
#[derive(Debug, Clone)]
struct NavEntry {
    project_idx: usize,
    task_idx: usize,
    focus: Focus,
}

struct App {
    all_tasks: Vec<Task>,
    projects: Vec<String>,
    project_idx: usize,
    task_idx: usize,
    completed_idx: usize,
    focus: Focus,
    mode: Mode,
    input: String,
    add_tag: String,
    status_msg: String,
    search_query: String,
    show_detail: bool,
    detail_scroll: u16,
    detail_note_idx: Option<usize>,
    detail_links: Vec<String>,
    detail_link_idx: usize,
    should_quit: bool,
    hide_empty_projects: bool,
    nav_stack: Vec<NavEntry>,
    /// Multi-file: config file entries
    file_entries: Vec<FileEntry>,
    /// Multi-file: which file to target for add (set after routing/picking)
    pending_file: Option<std::path::PathBuf>,
    /// Multi-file: eligible files for the PickFile mode
    pick_file_options: Vec<FileEntry>,
    /// Multi-file: current selection in the PickFile picker
    pick_file_idx: usize,
    /// Multi-file: map tag -> file label (built during refresh)
    tag_file_labels: std::collections::HashMap<String, String>,
}

impl App {
    fn new() -> Result<Self> {
        let config = Config::load()?;
        let file_entries = config.effective_files();
        let mut app = App {
            all_tasks: Vec::new(),
            projects: Vec::new(),
            project_idx: 0,
            task_idx: 0,
            completed_idx: 0,
            focus: Focus::Projects,
            mode: Mode::Normal,
            input: String::new(),
            add_tag: String::new(),
            status_msg: String::from("? for help | Tab to switch panels"),
            search_query: String::new(),
            show_detail: false,
            detail_scroll: 0,
            detail_note_idx: None,
            detail_links: Vec::new(),
            detail_link_idx: 0,
            should_quit: false,
            hide_empty_projects: config.hide_empty_projects,
            nav_stack: Vec::new(),
            file_entries,
            pending_file: None,
            pick_file_options: Vec::new(),
            pick_file_idx: 0,
            tag_file_labels: std::collections::HashMap::new(),
        };
        app.refresh()?;
        Ok(app)
    }

    fn refresh(&mut self) -> Result<()> {
        let config = Config::load()?;
        self.file_entries = config.effective_files();

        if self.search_query.is_empty() {
            self.all_tasks = writer::all_tasks()?;
        } else {
            self.all_tasks = writer::search(&self.search_query)?;
        }

        // Build tag -> file label mapping
        self.tag_file_labels.clear();
        let multi_file = self.file_entries.len() > 1;
        if multi_file {
            for entry in &self.file_entries {
                let path = entry.resolved_path();
                if !path.exists() {
                    continue;
                }
                if let Ok(content) = std::fs::read_to_string(&path) {
                    let sections = parser::parse_log(&content, config.scan_window_lines);
                    for sec in &sections {
                        for task in &sec.tasks {
                            self.tag_file_labels
                                .entry(task.tag.clone())
                                .or_insert_with(|| entry.label.clone());
                        }
                    }
                }
            }
        }

        // Collect unique tags sorted
        let tags: BTreeSet<String> = self.all_tasks.iter().map(|t| t.tag.clone()).collect();
        self.projects = tags.into_iter().collect();

        if self.project_idx >= self.projects.len() && !self.projects.is_empty() {
            self.project_idx = self.projects.len() - 1;
        }

        self.clamp_task_idx();
        Ok(())
    }

    fn visible_projects(&self) -> Vec<&String> {
        if self.hide_empty_projects {
            self.projects
                .iter()
                .filter(|tag| self.all_tasks.iter().any(|t| t.tag == **tag && !t.done))
                .collect()
        } else {
            self.projects.iter().collect()
        }
    }

    fn filtered_tasks(&self) -> Vec<&Task> {
        let visible = self.visible_projects();
        if visible.is_empty() {
            return Vec::new();
        }
        let idx = self.project_idx.min(visible.len().saturating_sub(1));
        let tag = visible[idx];
        self.all_tasks.iter().filter(|t| t.tag == *tag).collect()
    }

    fn open_tasks(&self) -> Vec<&Task> {
        let mut tasks: Vec<&Task> = self.filtered_tasks().into_iter().filter(|t| !t.done).collect();
        // Sort: priority tasks first
        tasks.sort_by(|a, b| b.priority.cmp(&a.priority));
        tasks
    }

    fn completed_tasks(&self) -> Vec<&Task> {
        self.filtered_tasks().into_iter().filter(|t| t.done).collect()
    }

    fn clamp_task_idx(&mut self) {
        let proj_count = self.visible_projects().len();
        if proj_count == 0 {
            self.project_idx = 0;
        } else if self.project_idx >= proj_count {
            self.project_idx = proj_count - 1;
        }

        let open_count = self.open_tasks().len();
        if open_count == 0 {
            self.task_idx = 0;
        } else if self.task_idx >= open_count {
            self.task_idx = open_count - 1;
        }

        let done_count = self.completed_tasks().len();
        if done_count == 0 {
            self.completed_idx = 0;
        } else if self.completed_idx >= done_count {
            self.completed_idx = done_count - 1;
        }
    }

    fn selected_task(&self) -> Option<&Task> {
        match self.focus {
            Focus::Projects => None,
            Focus::Completed => self.completed_tasks().get(self.completed_idx).copied(),
            Focus::Tasks => self.open_tasks().get(self.task_idx).copied(),
        }
    }

    fn current_project_tag(&self) -> Option<String> {
        let visible = self.visible_projects();
        if visible.is_empty() {
            return None;
        }
        let idx = self.project_idx.min(visible.len().saturating_sub(1));
        Some(visible[idx].clone())
    }

    /// Extract all task-ID links from the currently selected task's notes and title.
    fn extract_task_links(&self, task: &Task) -> Vec<String> {
        let mut links = Vec::new();
        let task_id = task.id();

        // From title
        for link in parser::extract_links(&task.title) {
            if link != task_id && !links.contains(&link) {
                links.push(link);
            }
        }

        // From notes
        for note in &task.notes {
            for link in parser::extract_links(&note.text) {
                if link != task_id && !links.contains(&link) {
                    links.push(link);
                }
            }
        }

        links
    }

    /// Jump to a task by its ID. Pushes current position onto nav stack.
    fn jump_to_task(&mut self, target_id: &str) -> Result<bool> {
        // Find which project/tag the target belongs to
        let target_task = self.all_tasks.iter().find(|t| t.id() == target_id);
        let target_task = match target_task {
            Some(t) => t.clone(),
            None => {
                self.status_msg = format!("Task {} not found", target_id);
                return Ok(false);
            }
        };

        let visible = self.visible_projects();
        let target_project_idx = visible.iter().position(|p| **p == target_task.tag);
        let target_project_idx = match target_project_idx {
            Some(idx) => idx,
            None => {
                self.status_msg = format!("Project {} not visible", target_task.tag);
                return Ok(false);
            }
        };

        // Push current position
        self.nav_stack.push(NavEntry {
            project_idx: self.project_idx,
            task_idx: self.task_idx,
            focus: self.focus,
        });

        // Navigate
        self.project_idx = target_project_idx;
        self.task_idx = 0;
        self.completed_idx = 0;

        // Find the task index in open or completed
        if target_task.done {
            self.focus = Focus::Completed;
            let completed = self.completed_tasks();
            if let Some(idx) = completed.iter().position(|t| t.id() == target_id) {
                self.completed_idx = idx;
            }
        } else {
            self.focus = Focus::Tasks;
            let open = self.open_tasks();
            if let Some(idx) = open.iter().position(|t| t.id() == target_id) {
                self.task_idx = idx;
            }
        }

        // Open detail on the jumped-to task
        self.show_detail = true;
        self.detail_scroll = 0;
        self.detail_note_idx = None;
        if let Some(task) = self.selected_task() {
            self.detail_links = self.extract_task_links(task);
            self.detail_link_idx = 0;
        }
        self.status_msg = format!("Jumped to {}", target_id);
        Ok(true)
    }

    /// Go back in nav stack.
    fn nav_back(&mut self) {
        if let Some(entry) = self.nav_stack.pop() {
            self.project_idx = entry.project_idx;
            self.task_idx = entry.task_idx;
            self.focus = entry.focus;
            self.show_detail = false;
            self.detail_note_idx = None;
            self.status_msg = "Jumped back".to_string();
        }
    }

    fn handle_key(&mut self, key: KeyEvent) -> Result<()> {
        match self.mode {
            Mode::Normal => self.handle_normal_key(key),
            Mode::AddTag => self.handle_add_tag_key(key),
            Mode::AddTitle => self.handle_add_title_key(key),
            Mode::PickFile => self.handle_pick_file_key(key),
            Mode::NoteInput => self.handle_note_input_key(key),
            Mode::Search => self.handle_search_key(key),
            Mode::RenameTag => self.handle_rename_tag_key(key),
            Mode::EditTitle => self.handle_edit_title_key(key),
            Mode::ConfirmDeleteNote => self.handle_confirm_delete_note_key(key),
            Mode::ConfirmDeleteTask => self.handle_confirm_delete_task_key(key),
        }
    }

    fn handle_normal_key(&mut self, key: KeyEvent) -> Result<()> {
        // Ctrl+C always quits
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.should_quit = true;
            return Ok(());
        }

        // When detail popup is open
        if self.show_detail {
            return self.handle_detail_key(key);
        }

        match key.code {
            KeyCode::Char('q') | KeyCode::Esc => self.should_quit = true,
            KeyCode::Enter => {
                if let Some(task) = self.selected_task() {
                    self.detail_links = self.extract_task_links(task);
                    self.detail_link_idx = 0;
                    self.show_detail = true;
                    self.detail_scroll = 0;
                    self.detail_note_idx = None;
                }
            }
            KeyCode::Tab => {
                self.focus = match self.focus {
                    Focus::Projects => Focus::Tasks,
                    Focus::Tasks => Focus::Completed,
                    Focus::Completed => Focus::Projects,
                };
            }
            KeyCode::BackTab => {
                self.focus = match self.focus {
                    Focus::Projects => Focus::Completed,
                    Focus::Tasks => Focus::Projects,
                    Focus::Completed => Focus::Tasks,
                };
            }
            KeyCode::Char('h') | KeyCode::Left => match self.focus {
                Focus::Tasks => self.focus = Focus::Projects,
                Focus::Completed => self.focus = Focus::Tasks,
                Focus::Projects => {}
            },
            KeyCode::Char('l') | KeyCode::Right => match self.focus {
                Focus::Projects => self.focus = Focus::Tasks,
                Focus::Tasks => self.focus = Focus::Completed,
                Focus::Completed => {}
            },
            KeyCode::Char('j') | KeyCode::Down => match self.focus {
                Focus::Projects => {
                    let count = self.visible_projects().len();
                    if count > 0 {
                        self.project_idx = (self.project_idx + 1).min(count - 1);
                        self.task_idx = 0;
                        self.completed_idx = 0;
                    }
                }
                Focus::Tasks => {
                    let count = self.open_tasks().len();
                    if count > 0 {
                        self.task_idx = (self.task_idx + 1).min(count - 1);
                    }
                }
                Focus::Completed => {
                    let count = self.completed_tasks().len();
                    if count > 0 {
                        self.completed_idx = (self.completed_idx + 1).min(count - 1);
                    }
                }
            },
            KeyCode::Char('k') | KeyCode::Up => match self.focus {
                Focus::Projects => {
                    if self.project_idx > 0 {
                        self.project_idx -= 1;
                        self.task_idx = 0;
                        self.completed_idx = 0;
                    }
                }
                Focus::Tasks => {
                    if self.task_idx > 0 {
                        self.task_idx -= 1;
                    }
                }
                Focus::Completed => {
                    if self.completed_idx > 0 {
                        self.completed_idx -= 1;
                    }
                }
            },
            KeyCode::Char('g') => match self.focus {
                Focus::Projects => {
                    self.project_idx = 0;
                    self.task_idx = 0;
                    self.completed_idx = 0;
                }
                Focus::Tasks => self.task_idx = 0,
                Focus::Completed => self.completed_idx = 0,
            },
            KeyCode::Char('G') => match self.focus {
                Focus::Projects => {
                    let count = self.visible_projects().len();
                    if count > 0 {
                        self.project_idx = count - 1;
                        self.task_idx = 0;
                        self.completed_idx = 0;
                    }
                }
                Focus::Tasks => {
                    let count = self.open_tasks().len();
                    if count > 0 {
                        self.task_idx = count - 1;
                    }
                }
                Focus::Completed => {
                    let count = self.completed_tasks().len();
                    if count > 0 {
                        self.completed_idx = count - 1;
                    }
                }
            },
            KeyCode::Char('a') => {
                // Auto-select tag if focused on tasks/completed panel
                if self.focus == Focus::Tasks || self.focus == Focus::Completed {
                    if let Some(tag) = self.current_project_tag() {
                        self.add_tag = tag.clone();
                        self.input.clear();
                        self.resolve_file_for_add()?;
                    } else {
                        self.mode = Mode::AddTag;
                        self.input.clear();
                        self.add_tag.clear();
                        self.pending_file = None;
                        self.status_msg = "Enter tag (then Enter for title):".to_string();
                    }
                } else {
                    self.mode = Mode::AddTag;
                    self.input.clear();
                    self.add_tag.clear();
                    self.pending_file = None;
                    self.status_msg = "Enter tag (then Enter for title):".to_string();
                }
            }
            KeyCode::Char('d') => {
                if let Some(task) = self.selected_task() {
                    let id = task.id();
                    match writer::complete_task(&id) {
                        Ok(()) => {
                            self.status_msg = format!("Completed {}", id);
                            self.refresh()?;
                        }
                        Err(e) => self.status_msg = format!("Error: {}", e),
                    }
                }
            }
            KeyCode::Char('u') => {
                // Undo: only works on completed tasks
                if self.focus == Focus::Completed {
                    if let Some(task) = self.selected_task() {
                        let id = task.id();
                        match writer::undo_task(&id) {
                            Ok(()) => {
                                self.status_msg = format!("Reopened {}", id);
                                self.refresh()?;
                            }
                            Err(e) => self.status_msg = format!("Error: {}", e),
                        }
                    }
                } else {
                    self.status_msg = "u: select a completed task first (Tab to Completed panel)".to_string();
                }
            }
            KeyCode::Char('n') => {
                if self.selected_task().is_some() {
                    self.mode = Mode::NoteInput;
                    self.input.clear();
                    self.status_msg = "Enter note text:".to_string();
                }
            }
            KeyCode::Char('p') => {
                // Toggle priority
                if let Some(task) = self.selected_task() {
                    let id = task.id();
                    match writer::toggle_priority(&id) {
                        Ok(new_p) => {
                            self.status_msg = if new_p {
                                format!("{} marked HIGH priority", id)
                            } else {
                                format!("{} marked normal priority", id)
                            };
                            self.refresh()?;
                        }
                        Err(e) => self.status_msg = format!("Error: {}", e),
                    }
                }
            }
            KeyCode::Char('e') => {
                // Edit task title
                if let Some(task) = self.selected_task() {
                    let id = task.id();
                    let title = task.title.clone();
                    self.add_tag = id;
                    self.input = title;
                    self.mode = Mode::EditTitle;
                    self.status_msg = "Edit title (Enter to save, Esc to cancel):".to_string();
                }
            }
            KeyCode::Char('x') => {
                // Delete task
                if let Some(task) = self.selected_task() {
                    let id = task.id();
                    self.add_tag = id.clone();
                    self.mode = Mode::ConfirmDeleteTask;
                    self.status_msg = format!("Delete task {}? y/n", id);
                }
            }
            KeyCode::Char('R') => {
                // Rename tag — only from projects panel
                if self.focus == Focus::Projects {
                    if let Some(tag) = self.current_project_tag() {
                        self.add_tag = tag.clone();
                        self.input.clear();
                        self.mode = Mode::RenameTag;
                        self.status_msg = format!("Rename '{}' to:", tag);
                    }
                } else {
                    self.status_msg = "R: focus Projects panel first".to_string();
                }
            }
            KeyCode::Char('/') => {
                self.mode = Mode::Search;
                self.input.clear();
                self.status_msg = "Search:".to_string();
            }
            KeyCode::Char('c') => {
                self.search_query.clear();
                self.status_msg = "Filter cleared".to_string();
                self.refresh()?;
            }
            KeyCode::Char('r') => {
                self.refresh()?;
                self.status_msg = "Refreshed".to_string();
            }
            KeyCode::Char('.') => {
                self.hide_empty_projects = !self.hide_empty_projects;
                self.status_msg = if self.hide_empty_projects {
                    "Hiding projects with no open tasks (. to show)".to_string()
                } else {
                    "Showing all projects (. to hide empty)".to_string()
                };
                self.clamp_task_idx();
            }
            KeyCode::Char('b') => {
                // Go back in navigation stack
                self.nav_back();
            }
            KeyCode::Char('?') => {
                self.status_msg =
                    "j/k:nav h/l:panel a:add e:edit x:del d:done u:undo n:note p:priority R:rename /:search q:quit"
                        .to_string();
            }
            _ => {}
        }
        Ok(())
    }

    fn handle_detail_key(&mut self, key: KeyEvent) -> Result<()> {
        match key.code {
            KeyCode::Esc | KeyCode::Char('q') => {
                self.show_detail = false;
                self.detail_scroll = 0;
                self.detail_note_idx = None;
            }
            KeyCode::Char('j') | KeyCode::Down => {
                if let Some(task) = self.selected_task() {
                    let note_count = task.notes.len();
                    if note_count > 0 {
                        match self.detail_note_idx {
                            None => self.detail_note_idx = Some(0),
                            Some(idx) if idx < note_count - 1 => {
                                self.detail_note_idx = Some(idx + 1)
                            }
                            _ => {}
                        }
                    } else {
                        self.detail_scroll += 1;
                    }
                }
            }
            KeyCode::Char('k') | KeyCode::Up => {
                match self.detail_note_idx {
                    Some(0) => self.detail_note_idx = None,
                    Some(idx) => self.detail_note_idx = Some(idx - 1),
                    None => self.detail_scroll = self.detail_scroll.saturating_sub(1),
                }
            }
            KeyCode::Char('g') => {
                self.detail_scroll = 0;
                self.detail_note_idx = None;
            }
            KeyCode::Char('G') => {
                if let Some(task) = self.selected_task() {
                    if !task.notes.is_empty() {
                        self.detail_note_idx = Some(task.notes.len() - 1);
                    } else {
                        self.detail_scroll = u16::MAX;
                    }
                }
            }
            KeyCode::Char('x') => {
                if let Some(task) = self.selected_task() {
                    let id = task.id();
                    if let Some(note_idx) = self.detail_note_idx {
                        // Delete selected note
                        self.add_tag = id;
                        self.input = note_idx.to_string();
                        self.mode = Mode::ConfirmDeleteNote;
                        self.status_msg = "Delete this note? y/n".to_string();
                    } else {
                        // Delete the task itself
                        self.add_tag = id.clone();
                        self.mode = Mode::ConfirmDeleteTask;
                        self.status_msg = format!("Delete task {}? y/n", id);
                    }
                }
            }
            KeyCode::Char('e') => {
                // Edit task title from detail view
                if let Some(task) = self.selected_task() {
                    let id = task.id();
                    let title = task.title.clone();
                    self.add_tag = id;
                    self.input = title;
                    self.show_detail = false;
                    self.detail_note_idx = None;
                    self.mode = Mode::EditTitle;
                    self.status_msg = "Edit title (Enter to save, Esc to cancel):".to_string();
                }
            }
            KeyCode::Char('f') => {
                // Follow a link
                if !self.detail_links.is_empty() {
                    let link = self.detail_links[self.detail_link_idx].clone();
                    self.show_detail = false;
                    self.jump_to_task(&link)?;
                }
            }
            KeyCode::Char('n') => {
                // Cycle to next link
                if !self.detail_links.is_empty() {
                    self.detail_link_idx =
                        (self.detail_link_idx + 1) % self.detail_links.len();
                    self.status_msg = format!(
                        "Link [{}/{}]: {}",
                        self.detail_link_idx + 1,
                        self.detail_links.len(),
                        self.detail_links[self.detail_link_idx]
                    );
                }
            }
            KeyCode::Char('p') => {
                // Toggle priority from detail view
                if let Some(task) = self.selected_task() {
                    let id = task.id();
                    match writer::toggle_priority(&id) {
                        Ok(new_p) => {
                            self.status_msg = if new_p {
                                format!("{} marked HIGH priority", id)
                            } else {
                                format!("{} marked normal priority", id)
                            };
                            self.refresh()?;
                        }
                        Err(e) => self.status_msg = format!("Error: {}", e),
                    }
                }
            }
            KeyCode::Char('b') => {
                // Back from link jump
                self.nav_back();
            }
            _ => {}
        }
        Ok(())
    }

    /// After the tag is known (either typed or auto-selected), resolve the
    /// target file. Returns true if we can proceed directly to title entry,
    /// false if we need to show the file picker first.
    fn resolve_file_for_add(&mut self) -> Result<bool> {
        let config = Config::load()?;
        match router::resolve_file_for_tag(&config, &self.add_tag)? {
            router::RouteResult::Resolved(p) => {
                self.pending_file = Some(p);
                self.input.clear();
                self.mode = Mode::AddTitle;
                self.status_msg = format!("Tag: {} | Enter title:", self.add_tag);
                Ok(true)
            }
            router::RouteResult::Ambiguous(files) => {
                self.pick_file_options = files;
                self.pick_file_idx = 0;
                self.mode = Mode::PickFile;
                self.status_msg = "Select target file (j/k, Enter to confirm):".to_string();
                Ok(false)
            }
        }
    }

    fn handle_add_tag_key(&mut self, key: KeyEvent) -> Result<()> {
        match key.code {
            KeyCode::Esc => {
                self.mode = Mode::Normal;
                self.status_msg = "Cancelled".to_string();
            }
            KeyCode::Enter => {
                if self.input.is_empty() {
                    self.status_msg = "Tag cannot be empty".to_string();
                } else {
                    self.add_tag = self.input.clone();
                    self.input.clear();
                    self.resolve_file_for_add()?;
                }
            }
            KeyCode::Backspace => {
                self.input.pop();
            }
            KeyCode::Char(c) if c.is_ascii_lowercase() || c.is_ascii_digit() => {
                self.input.push(c);
            }
            _ => {}
        }
        Ok(())
    }

    fn handle_add_title_key(&mut self, key: KeyEvent) -> Result<()> {
        match key.code {
            KeyCode::Esc => {
                self.mode = Mode::Normal;
                self.pending_file = None;
                self.status_msg = "Cancelled".to_string();
            }
            KeyCode::Enter => {
                if self.input.is_empty() {
                    self.mode = Mode::Normal;
                    self.pending_file = None;
                    self.status_msg = "Title cannot be empty".to_string();
                } else {
                    let result = if let Some(ref path) = self.pending_file {
                        writer::add_task_to_file(&self.add_tag, &self.input, false, path)
                    } else {
                        writer::add_task(&self.add_tag, &self.input)
                    };
                    match result {
                        Ok(id) => {
                            self.status_msg = format!("Created {}", id);
                            self.mode = Mode::Normal;
                            self.pending_file = None;
                            self.refresh()?;
                        }
                        Err(e) => {
                            self.mode = Mode::Normal;
                            self.pending_file = None;
                            self.status_msg = format!("Error: {}", e);
                        }
                    }
                }
            }
            KeyCode::Backspace => {
                self.input.pop();
            }
            KeyCode::Char(c) => {
                self.input.push(c);
            }
            _ => {}
        }
        Ok(())
    }

    fn handle_pick_file_key(&mut self, key: KeyEvent) -> Result<()> {
        match key.code {
            KeyCode::Esc => {
                self.mode = Mode::Normal;
                self.pending_file = None;
                self.status_msg = "Cancelled".to_string();
            }
            KeyCode::Char('j') | KeyCode::Down => {
                if !self.pick_file_options.is_empty() {
                    self.pick_file_idx =
                        (self.pick_file_idx + 1).min(self.pick_file_options.len() - 1);
                }
            }
            KeyCode::Char('k') | KeyCode::Up => {
                self.pick_file_idx = self.pick_file_idx.saturating_sub(1);
            }
            KeyCode::Enter => {
                if let Some(entry) = self.pick_file_options.get(self.pick_file_idx) {
                    self.pending_file = Some(entry.resolved_path());
                    self.input.clear();
                    self.mode = Mode::AddTitle;
                    self.status_msg = format!(
                        "Tag: {} -> [{}] | Enter title:",
                        self.add_tag, entry.label
                    );
                }
            }
            _ => {}
        }
        Ok(())
    }

    fn handle_note_input_key(&mut self, key: KeyEvent) -> Result<()> {
        match key.code {
            KeyCode::Esc => {
                self.mode = Mode::Normal;
                self.status_msg = "Cancelled".to_string();
            }
            KeyCode::Enter => {
                if self.input.is_empty() {
                    self.mode = Mode::Normal;
                    self.status_msg = "Note cannot be empty".to_string();
                } else if let Some(task) = self.selected_task() {
                    let id = task.id();
                    match writer::add_note(&id, &self.input) {
                        Ok(()) => {
                            self.status_msg = format!("Note added to {}", id);
                            self.mode = Mode::Normal;
                            self.refresh()?;
                        }
                        Err(e) => {
                            self.mode = Mode::Normal;
                            self.status_msg = format!("Error: {}", e);
                        }
                    }
                }
            }
            KeyCode::Backspace => {
                self.input.pop();
            }
            KeyCode::Char(c) => {
                self.input.push(c);
            }
            _ => {}
        }
        Ok(())
    }

    fn handle_search_key(&mut self, key: KeyEvent) -> Result<()> {
        match key.code {
            KeyCode::Esc => {
                self.mode = Mode::Normal;
                self.search_query.clear();
                self.status_msg = "Search cancelled".to_string();
                self.refresh()?;
            }
            KeyCode::Enter => {
                self.search_query = self.input.clone();
                self.mode = Mode::Normal;
                self.project_idx = 0;
                self.task_idx = 0;
                self.refresh()?;
                self.status_msg = if self.all_tasks.is_empty() {
                    format!("No results for \"{}\"", self.search_query)
                } else {
                    format!(
                        "{} results for \"{}\"",
                        self.all_tasks.len(),
                        self.search_query
                    )
                };
            }
            KeyCode::Backspace => {
                self.input.pop();
            }
            KeyCode::Char(c) => {
                self.input.push(c);
            }
            _ => {}
        }
        Ok(())
    }

    fn handle_rename_tag_key(&mut self, key: KeyEvent) -> Result<()> {
        match key.code {
            KeyCode::Esc => {
                self.mode = Mode::Normal;
                self.status_msg = "Cancelled".to_string();
            }
            KeyCode::Enter => {
                if self.input.is_empty() {
                    self.status_msg = "New tag name cannot be empty".to_string();
                } else {
                    let old = self.add_tag.clone();
                    let new = self.input.clone();
                    match writer::rename_tag(&old, &new) {
                        Ok(()) => {
                            self.status_msg = format!("Renamed {} -> {}", old, new);
                            self.mode = Mode::Normal;
                            self.refresh()?;
                        }
                        Err(e) => {
                            self.mode = Mode::Normal;
                            self.status_msg = format!("Error: {}", e);
                        }
                    }
                }
            }
            KeyCode::Backspace => {
                self.input.pop();
            }
            KeyCode::Char(c) if c.is_ascii_lowercase() || c.is_ascii_digit() => {
                self.input.push(c);
            }
            _ => {}
        }
        Ok(())
    }

    fn handle_confirm_delete_note_key(&mut self, key: KeyEvent) -> Result<()> {
        match key.code {
            KeyCode::Char('y') | KeyCode::Char('Y') => {
                let task_id = self.add_tag.clone();
                let note_idx: usize = self.input.parse().unwrap_or(0);
                match writer::delete_note(&task_id, note_idx) {
                    Ok(()) => {
                        self.status_msg = format!("Note deleted from {}", task_id);
                        self.mode = Mode::Normal;
                        self.detail_note_idx = None;
                        self.refresh()?;
                    }
                    Err(e) => {
                        self.mode = Mode::Normal;
                        self.status_msg = format!("Error: {}", e);
                    }
                }
            }
            _ => {
                self.mode = Mode::Normal;
                self.status_msg = "Delete cancelled".to_string();
            }
        }
        Ok(())
    }

    fn handle_edit_title_key(&mut self, key: KeyEvent) -> Result<()> {
        match key.code {
            KeyCode::Esc => {
                self.mode = Mode::Normal;
                self.status_msg = "Edit cancelled".to_string();
            }
            KeyCode::Enter => {
                if self.input.is_empty() {
                    self.mode = Mode::Normal;
                    self.status_msg = "Title cannot be empty".to_string();
                } else {
                    let task_id = self.add_tag.clone();
                    match writer::edit_task(&task_id, &self.input) {
                        Ok(()) => {
                            self.status_msg = format!("Edited {}", task_id);
                            self.mode = Mode::Normal;
                            self.refresh()?;
                        }
                        Err(e) => {
                            self.mode = Mode::Normal;
                            self.status_msg = format!("Error: {}", e);
                        }
                    }
                }
            }
            KeyCode::Backspace => {
                self.input.pop();
            }
            KeyCode::Char(c) => {
                self.input.push(c);
            }
            _ => {}
        }
        Ok(())
    }

    fn handle_confirm_delete_task_key(&mut self, key: KeyEvent) -> Result<()> {
        match key.code {
            KeyCode::Char('y') | KeyCode::Char('Y') => {
                let task_id = self.add_tag.clone();
                match writer::delete_task(&task_id) {
                    Ok(()) => {
                        self.status_msg = format!("Deleted {}", task_id);
                        self.mode = Mode::Normal;
                        self.show_detail = false;
                        self.detail_note_idx = None;
                        self.refresh()?;
                    }
                    Err(e) => {
                        self.mode = Mode::Normal;
                        self.status_msg = format!("Error: {}", e);
                    }
                }
            }
            _ => {
                self.mode = Mode::Normal;
                self.status_msg = "Delete cancelled".to_string();
            }
        }
        Ok(())
    }
}

/// Truncate a string to fit within `max_width` chars, appending "…" if truncated.
fn truncate(s: &str, max_width: usize) -> String {
    if max_width == 0 {
        return String::new();
    }
    let chars: Vec<char> = s.chars().collect();
    if chars.len() <= max_width {
        s.to_string()
    } else if max_width <= 1 {
        "…".to_string()
    } else {
        let mut result: String = chars[..max_width - 1].iter().collect();
        result.push('…');
        result
    }
}

fn ui(frame: &mut Frame, app: &App) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3), // header
            Constraint::Min(5),   // main
            Constraint::Length(3), // status
        ])
        .split(frame.area());

    // Header
    let title = if app.search_query.is_empty() {
        " tasklog ".to_string()
    } else {
        format!(" tasklog — search: \"{}\" ", app.search_query)
    };
    let header = Block::default()
        .borders(Borders::ALL)
        .title(title)
        .border_style(Style::default().fg(Color::Cyan));
    let header_text = Paragraph::new(format!(
        " {} projects | {} tasks | ? for help",
        app.projects.len(),
        app.all_tasks.len(),
    ))
    .block(header);
    frame.render_widget(header_text, chunks[0]);

    // Main: Projects | Open Tasks | Completed Tasks
    let main_chunks = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Length(20),
            Constraint::Percentage(50),
            Constraint::Percentage(50),
        ])
        .split(chunks[1]);

    // Available inner widths (subtract 2 for borders)
    let project_width = main_chunks[0].width.saturating_sub(2) as usize;
    let open_width = main_chunks[1].width.saturating_sub(2) as usize;
    let completed_width = main_chunks[2].width.saturating_sub(2) as usize;

    // Projects panel
    let project_border_color = if app.focus == Focus::Projects {
        Color::Yellow
    } else {
        Color::DarkGray
    };
    let visible_projects = app.visible_projects();
    let multi_file = app.file_entries.len() > 1;
    let project_items: Vec<ListItem> = visible_projects
        .iter()
        .enumerate()
        .map(|(i, tag)| {
            let task_count = app.all_tasks.iter().filter(|t| t.tag == **tag).count();
            let open_count = app
                .all_tasks
                .iter()
                .filter(|t| t.tag == **tag && !t.done)
                .count();
            let label = truncate(
                &format!("{} ({}/{})", tag, open_count, task_count),
                project_width,
            );
            let style = if i == app.project_idx {
                Style::default().bg(Color::DarkGray).fg(Color::White)
            } else {
                Style::default()
            };
            ListItem::new(label).style(style)
        })
        .collect();

    let project_title = if app.hide_empty_projects {
        " Projects [.] "
    } else {
        " Projects "
    };
    let project_list = List::new(project_items).block(
        Block::default()
            .borders(Borders::ALL)
            .title(project_title)
            .border_style(Style::default().fg(project_border_color)),
    );
    frame.render_widget(project_list, main_chunks[0]);

    // Open Tasks panel
    let open_border_color = if app.focus == Focus::Tasks {
        Color::Magenta
    } else {
        Color::DarkGray
    };
    let open = app.open_tasks();
    let open_items: Vec<ListItem> = open
        .iter()
        .enumerate()
        .map(|(i, task)| {
            let note_hint = if task.notes.is_empty() {
                String::new()
            } else {
                format!(" [{}]", task.notes.len())
            };
            let priority_marker = if task.priority { "! " } else { "" };
            let label = truncate(
                &format!(
                    "{}[ ] {} {}{}",
                    priority_marker,
                    task.id(),
                    task.title,
                    note_hint
                ),
                open_width,
            );
            let style = if i == app.task_idx && app.focus == Focus::Tasks {
                if task.priority {
                    Style::default().bg(Color::DarkGray).fg(Color::Red)
                } else {
                    Style::default().bg(Color::DarkGray).fg(Color::White)
                }
            } else if i == app.task_idx {
                if task.priority {
                    Style::default().fg(Color::Red)
                } else {
                    Style::default().fg(Color::White)
                }
            } else if task.priority {
                Style::default().fg(Color::Red)
            } else {
                Style::default()
            };
            ListItem::new(label).style(style)
        })
        .collect();

    let open_title = if visible_projects.is_empty() {
        " Open ".to_string()
    } else {
        let idx = app.project_idx.min(visible_projects.len().saturating_sub(1));
        let tag = visible_projects[idx];
        if multi_file {
            let file_label = app.tag_file_labels.get(tag.as_str()).map(|l| l.as_str()).unwrap_or("?");
            format!(" Open — {} — {} ", tag, file_label)
        } else {
            format!(" Open — {} ", tag)
        }
    };
    let open_list = List::new(open_items).block(
        Block::default()
            .borders(Borders::ALL)
            .title(open_title)
            .border_style(Style::default().fg(open_border_color)),
    );
    frame.render_widget(open_list, main_chunks[1]);

    // Completed Tasks panel
    let completed_border_color = if app.focus == Focus::Completed {
        Color::Green
    } else {
        Color::DarkGray
    };
    let completed = app.completed_tasks();
    let completed_items: Vec<ListItem> = completed
        .iter()
        .enumerate()
        .map(|(i, task)| {
            let note_hint = if task.notes.is_empty() {
                String::new()
            } else {
                format!(" [{}]", task.notes.len())
            };
            let label = truncate(
                &format!("[x] {} {}{}", task.id(), task.title, note_hint),
                completed_width,
            );
            let style = if i == app.completed_idx && app.focus == Focus::Completed {
                Style::default().bg(Color::DarkGray).fg(Color::White)
            } else {
                Style::default().fg(Color::Green)
            };
            ListItem::new(label).style(style)
        })
        .collect();

    let completed_title = if visible_projects.is_empty() {
        " Completed ".to_string()
    } else {
        let idx = app.project_idx.min(visible_projects.len().saturating_sub(1));
        let tag = visible_projects[idx];
        if multi_file {
            let file_label = app.tag_file_labels.get(tag.as_str()).map(|l| l.as_str()).unwrap_or("?");
            format!(" Completed — {} — {} ", tag, file_label)
        } else {
            format!(" Completed — {} ", tag)
        }
    };
    let completed_list = List::new(completed_items).block(
        Block::default()
            .borders(Borders::ALL)
            .title(completed_title)
            .border_style(Style::default().fg(completed_border_color)),
    );
    frame.render_widget(completed_list, main_chunks[2]);

    // Detail popup
    if app.show_detail {
        if let Some(task) = app.selected_task() {
            let area = frame.area();
            let popup_width = (area.width * 90 / 100).max(50).min(area.width.saturating_sub(2));
            let max_popup_height = area.height * 70 / 100;
            let popup_height = max_popup_height.max(10).min(area.height.saturating_sub(2));
            let x = (area.width.saturating_sub(popup_width)) / 2;
            let y = (area.height.saturating_sub(popup_height)) / 2;
            let popup_area = Rect::new(x, y, popup_width, popup_height);

            frame.render_widget(Clear, popup_area);

            let status_str = if task.done { "done" } else { "open" };
            let status_color = if task.done { Color::Green } else { Color::Yellow };

            let mut lines: Vec<Line> = Vec::new();

            // ID + status + priority
            let mut id_spans = vec![
                Span::styled(
                    "ID:    ",
                    Style::default()
                        .fg(Color::Cyan)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw(task.id()),
                Span::raw("  "),
                Span::styled(
                    format!("[{}]", status_str),
                    Style::default()
                        .fg(status_color)
                        .add_modifier(Modifier::BOLD),
                ),
            ];
            if task.priority {
                id_spans.push(Span::raw("  "));
                id_spans.push(Span::styled(
                    "HIGH PRIORITY",
                    Style::default()
                        .fg(Color::Red)
                        .add_modifier(Modifier::BOLD),
                ));
            }
            lines.push(Line::from(id_spans));

            lines.push(Line::from(vec![
                Span::styled(
                    "Title: ",
                    Style::default()
                        .fg(Color::Cyan)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw(&task.title),
            ]));
            if !task.date.is_empty() {
                lines.push(Line::from(vec![
                    Span::styled(
                        "Date:  ",
                        Style::default()
                            .fg(Color::Cyan)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Span::raw(&task.date),
                ]));
            }

            // Links
            if !app.detail_links.is_empty() {
                lines.push(Line::from(""));
                let links_str = app
                    .detail_links
                    .iter()
                    .enumerate()
                    .map(|(i, l)| {
                        if i == app.detail_link_idx {
                            format!("[{}]", l)
                        } else {
                            l.clone()
                        }
                    })
                    .collect::<Vec<_>>()
                    .join("  ");
                lines.push(Line::from(vec![
                    Span::styled(
                        "Links: ",
                        Style::default()
                            .fg(Color::Cyan)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(links_str, Style::default().fg(Color::LightBlue)),
                ]));
            }

            if !task.notes.is_empty() {
                lines.push(Line::from(""));
                lines.push(Line::from(Span::styled(
                    "Notes: (j/k select, x delete)",
                    Style::default()
                        .fg(Color::Cyan)
                        .add_modifier(Modifier::BOLD),
                )));
                for (i, note) in task.notes.iter().enumerate() {
                    let is_selected = app.detail_note_idx == Some(i);
                    let bullet_style = if is_selected {
                        Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)
                    } else {
                        Style::default().fg(Color::DarkGray)
                    };
                    let text_style = if is_selected {
                        Style::default()
                            .fg(Color::White)
                            .add_modifier(Modifier::BOLD)
                    } else {
                        Style::default().fg(Color::White)
                    };

                    // Highlight task links within note text
                    let note_links = parser::extract_links(&note.text);
                    if note_links.is_empty() {
                        lines.push(Line::from(vec![
                            Span::styled(
                                if is_selected { "▶ - " } else { "  - " },
                                bullet_style,
                            ),
                            Span::styled(&note.text, text_style),
                        ]));
                    } else {
                        let mut spans = vec![Span::styled(
                            if is_selected { "▶ - " } else { "  - " },
                            bullet_style,
                        )];
                        // Simple: render whole text, highlight link portions
                        let link_re = regex::Regex::new(r"([a-z][a-z0-9]*-\d+)").unwrap();
                        let mut last_end = 0;
                        for mat in link_re.find_iter(&note.text) {
                            if mat.start() > last_end {
                                spans.push(Span::styled(
                                    &note.text[last_end..mat.start()],
                                    text_style,
                                ));
                            }
                            spans.push(Span::styled(
                                mat.as_str(),
                                Style::default()
                                    .fg(Color::LightBlue)
                                    .add_modifier(Modifier::UNDERLINED),
                            ));
                            last_end = mat.end();
                        }
                        if last_end < note.text.len() {
                            spans.push(Span::styled(&note.text[last_end..], text_style));
                        }
                        lines.push(Line::from(spans));
                    }
                    lines.push(Line::from(""));
                }
            }

            let mut hints = Vec::new();
            if !app.detail_links.is_empty() {
                hints.push("n:next-link f:follow");
            }
            if !app.nav_stack.is_empty() {
                hints.push("b:back");
            }
            hints.push("p:priority Esc:close");
            let scroll_hint = format!(" {} ", hints.join(" | "));

            let popup = Paragraph::new(Text::from(lines))
                .wrap(Wrap { trim: false })
                .scroll((app.detail_scroll, 0))
                .block(
                    Block::default()
                        .borders(Borders::ALL)
                        .padding(Padding::horizontal(1))
                        .title(" Task Detail ")
                        .title_bottom(scroll_hint)
                        .border_style(Style::default().fg(Color::Cyan)),
                );
            frame.render_widget(popup, popup_area);
        }
    }

    // Status bar
    let input_text = match app.mode {
        Mode::Normal => app.status_msg.clone(),
        Mode::AddTag => format!("Tag: {}_", app.input),
        Mode::AddTitle => format!("[{}] Title: {}_", app.add_tag, app.input),
        Mode::PickFile => {
            let opts: Vec<String> = app
                .pick_file_options
                .iter()
                .enumerate()
                .map(|(i, f)| {
                    if i == app.pick_file_idx {
                        format!("[>{}]", f.label)
                    } else {
                        f.label.clone()
                    }
                })
                .collect();
            format!("Tag: {} | File: {} (j/k, Enter)", app.add_tag, opts.join("  "))
        }
        Mode::NoteInput => format!("Note: {}_", app.input),
        Mode::Search => format!("/{}_", app.input),
        Mode::RenameTag => format!("Rename '{}' to: {}_", app.add_tag, app.input),
        Mode::EditTitle => format!("[{}] Title: {}_", app.add_tag, app.input),
        Mode::ConfirmDeleteNote => app.status_msg.clone(),
        Mode::ConfirmDeleteTask => app.status_msg.clone(),
    };
    let mode_label = match app.mode {
        Mode::Normal => "NORMAL",
        Mode::AddTag | Mode::AddTitle => "ADD",
        Mode::PickFile => "FILE",
        Mode::NoteInput => "NOTE",
        Mode::Search => "SEARCH",
        Mode::RenameTag => "RENAME",
        Mode::EditTitle => "EDIT",
        Mode::ConfirmDeleteNote => "CONFIRM",
        Mode::ConfirmDeleteTask => "CONFIRM",
    };
    let status_block = Block::default()
        .borders(Borders::ALL)
        .title(format!(" {} ", mode_label))
        .border_style(Style::default().fg(match app.mode {
            Mode::Normal => Color::Gray,
            Mode::ConfirmDeleteNote | Mode::ConfirmDeleteTask => Color::Red,
            Mode::PickFile => Color::Cyan,
            _ => Color::Green,
        }));
    let status = Paragraph::new(input_text).block(status_block);
    frame.render_widget(status, chunks[2]);
}

pub fn run() -> Result<()> {
    if !Config::config_path().exists() {
        return Err(TlError::NotInitialized);
    }

    enable_raw_mode().map_err(|e| TlError::Other(e.to_string()))?;
    stdout()
        .execute(EnterAlternateScreen)
        .map_err(|e| TlError::Other(e.to_string()))?;

    let backend = CrosstermBackend::new(stdout());
    let mut terminal = Terminal::new(backend).map_err(|e| TlError::Other(e.to_string()))?;

    let mut app = App::new()?;

    loop {
        terminal
            .draw(|f| ui(f, &app))
            .map_err(|e| TlError::Other(e.to_string()))?;

        if event::poll(Duration::from_millis(100)).map_err(|e| TlError::Other(e.to_string()))? {
            if let Event::Key(key) = event::read().map_err(|e| TlError::Other(e.to_string()))? {
                if key.kind != KeyEventKind::Press {
                    continue;
                }
                app.handle_key(key)?;
                if app.should_quit {
                    break;
                }
            }
        }
    }

    disable_raw_mode().map_err(|e| TlError::Other(e.to_string()))?;
    stdout()
        .execute(LeaveAlternateScreen)
        .map_err(|e| TlError::Other(e.to_string()))?;

    Ok(())
}
