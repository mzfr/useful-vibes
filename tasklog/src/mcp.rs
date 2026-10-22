use crate::error::TlError;
use crate::writer;

use rmcp::handler::server::router::tool::ToolRouter;
use rmcp::handler::server::wrapper::Parameters;
use rmcp::model::{Implementation, ProtocolVersion, ServerCapabilities, ServerInfo};
use rmcp::schemars;
use rmcp::schemars::JsonSchema;
use rmcp::{tool, tool_handler, tool_router, ServerHandler, ServiceExt};
use serde::Deserialize;

#[derive(Debug, Deserialize, JsonSchema)]
pub struct CreateTaskParams {
    /// Task tag (lowercase alphanumeric, e.g. "osv", "infra")
    pub tag: String,
    /// Task title
    pub title: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct CompleteTaskParams {
    /// Task ID (e.g. "osv-12")
    pub id: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct AddNoteParams {
    /// Task ID (e.g. "osv-12")
    pub id: String,
    /// Note text
    pub text: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct SearchParams {
    /// Search query
    pub query: String,
    /// Optional tag filter
    pub tag: Option<String>,
}

#[derive(Clone)]
pub struct TlMcpServer {
    tool_router: ToolRouter<Self>,
}

#[tool_router]
impl TlMcpServer {
    pub fn new() -> Self {
        Self {
            tool_router: Self::tool_router(),
        }
    }

    /// Initialize the task log environment. Creates config, log, and state files if missing.
    #[tool(description = "Initialize the task log environment. Creates config, log, and state files if missing.")]
    fn init_log(&self) -> String {
        match writer::init(None) {
            Ok(()) => "Task log initialized successfully.".to_string(),
            Err(e) => format!("Error: {}", e),
        }
    }

    /// Create a new task with a tag and title. Returns the assigned task ID.
    #[tool(description = "Create a new task with a tag and title. Returns the assigned task ID.")]
    fn create_task(&self, Parameters(params): Parameters<CreateTaskParams>) -> String {
        match writer::add_task(&params.tag, &params.title) {
            Ok(id) => format!("Created task: {}", id),
            Err(e) => format!("Error: {}", e),
        }
    }

    /// Mark a task as completed by its ID (e.g. 'osv-12').
    #[tool(description = "Mark a task as completed by its ID (e.g. 'osv-12').")]
    fn complete_task(&self, Parameters(params): Parameters<CompleteTaskParams>) -> String {
        match writer::complete_task(&params.id) {
            Ok(()) => format!("Completed task: {}", params.id),
            Err(e) => format!("Error: {}", e),
        }
    }

    /// Add a note to an existing task by its ID.
    #[tool(description = "Add a note to an existing task by its ID.")]
    fn add_note(&self, Parameters(params): Parameters<AddNoteParams>) -> String {
        match writer::add_note(&params.id, &params.text) {
            Ok(()) => format!("Note added to task: {}", params.id),
            Err(e) => format!("Error: {}", e),
        }
    }

    /// Search tasks and notes. Optionally filter by tag.
    #[tool(description = "Search tasks and notes. Optionally filter by tag.")]
    fn search_tasks(&self, Parameters(params): Parameters<SearchParams>) -> String {
        match writer::search(&params.query) {
            Ok(tasks) => {
                let filtered: Vec<_> = if let Some(ref tag) = params.tag {
                    tasks.into_iter().filter(|t| t.tag == *tag).collect()
                } else {
                    tasks
                };

                if filtered.is_empty() {
                    return format!("No tasks found matching '{}'", params.query);
                }

                let mut output = String::new();
                for task in &filtered {
                    let status = if task.done { "x" } else { " " };
                    output.push_str(&format!("[{}] {} {}\n", status, task.id(), task.title));
                    for note in &task.notes {
                        output.push_str(&format!("      - {}\n", note.text));
                    }
                }
                output
            }
            Err(e) => format!("Error: {}", e),
        }
    }

    /// Get the raw text of today's section from the log.
    #[tool(description = "Get the raw text of today's section from the log.")]
    fn get_today_section(&self) -> String {
        match writer::get_today() {
            Ok(text) => text,
            Err(e) => format!("Error: {}", e),
        }
    }
}

#[tool_handler]
impl ServerHandler for TlMcpServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo {
            protocol_version: ProtocolVersion::V_2025_03_26,
            capabilities: ServerCapabilities::builder()
                .enable_tools()
                .build(),
            server_info: Implementation {
                name: "tl".to_string(),
                version: env!("CARGO_PKG_VERSION").to_string(),
                ..Default::default()
            },
            instructions: Some(
                "Task log tool. Use create_task to add tasks, complete_task to mark done, add_note to annotate, search_tasks to find tasks.".to_string(),
            ),
        }
    }
}

pub fn run_mcp_server() -> crate::error::Result<()> {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .map_err(|e| TlError::Other(format!("failed to build tokio runtime: {}", e)))?
        .block_on(async {
            let server = TlMcpServer::new();
            let transport = rmcp::transport::io::stdio();
            let running = server
                .serve(transport)
                .await
                .map_err(|e| TlError::Other(format!("MCP server error: {}", e)))?;
            running
                .waiting()
                .await
                .map_err(|e| TlError::Other(format!("MCP server error: {}", e)))?;
            Ok(())
        })
}
