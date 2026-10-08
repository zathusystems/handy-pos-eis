use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
#[cfg(not(target_os = "android"))]
use std::thread;
#[cfg(not(target_os = "android"))]
use std::time::Duration;
use tauri::Manager;
#[cfg(not(target_os = "android"))]
use tauri::{AppHandle, Listener, PhysicalSize, Size, WebviewWindow};

mod backend;
mod printer;

#[derive(Debug, Default, Serialize, Deserialize)]
struct PersistedSessionSnapshot {
    entries: BTreeMap<String, String>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct PersistedDeviceIdentity {
    device_serial: String,
}

fn session_snapshot_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    let base_dir = app
        .path()
        .app_data_dir()
        .or_else(|_| app.path().data_dir())
        .map_err(|error| format!("Could not resolve app data directory: {}", error))?;

    std::fs::create_dir_all(&base_dir)
        .map_err(|error| format!("Could not create app data directory: {}", error))?;

    Ok(base_dir.join("session-snapshot.json"))
}

fn device_identity_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    let base_dir = app
        .path()
        .app_data_dir()
        .or_else(|_| app.path().data_dir())
        .map_err(|error| format!("Could not resolve app data directory: {}", error))?;

    std::fs::create_dir_all(&base_dir)
        .map_err(|error| format!("Could not create app data directory: {}", error))?;

    Ok(base_dir.join("device-identity.json"))
}

fn sanitize_device_serial(value: Option<String>) -> Option<String> {
    let trimmed = value?.trim().to_string();
    if trimmed.len() < 8 || trimmed.len() > 100 {
        return None;
    }

    let valid = trimmed
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_'));
    if valid {
        Some(trimmed)
    } else {
        None
    }
}

fn normalize_mac_address(value: &str) -> Option<String> {
    let hex = value
        .chars()
        .filter(|ch| ch.is_ascii_hexdigit())
        .map(|ch| ch.to_ascii_uppercase())
        .collect::<String>();

    if hex.len() != 12 || hex == "000000000000" {
        return None;
    }

    let pairs = (0..hex.len())
        .step_by(2)
        .map(|index| hex[index..index + 2].to_string())
        .collect::<Vec<String>>();

    Some(pairs.join("-"))
}

fn os_serial_code() -> &'static str {
    if cfg!(target_os = "windows") {
        "WIN"
    } else if cfg!(target_os = "macos") {
        "MAC"
    } else if cfg!(target_os = "linux") {
        "LIN"
    } else if cfg!(target_os = "android") {
        "AND"
    } else if cfg!(target_os = "ios") {
        "IOS"
    } else {
        "DSK"
    }
}

fn generate_device_serial() -> String {
    let uuid = uuid::Uuid::new_v4().simple().to_string().to_uppercase();
    format!("HANDY-{}-{}", os_serial_code(), &uuid[..16])
}

#[tauri::command]
fn get_device_identity(
    app: tauri::AppHandle,
    preferred_serial: Option<String>,
) -> Result<String, String> {
    let identity_path = device_identity_path(&app)?;

    if identity_path.exists() {
        let raw = std::fs::read_to_string(&identity_path)
            .map_err(|error| format!("Could not read device identity: {}", error))?;
        let parsed: PersistedDeviceIdentity = serde_json::from_str(&raw)
            .map_err(|error| format!("Could not parse device identity: {}", error))?;
        if let Some(device_serial) = sanitize_device_serial(Some(parsed.device_serial)) {
            return Ok(device_serial);
        }
    }

    let device_serial =
        sanitize_device_serial(preferred_serial).unwrap_or_else(generate_device_serial);
    let payload = PersistedDeviceIdentity {
        device_serial: device_serial.clone(),
    };
    let serialized = serde_json::to_string_pretty(&payload)
        .map_err(|error| format!("Could not serialize device identity: {}", error))?;

    std::fs::write(&identity_path, serialized.as_bytes())
        .map_err(|error| format!("Could not write device identity: {}", error))?;

    Ok(device_serial)
}

#[tauri::command]
fn get_device_mac_address() -> Result<Option<String>, String> {
    read_device_mac_address()
}

#[cfg(not(target_os = "android"))]
fn read_device_mac_address() -> Result<Option<String>, String> {
    mac_address::get_mac_address()
        .map_err(|error| format!("Could not read device MAC address: {}", error))
        .map(|mac_address| {
            mac_address.and_then(|address| normalize_mac_address(&address.to_string()))
        })
}

#[cfg(target_os = "android")]
fn read_device_mac_address() -> Result<Option<String>, String> {
    Ok(None)
}

#[tauri::command]
fn save_session_snapshot(
    app: tauri::AppHandle,
    entries: BTreeMap<String, String>,
) -> Result<(), String> {
    let snapshot_path = session_snapshot_path(&app)?;
    let payload = PersistedSessionSnapshot { entries };
    let serialized = serde_json::to_string_pretty(&payload)
        .map_err(|error| format!("Could not serialize session snapshot: {}", error))?;

    std::fs::write(&snapshot_path, serialized.as_bytes())
        .map_err(|error| format!("Could not write session snapshot: {}", error))?;

    Ok(())
}

#[tauri::command]
fn load_session_snapshot(app: tauri::AppHandle) -> Result<BTreeMap<String, String>, String> {
    let snapshot_path = session_snapshot_path(&app)?;
    if !snapshot_path.exists() {
        return Ok(BTreeMap::new());
    }

    let raw = std::fs::read_to_string(&snapshot_path)
        .map_err(|error| format!("Could not read session snapshot: {}", error))?;
    let parsed: PersistedSessionSnapshot = serde_json::from_str(&raw)
        .map_err(|error| format!("Could not parse session snapshot: {}", error))?;

    Ok(parsed.entries)
}

#[tauri::command]
fn clear_session_snapshot(app: tauri::AppHandle) -> Result<(), String> {
    let snapshot_path = session_snapshot_path(&app)?;
    if snapshot_path.exists() {
        std::fs::remove_file(&snapshot_path)
            .map_err(|error| format!("Could not remove session snapshot: {}", error))?;
    }

    Ok(())
}

#[tauri::command]
fn save_receipt_pdf(
    app: tauri::AppHandle,
    filename: String,
    content: Vec<u8>,
) -> Result<String, String> {
    if content.is_empty() {
        return Err("Receipt PDF is empty.".to_string());
    }

    let sanitized_filename = filename
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '.' | '-' | '_') {
                ch
            } else {
                '_'
            }
        })
        .collect::<String>();

    let mut final_filename = if sanitized_filename.trim().is_empty() {
        "thermal-receipt.pdf".to_string()
    } else {
        sanitized_filename
    };
    if !final_filename.to_ascii_lowercase().ends_with(".pdf") {
        final_filename.push_str(".pdf");
    }

    let mut target_directories: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(dir) = app.path().download_dir() {
        target_directories.push(dir);
    }
    if let Ok(dir) = app.path().document_dir() {
        target_directories.push(dir);
    }
    if let Ok(dir) = app.path().data_dir() {
        target_directories.push(dir);
    }
    if let Ok(dir) = app.path().cache_dir() {
        target_directories.push(dir);
    }

    let mut last_error: Option<String> = None;
    for directory in target_directories {
        if let Err(error) = std::fs::create_dir_all(&directory) {
            last_error = Some(format!(
                "Could not create directory {}: {}",
                directory.display(),
                error
            ));
            continue;
        }

        let output_path = directory.join(&final_filename);
        match std::fs::write(&output_path, &content) {
            Ok(_) => return Ok(output_path.display().to_string()),
            Err(error) => {
                last_error = Some(format!(
                    "Failed writing {}: {}",
                    output_path.display(),
                    error
                ));
            }
        }
    }

    Err(last_error.unwrap_or_else(|| "No writable directory available for receipt PDF export.".to_string()))
}

#[tauri::command]
fn save_inventory_template_csv(
    app: tauri::AppHandle,
    filename: String,
    content: String,
) -> Result<String, String> {
    let sanitized_filename = filename
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '.' | '-' | '_') {
                ch
            } else {
                '_'
            }
        })
        .collect::<String>();

    let fallback_name = "inventory-template.csv".to_string();
    let final_filename = if sanitized_filename.trim().is_empty() {
        fallback_name
    } else {
        sanitized_filename
    };

    let mut target_directories: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(dir) = app.path().download_dir() {
        target_directories.push(dir);
    }
    if let Ok(dir) = app.path().document_dir() {
        target_directories.push(dir);
    }
    if let Ok(dir) = app.path().data_dir() {
        target_directories.push(dir);
    }
    if let Ok(dir) = app.path().cache_dir() {
        target_directories.push(dir);
    }

    let mut last_error: Option<String> = None;

    for directory in target_directories {
        if let Err(err) = std::fs::create_dir_all(&directory) {
            last_error = Some(format!(
                "Could not create directory {}: {}",
                directory.display(),
                err
            ));
            continue;
        }

        let output_path = directory.join(&final_filename);
        match std::fs::write(&output_path, content.as_bytes()) {
            Ok(_) => return Ok(output_path.display().to_string()),
            Err(err) => {
                last_error = Some(format!("Failed writing {}: {}", output_path.display(), err));
            }
        }
    }

    Err(last_error
        .unwrap_or_else(|| "No writable directory available for template export.".to_string()))
}

#[cfg(not(target_os = "android"))]
fn fit_window_to_monitor(window: &WebviewWindow) {
    let monitor = window
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| window.primary_monitor().ok().flatten());

    let (Some(monitor), Ok(current_size)) = (monitor, window.outer_size()) else {
        return;
    };

    // Reserve space for desktop bars/window decorations so the window does not overflow.
    let max_width = monitor.size().width.saturating_sub(32);
    let max_height = monitor.size().height.saturating_sub(72);

    if max_width < 320 || max_height < 320 {
        return;
    }

    let target_width = current_size.width.min(max_width);
    let target_height = current_size.height.min(max_height);

    if target_width != current_size.width || target_height != current_size.height {
        let _ = window.set_size(Size::Physical(PhysicalSize::new(
            target_width,
            target_height,
        )));
    }
}

#[cfg(not(target_os = "android"))]
fn center_window_stable(app: AppHandle, label: &'static str) {
    thread::spawn(move || {
        // Some window managers report transient geometry right after show().
        // Retry centering briefly so splash/main end up centered reliably.
        for _ in 0..6 {
            if let Some(window) = app.get_webview_window(label) {
                if label == "main" {
                    fit_window_to_monitor(&window);
                }
                let _ = window.center();
            }
            thread::sleep(Duration::from_millis(80));
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    println!("[Tauri] Starting HandyPOS application");

    let builder = tauri::Builder::default().invoke_handler(tauri::generate_handler![
        printer::get_printers,
        printer::print_receipt,
        printer::open_cash_drawer,
        save_session_snapshot,
        load_session_snapshot,
        clear_session_snapshot,
        get_device_identity,
        get_device_mac_address,
        save_receipt_pdf,
        save_inventory_template_csv,
    ]);

    #[cfg(not(target_os = "android"))]
    let builder = builder.setup(|app| {
        if let Some(backend_state) = backend::maybe_spawn_backend(&app.handle()) {
            app.manage(backend_state);
        }

        let splash_window = app.get_webview_window("splash");
        let main_window = app.get_webview_window("main");

        // Mobile targets and simplified desktop configs may not define a splash window.
        if splash_window.is_none() || main_window.is_none() {
            return Ok(());
        }

        let splash_window = splash_window.expect("validated above");
        let main_window = main_window.expect("validated above");

        let ready_handle = app.handle().clone();
        app.listen("frontend-ready", move |_event| {
            println!("[Tauri] Received frontend-ready event");

            if let Some(main) = ready_handle.get_webview_window("main") {
                let already_visible = main.is_visible().unwrap_or(false);
                if already_visible {
                    return;
                }
            }

            if let Some(splash) = ready_handle.get_webview_window("splash") {
                let _ = splash.hide();
            }

            if let Some(main) = ready_handle.get_webview_window("main") {
                let _ = main.show();
                fit_window_to_monitor(&main);
                let _ = main.center();
                let _ = main.set_focus();
                center_window_stable(ready_handle.clone(), "main");
            }

            println!("[Tauri] Main window shown after frontend-ready event");
        });

        let _ = splash_window.hide();
        let _ = splash_window.show();
        let _ = splash_window.center();
        let _ = splash_window.set_focus();
        center_window_stable(app.handle().clone(), "splash");

        let _ = main_window.hide();

        let fallback_handle = app.handle().clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_secs(25));

            if let Some(main) = fallback_handle.get_webview_window("main") {
                let should_show = main.is_visible().map(|visible| !visible).unwrap_or(true);
                if should_show {
                    println!("[Tauri] frontend-ready timeout after 25s, showing main window");

                    if let Some(splash) = fallback_handle.get_webview_window("splash") {
                        let _ = splash.hide();
                    }

                    let _ = main.show();
                    fit_window_to_monitor(&main);
                    let _ = main.center();
                    let _ = main.set_focus();
                    center_window_stable(fallback_handle.clone(), "main");
                }
            }
        });

        Ok(())
    });

    let app = builder
        .build(tauri::generate_context!())
        .expect("error while running tauri application");

    app.run(|app_handle, event| {
        #[cfg(target_os = "windows")]
        match event {
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. } => {
                if let Some(state) = app_handle.try_state::<backend::BackendState>() {
                    state.stop();
                }
            }
            _ => {}
        }
    });
}
