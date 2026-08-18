"""本地桌面 sidecar 的启动令牌边界。"""

from hmac import compare_digest

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

DESKTOP_LAUNCH_TOKEN_HEADER = "x-workpilot-launch-token"


class DesktopLaunchTokenMiddleware:
    """桌面模式下拒绝所有未持有本次启动令牌的 localhost 请求。

    令牌证明请求来自当前 Tauri 壳，并在 request state 中标记本机
    owner 身份。它不替代会话 root 与 capability grant：真正的文件读写仍在
    每个 Cowork 工具入口按路径重新校验。
    """

    def __init__(self, app: ASGIApp, *, enabled: bool, launch_token: str) -> None:
        self.app = app
        self.enabled = enabled
        self.launch_token = launch_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = Headers(scope=scope).get(DESKTOP_LAUNCH_TOKEN_HEADER, "")
        if not supplied or not compare_digest(supplied, self.launch_token):
            response = JSONResponse(
                status_code=401,
                content={"detail": "桌面启动令牌缺失或无效"},
            )
            await response(scope, receive, send)
            return
        # token 每次桌面启动随机生成，只经子进程环境与 webview
        # 内存传递。身份依赖只读这个 server-side state，不信任普通 header。
        scope.setdefault("state", {})["desktop_authenticated"] = True
        await self.app(scope, receive, send)
