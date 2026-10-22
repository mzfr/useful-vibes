use thiserror::Error;

#[derive(Error, Debug)]
pub enum TlError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    #[error("Config error: {0}")]
    Config(String),

    #[error("State error: {0}")]
    State(String),

    #[error("Parse error: {0}")]
    Parse(String),

    #[error("Duplicate task ID: {0}")]
    DuplicateId(String),

    #[error("Task not found: {0}")]
    TaskNotFound(String),

    #[error("Lock error: {0}")]
    Lock(String),

    #[error("Not initialized. Run `tl init` first.")]
    NotInitialized,

    #[error("{0}")]
    Other(String),
}

pub type Result<T> = std::result::Result<T, TlError>;
