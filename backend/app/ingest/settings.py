"""Settings → 解析器配置的适配器。

放在共享的 `app/ingest/` 而不是 `app/rag/`：Cowork 的 `read_local_pdf` 工具同样要
按部署配置构造 `PdfParserConfig`，它不该为了拿一个配置去 import RAG 的入库模块。
"""

from app.core.config import Settings
from app.ingest.pdf import PdfParserConfig


def pdf_parser_config_from_settings(settings: Settings) -> PdfParserConfig:
    return PdfParserConfig(
        mode=settings.pdf_parser_mode,
        timeout_s=settings.pdf_parse_timeout_s,
        max_pages=settings.pdf_max_pages,
        memory_mb=settings.pdf_worker_memory_mb,
        cpu_seconds=settings.pdf_worker_cpu_s,
        mineru_command=settings.pdf_mineru_command,
        mineru_revision=settings.pdf_mineru_revision,
        mineru_backend=settings.pdf_mineru_backend,
        mineru_effort=settings.pdf_mineru_effort,
        mineru_method=settings.pdf_mineru_method,
        mineru_timeout_s=settings.pdf_mineru_timeout_s,
        mineru_fallback_enabled=settings.pdf_mineru_fallback_enabled,
        mineru_processing_window_size=settings.pdf_mineru_processing_window_size,
    )
