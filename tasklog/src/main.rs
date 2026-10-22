mod config;
mod error;
mod lock;
mod mcp;
mod parser;
mod router;
mod state;
mod tui;
mod writer;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "tl", about = "Minimal global markdown task log")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Initialize tl: create config, log, and state files
    Init {
        /// Path to your existing log.md file (default: ~/.config/tl/log.md)
        #[arg(long)]
        log: Option<String>,
    },

    /// Add a new task: tl add <tag> <title>
    Add {
        /// Task tag (lowercase alphanumeric, e.g. "osv", "infra")
        tag: String,
        /// Task title
        title: Vec<String>,
        /// Mark as high priority
        #[arg(short, long)]
        priority: bool,
    },

    /// Mark a task as done: tl done <id>
    Done {
        /// Task ID (e.g. "osv-12")
        id: String,
    },

    /// Undo a completed task: tl undo <id>
    Undo {
        /// Task ID (e.g. "osv-12")
        id: String,
    },

    /// Add a note to a task: tl note <id> <text>
    Note {
        /// Task ID (e.g. "osv-12")
        id: String,
        /// Note text
        text: Vec<String>,
    },

    /// Search tasks: tl search <query>
    Search {
        /// Search query
        query: Vec<String>,
    },

    /// Show today's section
    Today,

    /// Rename a tag: tl rename <old> <new>
    Rename {
        /// Current tag name
        old: String,
        /// New tag name
        new: String,
    },

    /// Toggle priority on a task: tl priority <id>
    Priority {
        /// Task ID (e.g. "osv-12")
        id: String,
    },

    /// Edit a task's title: tl edit <id> <new title>
    Edit {
        /// Task ID (e.g. "osv-12")
        id: String,
        /// New title
        title: Vec<String>,
    },

    /// Delete a task and its notes: tl delete <id>
    Delete {
        /// Task ID (e.g. "osv-12")
        id: String,
    },

    /// Open interactive TUI
    Tui,

    /// Start MCP server (stdio transport)
    Mcp,

    /// Manage log files
    File {
        #[command(subcommand)]
        action: FileAction,
    },
}

#[derive(Subcommand)]
enum FileAction {
    /// Add a new log file
    Add {
        /// Path to the markdown file
        #[arg(long)]
        path: String,
        /// Short label for this file (e.g. "wishlist", "work")
        #[arg(long)]
        label: String,
        /// File mode: "variable" (any tag) or "fixed" (specific tags only)
        #[arg(long, default_value = "variable")]
        mode: String,
        /// Tags this file accepts (only for fixed mode, comma-separated)
        #[arg(long, value_delimiter = ',')]
        tags: Vec<String>,
        /// Where new sections are inserted: "top" or "bottom" (default)
        #[arg(long, default_value = "bottom")]
        insert: String,
    },
    /// List configured log files
    List,
    /// Remove a log file by label
    Remove {
        /// Label of the file to remove
        label: String,
    },
}

fn main() {
    let cli = Cli::parse();

    let result = match cli.command {
        Commands::Init { log } => cmd_init(log.as_deref()),
        Commands::Add {
            tag,
            title,
            priority,
        } => cmd_add(&tag, &title.join(" "), priority),
        Commands::Done { id } => cmd_done(&id),
        Commands::Undo { id } => cmd_undo(&id),
        Commands::Note { id, text } => cmd_note(&id, &text.join(" ")),
        Commands::Search { query } => cmd_search(&query.join(" ")),
        Commands::Today => cmd_today(),
        Commands::Rename { old, new } => cmd_rename(&old, &new),
        Commands::Priority { id } => cmd_priority(&id),
        Commands::Edit { id, title } => cmd_edit(&id, &title.join(" ")),
        Commands::Delete { id } => cmd_delete(&id),
        Commands::Tui => cmd_tui(),
        Commands::Mcp => cmd_mcp(),
        Commands::File { action } => match action {
            FileAction::Add {
                path,
                label,
                mode,
                tags,
                insert,
            } => cmd_file_add(&path, &label, &mode, &tags, &insert),
            FileAction::List => cmd_file_list(),
            FileAction::Remove { label } => cmd_file_remove(&label),
        },
    };

    if let Err(e) = result {
        eprintln!("error: {}", e);
        std::process::exit(1);
    }
}

fn cmd_init(log_path: Option<&str>) -> error::Result<()> {
    writer::init(log_path)?;
    let config = config::Config::load()?;
    println!("initialized at {}", config::Config::base_dir().display());
    println!("log file: {}", config.resolved_log_path().display());
    Ok(())
}

fn cmd_add(tag: &str, title: &str, priority: bool) -> error::Result<()> {
    if title.is_empty() {
        return Err(error::TlError::Other("title cannot be empty".to_string()));
    }
    let id = writer::add_task_with_priority(tag, title, priority)?;
    println!("created {}", id);
    Ok(())
}

fn cmd_done(id: &str) -> error::Result<()> {
    writer::complete_task(id)?;
    println!("completed {}", id);
    Ok(())
}

fn cmd_undo(id: &str) -> error::Result<()> {
    writer::undo_task(id)?;
    println!("reopened {}", id);
    Ok(())
}

fn cmd_note(id: &str, text: &str) -> error::Result<()> {
    if text.is_empty() {
        return Err(error::TlError::Other("note text cannot be empty".to_string()));
    }
    writer::add_note(id, text)?;
    println!("noted on {}", id);
    Ok(())
}

fn cmd_search(query: &str) -> error::Result<()> {
    if query.is_empty() {
        return Err(error::TlError::Other("search query cannot be empty".to_string()));
    }
    let tasks = writer::search(query)?;
    if tasks.is_empty() {
        println!("no tasks found matching \"{}\"", query);
        return Ok(());
    }
    for task in &tasks {
        let status = if task.done { "x" } else { " " };
        let priority = if task.priority { "!" } else { "" };
        println!("[{}] {}{} {}", status, task.id(), priority, task.title);
        for note in &task.notes {
            println!("      - {}", note.text);
        }
    }
    Ok(())
}

fn cmd_today() -> error::Result<()> {
    let text = writer::get_today()?;
    println!("{}", text);
    Ok(())
}

fn cmd_rename(old: &str, new: &str) -> error::Result<()> {
    writer::rename_tag(old, new)?;
    println!("renamed {} -> {}", old, new);
    Ok(())
}

fn cmd_priority(id: &str) -> error::Result<()> {
    let new_priority = writer::toggle_priority(id)?;
    if new_priority {
        println!("{} marked as high priority", id);
    } else {
        println!("{} marked as normal priority", id);
    }
    Ok(())
}

fn cmd_edit(id: &str, new_title: &str) -> error::Result<()> {
    if new_title.is_empty() {
        return Err(error::TlError::Other("title cannot be empty".to_string()));
    }
    writer::edit_task(id, new_title)?;
    println!("edited {}", id);
    Ok(())
}

fn cmd_delete(id: &str) -> error::Result<()> {
    writer::delete_task(id)?;
    println!("deleted {}", id);
    Ok(())
}

fn cmd_tui() -> error::Result<()> {
    tui::run()
}

fn cmd_mcp() -> error::Result<()> {
    mcp::run_mcp_server()
}

fn cmd_file_add(path: &str, label: &str, mode: &str, tags: &[String], insert: &str) -> error::Result<()> {
    let file_mode = match mode {
        "variable" => config::FileMode::Variable,
        "fixed" => config::FileMode::Fixed,
        _ => {
            return Err(error::TlError::Other(
                "mode must be 'variable' or 'fixed'".to_string(),
            ))
        }
    };

    let insert_pos = match insert {
        "top" => config::InsertPosition::Top,
        "bottom" => config::InsertPosition::Bottom,
        _ => {
            return Err(error::TlError::Other(
                "insert must be 'top' or 'bottom'".to_string(),
            ))
        }
    };

    if file_mode == config::FileMode::Fixed && tags.is_empty() {
        return Err(error::TlError::Other(
            "fixed mode requires at least one tag (use --tags)".to_string(),
        ));
    }

    let mut cfg = config::Config::load()?;

    // Check for duplicate label
    if cfg.files.iter().any(|f| f.label == label) {
        return Err(error::TlError::Other(format!(
            "file with label '{}' already exists",
            label
        )));
    }

    // If this is the first file being added and there's no existing files
    // array, migrate the current log_path as the first "main" variable file
    if cfg.files.is_empty() {
        cfg.files.push(config::FileEntry {
            path: cfg.log_path.clone(),
            label: "main".to_string(),
            mode: config::FileMode::Variable,
            tags: Vec::new(),
            insert: config::InsertPosition::default(),
        });
    }

    cfg.files.push(config::FileEntry {
        path: path.to_string(),
        label: label.to_string(),
        mode: file_mode,
        tags: tags.to_vec(),
        insert: insert_pos,
    });

    cfg.save()?;

    // Initialize the new file
    writer::init(None)?;

    println!("added file [{}] -> {}", label, path);
    Ok(())
}

fn cmd_file_list() -> error::Result<()> {
    let cfg = config::Config::load()?;
    let files = cfg.effective_files();

    if files.len() == 1 && cfg.files.is_empty() {
        println!("single file mode: {}", cfg.log_path);
        println!("(use `tl file add` to enable multi-file)");
        return Ok(());
    }

    for f in &files {
        let mode_str = match f.mode {
            config::FileMode::Variable => "variable".to_string(),
            config::FileMode::Fixed => format!("fixed({})", f.tags.join(",")),
        };
        let insert_str = match f.insert {
            config::InsertPosition::Top => ", insert=top",
            config::InsertPosition::Bottom => "",
        };
        println!("[{}] {} ({}{})", f.label, f.path, mode_str, insert_str);
    }
    Ok(())
}

fn cmd_file_remove(label: &str) -> error::Result<()> {
    let mut cfg = config::Config::load()?;

    if cfg.files.is_empty() {
        return Err(error::TlError::Other(
            "no multi-file config to remove from".to_string(),
        ));
    }

    let before = cfg.files.len();
    cfg.files.retain(|f| f.label != label);

    if cfg.files.len() == before {
        return Err(error::TlError::Other(format!(
            "no file with label '{}'",
            label
        )));
    }

    cfg.save()?;
    println!("removed file [{}]", label);
    Ok(())
}
