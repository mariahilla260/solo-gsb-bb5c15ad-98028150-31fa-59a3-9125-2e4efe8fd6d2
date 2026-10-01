"""统一的 4xx 错误构造。"""
from fastapi import HTTPException


def blocked(code: str, message: str, **extra) -> HTTPException:
    detail = {"code": code, "message": message}
    detail.update(extra)
    # 422：业务上不满足放行/更正条件
    return HTTPException(status_code=422, detail=detail)


def conflict(code: str, message: str, **extra) -> HTTPException:
    detail = {"code": code, "message": message}
    detail.update(extra)
    # 409：版本冲突 / 区间冲突 / 唯一约束冲突等并发或状态冲突
    return HTTPException(status_code=409, detail=detail)


def not_found(resource: str, identifier: object) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "NOT_FOUND", "message": f"{resource} 不存在: {identifier}"},
    )
