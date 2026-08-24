# deploy

WorkPilot 当前是本地优先桌面应用，不再部署 PostgreSQL、Redis、Arq 或 pgvector。
`docker-compose.yml` 保留为空，是为了以后可选加入用户自行部署的 embedding/reranker 服务，
不是启动 WorkPilot 的前置条件。

开发和桌面启动请使用：

```bash
cd ../backend
uv sync --locked

cd ../frontend
npm ci
npm run dev:desktop
```

完整说明见 [本地启动指南](../docs/17-本地启动指南.md)。发布安装包由 Tauri 构建链负责，
不通过本目录的 Compose 文件生成。原生机器上使用 `cd ../frontend && npm run bundle:desktop`；
它会先冻结并烟测 sidecar，再生成当前平台安装包。正式分发仍需平台代码签名与公证。
