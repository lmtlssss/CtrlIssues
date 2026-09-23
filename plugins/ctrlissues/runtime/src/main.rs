mod backend;
mod environment;
mod hooks;
mod markdown;
mod private_fs;
mod state;

use state::Store;
use std::{env, path::PathBuf, process::exit};

fn main() {
    if let Err(e) = run() {
        eprintln!("ctrlissues: {e}");
        exit(2);
    }
}
fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut a: Vec<String> = env::args().skip(1).collect();
    if matches!(a.first().map(String::as_str), Some("--version" | "version")) {
        println!("ctrlissues {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    let mut data = None;
    let mut session = env::var("CODEX_THREAD_ID")
        .ok()
        .or_else(|| env::var("CTRLISSUES_SESSION").ok());
    let mut expected_revision = None;
    while !a.is_empty() && (a[0] == "--data-dir" || a[0] == "--session" || a[0] == "--revision") {
        let k = a.remove(0);
        let v = a.first().ok_or("missing option value")?.clone();
        a.remove(0);
        if k == "--revision" {
            expected_revision = Some(v.parse::<u64>()?);
        } else if k == "--data-dir" {
            data = Some(PathBuf::from(v))
        } else {
            session = Some(v)
        }
    }
    let data = data
        .or_else(|| env::var_os("CTRLISSUES_DATA").map(PathBuf::from))
        .or_else(|| {
            env::var_os("CODEX_HOME")
                .map(|p| PathBuf::from(p).join("plugins/data/ctrlissues-ctrlissues"))
        })
        .or_else(|| {
            env::var_os("HOME")
                .map(|p| PathBuf::from(p).join(".codex/plugins/data/ctrlissues-ctrlissues"))
        })
        .ok_or("no plugin data directory available")?;
    let cmd = a.first().map(String::as_str).unwrap_or("status");
    if cmd == "hook" {
        if let Err(e) = hooks::run(&data) {
            eprintln!("ctrlissues hook advisory skipped: {e}");
        }
        return Ok(());
    }
    if cmd == "doctor" {
        return Store::open(&data, session.as_deref().unwrap_or("doctor"))?
            .doctor()
            .map_err(Into::into);
    }
    if matches!(
        cmd,
        "work"
            | "decide"
            | "select"
            | "assess"
            | "verify"
            | "guard"
            | "stats"
            | "configure"
            | "browser"
    ) {
        let canonical = session
            .as_deref()
            .map(|id| Store::open(&data, id).map(|s| s.canonical_id().to_owned()))
            .transpose()?;
        return backend::run(&data, canonical.as_deref(), &a);
    }
    let session = session.ok_or("--session or CODEX_THREAD_ID is required")?;
    let mut s = Store::open(&data, &session)?;
    s.set_expected_revision(expected_revision);
    let out = match cmd {
        "status" if a.get(1).map(String::as_str) == Some("--compact") => s.compact_status()?,
        "status" => s.status()?,
        "plan" => s.plan(a.get(1).ok_or("plan requires FILE")?)?,
        "revise" => s.revise(a.get(1).ok_or("revise requires FILE")?)?,
        "cursor" => s.cursor(a[1..].join(" "))?,
        "mark" => s.mark(a.get(1).ok_or("component")?, a.get(2).ok_or("evidence")?)?,
        "advance" => s.advance()?,
        "issue" => s.issue(
            a.get(1).ok_or("component")?,
            a.get(2..).unwrap_or(&[]).join(" "),
        )?,
        "changed" if a.get(1).map(String::as_str) == Some("--component") => s.changed_component(
            a.get(2).ok_or("changed --component requires ID")?,
            a.get(3..).unwrap_or(&[]).join(" "),
        )?,
        "changed" => s.changed(a[1..].join(" "))?,
        "finish" => s.finish()?,
        "reset" => s.reset(a[1..].join(" "))?,
        "check" => s.check(&a[1..])?,
        _ => return Err("unknown command".into()),
    };
    println!("{}", serde_json::to_string(&out)?);
    Ok(())
}
