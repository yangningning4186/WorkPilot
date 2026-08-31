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
    log_level: str = "INFO"
    # API 与 worker 同进程运行；内存队列只负责低延迟唤醒，任务真相始终在本地 store，
    # dispatcher 会轮询补偿进程退出时丢失的通知。
    admin_cookie_name: str = "workpilot_admin_session"
    admin_session_ttl_s: int = Field(default=8 * 60 * 60, ge=300, le=7 * 24 * 60 * 60)
    demo_admin_password_hash: str = ""
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
    # Office 工具只加载受控大小的 Markdown；AI 改写进一步限制单次选区，避免把整本书
    # 塞进 light 档或让一个异常 replacement 撑爆运行上下文。
    editor_max_document_chars: int = Field(default=500_000, ge=1_000, le=5_000_000)
    editor_max_selection_chars: int = Field(default=12_000, ge=100, le=100_000)
    editor_max_replacement_chars: int = Field(default=50_000, ge=100, le=500_000)
    editor_rewrite_max_tokens: int = Field(default=4_096, ge=64, le=32_768)
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
    office_preview_model_entries_per_run: int = Field(default=20, ge=1, le=200)
    office_preview_model_cache_max_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=16 * 1024 * 1024,
        le=10 * 1024 * 1024 * 1024,
    )
    # Cowork 是现有 answer/review 运行时上的第三种工作流。目录授权与 artifact API
    # 可先独立上线；真正的通用工具循环仍可用此总开关紧急关闭。
    cowork_enabled: bool = True
    # Cowork 控制面的落点。原来这里还有一个 `cowork_store_backend` 开关，
    # ADR-0012 退役 PostgreSQL 之后它只剩一个合法值——留着比删掉更危险：
    # 唯一的读者是启动时那句 `if backend == "sqlite"`，填成 "postgres"
    # 不会报错，只会静默跳过本地 store 初始化，然后每一次请求都撞
    # 「Cowork 本地 store 尚未初始化」。隔离改由 cowork_data_path 承担
    # （评测跑批把它指到自己的包目录里，见 eval/cowork_runner.py）。
    cowork_data_path: Path = Path("~/.workpilot")
    # 本地知识库的根目录，一个 KB 一个子目录。索引版一经发布不可变；每篇文档还会把
    # 原始字节按内容哈希固化到该 KB 的 sources/，因此源路径移动或覆盖后仍可重建和审计。
    # 这些都是用户本机私有数据，绝不进入仓库的 data/ 目录。
    knowledge_base_path: Path = Path("~/.workpilot/kb")
    cowork_dispatch_poll_s: float = Field(default=1.0, gt=0, le=30)
    # 仅用于上下文压缩、run 计量和费用预留。Cowork 主循环对支持省略该参数的 Provider
    # 不再下发 max_tokens，避免 reasoning 把客户端额度耗尽后留不出正文。
    cowork_decision_max_tokens: int = Field(default=8_192, ge=128, le=16_384)
    # Cowork 的一次决策会携带工具 schema、网页结果和跨轮历史，明显比普通聊天更重。
    # 独立配置避免为了复杂任务放宽 Provider 探测等短请求的超时。
    cowork_model_timeout_s: float = Field(default=120.0, gt=0, le=600)
    # Grounded-generation 会携带 12k 证据并允许 reasoning，30 秒的通用短任务超时会把
    # 多跳/表格题系统性截断。与 Cowork 分开配置，避免评测调参悄悄改变线上主循环。
    evaluation_generation_timeout_s: float = Field(default=120.0, gt=0, le=600)
    # 整条模型路由都超时时不丢弃长任务：释放 worker/租约，到点后从最新 checkpoint
    # 继续。次数与 worker 失联恢复共用 run_max_recovery，避免两种故障交替造成无限重投。
    cowork_provider_timeout_retry_base_s: float = Field(default=5.0, gt=0, le=300)
    cowork_provider_timeout_retry_max_s: float = Field(default=60.0, gt=0, le=900)
    cowork_tool_result_max_chars: int = Field(default=20_000, ge=1_000, le=100_000)
    cowork_file_read_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1_024, le=100 * 1024 * 1024)
    cowork_file_write_max_bytes: int = Field(
        default=5 * 1024 * 1024, ge=1_024, le=100 * 1024 * 1024
    )
    cowork_file_max_lines: int = Field(default=2_000, ge=1, le=50_000)
    cowork_search_max_results: int = Field(default=200, ge=1, le=2_000)
    cowork_pdf_text_max_chars: int = Field(default=60_000, ge=1_000, le=500_000)
    # 一份文档上最多留多少条持久化批注。上限存在的理由不是磁盘，是可读性：
    # 一页里几十个高亮等于没有高亮，而模型在长文里逐句标注是很容易发生的。
    cowork_reading_max_annotations: int = Field(default=200, ge=1, le=2_000)
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
    # 蒸馏候选与它的作业队列属于本机运行数据，不是仓库源码。旧默认
    # `../skills-candidates` 会让 pytest 与桌面端共用一条队列，最终把测试 run 当成真实
    # 用户证据；放进 ~/.workpilot 后由测试夹具随 cowork_data_path 一起显式隔离。
    cowork_skill_candidates_path: Path = Path("~/.workpilot/skills-candidates")
    # 长期记忆：注入块整体有上限，单条超过 preview 就截断并让模型按需 memory_read，
    # 避免一条几千字的记忆吃掉整个上下文预算。
    # save 是所有新增/改写的部署硬开关；extraction 只控制后台自动抽取流水线。两者分开，
    # 避免管理员只是停自动学习时连用户显式保存也一起关掉。
    memory_save_enabled: bool = True
    memory_recall_enabled: bool = True
    cowork_memory_max_items: int = Field(default=200, ge=1, le=500)
    cowork_memory_block_max_chars: int = Field(default=4_000, ge=0, le=40_000)
    cowork_memory_preview_chars: int = Field(default=240, ge=40, le=4_000)
    cowork_skill_max_files: int = Field(default=200, ge=1, le=2_000)
    cowork_skill_max_bytes: int = Field(default=256 * 1024, ge=1_024, le=2 * 1024 * 1024)
    # 自动蒸馏先积累独立成功运行证据，再安装 learned-* Skill。高风险工具不会进入
    # 自动晋升候选；阈值不是模型自己决定，而是服务端确定性门禁。
    skill_distillation_enabled: bool = True
    # Skill 会改变未来会话遵循的指令，默认只产出候选并等待用户在管理界面晋升。
    skill_auto_promotion_enabled: bool = False
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
    # 模型只拿尾部短视图；完整输出写入授权工作区供后续 search/grep。仍设磁盘硬上限，
    # 防止无限刷 stdout 的命令在超时前耗尽磁盘。
    cowork_shell_full_output_max_bytes: int = Field(
        default=64 * 1024 * 1024, ge=1_024, le=256 * 1024 * 1024
    )
    # macOS/Linux 优先使用原生隔离；Windows 的 auto 保留 Docker/Podman 后端。
    # 所有后端都 fail closed，绝不退回 host.execute。
    cowork_sandbox_runtime: Literal["auto", "disabled", "native", "docker", "podman"] = "auto"
    cowork_sandbox_python_path: Path | None = None
    cowork_sandbox_profile: str = "artifact-python:1.0.0"
    cowork_sandbox_image: str = "workpilot-artifact-python:1.0.0"
    cowork_sandbox_memory_mb: int = Field(default=512, ge=64, le=16_384)
    cowork_sandbox_pids_limit: int = Field(default=128, ge=16, le=4_096)
    cowork_sandbox_cpus: float = Field(default=1.0, gt=0, le=16)
    # 前台 Shell 完成后只对这批候选做格式校验与 Artifact 登记。扫描条目总量仍受
    # workspace_max_scan_entries 约束，两个上限分别控制磁盘遍历和 UI 产物洪泛。
    cowork_shell_artifact_max_files: int = Field(default=100, ge=1, le=2_000)
    # 后台 shell 任务。进程活在 worker 内存里，所以三个上限都是硬约束而不是建议：
    # 没人来收的后台进程会一直占着这台机器。
    cowork_shell_background_max_tasks: int = Field(default=4, ge=1, le=32)
    cowork_shell_background_output_max_bytes: int = Field(
        default=256 * 1024, ge=4_096, le=8 * 1024 * 1024
    )
    cowork_shell_background_ttl_s: float = Field(default=3_600.0, gt=0, le=86_400)
    # 飞书事件回调。没有 encrypt_key 就没有验签能力，那个入口会直接关掉——
    # 接受未验签的事件等于接受任何人伪造的"用户批准了那条命令"。
    cowork_feishu_encrypt_key: str | None = None
    # 机器人自己的 open_id。判断"这条消息 @了我吗"要用它；没配就等于永远没被 @到。
    cowork_feishu_bot_open_id: str | None = None
    # 只读 git 视图的单次输出上限。补丁很容易上兆，截断后由工具提示模型改用 stat_only。
    cowork_git_output_max_bytes: int = Field(default=256 * 1024, ge=4 * 1024, le=4 * 1024 * 1024)
    # wake_on 单次等待上限。它不释放 worker（进程活在这个 worker 里），所以上限的
    # 意义是「一个卡住的后台任务最多霸占一个槽位多久」，不宜配得比后台任务 TTL 还长。
    cowork_wake_on_max_s: float = Field(default=3_600.0, gt=0, le=86_400)
    # 自唤醒单次上限。超过这个量级应该走 create_schedule：那条路不依赖某个 run 一直挂着。
    cowork_sleep_max_s: float = Field(default=6 * 3_600.0, gt=0, le=86_400)
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
    # 精确缓存改成进程内 LRU（重启即失效），必须封顶：Redis 有 maxmemory 兜底，
    # 进程内没有，不封顶就是一条随运行时长单调增长的内存曲线。
    llm_cache_max_entries: int = Field(default=512, ge=1, le=100_000)
    # Provider 侧 Prompt Cache 只复用 KV 前缀，不复用模型输出。evaluation 仍强制关闭
    # 显式写入，确保跑批延迟与 token 台账不被历史缓存污染。
    provider_prompt_cache_enabled: bool = True
    # 同一 endpoint 上先吸收瞬时 429/5xx，再允许路由 fallback。配额耗尽的 429 不重试。
    llm_provider_max_retries: int = Field(default=2, ge=0, le=10)
    llm_provider_retry_base_delay_s: float = Field(default=0.5, ge=0, le=60)
    llm_provider_retry_max_delay_s: float = Field(default=8.0, ge=0, le=300)
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
    # 单次 run 的可选安全上限；0 表示不限制但仍持续记账。桌面 Cowork 默认允许长任务，
    # 由取消、重复调用刹车、上下文压缩与每日费用上限兜底。无人值守部署可按需设正数。
    run_budget_tokens: int = Field(default=0, ge=0)
    run_budget_calls: int = Field(default=0, ge=0)
    run_budget_wall_ms: int = Field(default=0, ge=0)
    run_lease_s: int = Field(default=60, ge=5, le=3600)
    # 心跳必须明显短于租约, 否则正常执行中的 run 会被 watchdog 误判为失联。
    run_heartbeat_s: float = Field(default=15.0, gt=0)
    # 自动恢复次数上限。稳定把 worker 拖垮的 run 必须停下来交给人, 不能无限重投。
    run_max_recovery: int = Field(default=3, ge=0, le=20)
    run_delta_flush_ms: int = Field(default=50, ge=10, le=1000)
    run_delta_flush_chars: int = Field(default=120, ge=1, le=4000)
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
    # owner 长期记忆。demo 路径无视开关也不抽取；召回开关和实际 Cowork 注入配置
    # 放在上面的 cowork memory 段，避免两套 top_k/context/pinned 参数互相冒充生效。
    memory_extraction_enabled: bool = True
    memory_job_lease_s: int = Field(default=120, ge=10, le=1800)
    memory_job_retry_delay_s: int = Field(default=30, ge=0, le=3600)
    memory_job_max_attempts: int = Field(default=3, ge=1, le=10)
    # 真实子问题的逐查询排名近似 gold coverage oracle；默认关闭，P1-K 验证后再决定上线。
    # 只在 query_decomposition 确实返回 >=2 个子问题时介入，简单题逐位回退原 RRF。
    coverage_selection_enabled: bool = False
    coverage_rank_cutoff: int = Field(default=10, ge=1, le=50)
    # 文件系统 KB 的本地 cross-encoder 精排。服务不可用时保留 RRF 原排序，不影响可用性。
    # 默认仍关闭：桌面包尚未内置约 2.3GB 的模型；开发/高级用户启动本机服务后显式开启。
    rerank_enabled: bool = False
    # 线上默认返回 Top-5；实验表明 RRF Top-10 → rerank → Top-5 在基本不增加证据 token 的
    # 前提下显著改善排序，而历史 Top-50 会把纯精排延迟重新推到约 4 秒。
    rerank_candidate_k: int = Field(default=10, ge=2, le=50)
    reranker_base_url: str = "http://127.0.0.1:8011"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_timeout_s: float = Field(default=3.0, gt=0, le=120)
    rerank_max_candidate_chars: int = Field(default=1200, ge=100, le=8000)
    rerank_candidate_text_mode: Literal["title_heading_content", "heading_content", "content"] = (
        "content"
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
        if self.cowork_provider_timeout_retry_base_s > self.cowork_provider_timeout_retry_max_s:
            raise ValueError("Cowork Provider 超时重试的基础退避不能大于最大退避")
        if self.llm_provider_retry_base_delay_s > self.llm_provider_retry_max_delay_s:
            raise ValueError("Provider 响应重试的基础退避不能大于最大退避")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
