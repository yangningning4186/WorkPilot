"""把数据库加密的 OAuth token 临时注入 MCP 内存配置。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.cowork.connectors import connector_secrets, get_connector_account
from app.cowork.mcp.config import McpConfiguration
from app.security.secret_store import LocalSecretStore


async def hydrate_mcp_oauth_credentials(
    session: AsyncSession,
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
        account = await get_connector_account(session, connector_id)
        if account is None or not account.enabled or account.status != "connected":
            raise ValueError(f"MCP 服务 {name} 绑定的 OAuth connector 未连接")
        token = str(connector_secrets(account, secret_store).get("access_token") or "").strip()
        if not token:
            raise ValueError(f"MCP 服务 {name} 绑定的 OAuth connector 缺少 access_token")
        servers[name] = server.model_copy(
            update={"headers": {**server.headers, "Authorization": f"Bearer {token}"}}
        )
    return configuration.model_copy(update={"servers": servers})
