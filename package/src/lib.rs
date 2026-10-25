//! Standalone cmux Host V2 adapter. Provider operations are never retried.
mod host;
mod json;
mod process;
mod wire;

pub use host::{Error, Host, METHODS, Provider};
pub use process::Cli;
pub use wire::serve;
