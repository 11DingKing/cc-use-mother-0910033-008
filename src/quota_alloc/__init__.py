"""年度配额公平分配服务。

模块组成：
- allocation：确定性分配内核（保底/上限水填 + 最大余数尾差）
- storage：SQLite 存储与原子事务
- services：业务编排（快照、规则版本、试算、发布、放弃/撤销/递补）
- api：零依赖 HTTP 接口
"""
from .allocation import (
    AllocationLine,
    AllocationResult,
    Subject,
    allocate,
    build_line_reasons,
    score_int,
    validate_rule_params,
)
from .services import ApiError, QuotaService
from .storage import Repository

__all__ = [
    "AllocationLine",
    "AllocationResult",
    "Subject",
    "allocate",
    "build_line_reasons",
    "score_int",
    "validate_rule_params",
    "ApiError",
    "QuotaService",
    "Repository",
]
