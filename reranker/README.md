# WorkPilot 本地 reranker

独立运行的 cross-encoder 服务。默认加载 `BAAI/bge-reranker-v2-m3`，不依赖也不启动生成式
大模型。服务只监听本机回环地址，并提供一个小而稳定的 HTTP 契约。

在仓库根目录执行；首次启动会下载约 2.3GB 权重到 Git 忽略的 `.cache/huggingface/`：

```bash
uv sync --project reranker --group dev
HF_HOME="$PWD/.cache/huggingface" \
  uv run --project reranker uvicorn reranker_service.main:app \
  --app-dir reranker --host 127.0.0.1 --port 8011
curl http://127.0.0.1:8011/health
```

Apple Silicon 默认使用 MPS，NVIDIA 环境默认使用 CUDA，其余环境使用 CPU。可通过环境变量覆盖：

```bash
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RERANKER_REVISION=main
RERANKER_DEVICE=auto
RERANKER_DTYPE=auto
RERANKER_BATCH_SIZE=4
RERANKER_MAX_LENGTH=512
```

`POST /v1/rerank` 接收问题和候选文档，返回按 `relevance_score` 降序排列的结果。模型在
进程启动时加载；后端若连接失败，会保持原检索顺序继续工作。
