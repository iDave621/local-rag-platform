from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    app_env: str = 'production'
    cookie_secure: bool = True
    session_timeout_seconds: int = 900
    chat_retention_days: int = 7
    chat_result_timeout_seconds: int = 240
    technician_rate_limit_per_minute: int = 12
    technician_max_message_chars: int = 4000
    admin_rate_limit_per_minute: int = 60
    login_rate_limit_per_minute: int = 10
    login_account_rate_limit_per_minute: int = 20
    database_url: str = 'postgresql+psycopg://rag:change-me@postgres:5432/rag'
    redis_url: str = 'redis://redis:6379/0'
    chroma_host: str = 'chroma'
    chroma_port: int = 8000
    chroma_ssl: bool = False
    chroma_auth_token: str = ''
    chroma_ca_file: str = ''
    chroma_collection: str = 'technical_kb'
    data_root: str = '/data'
    ollama_base_url: str = 'http://ollama:11434'
    ollama_chat_model: str = 'qwen3:4b-instruct'
    ollama_embed_model: str = 'embeddinggemma'
    ollama_vision_model: str = 'qwen2.5vl:3b'
    ollama_context_length: int = Field(default=4096, ge=1024)
    ollama_request_timeout_seconds: int = 240
    app_max_inflight_generations: int = 1
    retrieval_top_k: int = Field(default=6, ge=1, le=30)
    relevance_threshold: float = .20
    max_history_messages: int = 6
    rag_output_token_budget: int = 768
    rag_prompt_token_budget: int = 0
    rag_history_token_budget: int = 600
    vision_enabled: bool = False
    vision_max_images_per_document: int = 100
    vision_max_image_bytes: int = 12 * 1024 * 1024
    vision_max_dimension: int = 1800
    vision_min_width: int = 96
    vision_min_height: int = 96
    vision_request_timeout_seconds: int = 180
    ocr_enabled: bool = True
    ocr_languages: str = 'eng+heb'
    ocr_timeout_seconds: int = 120
    ocr_pdf_dpi: int = 200
    ocr_min_text_chars: int = 24
    legacy_doc_conversion_timeout_seconds: int = 120
    literature_site_url: str = ''
    literature_sync_interval_seconds: int = 900
    literature_request_timeout_seconds: int = 45
    literature_crawl_max_pages: int = 1000
    literature_verify_tls: bool = True
    literature_auth_header: str = ''
    literature_auth_value: str = ''
    admin_upload_max_bytes: int = 25 * 1024 * 1024
    admin_docx_max_uncompressed_bytes: int = 200 * 1024 * 1024
    admin_pdf_max_pages: int = 1000
    queue_result_ttl_seconds: int = 900
    job_max_attempts: int = 3
    job_lease_seconds: int = Field(default=120, ge=30)
    job_heartbeat_seconds: int = Field(default=20, ge=1)
    job_retention_days: int = 30
    worker_health_timeout_seconds: int = 150
    ingest_chunk_size: int = 1000
    ingest_chunk_overlap: int = 150
    worker_queues: str = 'high,medium,low'

    @property
    def data_path(self) -> Path:
        return Path(self.data_root)

    @property
    def literature_headers(self):
        return {self.literature_auth_header: self.literature_auth_value} if self.literature_auth_header and self.literature_auth_value else {}


@lru_cache
def get_settings():
    return Settings()
