use crate::config::Config;
use crate::error::{Result, TlError};
use fs2::FileExt;
use std::fs::{File, OpenOptions};

pub struct FileLock {
    file: File,
}

impl FileLock {
    pub fn acquire() -> Result<Self> {
        let path = Config::lock_path();
        let file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .open(&path)?;

        file.lock_exclusive()
            .map_err(|e| TlError::Lock(format!("failed to acquire lock: {}", e)))?;

        Ok(Self { file })
    }
}

impl Drop for FileLock {
    fn drop(&mut self) {
        let _ = self.file.unlock();
    }
}
