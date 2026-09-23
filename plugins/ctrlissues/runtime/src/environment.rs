use std::{env, process::Command};

pub fn clean_command(command: &mut Command) {
    command.env_clear();
    for (key, value) in env::vars_os() {
        let Some(name) = key.to_str() else { continue };
        let name = name.to_ascii_uppercase();
        if allowed(&name) {
            command.env(key, value);
        }
    }
}

fn allowed(name: &str) -> bool {
    if name.starts_with("LC_") {
        return true;
    }
    matches!(
        name,
        // Process and locale basics.
        "PATH" | "HOME" | "USER" | "LOGNAME" | "SHELL" | "LANG" | "LANGUAGE" | "TMP" | "TEMP" | "TMPDIR" | "TERM" | "COLORTERM" | "NO_COLOR" | "CI"
        // Windows process startup and user paths.
        | "SYSTEMROOT" | "WINDIR" | "COMSPEC" | "PATHEXT" | "SYSTEMDRIVE" | "HOMEDRIVE" | "HOMEPATH" | "USERPROFILE" | "APPDATA" | "LOCALAPPDATA" | "PROGRAMDATA" | "PROGRAMFILES" | "PROGRAMFILES(X86)"
        // Display and local service sockets.
        | "DISPLAY" | "WAYLAND_DISPLAY" | "XAUTHORITY" | "XDG_RUNTIME_DIR" | "XDG_SESSION_TYPE" | "XDG_CONFIG_HOME" | "XDG_CACHE_HOME" | "XDG_DATA_HOME" | "XDG_STATE_HOME" | "DBUS_SESSION_BUS_ADDRESS" | "SSH_AUTH_SOCK" | "SSH_AGENT_PID"
        // CtrlIssues and Codex locators.
        | "CODEX_HOME" | "CODEX_THREAD_ID" | "CODEX_BIN" | "CTRLISSUES_DATA" | "CTRLISSUES_EXE" | "CTRLISSUES_SESSION" | "CTRLISSUES_PLUGIN_ROOT" | "PLUGIN_ROOT" | "PLUGIN_DATA"
        // Build and language toolchains required by bounded checks.
        | "CARGO_HOME" | "CARGO_TARGET_DIR" | "CARGO_BUILD_TARGET" | "CARGO_INCREMENTAL" | "CARGO_NET_OFFLINE" | "CARGO_TERM_COLOR" | "CARGO_ENCODED_RUSTFLAGS" | "RUSTUP_HOME" | "RUSTUP_TOOLCHAIN" | "RUSTC" | "RUSTDOC" | "RUSTFLAGS" | "RUSTDOCFLAGS" | "CC" | "CXX" | "AR" | "PKG_CONFIG" | "PKG_CONFIG_PATH" | "PKG_CONFIG_LIBDIR" | "PKG_CONFIG_SYSROOT_DIR" | "CMAKE_PREFIX_PATH" | "MAKEFLAGS"
        | "VIRTUAL_ENV" | "PYTHONHOME" | "PYTHONPATH" | "JAVA_HOME" | "ANDROID_HOME" | "ANDROID_SDK_ROOT" | "ANDROID_NDK_HOME" | "GRADLE_USER_HOME" | "NPM_CONFIG_PREFIX"
    ) || name.starts_with("XDG_") && name.ends_with("_DIR")
}
