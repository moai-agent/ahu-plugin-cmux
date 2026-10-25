use crate::{Error, Provider};
use std::ffi::{CStr, OsStr, OsString};
use std::io::Read;
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStringExt;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const LIMIT: usize = 1024 * 1024;
struct ChildGroup {
    child: Child,
    reaped: bool,
}
impl ChildGroup {
    fn kill_group(&self) {
        // SAFETY: the unreaped child's PID pins the identity of its private group.
        unsafe {
            libc::kill(-(self.child.id() as i32), libc::SIGKILL);
        }
    }
    fn exited(&self) -> Result<bool, Error> {
        // WNOWAIT observes completion without releasing the PID for reuse before
        // group cleanup. Child::try_wait would reap it too early.
        let mut info = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
        // SAFETY: info is writable; P_PID selects our unreaped direct child.
        let status = unsafe {
            libc::waitid(
                libc::P_PID,
                self.child.id(),
                info.as_mut_ptr(),
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
            )
        };
        if status < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                return Ok(false);
            }
            return Err(Error::failed("provider wait failed"));
        }
        // SAFETY: initialized zeroed storage; successful waitid writes siginfo.
        Ok(unsafe { info.assume_init().si_pid() } != 0)
    }
    fn finish(&mut self) -> Result<bool, Error> {
        self.kill_group();
        let status = self
            .child
            .wait()
            .map_err(|_| Error::failed("provider wait failed"))?;
        self.reaped = true;
        Ok(status.success())
    }
}
impl Drop for ChildGroup {
    fn drop(&mut self) {
        if !self.reaped {
            self.kill_group();
            let _ = self.child.wait();
        }
    }
}

/// Fixed executable paths and OS-account HOME; no inherited socket or token overrides.
pub struct Cli {
    cmux: PathBuf,
    git: PathBuf,
    home: PathBuf,
}
impl Cli {
    /// Construct an explicit provider for embedders and isolated integration fixtures.
    /// The shipped executable always uses `discover`; it accepts no path overrides.
    pub fn from_paths(cmux: PathBuf, git: PathBuf, home: PathBuf) -> Self {
        Self { cmux, git, home }
    }

    pub fn discover() -> Result<Self, Error> {
        let home = account_home()?;
        let mut paths: Vec<PathBuf> = [
            "/opt/homebrew/bin/cmux",
            "/usr/local/bin/cmux",
            "/usr/bin/cmux",
            "/bin/cmux",
            "/home/linuxbrew/.linuxbrew/bin/cmux",
        ]
        .iter()
        .map(PathBuf::from)
        .collect();
        paths.extend([home.join(".local/bin/cmux"), home.join("bin/cmux")]);
        let cmux = paths
            .into_iter()
            .find(|p| executable(p))
            .ok_or_else(|| Error::failed("cmux executable unavailable"))?;
        let git = [
            "/usr/bin/git",
            "/bin/git",
            "/opt/homebrew/bin/git",
            "/usr/local/bin/git",
        ]
        .iter()
        .map(PathBuf::from)
        .find(|p| executable(p))
        .ok_or_else(|| Error::failed("git executable unavailable"))?;
        Ok(Self { cmux, git, home })
    }
}
fn executable(path: &Path) -> bool {
    path.metadata()
        .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}
fn account_home() -> Result<PathBuf, Error> {
    // getpwuid_r stores all strings in caller-owned memory; retain it until copied.
    let mut buffer = vec![0u8; 65536];
    let mut entry = std::mem::MaybeUninit::<libc::passwd>::uninit();
    let mut result = std::ptr::null_mut();
    // SAFETY: valid writable entry, result and buffer; size matches allocation.
    let status = unsafe {
        libc::getpwuid_r(
            libc::getuid(),
            entry.as_mut_ptr(),
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut result,
        )
    };
    if status != 0 || result.is_null() {
        return Err(Error::failed("OS account home unavailable"));
    }
    // SAFETY: successful getpwuid_r initialized the caller-owned entry.
    let directory = unsafe { (*result).pw_dir };
    if directory.is_null() {
        return Err(Error::failed("OS account home unavailable"));
    }
    // SAFETY: getpwuid_r supplies a non-null NUL-terminated string in buffer.
    let bytes = unsafe { CStr::from_ptr(directory) }.to_bytes().to_vec();
    let home = PathBuf::from(OsString::from_vec(bytes));
    if !home.is_absolute() {
        return Err(Error::failed("OS account home is not absolute"));
    }
    Ok(home)
}
fn run(
    path: &Path,
    args: &[&OsStr],
    home: &Path,
    timeout: Duration,
) -> Result<(bool, Vec<u8>), Error> {
    let deadline = Instant::now() + timeout;
    let mut command = Command::new(path);
    command
        .args(args)
        .current_dir("/")
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", home)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    // SAFETY: the post-fork callback only invokes async-signal-safe setsid and
    // captures errno. A fresh session preserves the original CLI isolation and
    // gives the child a private process group whose ID equals its PID.
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() < 0 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
    let child = command
        .spawn()
        .map_err(|_| Error::failed("cannot start provider process"))?;
    let mut child = ChildGroup {
        child,
        reaped: false,
    };
    let mut stdout = child
        .child
        .stdout
        .take()
        .ok_or_else(|| Error::failed("provider output unavailable"))?;
    let fd = stdout.as_raw_fd();
    // SAFETY: fd is a live, exclusively owned pipe descriptor.
    if unsafe { libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) } < 0 {
        return Err(Error::failed("cannot configure provider pipe"));
    }
    let mut output = Vec::new();
    let mut eof = false;
    loop {
        if Instant::now() >= deadline {
            return Err(Error::failed("provider operation timed out"));
        }
        if !eof {
            let mut descriptor = libc::pollfd {
                fd,
                events: libc::POLLIN,
                revents: 0,
            };
            // SAFETY: descriptor points to one initialized pollfd for a live pipe.
            let ready = unsafe { libc::poll(&mut descriptor, 1, 10) };
            if ready < 0
                && std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted
            {
                return Err(Error::failed("provider poll failed"));
            }
            if ready > 0 {
                let mut buffer = [0u8; 8192];
                match stdout.read(&mut buffer) {
                    Ok(0) => eof = true,
                    Ok(n) => {
                        if output.len() + n > LIMIT {
                            return Err(Error::failed("provider response exceeded limit"));
                        }
                        output.extend_from_slice(&buffer[..n]);
                    }
                    Err(e)
                        if matches!(
                            e.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                        ) => {}
                    Err(_) => return Err(Error::failed("provider read failed")),
                }
            }
        } else if child.exited()? {
            return Ok((child.finish()?, output));
        } else {
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}
impl Provider for Cli {
    fn command(&mut self, args: &[String], timeout: Duration) -> Result<Vec<u8>, Error> {
        let args: Vec<_> = args.iter().map(OsStr::new).collect();
        let (success, output) = run(&self.cmux, &args, &self.home, timeout)?;
        if !success {
            return Err(Error::failed("cmux operation failed"));
        }
        Ok(output)
    }
    fn repository_identity(
        &mut self,
        directory: &str,
        timeout: Duration,
    ) -> Result<Option<String>, Error> {
        use sha2::{Digest, Sha256};
        use std::os::unix::ffi::OsStrExt;
        let args = [
            "-C",
            directory,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ]
        .map(OsStr::new);
        let (success, output) = run(
            &self.git,
            &args,
            &self.home,
            timeout.min(Duration::from_secs(3)),
        )?;
        if !success {
            return Ok(None);
        }
        let path = std::str::from_utf8(&output)
            .map_err(|_| Error::failed("invalid git path"))?
            .trim();
        if path.is_empty() {
            return Err(Error::failed("empty git path"));
        }
        let common = std::fs::canonicalize(path)
            .map_err(|_| Error::failed("cannot resolve git directory"))?;
        Ok(Some(
            format!("{:x}", Sha256::digest(common.as_os_str().as_bytes()))[..16].to_owned(),
        ))
    }
}
