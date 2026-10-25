use ahu_cmux_session_host_plugin::{Cli, Host, serve};
fn main() -> std::process::ExitCode {
    if std::env::args_os().skip(1).collect::<Vec<_>>() != ["--ahu-host-protocol=2"] {
        return std::process::ExitCode::from(2);
    }
    let result = Cli::discover().and_then(|provider| {
        serve(
            &mut Host::new(provider),
            &mut std::io::stdin().lock(),
            &mut std::io::stdout().lock(),
        )
    });
    if result.is_ok() {
        std::process::ExitCode::SUCCESS
    } else {
        std::process::ExitCode::from(2)
    }
}
