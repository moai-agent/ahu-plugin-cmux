use crate::json;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::time::{Duration, Instant};

pub const METHODS: [&str; 9] = [
    "create_task_session",
    "verify_task_session",
    "select_task_session",
    "close_task_session",
    "set_task_status",
    "expand_task_session",
    "place_coordinator",
    "set_agent_metadata",
    "open_markdown",
];

#[derive(Debug)]
pub struct Error {
    pub code: &'static str,
    pub message: &'static str,
}
impl Error {
    pub fn failed(message: &'static str) -> Self {
        Self {
            code: "failed",
            message,
        }
    }
    fn unknown(mut self) -> Self {
        self.code = "outcome_unknown";
        self
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message)
    }
}
impl std::error::Error for Error {}
type Result<T> = std::result::Result<T, Error>;

/// Explicit provider boundary; implementations must bound duration and output.
pub trait Provider {
    fn command(&mut self, args: &[String], timeout: Duration) -> Result<Vec<u8>>;
    fn repository_identity(&mut self, directory: &str, timeout: Duration)
    -> Result<Option<String>>;
}
#[derive(Clone, Debug)]
struct Group {
    id: String,
    name: String,
    members: Vec<String>,
}
#[derive(Clone, Debug, PartialEq)]
struct Session {
    workspace: String,
    window: String,
    group: String,
}
struct WorkspaceRequest<'a> {
    title: &'a str,
    summary: &'a str,
    cwd: &'a str,
    command: &'a str,
    focus: bool,
}
#[derive(Debug, PartialEq)]
enum Ownership {
    Absent,
    Mismatch,
    Owned(Session),
}

pub(crate) fn require(p: &Value, keys: &[&str]) -> Result<()> {
    let object = p
        .as_object()
        .ok_or_else(|| Error::failed("invalid method parameters"))?;
    if object.len() != keys.len() || keys.iter().any(|k| !object.contains_key(*k)) {
        return Err(Error::failed("invalid method parameters"));
    }
    Ok(())
}
fn string(v: &Value) -> Result<&str> {
    v.as_str()
        .filter(|s| !s.is_empty() && s.len() <= 4096 && !s.chars().any(|c| c < ' '))
        .ok_or_else(|| Error::failed("invalid string"))
}
fn field<'a>(p: &'a Value, key: &str) -> Result<&'a str> {
    string(&p[key])
}
fn boolean(p: &Value, key: &str) -> Result<bool> {
    p[key]
        .as_bool()
        .ok_or_else(|| Error::failed("invalid boolean"))
}
fn rows<'a>(p: &'a Value, key: &str) -> Result<&'a Vec<Value>> {
    p[key]
        .as_array()
        .ok_or_else(|| Error::failed("provider inventory incomplete"))
}
fn parse_group(v: &Value) -> Result<Group> {
    let id = field(v, "id")?.to_owned();
    let anchor = field(v, "anchor_workspace_id")?;
    let members = rows(v, "member_workspace_ids")?
        .iter()
        .map(|v| string(v).map(str::to_owned))
        .collect::<Result<Vec<_>>>()?;
    if !members.iter().any(|m| m == anchor)
        || members.iter().collect::<BTreeSet<_>>().len() != members.len()
    {
        return Err(Error::failed("incomplete group membership"));
    }
    Ok(Group {
        id,
        name: v["name"].as_str().unwrap_or("").to_owned(),
        members,
    })
}
// Python's original reference digest used sorted JSON with ensure_ascii=True.
// Preserve those exact bytes, including UTF-16 surrogate escapes for non-BMP paths.
fn scope(identity: &str, directory: &str) -> String {
    let encoded =
        json!({"version":2,"repository_identity":identity,"directory":directory}).to_string();
    let mut ascii = String::new();
    for c in encoded.chars() {
        if c < '\u{7f}' {
            ascii.push(c);
        } else {
            for unit in c.encode_utf16(&mut [0; 2]) {
                ascii.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    format!("{:x}", Sha256::digest(ascii.as_bytes()))
}
fn reference(identity: &str, directory: &str, s: &Session) -> Result<String> {
    let text = format!(
        "cmux2:{}:{}:{}:{}",
        scope(identity, directory),
        s.workspace,
        s.window,
        s.group
    );
    parse_reference(&text)?;
    Ok(text)
}
fn parse_reference(text: &str) -> Result<(&str, Session)> {
    let parts: Vec<_> = text.split(':').collect();
    if text.len() > 256
        || parts.len() != 5
        || parts[0] != "cmux2"
        || parts[1].len() != 64
        || !parts[1]
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        || parts[2..].iter().any(|s| {
            s.is_empty()
                || s.len() > 64
                || !s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        })
    {
        return Err(Error::failed("invalid session reference"));
    }
    Ok((
        parts[1],
        Session {
            workspace: parts[2].into(),
            window: parts[3].into(),
            group: parts[4].into(),
        },
    ))
}

/// Host state with a shared 12-second operation deadline, including inventory/Git probes.
pub struct Host<P> {
    provider: P,
    deadline: Instant,
}
impl<P: Provider> Host<P> {
    pub fn new(provider: P) -> Self {
        Self {
            provider,
            deadline: Instant::now() + Duration::from_secs(12),
        }
    }
    pub(crate) fn reset_deadline(&mut self) {
        self.deadline = Instant::now() + Duration::from_secs(12);
    }
    fn remaining(&self, limit: Duration) -> Result<Duration> {
        let remaining = self.deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            Err(Error::failed("operation timed out"))
        } else {
            Ok(remaining.min(limit))
        }
    }
    fn command(&mut self, args: &[String], limit: Duration) -> Result<Vec<u8>> {
        let timeout = self.remaining(limit)?;
        self.provider.command(args, timeout)
    }
    fn rpc(&mut self, method: &str, params: Value) -> Result<Value> {
        let raw = self.command(
            &["rpc".into(), method.into(), params.to_string()],
            Duration::from_secs(12),
        )?;
        let value = json::parse(&raw)?;
        if !value.is_object() {
            return Err(Error::failed("invalid provider response"));
        }
        Ok(value)
    }
    fn repo(&mut self, directory: &str) -> Result<Option<String>> {
        let timeout = self.remaining(Duration::from_secs(3))?;
        self.provider.repository_identity(directory, timeout)
    }
    pub(crate) fn handshake(&mut self) -> Result<Value> {
        self.reset_deadline();
        self.command(&["ping".into()], Duration::from_secs(12))?;
        let raw = self.command(&["capabilities".into()], Duration::from_secs(5))?;
        let capabilities = json::parse(&raw)?;
        let advertised = rows(&capabilities, "capabilities")?;
        if advertised.iter().any(|v| !v.is_string())
            || [
                "workspace.groups.v1",
                "workspace.group_create.v1",
                "workspace.create_in_group.v1",
            ]
            .iter()
            .any(|c| !advertised.contains(&json!(c)))
        {
            return Err(Error::failed("required cmux capabilities unavailable"));
        }
        Ok(
            json!({"protocol_version":2,"host_api":1,"plugin_id":"cmux-host","capabilities":METHODS,"inherited_workspace":self.current_workspace()?}),
        )
    }
    fn workspaces(&mut self, window: &str) -> Result<BTreeMap<String, String>> {
        let v = self.rpc("workspace.list", json!({"window_id":window}))?;
        let mut out = BTreeMap::new();
        for row in rows(&v, "workspaces")? {
            let id = field(row, "id")?;
            let directory = match &row["current_directory"] {
                Value::String(s) => s.clone(),
                Value::Null => String::new(),
                _ => return Err(Error::failed("invalid workspace directory")),
            };
            if out.insert(id.into(), directory).is_some() {
                return Err(Error::failed("duplicate workspace identity"));
            }
        }
        Ok(out)
    }
    fn windows(&mut self) -> Result<Vec<String>> {
        let v = self.rpc("window.list", json!({}))?;
        let ids = rows(&v, "windows")?
            .iter()
            .map(|v| field(v, "id").map(str::to_owned))
            .collect::<Result<Vec<_>>>()?;
        if ids.iter().collect::<BTreeSet<_>>().len() != ids.len() {
            return Err(Error::failed("duplicate window identity"));
        }
        Ok(ids)
    }
    fn inventory(&mut self) -> Result<Vec<(String, String, String)>> {
        let mut out = Vec::new();
        let mut seen = BTreeSet::new();
        for window in self.windows()? {
            for (id, directory) in self.workspaces(&window)? {
                if !seen.insert(id.clone()) {
                    return Err(Error::failed("duplicate workspace identity"));
                }
                out.push((window.clone(), id, directory));
            }
        }
        Ok(out)
    }
    fn groups(&mut self, window: &str) -> Result<Vec<Group>> {
        let v = self.rpc("workspace.group.list", json!({"window_id":window}))?;
        let groups = rows(&v, "groups")?
            .iter()
            .map(parse_group)
            .collect::<Result<Vec<_>>>()?;
        if groups.iter().map(|g| &g.id).collect::<BTreeSet<_>>().len() != groups.len() {
            return Err(Error::failed("duplicate group identity"));
        }
        Ok(groups)
    }
    fn current_workspace(&mut self) -> Result<Option<String>> {
        let v = self.rpc("workspace.list", json!({}))?;
        let mut selected = None;
        let mut ids = BTreeSet::new();
        for row in rows(&v, "workspaces")? {
            let id = field(row, "id")?;
            if !ids.insert(id) {
                return Err(Error::failed("duplicate workspace identity"));
            }
            if row["selected"] == true && selected.replace(id.into()).is_some() {
                return Err(Error::failed("selected workspace ambiguous"));
            }
        }
        Ok(selected)
    }
    fn current_window(&mut self) -> Result<String> {
        let v = self.rpc("workspace.list", json!({}))?;
        Ok(field(&v, "window_id")?.into())
    }
    fn window_for(&mut self, workspace: &str) -> Result<String> {
        self.inventory()?
            .into_iter()
            .find(|(_, id, _)| id == workspace)
            .map(|(window, _, _)| window)
            .ok_or_else(|| Error::failed("workspace window absent"))
    }
    fn ensure_group(&mut self, identity: &str, name: &str, root: &str) -> Result<(Group, String)> {
        let window = self.current_window()?;
        let current = self.current_workspace()?;
        let available = self.groups(&window)?;
        let inventory = self.workspaces(&window)?;
        let mut candidates = Vec::new();
        for group in available.into_iter().filter(|g| g.name == name) {
            let mut matches = false;
            for member in &group.members {
                if let Some(directory) = inventory.get(member)
                    && self.repo(directory)?.as_deref() == Some(identity)
                {
                    matches = true;
                    break;
                }
            }
            if matches {
                candidates.push(group);
            }
        }
        if let Some(group) = candidates
            .iter()
            .find(|g| current.as_ref().is_some_and(|id| g.members.contains(id)))
        {
            return Ok((group.clone(), window));
        }
        if candidates.len() > 1 {
            return Err(Error::failed(
                "multiple cmux groups match repository; run Ahu from the intended group",
            ));
        }
        if let Some(group) = candidates.pop() {
            return Ok((group, window));
        }
        let value = self.rpc(
            "workspace.group.create",
            json!({"name":name,"cwd":root,"window_id":window}),
        )?;
        Ok((parse_group(&value["group"])?, window))
    }
    fn verify_workspace(&mut self, s: &Session, directory: &str) -> Result<Ownership> {
        let inventory = self.workspaces(&s.window)?;
        match inventory.get(&s.workspace) {
            None => return Ok(Ownership::Absent),
            Some(actual) if actual != directory => return Ok(Ownership::Mismatch),
            _ => {}
        }
        if self.window_for(&s.workspace)? != s.window {
            return Ok(Ownership::Mismatch);
        }
        let memberships: Vec<_> = self
            .groups(&s.window)?
            .into_iter()
            .filter(|g| g.members.contains(&s.workspace))
            .map(|g| g.id)
            .collect();
        if memberships != [s.group.clone()] {
            return Ok(Ownership::Mismatch);
        }
        Ok(Ownership::Owned(s.clone()))
    }
    fn ownership(&mut self, p: &Value) -> Result<Ownership> {
        let identity = field(p, "repository_identity")?;
        let directory = field(p, "expected_directory")?;
        let raw = p["session_ref"]
            .as_str()
            .ok_or_else(|| Error::failed("invalid session reference"))?;
        let session = if raw.is_empty() {
            None
        } else {
            let (digest, s) = parse_reference(raw)?;
            if digest != scope(identity, directory) {
                return Ok(Ownership::Mismatch);
            }
            Some(s)
        };
        let candidates: Vec<_> = self
            .inventory()?
            .into_iter()
            .filter(|(_, id, dir)| {
                dir == directory || session.as_ref().is_some_and(|s| s.workspace == *id)
            })
            .collect();
        if candidates.is_empty() {
            return Ok(Ownership::Absent);
        }
        let s = session.ok_or_else(|| {
            Error::failed("unrecorded workspace ownership requires operator reconciliation")
                .unknown()
        })?;
        if candidates != [(s.window.clone(), s.workspace.clone(), directory.into())]
            || self.repo(directory)?.as_deref() != Some(identity)
        {
            return Ok(Ownership::Mismatch);
        }
        self.verify_workspace(&s, directory)
    }
    fn owned(&mut self, p: &Value) -> Result<Session> {
        match self.ownership(p)? {
            Ownership::Owned(s) => Ok(s),
            _ => Err(Error::failed(
                "session absent or ownership does not match task worktree",
            )),
        }
    }
    fn created_workspace(
        &mut self,
        group: &Group,
        window: &str,
        request: WorkspaceRequest<'_>,
    ) -> Result<Session> {
        let WorkspaceRequest {
            title,
            summary,
            cwd,
            command,
            focus,
        } = request;
        let initial = self
            .groups(window)?
            .into_iter()
            .find(|g| g.id == group.id)
            .ok_or(Error {
                code: "not_created",
                message: "group not found",
            })?;
        let args = [
            "new-workspace",
            "--name",
            title,
            "--description",
            summary,
            "--cwd",
            cwd,
            "--command",
            command,
            "--group",
            &group.id,
            "--group-placement",
            "end",
            "--focus",
            if focus { "true" } else { "false" },
            "--window",
            window,
        ]
        .map(str::to_owned);
        self.command(&args, Duration::from_secs(4))
            .map_err(Error::unknown)?;
        // Only read-only observation repeats. The create command is dispatched exactly once.
        let observed = (|| {
            loop {
                self.remaining(Duration::from_secs(12))?;
                let current = self
                    .groups(window)?
                    .into_iter()
                    .find(|g| g.id == group.id)
                    .ok_or_else(|| Error::failed("group disappeared after workspace request"))?;
                let inventory = self.workspaces(window)?;
                let matches: Vec<_> = current
                    .members
                    .iter()
                    .filter(|id| {
                        !initial.members.contains(id)
                            && inventory.get(*id).is_some_and(|dir| dir == cwd)
                    })
                    .collect();
                if matches.len() > 1 {
                    return Err(Error::failed("workspace creation ambiguous"));
                }
                if let Some(id) = matches.first() {
                    let actual_window = self.window_for(id)?;
                    if actual_window != window {
                        return Err(Error::failed(
                            "created workspace appeared in unexpected window",
                        ));
                    }
                    let s = Session {
                        workspace: (*id).clone(),
                        window: actual_window,
                        group: group.id.clone(),
                    };
                    if self.verify_workspace(&s, cwd)? != Ownership::Owned(s.clone()) {
                        return Err(Error::failed("created workspace ownership changed"));
                    }
                    return Ok(s);
                }
                std::thread::sleep(self.remaining(Duration::from_millis(150))?);
            }
        })();
        observed.map_err(Error::unknown)
    }
    fn repository_params<'a>(&mut self, p: &'a Value) -> Result<(&'a str, &'a str, &'a str)> {
        let identity = field(p, "repository_identity")?;
        let root = field(p, "repository_root")?;
        let name = field(p, "repository_name")?;
        if identity.len() != 16
            || !identity
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || !Path::new(root).is_absolute()
        {
            return Err(Error::failed("invalid repository identity or root"));
        }
        if self.repo(root)?.as_deref() != Some(identity) {
            return Err(Error::failed("repository identity does not match root"));
        }
        Ok((identity, name, root))
    }
    fn create(&mut self, p: &Value) -> Result<Value> {
        require(
            p,
            &[
                "repository_identity",
                "repository_name",
                "repository_root",
                "title",
                "summary",
                "worktree",
                "startup_command",
                "focus",
            ],
        )?;
        // Validate all caller data before any group/workspace mutation.
        let title = field(p, "title")?;
        let summary = field(p, "summary")?;
        let worktree = field(p, "worktree")?;
        let command = field(p, "startup_command")?;
        let focus = boolean(p, "focus")?;
        if !Path::new(worktree).is_absolute() {
            return Err(Error::failed("invalid worktree path"));
        }
        let (identity, name, root) = self.repository_params(p)?;
        if self.repo(worktree)?.as_deref() != Some(identity) {
            return Err(Error::failed("worktree does not belong to repository"));
        }
        let (group, window) = self.ensure_group(identity, name, root)?;
        let s = self.created_workspace(
            &group,
            &window,
            WorkspaceRequest {
                title,
                summary,
                cwd: worktree,
                command,
                focus,
            },
        )?;
        let reference = reference(identity, worktree, &s).map_err(Error::unknown)?;
        Ok(json!({"session_ref":reference}))
    }
    fn metadata(&mut self, workspace: &str, agent: &str, harness: &str, model: &str) -> Result<()> {
        self.command(
            &[
                "set-status".into(),
                "ahu.agent".into(),
                format!("{agent} · {harness} · {model}"),
                "--workspace".into(),
                workspace.into(),
            ],
            Duration::from_secs(12),
        )?;
        Ok(())
    }
    fn coordinator(&mut self, p: &Value) -> Result<Value> {
        require(
            p,
            &[
                "repository_identity",
                "repository_name",
                "repository_root",
                "label",
                "harness",
                "model",
                "startup_command",
            ],
        )?;
        let label = field(p, "label")?;
        let title = format!("{label} coordinator");
        if title.len() > 4096 {
            return Err(Error::failed("coordinator title exceeds limit"));
        }
        let harness = field(p, "harness")?;
        let model = field(p, "model")?;
        let command = field(p, "startup_command")?;
        let (identity, name, root) = self.repository_params(p)?;
        let Some(caller) = self.current_workspace()? else {
            return Ok(json!({"notes":[],"opened_session":false}));
        };
        let (group, window) = self.ensure_group(identity, name, root)?;
        let caller_window = self.window_for(&caller)?;
        let other = self
            .groups(&caller_window)?
            .iter()
            .any(|g| g.id != group.id && g.members.contains(&caller));
        if other {
            let s = self.created_workspace(
                &group,
                &window,
                WorkspaceRequest {
                    title: &title,
                    summary: &title,
                    cwd: root,
                    command,
                    focus: true,
                },
            )?;
            self.metadata(&s.workspace, "director", harness, model)
                .map_err(Error::unknown)?;
            return Ok(
                json!({"notes":["the invoking session already belongs to another group; the provider opened a coordinator session under this repository"],"opened_session":true}),
            );
        }
        let actual = self.window_for(&caller)?;
        if actual != window {
            self.rpc(
                "workspace.move_to_window",
                json!({"workspace_id":caller,"window_id":window}),
            )?;
        }
        self.rpc(
            "workspace.group.add",
            json!({"group_id":group.id,"workspace_id":caller,"window_id":window}),
        )?;
        if !self
            .groups(&window)?
            .iter()
            .any(|g| g.id == group.id && g.members.contains(&caller))
        {
            return Err(Error::failed("coordinator group membership not confirmed").unknown());
        }
        self.metadata(&caller, "director", harness, model)?;
        self.rpc("workspace.group.expand", json!({"group_id":group.id}))?;
        Ok(json!({"notes":[],"opened_session":false}))
    }
    /// Execute one advertised operation. Unknown keys are refused before provider access.
    pub fn dispatch(&mut self, method: &str, p: &Value) -> Result<Value> {
        self.reset_deadline();
        const OWN: &[&str] = &["session_ref", "repository_identity", "expected_directory"];
        match method {
            "create_task_session" => self.create(p),
            "place_coordinator" => self.coordinator(p),
            "verify_task_session" => {
                require(p, OWN)?;
                Ok(json!(match self.ownership(p)? {
                    Ownership::Absent => "absent",
                    Ownership::Mismatch => "mismatch",
                    Ownership::Owned(_) => "owned",
                }))
            }
            "close_task_session" => {
                require(p, OWN)?;
                match self.ownership(p)? {
                    Ownership::Owned(s) => {
                        self.rpc("workspace.close", json!({"workspace_id":s.workspace}))?;
                    }
                    Ownership::Mismatch => {
                        return Err(Error::failed(
                            "session ownership does not match task worktree",
                        ));
                    }
                    Ownership::Absent => {}
                }
                Ok(Value::Null)
            }
            "select_task_session" | "expand_task_session" => {
                require(p, OWN)?;
                let s = self.owned(p)?;
                if method == "select_task_session" {
                    self.rpc("workspace.select", json!({"workspace_id":s.workspace}))?;
                } else {
                    self.rpc("workspace.group.expand", json!({"group_id":s.group}))?;
                }
                Ok(Value::Null)
            }
            "set_task_status" => {
                require(
                    p,
                    &[
                        "session_ref",
                        "repository_identity",
                        "expected_directory",
                        "value",
                    ],
                )?;
                let value = field(p, "value")?;
                let s = self.owned(p)?;
                self.command(
                    &[
                        "set-status".into(),
                        "ahu.task".into(),
                        value.into(),
                        "--workspace".into(),
                        s.workspace,
                    ],
                    Duration::from_secs(12),
                )?;
                Ok(Value::Null)
            }
            "set_agent_metadata" => {
                require(
                    p,
                    &[
                        "session_ref",
                        "repository_identity",
                        "expected_directory",
                        "agent",
                        "harness",
                        "model",
                    ],
                )?;
                let agent = field(p, "agent")?;
                let harness = field(p, "harness")?;
                let model = field(p, "model")?;
                let s = self.owned(p)?;
                self.metadata(&s.workspace, agent, harness, model)?;
                Ok(Value::Null)
            }
            "open_markdown" => {
                require(p, &["path", "focus"])?;
                let path = field(p, "path")?;
                let focus = boolean(p, "focus")?;
                let v = self.rpc("markdown.open", json!({"path":path,"focus":focus}))?;
                Ok(json!(field(&v, "surface_id")?))
            }
            _ => Err(Error::failed("unsupported method")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn references_are_strict_and_compatible() {
        let s = Session {
            workspace: "w".into(),
            window: "win".into(),
            group: "g".into(),
        };
        let text = reference("0123456789abcdef", "/work/task", &s).unwrap();
        assert_eq!(parse_reference(&text).unwrap().1, s);
        assert!(
            reference("0123456789abcdef", &format!("/{}", "long/".repeat(100)), &s)
                .unwrap()
                .len()
                < 256
        );
        for bad in [
            "",
            "cmux1:bad",
            "cmux2:x:w:win:g",
            &text.replace(":w:", ":w_x:"),
        ] {
            assert!(parse_reference(bad).is_err());
        }
        // Independent Python oracle vectors, including ASCII DEL and surrogate pairs.
        assert_eq!(
            scope("0123456789abcdef", "/work/task"),
            "89c6257d755ebdeca5db8ef65bcb3c9981060b70096ebc23cc25b30e80157b88"
        );
        assert_eq!(
            scope("0123456789abcdef", "/é/😀/\u{7f}"),
            "b5feb56bf843c7c50144d19331fe51f2f85f42c5e0efcbdd7b3b6830bbf30d56"
        );
    }
    #[test]
    fn group_inventory_requires_unique_members_and_anchor() {
        for v in [
            json!({"id":"g","anchor_workspace_id":"a","member_workspace_ids":[]}),
            json!({"id":"g","anchor_workspace_id":"a","member_workspace_ids":["a","a"]}),
            json!({"id":"g","anchor_workspace_id":"a","member_workspace_ids":[1]}),
        ] {
            assert!(parse_group(&v).is_err());
        }
        assert!(
            parse_group(&json!({"id":"g","anchor_workspace_id":"a","member_workspace_ids":["a"]}))
                .is_ok()
        );
    }
}
