"""供进程内调用的轻量请求适配层。

请求是一段 JSON，``action`` 决定调用哪个服务方法，``actor`` 携带角色信息
``{"id": ..., "role": ...}``。对外查询不需要 actor，默认按公众视角。
"""
from __future__ import annotations

import json

from .service import AuthError, Service


def handle(payload: str, service: Service | None = None) -> str:
    service = service or Service()
    body = json.loads(payload)
    action = body.get("action")
    actor = body.get("actor")
    args = {k: v for k, v in body.items() if k not in ("action", "actor")}
    at = args.get("at")

    # ---- 无需鉴权 ----
    if action == "health":
        return json.dumps(service.health(), ensure_ascii=False)
    if action == "public_status":
        return json.dumps(service.public_status(args["merchant_id"], at=at),
                          ensure_ascii=False)
    if action == "evidence_chain":
        return json.dumps(
            service.evidence_chain(args["merchant_id"], actor, at=at),
            ensure_ascii=False)
    if action == "scan_expirations":
        return json.dumps(service.scan_expirations(at=at), ensure_ascii=False)
    if action == "reconcile":
        return json.dumps(service.reconcile_todos(at=at), ensure_ascii=False)
    if action == "snapshot":
        return json.dumps(service.snapshot(args["merchant_id"], at),
                          ensure_ascii=False)

    # ---- 需要登录但各方法自行鉴权 ----
    if actor is None:
        raise AuthError("该动作需要操作人信息")

    if action == "register":
        return json.dumps(
            service.register(str(args["record_id"]), str(args["owner_id"]),
                             args.get("name", ""), actor), ensure_ascii=False)
    if action == "dossier":
        return json.dumps(
            service.dossier(args["merchant_id"], actor, at=at), ensure_ascii=False)
    if action == "list_todos":
        return json.dumps(
            service.list_todos(actor, merchant_id=args.get("merchant_id"),
                               status=args.get("status", "open")), ensure_ascii=False)
    if action == "upload_material":
        return json.dumps(service.upload_material(
            args["merchant_id"], args["category"], args["content_ref"],
            args["valid_until"], actor, valid_from=args.get("valid_from"),
            at=at), ensure_ascii=False)
    if action == "review_version":
        return json.dumps(service.review_version(
            args["version_id"], args["decision"], args.get("comment", ""),
            actor, at=at), ensure_ascii=False)
    if action == "record_inspection":
        return json.dumps(service.record_inspection(
            args["merchant_id"], args["result"], actor,
            category=args.get("category"), comment=args.get("comment", ""),
            inspection_id=args.get("inspection_id"), at=at),
            ensure_ascii=False)
    if action == "resolve_remediation":
        return json.dumps(service.resolve_remediation(
            args["inspection_id"], args.get("version_id"), actor,
            comment=args.get("comment", ""), at=at), ensure_ascii=False)
    if action == "suspend":
        return json.dumps(service.suspend(
            args["merchant_id"], args.get("reason", ""), actor,
            suspension_id=args.get("suspension_id"), at=at),
            ensure_ascii=False)
    if action == "resume":
        return json.dumps(
            service.resume(args["suspension_id"], actor, at=at),
            ensure_ascii=False)
    if action == "raise_dispute":
        return json.dumps(service.raise_dispute(
            args["merchant_id"], args["ref_type"], args["ref_id"],
            args.get("reason", ""), actor, at=at), ensure_ascii=False)
    if action == "resolve_dispute":
        return json.dumps(service.resolve_dispute(
            args["dispute_id"], bool(args["upheld"]), actor,
            comment=args.get("comment", ""), at=at), ensure_ascii=False)
    raise ValueError("不支持的请求动作")
