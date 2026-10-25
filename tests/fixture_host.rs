// Development-only driver: same wire/CLI implementation, isolated executable paths.
use ahu_cmux_session_host_plugin::{Cli, Host, serve};
fn main() -> std::process::ExitCode {
    let paths: Vec<_> = std::env::args_os()
        .skip(1)
        .map(std::path::PathBuf::from)
        .collect();
    if paths.len() != 3 {
        return std::process::ExitCode::from(2);
    }
    let provider = Cli::from_paths(paths[0].clone(), paths[1].clone(), paths[2].clone());
    match serve(
        &mut Host::new(provider),
        &mut std::io::stdin().lock(),
        &mut std::io::stdout().lock(),
    ) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(_) => std::process::ExitCode::from(2),
    }
}
