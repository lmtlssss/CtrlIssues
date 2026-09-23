use std::{
    env,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

pub fn run(
    data: &Path,
    session: Option<&str>,
    args: &[String],
) -> Result<(), Box<dyn std::error::Error>> {
    let exe = env::current_exe()?;
    let root = plugin_root(&exe)?;
    let hands = args.first().map(String::as_str) == Some("browser");
    let script = if hands {
        root.join("hands/main.py")
    } else {
        root.join("backend/main.py")
    };
    if !script.is_file() {
        return Err(format!(
            "Python backend is not assembled yet (expected {})",
            script.display()
        )
        .into());
    }
    let mut child = if hands {
        #[cfg(windows)]
        let python = data.join("hands/browser-env/Scripts/python.exe");
        #[cfg(not(windows))]
        let python = data.join("hands/browser-env/bin/python");
        let mut command = Command::new("envstack");
        command
            .args(["run", "--"])
            .arg(python)
            .arg(&script)
            .args(&args[1..]);
        command
    } else {
        let python = env::var_os("CTRLISSUES_PYTHON").unwrap_or_else(|| {
            if cfg!(windows) {
                "python".into()
            } else {
                "python3".into()
            }
        });
        let mut command = Command::new(python);
        command.arg(&script).args(args);
        command
    };
    child
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    crate::environment::clean_command(&mut child);
    child.env("CTRLISSUES_DATA", data);
    child.env("CTRLISSUES_EXE", exe);
    child.env("CTRLISSUES_PLUGIN_ROOT", &root);
    if hands {
        child.env(
            "CTRLISSUES_BROWSER_CDP_URL",
            env::var("CTRLISSUES_BROWSER_CDP_URL")
                .unwrap_or_else(|_| "http://127.0.0.1:9222".into()),
        );
    }
    if let Some(id) = session {
        child.env("CTRLISSUES_SESSION", id);
    }
    let status = child.status()?;
    if !status.success() {
        return Err(format!("backend exited with {status}").into());
    }
    Ok(())
}

fn plugin_root(exe: &Path) -> Result<PathBuf, Box<dyn std::error::Error>> {
    for name in ["CTRLISSUES_PLUGIN_ROOT", "PLUGIN_ROOT"] {
        if let Some(path) = env::var_os(name) {
            let root = PathBuf::from(path);
            if root.join("backend/main.py").is_file() {
                return Ok(root);
            }
        }
    }
    for parent in exe.ancestors() {
        if parent.join("backend/main.py").is_file() {
            return Ok(parent.to_owned());
        }
        let nested = parent.join("plugins/ctrlissues");
        if nested.join("backend/main.py").is_file() {
            return Ok(nested);
        }
    }
    Err("cannot locate CtrlIssues backend/main.py; set CTRLISSUES_PLUGIN_ROOT".into())
}
