use rand::{rngs::OsRng, RngCore};
use serde::Serialize;
use std::{
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::Duration,
};
use tauri::{Manager, State};

const LAUNCH_TOKEN_HEADER: &str = "x-workpilot-launch-token";

#[derive(Clone, Serialize)]
struct DesktopContext {
    api_base: String,
    launch_token: String,
}

#[derive(Default)]
struct DesktopState {
    context: Mutex<Option<DesktopContext>>,
    children: Mutex<Vec<Child>>,
    startup_error: Mutex<Option<String>>,
}

#[tauri::command]
fn desktop_context(state: State<'_, DesktopState>) -> Result<DesktopContext, String> {
    if let Some(context) = state
        .context
        .lock()
        .map_err(|_| "desktop context lock poisoned".to_string())?
        .clone()
    {
        eprintln!("[desktop] desktop_context: ready ({})", context.api_base);
        return Ok(context);
    }
    if let Some(error) = state
        .startup_error
        .lock()
        .map_err(|_| "desktop startup error lock poisoned".to_string())?
        .clone()
    {
        eprintln!("[desktop] desktop_context: startup failed: {error}");
        return Err(error);
    }
    Err("WorkPilot sidecar 正在后台启动".to_string())
}

fn random_token() -> String {
    let mut bytes = [0_u8; 32];
    OsRng.fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn available_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .map_err(|error| format!("无法分配 sidecar 端口: {error}"))?;
    let port = listener
        .local_addr()
        .map_err(|error| format!("无法读取 sidecar 端口: {error}"))?
        .port();
    drop(listener);
    Ok(port)
}

fn workspace_backend() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")))
        .join("backend")
}

fn apply_runtime_env(command: &mut Command, token: &str) {
    command
        .env("DESKTOP_MODE_ENABLED", "true")
        .env("DESKTOP_LAUNCH_TOKEN", token)
        .env("COWORK_ENABLED", "true")
        .env("COWORK_STORE_BACKEND", "sqlite")
        .env("TASK_QUEUE_BACKEND", "in_process")
        .env("RUN_BUS_BACKEND", "in_process")
        .stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
}

fn development_command(backend: &Path, args: &[&str], token: &str) -> Command {
    // 直接跟踪 venv 中的 Python 进程。若经 `uv run` 再启动一层，Tauri 退出时
    // kill 的只是 uv 包装进程，真正的 API/worker 会被 PID 1 接管并继续消费队列，
    // 随后和新版本 worker 竞争同一任务。
    let python = if cfg!(windows) {
        backend.join(".venv").join("Scripts").join("python.exe")
    } else {
        backend.join(".venv").join("bin").join("python")
    };
    let mut command = Command::new(python);
    command.current_dir(backend).args(args);
    apply_runtime_env(&mut command, token);
    command
}

fn packaged_command(mode: &str, extra: &[String], token: &str) -> Result<Command, String> {
    let executable = std::env::var_os("WORKPILOT_SIDECAR")
        .ok_or_else(|| "打包版需通过 WORKPILOT_SIDECAR 指定后端 sidecar 可执行文件".to_string())?;
    let mut command = Command::new(executable);
    command.arg(mode).args(extra);
    apply_runtime_env(&mut command, token);
    Ok(command)
}

fn wait_until_ready(api_base: &str, token: &str) -> Result<(), String> {
    let client = reqwest::blocking::Client::builder()
        // sidecar 固定监听 loopback。开发机可能设置 HTTP(S)_PROXY；若让
        // reqwest 继承代理，ready 请求会被错误送到代理端口而永远到不了本机 API。
        .no_proxy()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|error| format!("无法创建 sidecar 健康检查客户端: {error}"))?;
    let url = format!("{api_base}/health/ready");
    for _ in 0..240 {
        if client
            .get(&url)
            .header(LAUNCH_TOKEN_HEADER, token)
            .send()
            .is_ok_and(|response| response.status().is_success())
        {
            eprintln!("[desktop] sidecar readiness passed at {api_base}");
            return Ok(());
        }
        thread::sleep(Duration::from_millis(250));
    }
    Err("sidecar 在 60 秒内未就绪，请检查本地存储与迁移日志".to_string())
}

fn start_sidecars() -> Result<(DesktopContext, Vec<Child>), String> {
    let token = random_token();
    let port = available_port()?;
    let port_string = port.to_string();
    let api_base = format!("http://127.0.0.1:{port}");
    let backend = workspace_backend();

    let (mut migration, mut api) = if cfg!(debug_assertions) {
        (
            development_command(
                &backend,
                &["-m", "app.desktop_sidecar", "migrate"],
                &token,
            ),
            development_command(
                &backend,
                &[
                    "-m",
                    "app.desktop_sidecar",
                    "api",
                    "--port",
                    port_string.as_str(),
                ],
                &token,
            ),
        )
    } else {
        (
            packaged_command("migrate", &[], &token)?,
            packaged_command("api", &["--port".into(), port.to_string()], &token)?,
        )
    };

    let migration_status = migration
        .status()
        .map_err(|error| format!("无法启动数据库迁移: {error}"))?;
    if !migration_status.success() {
        return Err(format!("数据库迁移失败: {migration_status}"));
    }

    let api_child = api
        .spawn()
        .map_err(|error| format!("无法启动 API sidecar: {error}"))?;
    let mut children = vec![api_child];
    if let Err(error) = wait_until_ready(&api_base, &token) {
        for child in &mut children {
            let _ = child.kill();
        }
        return Err(error);
    }
    Ok((
        DesktopContext {
            api_base,
            launch_token: token,
        },
        children,
    ))
}

fn stop_sidecars(state: &DesktopState) {
    if let Ok(mut children) = state.children.lock() {
        for child in children.iter_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
        children.clear();
    }
}

fn initialize_sidecars(app_handle: tauri::AppHandle) {
    match start_sidecars() {
        Ok((context, mut children)) => {
            let state = app_handle.state::<DesktopState>();
            let Ok(mut child_state) = state.children.lock() else {
                for child in &mut children {
                    let _ = child.kill();
                }
                return;
            };
            *child_state = children;
            drop(child_state);

            match state.context.lock() {
                Ok(mut context_state) => {
                    eprintln!("[desktop] sidecar context published ({})", context.api_base);
                    *context_state = Some(context);
                }
                Err(_) => stop_sidecars(&state),
            };
        }
        Err(error) => {
            eprintln!("WorkPilot sidecar startup failed: {error}");
            if let Ok(mut startup_error) = app_handle.state::<DesktopState>().startup_error.lock() {
                *startup_error = Some(error);
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let application = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(DesktopState::default())
        .invoke_handler(tauri::generate_handler![desktop_context])
        .setup(|app| {
            let app_handle = app.handle().clone();
            thread::spawn(move || initialize_sidecars(app_handle));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building WorkPilot desktop application");

    application.run(|app_handle, event| {
        if matches!(
            event,
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
        ) {
            stop_sidecars(&app_handle.state::<DesktopState>());
        }
    });
}
