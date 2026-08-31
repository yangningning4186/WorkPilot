# deploy

WorkPilot 当前是本地优先桌面应用，不再部署 PostgreSQL、Redis、Arq 或 pgvector。
`docker-compose.yml` 保留为空，是为了以后可选加入用户自行部署的 embedding/reranker 服务，
不是启动 WorkPilot 的前置条件。

开发和桌面启动请使用：

```bash
cd ../backend
uv sync --locked
npm --prefix app/cowork/skills/builtin/pptx/scripts/pptxgenjs ci

cd ../frontend
npm ci
npm run dev:desktop
```

完整说明见 [本地启动指南](../docs/17-本地启动指南.md)。发布安装包由 Tauri 构建链负责，
不通过本目录的 Compose 文件生成。原生机器上使用 `cd ../frontend && npm run bundle:desktop`；
它会先冻结并烟测 sidecar，再生成当前平台安装包。正式分发仍需平台代码签名与公证。

## Artifact Python 原生沙箱

WorkPilot 不依赖 Docker。`npm run bundle:desktop` 会额外冻结并烟测
`workpilot-artifact-python`，并把 PptxGenJS 与 Node 22 封装为独立的
`workpilot-pptx-renderer`；三者一起作为 Tauri external binary 放进安装包。
Office/PDF 依赖版本记录在 `artifact-runtime/requirements.lock`，构建入口还会逐项核对实际版本。

运行时在 macOS 使用 Seatbelt，在 Linux 使用 bubblewrap：输入与 Skill 只读、临时工作区和候选
输出可写、网络关闭。候选产物通过格式与路径校验后才由可信 sidecar 原子提交回授权目录；原生沙箱
或随包运行时缺失时直接失败，不会改用普通宿主 Shell。
