# deploy

docker-compose、Dockerfile、初始化 SQL。

- M0：postgres+pgvector / redis
- M1 以后按需加入：minio / langfuse

前置：macOS 使用 OrbStack；当前开发机已安装并验证可用。

```bash
docker compose up -d
docker compose ps
```

数据库与 Redis 只绑定 `127.0.0.1`，不暴露到局域网。首次创建 PostgreSQL volume 时，
初始化脚本会同时创建隔离的 `workpilot_test` 数据库。
