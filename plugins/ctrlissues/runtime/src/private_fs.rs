use std::{fs, io, path::Path};

pub fn create_private_dir_all(path: &Path) -> io::Result<()> {
    match fs::metadata(path) {
        Ok(metadata) => {
            if metadata.is_dir() {
                return Ok(());
            }
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "path exists and is not a directory",
            ));
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    if let Some(parent) = path.parent().filter(|parent| *parent != path) {
        create_private_dir_all(parent)?;
    }
    match fs::create_dir(path) {
        Ok(()) => set_private(path),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            if fs::metadata(path)?.is_dir() {
                Ok(())
            } else {
                Err(error)
            }
        }
        Err(error) => Err(error),
    }
}

#[cfg(unix)]
fn set_private(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
}

#[cfg(not(unix))]
fn set_private(_: &Path) -> io::Result<()> {
    Ok(())
}
