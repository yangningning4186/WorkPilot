from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全部运行时设置；环境变量是唯一的部署覆盖入口。"""

    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    annotation_tool_enabled: bool = True
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://workpilot:workpilot@localhost:5432/workpilot"
    redis_url: str = "redis://localhost:6379/0"
    # Web/集群部署继续使用 Arq + Redis；桌面 sidecar 使用同进程队列，任务真相仍在
    # 持久化 store 中，并由轮询 dispatcher 补偿进程退出时丢失的内存唤醒。
    task_queue_backend: Literal["redis", "in_process"] = "redis"
    run_bus_backend: Literal["redis", "in_process"] = "redis"
    session_cookie_name: str = "workpilot_session"
    session_ttl_s: int = Field(default=30 * 60, ge=300, le=90 * 24 * 60 * 60)
    session_cookie_secure: bool | None = None
    admin_cookie_name: str = "workpilot_admin_session"
    admin_session_ttl_s: int = Field(default=8 * 60 * 60, ge=300, le=7 * 24 * 60 * 60)
    demo_admin_password_hash: str = ""
    ip_rate_limit_enabled: bool | None = None
    ip_rate_limit_per_minute: int = Field(default=20, ge=1, le=10_000)
    ip_rate_limit_burst: int = Field(default=5, ge=1, le=1_000)
    demo_session_question_limit: int = Field(default=20, ge=1, le=10_000)
    daily_cost_limit_usd: Decimal = Field(default=Decimal("5.00"), ge=0)
    cost_budget_timezone: str = "Asia/Shanghai"
    cost_reservation_ttl_s: int = Field(default=900, ge=60, le=7200)
    # 1 字符 = 1 token 是保守上界; 预留偏大只会多占额度, 估小则会穿透每日上限。
    cost_estimate_chars_per_token: float = Field(default=1.0, gt=0, le=8)
    # 每百万 token 单价。默认 0 = 本地自部署模型, 此时网关跳过预留直接调用。
    # 自建档位的"成本"不是 API 账单, 而是按整批 GPU wall time 摊销(docs/07 §7.2);
    # 这里的单价只用于预算闸门, 填 0 表示不占用每日美元额度。
    price_light_input_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    price_light_output_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    price_main_input_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    price_main_output_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    price_heavy_input_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    price_heavy_output_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    price_external_input_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    price_external_output_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    price_embedding_input_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    local_library_path: Path = Path("../data/library")
    # Agent 写回与资料库导入目录物理隔离；output_path 只能是该根目录内的相对 .md 路径。
    agent_output_path: Path = Path("../data/agent-output")
    # 办公工作台只加载受控大小的 Markdown；AI 改写进一步限制单次选区，避免把整本书
    # 塞进 light 档或让一个异常 replacement 撑爆浏览器。
    editor_max_document_chars: int = Field(default=500_000, ge=1_000, le=5_000_000)
    editor_max_selection_chars: int = Field(default=12_000, ge=100, le=100_000)
    editor_max_replacement_chars: int = Field(default=50_000, ge=100, le=500_000)
    editor_rewrite_max_tokens: int = Field(default=4_096, ge=64, le=32_768)
    editor_permission_ttl_s: int = Field(default=3_600, ge=300, le=8 * 60 * 60)
    workspace_max_file_bytes: int = Field(default=20 * 1024 * 1024, ge=1_024, le=200 * 1024 * 1024)
    workspace_max_files: int = Field(default=2_000, ge=1, le=20_000)
    workspace_max_scan_entries: int = Field(default=50_000, ge=100, le=2_000_000)
    workspace_max_excel_cells: int = Field(default=5_000, ge=10, le=100_000)
    workspace_max_excel_scan_cells: int = Field(default=200_000, ge=100, le=5_000_000)
    workspace_max_operations: int = Field(default=100, ge=1, le=1_000)
    workspace_backup_versions_per_file: int = Field(default=10, ge=1, le=100)
    workspace_edit_max_tokens: int = Field(default=8_192, ge=128, le=32_768)
    # Office 交付物预览使用系统 Quick Look 或 LibreOffice 做真实版面渲染。缓存属于
    # WorkPilot 自有数据，不得写到用户授权工作区中。
    office_preview_cache_path: Path = Path("../data/preview-cache")
    office_preview_timeout_s: float = Field(default=30.0, gt=0, le=180)
    office_preview_max_source_bytes: int = Field(
        default=50 * 1024 * 1024, ge=1_024, le=500 * 1024 * 1024
    )
    office_preview_max_cache_entries: int = Field(default=100, ge=1, le=2_000)
    # Cowork 是现有 answer/review 运行时上的第三种工作流。目录授权与 artifact API
    # 可先独立上线；真正的通用工具循环仍可用此总开关紧急关闭。
    cowork_enabled: bool = True
    # Cowork 本地存储采用兼容迁移：postgres 是现有实现，sqlite 是新的桌面实现。
    # RAG 的 documents/chunks/pgvector 不受这个开关影响。
    cowork_store_backend: Literal["postgres", "sqlite"] = "sqlite"
    cowork_data_path: Path = Path("~/.workpilot")
    cowork_dispatch_poll_s: float = Field(default=1.0, gt=0, le=30)
    cowork_max_steps: int = Field(default=30, ge=1, le=50)
    cowork_decision_max_tokens: int = Field(default=8_192, ge=128, le=16_384)
    # Cowork 的一次决策会携带工具 schema、网页结果和跨轮历史，明显比普通聊天更重。
    # 独立配置避免为了复杂任务放宽 Provider 探测等短请求的超时。
    cowork_model_timeout_s: float = Field(default=120.0, gt=0, le=600)
    cowork_tool_result_max_chars: int = Field(default=20_000, ge=1_000, le=100_000)
    cowork_file_read_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1_024, le=100 * 1024 * 1024)
    cowork_file_write_max_bytes: int = Field(
        default=5 * 1024 * 1024, ge=1_024, le=100 * 1024 * 1024
    )
    cowork_file_max_lines: int = Field(default=2_000, ge=1, le=50_000)
    cowork_search_max_results: int = Field(default=200, ge=1, le=2_000)
    cowork_pdf_text_max_chars: int = Field(default=60_000, ge=1_000, le=500_000)
    cowork_web_timeout_s: float = Field(default=30.0, gt=0, le=300)
    cowork_web_max_redirects: int = Field(default=5, ge=0, le=10)
    cowork_web_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1_024, le=100 * 1024 * 1024)
    cowork_web_text_max_chars: int = Field(default=60_000, ge=1_000, le=500_000)
    # Browser session 持有页面与登录态，不能随 worker 生命周期常驻。空闲 TTL 每次
    # 使用后顺延，让长时间的连续浏览不被中途掐断；绝对 TTL 永不顺延，是硬上限。
    cowork_browser_session_idle_ttl_s: int = Field(default=30 * 60, ge=60, le=24 * 60 * 60)
    cowork_browser_session_max_ttl_s: int = Field(default=4 * 60 * 60, ge=60, le=24 * 60 * 60)
    # 输入附件属于 WorkPilot 私有运行数据，不得写入用户授权工作区。单文件与单消息
    # 数量都在入口硬限制，防止上传或 checkpoint 被大文件撑爆。
    cowork_attachment_path: Path = Path("../data/cowork-attachments")
    cowork_attachment_max_bytes: int = Field(
        default=10 * 1024 * 1024, ge=1_024, le=100 * 1024 * 1024
    )
    cowork_attachment_max_count: int = Field(default=8, ge=1, le=20)
    cowork_attachment_text_max_chars: int = Field(default=60_000, ge=1_000, le=500_000)
    # 普通任务与未显式指定目录的自动化，都把新生成文件放进用户本机专用目录。
    # 其他本地目录仍须用户按需授权，不能把进程 cwd 或项目仓库当作默认目录。
    cowork_default_workspace_path: Path = Path("~/Documents/WorkPilot")
    # MCP 配置与人工 Skill 内容由本机管理员维护；自动蒸馏只能走下方独立候选与晋升门禁。
    cowork_mcp_config_path: Path = Path("../config/mcp.yaml")
    cowork_mcp_connect_timeout_s: float = Field(default=15.0, gt=0, le=120)
    cowork_mcp_call_timeout_s: float = Field(default=60.0, gt=0, le=600)
    cowork_mcp_result_max_chars: int = Field(default=20_000, ge=1_000, le=100_000)
    cowork_skills_path: Path = Path("../skills")
    cowork_skill_max_files: int = Field(default=200, ge=1, le=2_000)
    cowork_skill_max_bytes: int = Field(default=256 * 1024, ge=1_024, le=2 * 1024 * 1024)
    # 自动蒸馏先积累独立成功运行证据，再安装 learned-* Skill。高风险工具不会进入
    # 自动晋升候选；阈值不是模型自己决定，而是服务端确定性门禁。
    skill_distillation_enabled: bool = True
    skill_auto_promotion_enabled: bool = True
    skill_promotion_min_evidence: int = Field(default=3, ge=2, le=20)
    skill_promotion_min_confidence: float = Field(default=0.82, ge=0.0, le=1.0)
    skill_distillation_max_tokens: int = Field(default=900, ge=128, le=4_096)
    skill_distillation_job_lease_s: int = Field(default=120, ge=10, le=1_800)
    skill_distillation_job_max_attempts: int = Field(default=3, ge=1, le=10)
    # Provider API key、连接器凭证和 OAuth token 的数据库密文由此本机主密钥保护。
    # 主密钥不进入数据库、不进入 checkpoint，也不通过任何状态 API 返回。
    secret_store_key_path: Path = Path("../data/secrets/master.key")
    # canonical checkpoint 永远保留完整历史；这些参数只控制发给模型的 outbound 视图。
    cowork_compaction_enabled: bool = True
    cowork_compaction_trigger_ratio: float = Field(default=0.85, gt=0.0, le=1.0)
    cowork_compaction_keep_recent_tool_rounds: int = Field(default=2, ge=0, le=20)
    cowork_compaction_max_summary_chars: int = Field(default=4_000, ge=200, le=20_000)
    cowork_compaction_input_max_chars: int = Field(default=60_000, ge=1_000, le=500_000)
    cowork_compaction_max_tokens: int = Field(default=800, ge=64, le=4_096)
    # provider 实际窗口小于部署声明时，最多压缩并重试这么多次，防止 400 死循环。
    cowork_context_overflow_max_recoveries: int = Field(default=2, ge=1, le=5)
    # JSON 数组；条目按 shlex 解析后做 argv 精确前缀匹配。含 shell 操作符的命令
    # 永远不能命中 allowlist，只能逐命令展示并审批。
    cowork_shell_allowlist: list[str] = Field(default_factory=list)
    cowork_shell_timeout_s: float = Field(default=120.0, gt=0, le=3_600)
    cowork_shell_terminate_grace_s: float = Field(default=2.0, ge=0, le=30)
    cowork_shell_max_output_bytes: int = Field(default=64 * 1024, ge=1_024, le=4 * 1024 * 1024)
    cowork_cancel_poll_s: float = Field(default=0.5, ge=0.05, le=5.0)
    # 桌面壳启动 sidecar 时由父进程生成随机 token 并只通过进程环境传入。开启后，
    # 所有 HTTP 请求都必须携带固定 header，防止本机其他网页调用 localhost API。
    desktop_mode_enabled: bool = False
    desktop_launch_token: SecretStr = SecretStr("")
    # 三档 + external(docs/07 §1)。未部署的档位留空, 路由表加载时按"不可用"处理:
    # 线上沿 fallback 链下移并在启动时告警, 评测模式直接失败, 绝不静默替换。
    tier_light_base_url: str = ""
    tier_light_model: str = ""
    tier_light_enable_thinking: bool | None = None
    # 必须填写部署时真实的 max_model_len，而不是模型原生能力。Prompt 预算按这个值
    # 在发送前硬校验，避免把“模型支持 256K”误当成“当前 vLLM 也部署了 256K”。
    tier_light_context_window_tokens: int = Field(default=32_768, ge=1024, le=2_000_000)
    tier_main_base_url: str = "http://localhost:8000/v1"
    tier_main_model: str = "local-chat"
    tier_main_enable_thinking: bool | None = None
    tier_main_context_window_tokens: int = Field(default=102_400, ge=1024, le=2_000_000)
    tier_heavy_base_url: str = ""
    tier_heavy_model: str = ""
    tier_heavy_enable_thinking: bool | None = None
    tier_heavy_context_window_tokens: int = Field(default=1_048_576, ge=1024, le=2_000_000)
    tier_external_base_url: str = ""
    tier_external_model: str = ""
    tier_external_context_window_tokens: int = Field(default=128_000, ge=1024, le=2_000_000)
    # 预留给 ChatML 包装、token 估算误差和 provider 侧特殊 token。
    llm_context_safety_tokens: int = Field(default=512, ge=0, le=32_768)
    external_api_key: str = ""
    cluster_api_key: str = ""
    routing_config_path: Path = Path("../config/routing.yaml")
    # 精确缓存(docs/07 §6)。评测模式无论这里怎么配都不缓存: 命中意味着这一条
    # 根本没过模型, 却会被算进指标里。
    llm_cache_enabled: bool = True
    llm_cache_ttl_s: int = Field(default=24 * 60 * 60, ge=60, le=30 * 24 * 60 * 60)
    # Provider 侧 Prompt Cache 只复用 KV 前缀，不复用模型输出。evaluation 仍强制关闭
    # 显式写入，确保跑批延迟与 token 台账不被历史缓存污染。
    provider_prompt_cache_enabled: bool = True
    # 非官方 OpenAI-compatible 服务对 prompt_cache_key 的兼容性不统一。默认不发送，
    # 经端点能力验证后再打开；provider=openai 不受此开关影响。
    openai_compatible_prompt_cache_key_enabled: bool = False
    # 自建模型的等价云单价(docs/07 §7.2-7.3)。默认留空是**故意的**:
    # 编一个单价会让下游每一个成本数字都失去意义, 而且看不出来是编的。
    # 开批次前必须显式配置, 且 GPU_PRICE_SOURCE 要写得能被追溯。
    gpu_model: str = ""
    gpu_price_usd_per_hour: Decimal = Field(default=Decimal("0"), ge=0)
    gpu_price_source: str = ""
    gpu_node_count: int = Field(default=1, ge=1, le=64)
    embedding_base_url: str = ""
    embedding_model: str = "local-embedding"
    embedding_revision: str = "unversioned"
    embedding_dim: Literal[1024] = 1024
    model_timeout_s: float = Field(default=30.0, gt=0)
    model_trust_env: bool = False
    pdf_parser_mode: Literal["auto", "pymupdf", "mineru"] = "auto"
    pdf_parse_timeout_s: float = Field(default=120.0, gt=0, le=600)
    pdf_max_pages: int = Field(default=500, ge=1, le=2000)
    pdf_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    pdf_worker_memory_mb: int = Field(default=2048, ge=256, le=16384)
    pdf_worker_cpu_s: int = Field(default=120, ge=1, le=600)
    pdf_mineru_command: Path = Path("../.mineru/.venv/bin/mineru")
    pdf_mineru_revision: str = "3.4.4"
    pdf_mineru_backend: Literal[
        "pipeline",
        "vlm-engine",
        "hybrid-engine",
        "vlm-http-client",
        "hybrid-http-client",
    ] = "hybrid-engine"
    pdf_mineru_effort: Literal["medium", "high"] = "medium"
    pdf_mineru_method: Literal["auto", "txt", "ocr"] = "auto"
    pdf_mineru_timeout_s: float = Field(default=1800.0, gt=0, le=7200)
    pdf_mineru_fallback_enabled: bool = True
    pdf_mineru_processing_window_size: int = Field(default=4, ge=1, le=64)
    # run 预算上限(约束 5): 任一超限即熔断, 防止反思循环烧钱。
    run_budget_tokens: int = Field(default=200_000, ge=0)
    run_budget_calls: int = Field(default=40, ge=0)
    run_budget_wall_ms: int = Field(default=300_000, ge=1_000)
    run_lease_s: int = Field(default=60, ge=5, le=3600)
    # 心跳必须明显短于租约, 否则正常执行中的 run 会被 watchdog 误判为失联。
    run_heartbeat_s: float = Field(default=15.0, gt=0)
    # 自动恢复次数上限。稳定把 worker 拖垮的 run 必须停下来交给人, 不能无限重投。
    run_max_recovery: int = Field(default=3, ge=0, le=20)
    run_delta_flush_ms: int = Field(default=50, ge=10, le=1000)
    run_delta_flush_chars: int = Field(default=120, ge=1, le=4000)
    # pgvector 扫描参数(docs/03 §4.1)。部分索引只覆盖 strategy + is_searchable,
    # 其余过滤(embedding 身份、doc_type)仍在索引内进行, 靠迭代扫描兜底候选不足。
    hnsw_iterative_scan: Literal["off", "relaxed_order", "strict_order"] = "relaxed_order"
    hnsw_max_scan_tuples: int = Field(default=20_000, ge=1_000, le=1_000_000)
    # ef_search 必须不小于 top_k, 否则召回会被候选队列长度截断。
    hnsw_ef_search: int = Field(default=100, ge=1, le=1000)
    # 数值分数门必须绑定明确的排序器分数。默认关闭，因为现有 0.35 只在历史
    # 混合报告上扫过，而 dense cosine、RRF 与 cross-encoder 并不共享量纲；
    # 关闭时仍保留 fail-closed 的证据充分性门控。
    refusal_score_gate_source: Literal["disabled", "dense", "lexical", "fusion", "rerank"] = (
        "disabled"
    )
    refusal_threshold: float = Field(default=0.35, ge=-1.0, le=1.0)
    # margin 使用 (top1-top2)/abs(top1) 的相对差，避免随排序器量纲漂移。
    refusal_margin_threshold: float = Field(default=0.03, ge=0.0, le=1.0)
    evidence_gate_max_chars: int = Field(default=3000, ge=500, le=20000)
    rerank_evidence_gate_max_chars: int = Field(default=6000, ge=500, le=20000)
    evidence_gate_max_tokens: int = Field(default=300, ge=64, le=2048)
    query_decomposition_enabled: bool = False
    query_decomposition_max_subqueries: int = Field(default=4, ge=2, le=8)
    query_decomposition_max_tokens: int = Field(default=300, ge=64, le=2048)
    # 当前会话的短期上下文。只取已完成问答轮次；长期记忆仍是 owner 级跨会话层。
    conversation_context_enabled: bool = True
    # 原文历史不再固定卡在 6 回合 / 6000 字符；上限由当前回答模型的完整 token 窗口
    # 动态决定。这里仅保留数据库读取与异常输入的防御性上界。
    conversation_context_max_turns: int = Field(default=500, ge=0, le=2000)
    conversation_context_max_chars: int = Field(default=100_000, ge=200, le=500_000)
    contextual_query_rewrite_max_tokens: int = Field(default=300, ge=64, le=1024)
    # 未归档历史达到当前回答模型完整输入窗口的 90% 时滚动压缩。
    conversation_summary_enabled: bool = True
    conversation_summary_trigger_ratio: float = Field(default=0.9, gt=0.0, le=1.0)
    conversation_summary_keep_recent_turns: int = Field(default=4, ge=1, le=50)
    conversation_summary_max_chars: int = Field(default=2400, ge=200, le=10000)
    conversation_summary_input_max_chars: int = Field(default=100_000, ge=1000, le=500_000)
    conversation_summary_max_tokens: int = Field(default=600, ge=64, le=2048)
    # owner 长期记忆。demo 路径无视开关也不抽取/召回；两项仍可独立紧急关闭。
    memory_extraction_enabled: bool = True
    memory_recall_enabled: bool = True
    memory_job_lease_s: int = Field(default=120, ge=10, le=1800)
    memory_job_max_attempts: int = Field(default=3, ge=1, le=10)
    memory_recall_top_k: int = Field(default=5, ge=1, le=20)
    memory_pinned_limit: int = Field(default=3, ge=0, le=20)
    memory_context_max_chars: int = Field(default=2000, ge=200, le=10000)
    # 真实子问题的逐查询排名近似 gold coverage oracle；默认关闭，P1-K 验证后再决定上线。
    # 只在 query_decomposition 确实返回 >=2 个子问题时介入，简单题逐位回退原 RRF。
    coverage_selection_enabled: bool = False
    coverage_rank_cutoff: int = Field(default=10, ge=1, le=50)
    rerank_enabled: bool = False
    # 统一候选池深度：dense/lexical 各臂取候选后先做 RRF，并截到该深度；
    # 无论 rerank 是否开启都生效，避免线上 Top-5 与评测 Top-50 跑成两条链。
    # 字段名保留 rerank_candidate_k 以兼容已有部署环境变量。
    rerank_candidate_k: int = Field(default=50, ge=2, le=50)
    reranker_base_url: str = "http://127.0.0.1:8011"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_timeout_s: float = Field(default=10.0, gt=0, le=120)
    rerank_max_candidate_chars: int = Field(default=1200, ge=100, le=8000)
    rerank_candidate_text_mode: Literal["title_heading_content", "heading_content", "content"] = (
        "title_heading_content"
    )
    lexical_rrf_enabled: bool = True
    lexical_mode: Literal["ts_rank", "coverage", "ts_rank_cd"] = "ts_rank"
    rrf_k: int = Field(default=60, ge=1, le=1000)
    # 每篇文档在融合结果头部的名额上限（0 = 关闭）。跨文档题被单一文档霸榜的
    # 对策，见 retrieval/fusion.py::apply_document_cap 与台账 E7。
    # 默认关闭：它拿"集中"换"分散"，对答案本就集中在单篇的题有害，
    # 必须先在 70 条上做受控对照再决定默认值。
    document_cap_per_version: int = Field(default=0, ge=0, le=50)
    answer_max_evidence_chars: int = Field(default=12000, ge=1000, le=100000)
    answer_max_tokens: int = Field(default=1200, ge=64, le=8192)
    # 通用知识回答不可溯源, 刻意给得比资料库回答更短: 它是降级出口不是主路。
    general_answer_max_tokens: int = Field(default=800, ge=64, le=4096)

    @field_validator("embedding_dim", mode="before")
    @classmethod
    def parse_embedding_dim(cls, value: object) -> object:
        if isinstance(value, str) and value.isdecimal():
            return int(value)
        return value

    @model_validator(mode="after")
    def validate_desktop_launch_token(self) -> "Settings":
        token = self.desktop_launch_token.get_secret_value()
        if self.desktop_mode_enabled and len(token) < 32:
            raise ValueError("desktop 模式要求至少 32 字符的随机 launch token")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
