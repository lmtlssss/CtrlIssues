use crate::state::Store;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::{self, Read, Write},
    path::Path,
};

const INPUT_LIMIT: u64 = 131_072;

pub fn run(data: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let mut body = Vec::new();
    io::stdin().take(INPUT_LIMIT + 1).read_to_end(&mut body)?;
    if body.len() as u64 > INPUT_LIMIT {
        return Err("hook input exceeds 128 KiB".into());
    }
    let input: Value = serde_json::from_slice(&body)?;
    let event = input
        .get("hook_event_name")
        .and_then(Value::as_str)
        .ok_or("hook_event_name required")?;
    let id = if event == "SubagentStart" {
        input
            .get("session_id")
            .and_then(Value::as_str)
            .map(str::to_owned)
    } else {
        input
            .get("session_id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .or_else(|| std::env::var("CODEX_THREAD_ID").ok())
    };
    let Some(id) = id else {
        if event == "SubagentStart" {
            return Ok(());
        }
        return Err("explicit session identity required".into());
    };
    let mut store = Store::open(data, &id)?;
    let compact = store.compact_status()?;
    let useful = compact
        .pointer("/objective")
        .and_then(Value::as_str)
        .is_some_and(|x| !x.is_empty());
    let active = useful && compact["phase"] != "complete";
    if event == "PreCompact" && useful {
        write_snapshot(data, &compact)?;
    }
    if event == "PostToolUse"
        && input.get("tool_name").and_then(Value::as_str) == Some("apply_patch")
        && patch_succeeded(input.get("tool_response"))
        && compact
            .pointer("/objective")
            .and_then(Value::as_str)
            .is_some_and(|x| !x.is_empty())
    {
        let _ = store.changed("successful apply_patch".into())?;
    }
    if event == "PreToolUse" && active {
        if recognized_test_tool_input(&input) {
            deny_pretool(
                "recognized test command: wrap it with ctrlissues check KIND LABEL -- COMMAND",
            );
            return Ok(());
        }
        if let Some((tool_name, tool_input)) = tracked_observation_input(&input) {
            let cwd = std::env::current_dir()?
                .canonicalize()?
                .to_string_lossy()
                .into_owned();
            if let Some(receipt) = store.completed_observation(tool_name, tool_input, &cwd)? {
                let reason = format!(
                    "completed observation {} already records a {} result at generation {} (outcome {}); reuse the prior result",
                    receipt["receipt_id"], receipt["status"], receipt["generation"], receipt["outcome_sha256"]
                );
                deny_pretool(&reason);
                return Ok(());
            }
        }
    }
    if event == "PostToolUse" && active {
        if let (Some((tool_name, tool_input)), Some((succeeded, outcome))) = (
            tracked_observation_input(&input),
            known_outcome(input.get("tool_response")),
        ) {
            let cwd = std::env::current_dir()?
                .canonicalize()?
                .to_string_lossy()
                .into_owned();
            let _ = store.record_observation(tool_name, tool_input, &cwd, outcome, succeeded)?;
        }
    }
    if event == "PreToolUse" && active {
        return Ok(());
    } else if matches!(event, "SessionStart" | "UserPromptSubmit" | "SubagentStart") && useful {
        let next = cut(
            compact
                .pointer("/cursor/next")
                .and_then(Value::as_str)
                .unwrap_or("continue from the task cursor"),
            180,
        );
        let layer = compact
            .pointer("/cursor/layer")
            .and_then(Value::as_str)
            .unwrap_or("none");
        let objective = cut(compact["objective"].as_str().unwrap_or(""), 180);
        let session = compact["session_id"].as_str().unwrap_or("");
        let revision = compact["revision"].as_u64().unwrap_or(0);
        let exe = std::env::current_exe()
            .ok()
            .and_then(|path| path.into_os_string().into_string().ok())
            .unwrap_or_else(|| "ctrlissues".into());
        let prefix = format!(
            "{} --data-dir {} --session {} --revision {}",
            shell_quote(&exe),
            shell_quote(&data.display().to_string()),
            shell_quote(session),
            revision
        );
        let plan = compact["plan_path"].as_str().unwrap_or("");
        let snapshot = snapshot_path(data, &compact).display().to_string();
        let context = format!(
            "CtrlIssues command={} task={} revision={} generation={} phase={} layer={} next={} plan={} snapshot={} objective={}",
            prefix,
            compact["session_id"],
            revision,
            compact["generation"],
            compact["phase"],
            layer,
            next,
            shell_quote(plan),
            shell_quote(&snapshot),
            objective
        );
        println!(
            "{}",
            json!({"hookSpecificOutput":{"hookEventName":event,"additionalContext":cut(&context,900)}})
        );
    } else if event == "PreCompact" && useful {
        println!(
            "{}",
            json!({"hookSpecificOutput":{"hookEventName":"PreCompact","additionalContext":"CtrlIssues saved the bounded task cursor; continue with native compaction."}})
        );
    }
    Ok(())
}

fn write_snapshot(data: &Path, compact: &Value) -> Result<(), Box<dyn std::error::Error>> {
    let dir = data.join("snapshots");
    crate::private_fs::create_private_dir_all(&dir)?;
    let session = compact["session_id"]
        .as_str()
        .ok_or("canonical session id missing")?;
    let target = snapshot_path(data, compact);
    let key = target
        .file_stem()
        .and_then(|stem| stem.to_str())
        .ok_or("snapshot key invalid")?;
    let document = json!({"schema":"ctrlissues.snapshot.v1","session_id":session,"cursor":compact});
    let temp = dir.join(format!(
        ".{key}.{}.{}.tmp",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    ));
    let bytes = serde_json::to_vec(&document)?;
    if target.exists() {
        return Ok(());
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temp)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&temp, fs::Permissions::from_mode(0o600))?;
    }
    drop(file);
    if let Err(error) = fs::rename(&temp, &target) {
        if target.exists() {
            fs::remove_file(&temp)?;
        } else {
            return Err(error.into());
        }
    }
    Ok(())
}

fn recognized_test_tool_input(input: &Value) -> bool {
    let Some((_, command)) = direct_command(input) else {
        return false;
    };
    shell_segments(command)
        .iter()
        .any(|tokens| recognized_tokens(tokens))
}

pub fn recognized_argv(args: &[String]) -> bool {
    recognized_tokens(args)
}

fn direct_command<'a>(input: &'a Value) -> Option<(&'a str, &'a str)> {
    let tool = input.get("tool_name")?.as_str()?;
    if !matches!(tool, "exec_command" | "functions.exec_command") {
        return None;
    }
    let command = input
        .pointer("/tool_input/command")
        .and_then(Value::as_str)
        .or_else(|| input.pointer("/tool_input/cmd").and_then(Value::as_str))?;
    Some((tool, command))
}

fn tracked_observation_input(input: &Value) -> Option<(&str, &Value)> {
    let tool = input.get("tool_name")?.as_str()?;
    let tool_input = input.get("tool_input").unwrap_or(&Value::Null);
    if matches!(
        tool,
        "read_file"
            | "read_text_file"
            | "list_dir"
            | "list_directory"
            | "grep"
            | "glob"
            | "search_files"
            | "git_status"
            | "git_diff"
            | "functions.read_file"
            | "functions.list_dir"
            | "functions.grep"
            | "functions.glob"
    ) {
        return Some((tool, tool_input));
    }
    let (_, command) = direct_command(input)?;
    if command.contains('<') || command.contains('>') {
        return None;
    }
    let segments = shell_segments(command);
    if segments.len() != 1 || !read_only_command(&segments[0]) {
        return None;
    }
    Some((tool, tool_input))
}

fn known_outcome(response: Option<&Value>) -> Option<(bool, &Value)> {
    let response = response?;
    let object = response.as_object()?;
    let succeeded = if let Some(value) = object.get("success").and_then(Value::as_bool) {
        value
    } else if object.get("is_error").and_then(Value::as_bool) == Some(true)
        || object
            .get("error")
            .is_some_and(|value| !value.is_null() && value != "")
    {
        false
    } else if let Some(code) = object.get("exit_code").and_then(Value::as_i64) {
        code == 0
    } else if let Some(status) = object.get("status").and_then(Value::as_str) {
        match status {
            "success" | "completed" => true,
            "failed" | "error" => false,
            _ => return None,
        }
    } else {
        return None;
    };
    Some((succeeded, response))
}

fn read_only_command(tokens: &[String]) -> bool {
    let Some(first) = tokens.first() else {
        return false;
    };
    let exe = command_name(first);
    if exe == "find"
        && tokens.iter().any(|arg| {
            matches!(
                arg.as_str(),
                "-exec" | "-execdir" | "-delete" | "-ok" | "-okdir"
            )
        })
    {
        return false;
    }
    if exe == "sed"
        && tokens
            .iter()
            .any(|arg| arg == "--in-place" || arg == "-i" || arg.starts_with("-i"))
    {
        return false;
    }
    if exe == "git"
        && (tokens.iter().any(|arg| arg.starts_with("--output="))
            || tokens.windows(2).any(|pair| pair[0] == "--output"))
    {
        return false;
    }
    if matches!(
        exe.as_str(),
        "cat" | "head" | "tail" | "sed" | "rg" | "grep" | "ls" | "pwd" | "find"
    ) {
        return true;
    }
    exe == "git"
        && tokens
            .get(1)
            .is_some_and(|arg| matches!(arg.as_str(), "status" | "diff" | "show" | "log"))
}

fn recognized_tokens(tokens: &[String]) -> bool {
    let mut i = 0;
    while i < tokens.len() && tokens[i].contains('=') && !tokens[i].starts_with('-') {
        i += 1;
    }
    let rest = &tokens[i..];
    let Some(first) = rest.first() else {
        return false;
    };
    let exe = command_name(first);
    if exe == "ctrlissues" && rest.get(1).is_some_and(|arg| arg == "check") {
        return false;
    }
    match exe.as_str() {
        "cargo" => rest.get(1).is_some_and(|arg| arg == "test"),
        "npm" => {
            rest.get(1).is_some_and(|arg| arg == "test")
                || rest.get(1).is_some_and(|arg| arg == "run")
                    && rest.get(2).is_some_and(|arg| arg.starts_with("test"))
        }
        "npx" => rest
            .get(1)
            .is_some_and(|arg| matches!(arg.as_str(), "vitest" | "playwright")),
        "python" | "python3" | "py" => {
            rest.get(1).is_some_and(|arg| arg == "-m")
                && rest
                    .get(2)
                    .is_some_and(|arg| matches!(arg.as_str(), "unittest" | "pytest"))
        }
        "pytest" | "vitest" | "playwright" => true,
        _ => false,
    }
}

fn command_name(value: &str) -> String {
    let name = value.rsplit(['/', '\\']).next().unwrap_or(value);
    name.strip_suffix(".exe")
        .or_else(|| name.strip_suffix(".cmd"))
        .or_else(|| name.strip_suffix(".bat"))
        .unwrap_or(name)
        .to_ascii_lowercase()
}

fn shell_segments(command: &str) -> Vec<Vec<String>> {
    let mut out = Vec::new();
    let mut segment = Vec::new();
    let mut word = String::new();
    let mut quote = None;
    let mut escaped = false;
    let flush_word = |segment: &mut Vec<String>, word: &mut String| {
        if !word.is_empty() {
            segment.push(std::mem::take(word));
        }
    };
    let mut chars = command.chars().peekable();
    while let Some(ch) = chars.next() {
        if escaped {
            word.push(ch);
            escaped = false;
            continue;
        }
        if quote.is_some_and(|q| q == ch) {
            quote = None;
            continue;
        }
        if quote.is_some() {
            word.push(ch);
            continue;
        }
        if ch == '\\' {
            if chars.peek().is_some_and(|next| {
                next.is_whitespace() || matches!(*next, '\\' | '\'' | '"' | ';' | '|' | '&')
            }) {
                escaped = true;
            } else {
                word.push(ch);
            }
        } else if ch == '\'' || ch == '"' {
            quote = Some(ch);
        } else if ch.is_whitespace() {
            flush_word(&mut segment, &mut word);
        } else if matches!(ch, ';' | '|' | '&' | '\n') {
            flush_word(&mut segment, &mut word);
            if !segment.is_empty() {
                out.push(std::mem::take(&mut segment));
            }
        } else {
            word.push(ch);
        }
    }
    flush_word(&mut segment, &mut word);
    if !segment.is_empty() {
        out.push(segment);
    }
    out
}

fn deny_pretool(reason: &str) {
    println!(
        "{}",
        json!({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":reason}})
    );
}

fn snapshot_path(data: &Path, compact: &Value) -> std::path::PathBuf {
    let digest = Sha256::digest(serde_json::to_vec(compact).unwrap_or_default());
    let key: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    data.join("snapshots").join(format!("{key}.json"))
}

#[cfg(unix)]
fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

#[cfg(windows)]
fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn patch_succeeded(v: Option<&Value>) -> bool {
    match v {
        Some(Value::Object(x)) => {
            x.get("success").and_then(Value::as_bool) == Some(true)
                || x.get("error").is_none()
                    && x.get("output")
                        .and_then(Value::as_str)
                        .is_some_and(|s| s.to_ascii_lowercase().contains("success"))
        }
        Some(Value::String(s)) => s.to_ascii_lowercase().contains("success"),
        _ => false,
    }
}
fn cut(s: &str, n: usize) -> String {
    if s.len() <= n {
        s.into()
    } else {
        s.chars().take(n).collect()
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn direct_test_positions_do_not_scan_outer_code_or_quoted_source() {
        let direct =
            json!({"tool_name":"exec_command","tool_input":{"cmd":"cd runtime && cargo test"}});
        assert!(recognized_test_tool_input(&direct));
        let python = json!({"tool_name":"exec_command","tool_input":{"cmd":"python3 -m unittest"}});
        assert!(recognized_test_tool_input(&python));
        let search = json!({"tool_name":"exec_command","tool_input":{"cmd":"rg 'cargo test'"}});
        assert!(!recognized_test_tool_input(&search));
        let quote = json!({"tool_name":"exec_command","tool_input":{"cmd":"python -c 'print(\"cargo test\")'"}});
        assert!(!recognized_test_tool_input(&quote));
        let outer = json!({"tool_name":"functions.exec","tool_input":{"code":"tools.exec_command({cmd: 'cargo test'})"}});
        assert!(!recognized_test_tool_input(&outer));
        let wrapper = json!({"tool_name":"exec_command","tool_input":{"cmd":"ctrlissues check whole suite -- cargo test"}});
        assert!(!recognized_test_tool_input(&wrapper));
    }

    #[test]
    fn repeat_ledger_only_tracks_read_observations_with_known_completion() {
        let read = json!({"tool_name":"exec_command","tool_input":{"cmd":"cat task.md"}});
        assert!(tracked_observation_input(&read).is_some());
        let mutation = json!({"tool_name":"exec_command","tool_input":{"cmd":"rm task.md"}});
        assert!(tracked_observation_input(&mutation).is_none());
        let outer = json!({"tool_name":"functions.exec","tool_input":{"code":"tools.exec_command({cmd: 'cat task.md'})"}});
        assert!(tracked_observation_input(&outer).is_none());
        let success = json!({"success":true,"output":"observed"});
        assert_eq!(known_outcome(Some(&success)).map(|x| x.0), Some(true));
        let failure = json!({"exit_code":7});
        assert_eq!(known_outcome(Some(&failure)).map(|x| x.0), Some(false));
        let pending = json!({"status":"running"});
        assert!(known_outcome(Some(&pending)).is_none());
        let unknown = json!({"output":"result without completion status"});
        assert!(known_outcome(Some(&unknown)).is_none());
    }
}
