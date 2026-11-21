"""C-1 · 容器必须把已接线的 memory_service 注入 AutoMemoryService。

审计 C-1 的根因之一是容器只传 manager：AutoMemoryService 内部构造的
LifecycleService 落到裸 MemoryService（无 embedding / vector_store），
auto_merge / auto_expire 的向量钩子静默空转。这里用真实容器（deterministic
embedding + :memory: Qdrant，均来自共享 settings/manager 夹具）断言：

1. 注入的 memory_service 与容器共享同一 embedding / vector_store 实例；
2. 真实执行 auto_merge 后，来源向量点被删除、产物被重索引。

全程临时库，不触碰生产库与生产服务。
"""
from __future__ import annotations

from jarvismind.container import Container
from jarvismind.schemas.memory import MemoryCreate, MemoryType


def test_container_auto_memory_shares_the_wired_vector_backend(settings, manager):
    container = Container(settings=settings, manager=manager, logger=None)
    auto = container.auto_memory_service
    wired = auto._memory_service
    assert wired is not None, "容器必须注入已接线的 memory_service"
    assert wired.embedding is container.embedding_provider
    assert wired.vector_store is container.vector_store


def test_container_auto_merge_really_cleans_vector_points(settings, manager):
    container = Container(settings=settings, manager=manager, logger=None)
    svc = container.memory_service
    store = container.vector_store
    a = svc.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="用户偏好使用中文交流，喜欢简洁直接的回答",
        )
    )
    b = svc.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="用户偏好使用中文交流，喜欢简洁直接的回答。",
        )
    )
    assert {a.id, b.id} <= set(store.get_all_point_ids())

    report = container.auto_memory_service.auto_merge()
    assert report["status"] == "MERGED"
    assert len(report["merged"]) == 1
    product_id = report["merged"][0]["memory_id"]

    points = set(store.get_all_point_ids())
    assert a.id not in points
    assert b.id not in points
    assert product_id in points