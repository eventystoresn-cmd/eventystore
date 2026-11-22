"""Journal d'audit des actions sensibles (cahier §13.5)."""
from flask import has_request_context, request
from flask_login import current_user

from ..extensions import db
from ..models import AuditLog


def log(action, target=None, target_type=None, target_id=None, actor=None, **meta):
    if target is not None:
        target_type = target_type or target.__class__.__name__
        target_id = target_id or getattr(target, "id", None)
    if actor is None and has_request_context() and current_user.is_authenticated:
        actor = current_user
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) if has_request_context() else None
    entry = AuditLog(actor_id=getattr(actor, "id", None), action=action, target_type=target_type,
                     target_id=target_id, meta=meta or None, ip=(ip or "")[:64])
    db.session.add(entry)
    return entry
