"""Messagerie intégrée : toute conversation est rattachée à un contexte (devis ou commande)."""
from ..extensions import db
from ..models import Conversation, Message, QuoteRequest, Order, utcnow
from .notifications import notify

WARNING = "Ne communiquez jamais de numéro de carte, de code secret ou de mot de passe dans la messagerie."

TEMPLATES = {
    "vendor": [
        ("Disponibilité confirmée", "Bonjour, je confirme ma disponibilité à la date demandée. Je vous envoie mon devis rapidement."),
        ("Demande de précision", "Bonjour, pourriez-vous préciser le nombre d'invités, le lieu exact et vos attentes principales ?"),
        ("Envoi de devis", "Bonjour, vous trouverez mon devis détaillé dans votre espace. N'hésitez pas à me poser vos questions."),
        ("Confirmation", "Merci pour votre confiance ! Votre réservation est confirmée, je reste disponible pour toute question."),
        ("Rappel de paiement", "Bonjour, je vous rappelle que le règlement de l'acompte est nécessaire pour confirmer définitivement votre réservation."),
    ],
    "buyer": [],
}


def get_or_create(context_type, context_id, buyer_id, vendor_user_id, subject):
    conv = Conversation.query.filter_by(context_type=context_type, context_id=context_id).first()
    if conv is None:
        conv = Conversation(context_type=context_type, context_id=context_id, buyer_id=buyer_id,
                            vendor_user_id=vendor_user_id, subject=subject)
        db.session.add(conv)
        db.session.flush()
    return conv


def for_quote(req):
    return get_or_create("quote", req.id, req.buyer_id, req.vendor.user_id, f"Demande de devis {req.reference}")


def for_order(order):
    return get_or_create("order", order.id, order.buyer_id, order.vendor.user_id, f"Commande {order.reference}")


def context_url(conv, user):
    pro = user.id == conv.vendor_user_id
    base = "/pro" if pro else "/espace"
    return f"{base}/devis/{conv.context_id}" if conv.context_type == "quote" else f"{base}/commandes/{conv.context_id}"


def post(conv, sender, body, attachment=None, system=False):
    body = (body or "").strip()
    if not body and not attachment:
        return None
    msg = Message(conversation_id=conv.id, sender_id=sender.id, body=body[:4000], is_system=system)
    if attachment:
        msg.attachment_name = attachment["original_name"]
        msg.attachment_stored = attachment["stored_name"]
        msg.attachment_mime = attachment["mime"]
    conv.last_message_at = utcnow()
    db.session.add(msg)
    db.session.flush()
    if not system:
        other = conv.other_party(sender)
        url = f"/messages/{conv.id}"
        preview = (body[:120] + "…") if len(body) > 120 else body
        from ..models import Notification
        pending = Notification.query.filter_by(user_id=other.id, kind="message", url=url, read_at=None).first()
        if pending:  # une notification non lue existe déjà pour cette conversation : on la met à jour (pas de spam)
            pending.body, pending.created_at = preview, utcnow()
        else:
            notify(other, "message", f"Nouveau message — {conv.subject}", preview, url, dedupe_key=f"msg:{conv.id}:{other.id}:{msg.id}")
    return msg


def mark_read(conv, user):
    now = utcnow()
    Message.query.filter(Message.conversation_id == conv.id, Message.sender_id != user.id,
                         Message.read_at.is_(None)).update({"read_at": now})


def unread_total(user):
    q = (db.session.query(db.func.count(Message.id)).join(Conversation, Conversation.id == Message.conversation_id)
         .filter(Message.sender_id != user.id, Message.read_at.is_(None),
                 db.or_(Conversation.buyer_id == user.id, Conversation.vendor_user_id == user.id)))
    return q.scalar() or 0


def can_access(conv, user):
    return user.id in (conv.buyer_id, conv.vendor_user_id)
