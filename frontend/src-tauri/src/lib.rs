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

const CONNECTOR_AUTHORIZATION_URLS: &[&str] = &[
    "https://github.com/login/oauth/authorize",
    "https://accounts.feishu.cn/open-apis/authen/v1/authorize",
    "https://open.weixin.qq.com/connect/oauth2/authorize",
    "https://docs.qq.com/oauth/v2/authorize",
];

fn is_connector_authorization_url(url: &str) -> bool {
    CONNECTOR_AUTHORIZATION_URLS.iter().any(|base| {
        url == *base
            || url
                .strip_prefix(base)
                .is_some_and(|suffix| suffix.starts_with('?'))
    })
}

#[tauri::command]
fn open_connector_authorization(url: String) -> Result<(), String> {
    if !is_connector_authorization_url(&url) {
        return Err("拒绝打开非官方连接器授权地址".to_string());
    }

    #[cfg(target_os = "macos")]
    let mut command = Command::new("open");
    #[cfg(target_os = "windows")]
    let mut command = {
        let mut command = Command::new("rundll32");
        command.arg("url.dll,FileProtocolHandler");
        command
    };
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let mut command = Command::new("xdg-open");

    command
        .arg(url)
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("无法打开系统浏览器: {error}"))
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
        .stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());

    // PyInstaller one-file 在 Unix 上会再派生真正运行 Python 的子进程。把整棵 sidecar
    // 放进独立进程组，桌面壳退出时才能同时结束 bootloader 与 Python，而不是留下一个
    // 继续持有 ~/.workpilot/.sidecar.lock 的孤儿进程。
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
}

fn apply_packaged_runtime_env(command: &mut Command, browser_root: Option<&Path>) {
    command.env("WORKPILOT_PACKAGED", "true");
    if let Some(path) = browser_root {
        command.env("PLAYWRIGHT_BROWSERS_PATH", path);
    }
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

fn bundled_sidecar_path() -> Result<PathBuf, String> {
    let executable =
        std::env::current_exe().map_err(|error| format!("无法定位 WorkPilot 主程序: {error}"))?;
    let directory = executable
        .parent()
        .ok_or_else(|| "WorkPilot 主程序没有父目录".to_string())?;
    let filename = if cfg!(windows) {
        "workpilot-sidecar.exe"
    } else {
        "workpilot-sidecar"
    };
    let sidecar = directory.join(filename);
    if !sidecar.is_file() {
        return Err(format!(
            "安装包缺少内置后端 sidecar: {}。请重新安装完整的 WorkPilot 包。",
            sidecar.display()
        ));
    }
    Ok(sidecar)
}

fn packaged_command(
    executable: &Path,
    mode: &str,
    extra: &[String],
    token: &str,
    browser_root: Option<&Path>,
) -> Command {
    let mut command = Command::new(executable);
    command.arg(mode).args(extra);
    apply_runtime_env(&mut command, token);
    apply_packaged_runtime_env(&mut command, browser_root);
    command
}

fn wait_until_ready(api_child: &mut Child, api_base: &str, token: &str) -> Result<(), String> {
    let client = reqwest::blocking::Client::builder()
        // sidecar 固定监听 loopback。开发机可能设置 HTTP(S)_PROXY；若让
        // reqwest 继承代理，ready 请求会被错误送到代理端口而永远到不了本机 API。
        .no_proxy()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|error| format!("无法创建 sidecar 健康检查客户端: {error}"))?;
    let url = format!("{api_base}/health/ready");
    // one-file sidecar 首次启动需要先把冻结内容解包；低配机器与被实时防护扫描的机器
    // 明显慢于开发态。健康检查仍然每 250ms 响应一次，但给冷启动留足两分钟。
    for _ in 0..480 {
        if let Some(status) = api_child
            .try_wait()
            .map_err(|error| format!("无法读取 API sidecar 状态: {error}"))?
        {
            return Err(format!(
                "API sidecar 在健康检查通过前退出（{status}）。请查看启动日志。"
            ));
        }
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
    Err("sidecar 在 120 秒内未就绪，请检查本地存储与迁移日志".to_string())
}

fn terminate_child(child: &mut Child) {
    #[cfg(unix)]
    {
        let process_group = -(child.id() as i32);
        // 先给 uvicorn 清理数据库与临时文件的机会；随后无论 bootloader 是否已经退出，
        // 都再清理整个进程组，覆盖 one-file 内层进程仍存活的情况。
        unsafe {
            libc::kill(process_group, libc::SIGTERM);
        }
        thread::sleep(Duration::from_millis(500));
        unsafe {
            libc::kill(process_group, libc::SIGKILL);
        }
        let _ = child.wait();
    }

    #[cfg(not(unix))]
    {
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn start_sidecars(app_handle: &tauri::AppHandle) -> Result<(DesktopContext, Vec<Child>), String> {
    let token = random_token();
    let port = available_port()?;
    let port_string = port.to_string();
    let api_base = format!("http://127.0.0.1:{port}");
    let backend = workspace_backend();

    let (mut migration, mut api) = if cfg!(debug_assertions) {
        (
            development_command(&backend, &["-m", "app.desktop_sidecar", "migrate"], &token),
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
        // externalBin 会把带 target triple 的构建产物复制到主程序同目录，并去掉 triple。
        // 发布态只认这个固定位置，不接受环境变量替换，避免攻击者把启动 token 交给任意程序。
        let sidecar = bundled_sidecar_path()?;
        let browser_root = app_handle
            .path()
            .resource_dir()
            .ok()
            .map(|directory| directory.join("ms-playwright"))
            .filter(|directory| directory.is_dir());
        (
            packaged_command(&sidecar, "migrate", &[], &token, browser_root.as_deref()),
            packaged_command(
                &sidecar,
                "api",
                &["--port".into(), port.to_string()],
                &token,
                browser_root.as_deref(),
            ),
        )
    };

    let migration_status = migration
        .status()
        .map_err(|error| format!("无法启动数据库迁移: {error}"))?;
    if !migration_status.success() {
        return Err(format!("数据库迁移失败: {migration_status}"));
    }

    let mut api_child = api
        .spawn()
        .map_err(|error| format!("无法启动 API sidecar: {error}"))?;
    if let Err(error) = wait_until_ready(&mut api_child, &api_base, &token) {
        terminate_child(&mut api_child);
        return Err(error);
    }
    let children = vec![api_child];
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
            terminate_child(child);
        }
        children.clear();
    }
}

fn initialize_sidecars(app_handle: tauri::AppHandle) {
    match start_sidecars(&app_handle) {
        Ok((context, mut children)) => {
            let state = app_handle.state::<DesktopState>();
            let Ok(mut child_state) = state.children.lock() else {
                for child in &mut children {
                    terminate_child(child);
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
        .invoke_handler(tauri::generate_handler![
            desktop_context,
            open_connector_authorization
        ])
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

#[cfg(test)]
mod tests {
    use super::is_connector_authorization_url;

    #[test]
    fn connector_authorization_only_accepts_the_pinned_provider_paths() {
        assert!(is_connector_authorization_url(
            "https://accounts.feishu.cn/open-apis/authen/v1/authorize?state=opaque"
        ));
        assert!(is_connector_authorization_url(
            "https://github.com/login/oauth/authorize?client_id=123"
        ));
        assert!(!is_connector_authorization_url(
            "https://github.com.evil.example/login/oauth/authorize?state=opaque"
        ));
        assert!(!is_connector_authorization_url(
            "https://example.com/authorize"
        ));
    }
}
