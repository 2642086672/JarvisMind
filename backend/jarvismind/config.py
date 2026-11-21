"""JME runtime configuration loaded from environment variables.

Every value is read from the process environment so the service can run from a
PowerShell script, a test harness, or a container without code changes.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

VERSION = "1.0.0"
SERVICE_NAME = "JarvisMind"


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value != "" else default


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}, got {parsed}"
        )
    return parsed


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {parsed}")
    return parsed


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_str(name).lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings shared by every layer."""

    version: str = VERSION
    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8777
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    api_token: str = ""
    cors_origins: Tuple[str, ...] = ()
    database_url: str = ""
    db_echo: bool = False
    # LLM extraction settings. Empty base_url/model means extraction falls back
    # to the deterministic local engine.
    llm_provider_name: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: float = 20.0
    llm_max_tokens: int = 800
    llm_budget_cny: float = 0.0
    llm_price_in: float = 0.0
    llm_price_out: float = 0.0
    llm_auto_save_threshold: float = 0.85
    # 003-B (D1): AUTO_SAVE is OFF on first launch and can only be enabled by
    # an explicit user action after security acceptance. The old
    # confidence-threshold auto-approval path has been removed entirely.
    auto_save_enabled: bool = False

    # Hybrid search & vector index settings (JME-4).
    qdrant_collection: str = "memories"
    qdrant_path_override: str = ""
    embedding_provider: str = "fastembed"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dimension: int = 512
    hybrid_rrf_k: int = 60
    hybrid_vector_weight: float = 1.0
    hybrid_fts_weight: float = 1.0
    hybrid_max_candidates: int = 50
    # JME-MEMORY-CONTEXT-01: chat memory-context retrieval. The relevance floor
    # is the fraction of the question's topic tokens a memory must contain; a
    # lower value injects more, possibly unrelated memories. Tunable per install
    # without a schema change.
    memory_context_min_match_ratio: float = 0.4
    memory_context_candidate_limit: int = 40
    # P1-01 unified governance: OFF by default so the existing JME regression
    # keeps its exact behaviour. Enable explicitly with
    # JARVISMIND_P1_GOVERNANCE_ENABLED=1 after the four P0 fixes are verified.
    p1_governance_enabled: bool = False
    # JW-00: direct-write endpoints (POST /memories, PATCH /memories/{id})
    # bypass the candidate approval chain, so they are OFF by default. Enable
    # only with JARVISMIND_DIRECT_WRITE_ENABLED=1 when an operator explicitly needs
    # them. DELETE stays available for the iOS "forget" flow (JW-00 scope).
    direct_write_enabled: bool = False
    # JW-02: the four remaining lifecycle entries. Each has a DIFFERENT business
    # semantic, so each gets its own switch rather than one blanket flag:
    #
    #   import_enabled  - administrative bulk ingestion. OFF by default: it can
    #                     create authoritative memories with arbitrary ids and
    #                     statuses, so it must be a deliberate operator action.
    #   merge_enabled   - destructive-ish (creates a product, marks originals
    #                     MERGED). ON by default: it requires two existing
    #                     memories, refuses already-merged sources, and can only
    #                     ever produce one product per merge request.
    #   summary_review_enabled / correction_resolve_enabled - user decisions on
    #                     objects JME itself generated. ON by default: they are
    #                     the intended confirm step of the reflection flow.
    lifecycle_import_enabled: bool = False
    lifecycle_merge_enabled: bool = True
    lifecycle_summary_review_enabled: bool = True
    lifecycle_correction_resolve_enabled: bool = True
    # JW-01: lifetime of the one-time forget confirmation token, in seconds.
    # Short by design: the user confirms within the same interaction, so a
    # leaked token has a small window. Not a security switch, so it has a
    # working default rather than being off by default.
    forget_token_ttl_seconds: int = 300
    # P0-1: the JKG worker is OFF by default. It is a pull-based consumer (no
    # thread, no timer, no asyncio task), so "off" means nothing drains the
    # audit feed on its own — which is the auditable default. An operator turns
    # it on deliberately and drives it from its own scheduler.
    jkg_worker_enabled: bool = False
    # P0-2: how long one acquisition stays valid. Bounds how long a crashed run
    # can block the slot: after this the lease may be taken over. Short enough
    # that a dead run is not a permanent outage, long enough that a slow batch
    # does not lose its lease mid-flight.
    jkg_worker_lease_ttl_seconds: int = 300
    # P0-1: default batch size for one ``run_once``. Clamped by the lifecycle's
    # own ``MAX_SYNC_LIMIT``, so a larger value here cannot widen that ceiling.
    jkg_worker_batch_limit: int = 200
    # P0-1: run a reconcile page every N runs. 0 disables it entirely (the
    # default): reconcile repairs divergences that a normal tick cannot detect,
    # so it is a deliberate maintenance action, not an automatic side effect.
    jkg_worker_reconcile_every: int = 0
    # v1 API limits: requests per minute in a sliding 60-second window, per
    # process. Owner and agent traffic are bucketed separately; failed
    # authentication attempts are bucketed per client digest.
    api_rate_limit_owner_per_minute: int = 600
    api_rate_limit_agent_per_minute: int = 120
    api_auth_failure_limit_per_minute: int = 20
    # Optional directory with the built JarvisMind Console (served at /console).
    console_dir: str = ""

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jarvismind.db"

    @property
    def token_file(self) -> Path:
        return self.data_dir / ".jarvismind_token"

    @property
    def qdrant_path(self) -> str:
        if self.qdrant_path_override:
            return self.qdrant_path_override
        return str(self.data_dir / "qdrant")

    @property
    def models_cache_dir(self) -> Path:
        return self.data_dir / "models"


def _resolve_data_dir(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _load_token(data_dir: Path, configured: str) -> str:
    """Return the configured token, or load/generate the local token file."""
    if configured:
        return configured
    token_file = data_dir / ".jarvismind_token"
    if token_file.exists():
        existing = token_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    data_dir.mkdir(parents=True, exist_ok=True)
    # The mode is applied when the file is created, so the token is never readable
    # by other users, not even briefly before the permissions are tightened.
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        # Windows has no POSIX permission bits; the file still lives in the
        # private data directory.
        pass
    return token


def load_settings() -> Settings:
    """Build settings from the environment, failing fast on bad values."""
    data_dir = _resolve_data_dir(_env_str("JARVISMIND_DATA_DIR", "data"))
    allow_remote = _env_bool("JARVISMIND_ALLOW_REMOTE", False)

    host = _env_str("JARVISMIND_HOST", "0.0.0.0" if allow_remote else "127.0.0.1")
    if host in {"0.0.0.0", "::", "*"} and not allow_remote:
        raise ValueError(
            "JARVISMIND_HOST binds to all interfaces but JARVISMIND_ALLOW_REMOTE "
            "is not enabled"
        )
    origins = tuple(
        item.strip()
        for item in _env_str("JARVISMIND_CORS_ORIGINS").split(",")
        if item.strip()
    )
    db_path = data_dir / "jarvismind.db"
    return Settings(
        env=_env_str("JARVISMIND_ENV", "development"),
        host=host,
        port=_env_int("JARVISMIND_PORT", 8777, 1, 65535),
        data_dir=data_dir,
        log_level=_env_str("JARVISMIND_LOG_LEVEL", "INFO").upper(),
        api_token=_load_token(data_dir, _env_str("JARVISMIND_API_TOKEN")),
        cors_origins=origins,
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
        db_echo=_env_bool("JARVISMIND_DB_ECHO", False),
        llm_provider_name=_env_str("JARVISMIND_LLM_PROVIDER", "openai_compatible"),
        llm_base_url=_env_str("JARVISMIND_LLM_BASE_URL"),
        llm_api_key=_env_str("JARVISMIND_LLM_API_KEY"),
        llm_model=_env_str("JARVISMIND_LLM_MODEL"),
        llm_timeout=_env_float("JARVISMIND_LLM_TIMEOUT", 20.0, minimum=0.1),
        llm_max_tokens=_env_int("JARVISMIND_LLM_MAX_TOKENS", 800, 64, 8192),
        llm_budget_cny=_env_float("JARVISMIND_LLM_BUDGET_CNY", 0.0),
        llm_price_in=_env_float("JARVISMIND_LLM_PRICE_IN", 0.0),
        llm_price_out=_env_float("JARVISMIND_LLM_PRICE_OUT", 0.0),
        llm_auto_save_threshold=_env_float(
            "JARVISMIND_LLM_AUTO_SAVE_THRESHOLD", 0.85, minimum=0.0
        ),
        auto_save_enabled=_env_bool("JARVISMIND_AUTO_SAVE_ENABLED", False),

        p1_governance_enabled=_env_bool("JARVISMIND_P1_GOVERNANCE_ENABLED", False),
        direct_write_enabled=_env_bool("JARVISMIND_DIRECT_WRITE_ENABLED", False),
        # JW-02: import is the only lifecycle entry that is OFF by default
        # (it can fabricate authoritative rows); the other three are the
        # intended user-confirmation steps and stay available.
        lifecycle_import_enabled=_env_bool("JARVISMIND_LIFECYCLE_IMPORT_ENABLED", False),
        lifecycle_merge_enabled=_env_bool("JARVISMIND_LIFECYCLE_MERGE_ENABLED", True),
        lifecycle_summary_review_enabled=_env_bool(
            "JARVISMIND_LIFECYCLE_SUMMARY_REVIEW_ENABLED", True
        ),
        lifecycle_correction_resolve_enabled=_env_bool(
            "JARVISMIND_LIFECYCLE_CORRECTION_RESOLVE_ENABLED", True
        ),
        forget_token_ttl_seconds=_env_int(
            "JARVISMIND_FORGET_TOKEN_TTL_SECONDS", 300, 30, 3600
        ),
        jkg_worker_enabled=_env_bool("JARVISMIND_JKG_WORKER_ENABLED", False),
        jkg_worker_lease_ttl_seconds=_env_int(
            "JARVISMIND_JKG_WORKER_LEASE_TTL_SECONDS", 300, 30, 3600
        ),
        jkg_worker_batch_limit=_env_int("JARVISMIND_JKG_WORKER_BATCH_LIMIT", 200, 1, 500),
        jkg_worker_reconcile_every=_env_int(
            "JARVISMIND_JKG_WORKER_RECONCILE_EVERY", 0, 0, 100000
        ),
        qdrant_collection=_env_str("JARVISMIND_QDRANT_COLLECTION", "memories"),
        qdrant_path_override=_env_str("JARVISMIND_QDRANT_PATH", ""),
        embedding_provider=_env_str("JARVISMIND_EMBEDDING_PROVIDER", "fastembed"),
        embedding_model=_env_str("JARVISMIND_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
        embedding_dimension=_env_int("JARVISMIND_EMBEDDING_DIMENSION", 512, 1, 8192),
        hybrid_rrf_k=_env_int("JARVISMIND_HYBRID_RRF_K", 60, 1, 1000),
        hybrid_vector_weight=_env_float("JARVISMIND_HYBRID_VECTOR_WEIGHT", 1.0, minimum=0.0),
        hybrid_fts_weight=_env_float("JARVISMIND_HYBRID_FTS_WEIGHT", 1.0, minimum=0.0),
        hybrid_max_candidates=_env_int("JARVISMIND_HYBRID_MAX_CANDIDATES", 50, 1, 500),
        memory_context_min_match_ratio=_env_float(
            "JARVISMIND_CONTEXT_MIN_MATCH_RATIO", 0.4, minimum=0.0
        ),
        memory_context_candidate_limit=_env_int(
            "JARVISMIND_CONTEXT_CANDIDATE_LIMIT", 40, 10, 100
        ),
        api_rate_limit_owner_per_minute=_env_int(
            "JARVISMIND_RATE_LIMIT_OWNER_PER_MINUTE", 600, 0, 100000
        ),
        api_rate_limit_agent_per_minute=_env_int(
            "JARVISMIND_RATE_LIMIT_AGENT_PER_MINUTE", 120, 0, 100000
        ),
        api_auth_failure_limit_per_minute=_env_int(
            "JARVISMIND_AUTH_FAILURE_LIMIT_PER_MINUTE", 20, 0, 100000
        ),
        console_dir=_env_str("JARVISMIND_CONSOLE_DIR", ""),
    )
