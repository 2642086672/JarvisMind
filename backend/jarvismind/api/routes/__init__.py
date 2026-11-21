from .context import router as context_router
from .embedding import router as embedding_router
from .extraction import router as extraction_router
from .forget import memory_router as forget_memory_router
from .forget import request_router as forget_request_router
from .governance import router as governance_router
from .health import router as health_router
from .hybrid import router as hybrid_router
from .jmr import router as jmr_router
from .jmr_auto_execute import router as jmr_auto_execute_router
from .lifecycle import router as lifecycle_router
from .memories import router as memories_router
from .modification import memory_router as modification_memory_router
from .modification import request_router as modification_request_router
from .overview import router as overview_router
from .sources import router as sources_router
from .system import router as system_router

__all__ = [
    "context_router",
    "embedding_router",
    "extraction_router",
    "forget_memory_router",
    "forget_request_router",
    "governance_router",
    "health_router",
    "hybrid_router",
    "jmr_router",
    "jmr_auto_execute_router",
    "lifecycle_router",
    "memories_router",
    "modification_memory_router",
    "modification_request_router",
    "overview_router",
    "sources_router",
    "system_router",
]
