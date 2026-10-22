use crate::config::{atomic_write, Config};
use crate::error::{Result, TlError};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Serialize, Deserialize, Default)]
pub struct State {
    #[serde(flatten)]
    pub tags: HashMap<String, u64>,
}

impl State {
    pub fn load() -> Result<Self> {
        let path = Config::state_path();
        if !path.exists() {
            return Err(TlError::NotInitialized);
        }
        let content = std::fs::read_to_string(&path)?;
        if content.trim().is_empty() {
            return Ok(Self::default());
        }
        serde_json::from_str(&content).map_err(|e| TlError::State(e.to_string()))
    }

    pub fn save(&self) -> Result<()> {
        let path = Config::state_path();
        let content =
            serde_json::to_string_pretty(&self).map_err(|e| TlError::State(e.to_string()))?;
        atomic_write(&path, content.as_bytes())
    }

    /// Ensure the counter for a tag is at least `min`.
    /// Used to sync state with tasks that exist in the log but weren't tracked.
    pub fn sync_min(&mut self, tag: &str, min: u64) {
        let counter = self.tags.entry(tag.to_string()).or_insert(0);
        if *counter < min {
            *counter = min;
        }
    }

    pub fn next_id(&mut self, tag: &str) -> u64 {
        let counter = self.tags.entry(tag.to_string()).or_insert(0);
        *counter += 1;
        *counter
    }
}
