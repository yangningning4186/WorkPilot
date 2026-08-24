"""把加密存储的 OAuth token 临时注入 MCP 内存配置。"""

from app.core.config import Settings
from app.cowork.connectors import connector_secrets, get_connector_account
from app.cowork.mcp.config import McpConfiguration
from app.security.secret_store import LocalSecretStore


def hydrate_mcp_oauth_credentials(
    settings: Settings,
    configuration: McpConfiguration,
    secret_store: LocalSecretStore,
) -> McpConfiguration:
    servers = dict(configuration.servers)
    for name, server in configuration.servers.items():
        connector_id = server.oauth_connector_id
        if connector_id is None:
            continue
        if server.transport == "stdio":
            raise ValueError(f"MCP 服务 {name} 的 OAuth connector 只适用于 HTTP transport")
        account = get_connector_account(settings, connector_id)
        if account is None or not account.enabled or account.status != "connected":
            raise ValueError(f"MCP 服务 {name} 绑定的 OAuth connector 未连接")
        token = str(connector_secrets(account, secret_store).get("access_token") or "").strip()
        if not token:
            raise ValueError(f"MCP 服务 {name} 绑定的 OAuth connector 缺少 access_token")
        servers[name] = server.model_copy(
            update={"headers": {**server.headers, "Authorization": f"Bearer {token}"}}
        )
    return configuration.model_copy(update={"servers": servers})
