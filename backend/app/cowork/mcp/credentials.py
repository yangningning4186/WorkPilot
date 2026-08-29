"""把加密存储的 OAuth token 临时注入 MCP 内存配置。"""

from app.core.config import Settings
from app.cowork.connector_descriptors import (
    connector_allows_mcp_url,
    get_connector_descriptor,
)
from app.cowork.connectors import connector_secrets, get_connector_account
from app.cowork.mcp.config import McpConfiguration, validate_mcp_runtime_configuration
from app.security.secret_store import LocalSecretStore


def hydrate_mcp_oauth_credentials(
    settings: Settings,
    configuration: McpConfiguration,
    secret_store: LocalSecretStore,
) -> McpConfiguration:
    # Reject an unsafe endpoint before opening the encrypted store or decrypting any token.
    validate_mcp_runtime_configuration(configuration, require_resolved_credentials=True)
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
        if account.auth_type != "oauth2":
            raise ValueError(f"MCP 服务 {name} 绑定的 connector 不是 OAuth 账户")
        descriptor = get_connector_descriptor(account.kind)
        if descriptor.auth_style != "bearer":
            raise ValueError(f"MCP 服务 {name} 绑定的 connector 不支持 Bearer MCP 鉴权")
        if server.url is None or not connector_allows_mcp_url(descriptor, server.url):
            # 不回显 URL、origin 或账户详情：配置错误可能包含敏感 query，调用方只需知道
            # 这份 token 没有被其 descriptor 授权发送到目标服务。
            raise ValueError(f"MCP 服务 {name} 的 origin 未获绑定 connector 明确授权")
        try:
            token = str(connector_secrets(account, secret_store).get("access_token") or "").strip()
        except Exception:
            raise ValueError(f"MCP 服务 {name} 无法读取绑定 connector 的凭据") from None
        if not token:
            raise ValueError(f"MCP 服务 {name} 绑定的 OAuth connector 缺少 access_token")
        servers[name] = server.model_copy(
            update={"headers": {**server.headers, "Authorization": f"Bearer {token}"}}
        )
    hydrated = configuration.model_copy(update={"servers": servers})
    hydrated._runtime_credentials_resolved = True
    validate_mcp_runtime_configuration(hydrated, require_resolved_credentials=True)
    return hydrated
