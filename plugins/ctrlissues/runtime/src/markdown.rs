use serde_json::Value;
use sha2::{Digest, Sha256};
#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

pub fn publish(
    data: &Path,
    state: &Value,
    history: &[(u64, String)],
) -> Result<PathBuf, Box<dyn std::error::Error>> {
    let session = state
        .get("session_id")
        .and_then(Value::as_str)
        .ok_or("state.session_id required")?;
    let plans = data.join("plans");
    crate::private_fs::create_private_dir_all(&plans)?;
    let digest = Sha256::digest(session.as_bytes());
    let name: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    let path = plans.join(format!("{name}.md"));
    let body = render(state, history);
    if fs::read(&path).ok().as_deref() == Some(body.as_bytes()) {
        return Ok(path);
    }
    let tmp = plans.join(format!(".{name}.{}.{}.tmp", std::process::id(), now()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&tmp)?;
    set_mode(&tmp, 0o600)?;
    file.write_all(body.as_bytes())?;
    file.sync_all()?;
    drop(file);
    fs::rename(&tmp, &path)?;
    set_mode(&path, 0o600)?;
    Ok(path)
}

fn render(state: &Value, history: &[(u64, String)]) -> String {
    let bp = state.get("blueprint").filter(|v| !v.is_null());
    let objective = bp
        .and_then(|v| v.get("objective"))
        .and_then(Value::as_str)
        .unwrap_or("unplanned");
    let phase = state
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or("blueprint");
    let revision = state.get("revision").and_then(Value::as_u64).unwrap_or(0);
    let next = state
        .pointer("/cursor/next")
        .and_then(Value::as_str)
        .unwrap_or("plan a blueprint");
    let mut out = format!("# CtrlIssues task\n\n## objective\n{objective}\n\nrevision: {revision}\nphase: {phase}\nexact next action: {next}\n\n");
    if let Some(b) = bp {
        out.push_str("## components\n");
        let comps = b
            .get("components")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let deps = b.get("dependencies").and_then(Value::as_object);
        for c in &comps {
            let id = c.as_str().unwrap_or("");
            let d = deps
                .and_then(|x| x.get(id))
                .map(Value::to_string)
                .unwrap_or_else(|| "[]".into());
            out.push_str(&format!("- {id} (dependencies: {d})\n"));
        }
        out.push_str("\n## layer progress\n");
        let marks = state.get("marks").and_then(Value::as_object);
        for layer in b
            .get("layers")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
        {
            let l = layer.as_str().unwrap_or("");
            out.push_str(&format!("### {l}\n\n"));
            for c in &comps {
                let id = c.as_str().unwrap_or("");
                let ev = marks
                    .and_then(|m| m.get(l))
                    .and_then(|m| m.get(id))
                    .and_then(Value::as_str);
                out.push_str(&format!(
                    "- [{}] {}{}\n",
                    if ev.is_some() { "x" } else { " " },
                    id,
                    ev.map(|e| format!(": {e}")).unwrap_or_default()
                ));
            }
            out.push('\n');
        }
    }
    out.push_str("\n## open repair issues\n");
    if let Some(issues) = state.get("issues").and_then(Value::as_array) {
        for i in issues
            .iter()
            .filter(|i| i.get("resolved").and_then(Value::as_bool) != Some(true))
        {
            out.push_str(&format!(
                "- {}: {}\n",
                i.get("component").and_then(Value::as_str).unwrap_or("?"),
                i.get("reason").and_then(Value::as_str).unwrap_or("")
            ));
        }
    }
    if !history.is_empty() {
        out.push_str("\n## recent superseded revisions\n");
        for (r, reason) in history.iter().rev().take(8).rev() {
            out.push_str(&format!("- revision {r}: {reason}\n"));
        }
    }
    out
}
fn now() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
}
fn set_mode(path: &Path, mode: u32) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(mode))?;
    }
    Ok(())
}
