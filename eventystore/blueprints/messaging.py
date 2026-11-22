"""Messagerie intégrée (acheteur <-> prestataire), liée à un devis ou une commande."""
import io

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Conversation, ConversationAccessLog, Message
from ..services import audit, files, messaging

bp = Blueprint("messaging", __name__)


@bp.before_request
@login_required
def _guard():
    pass


def _conv_for(conv_id):
    conv = db.session.get(Conversation, conv_id) or abort(404)
    if messaging.can_access(conv, current_user):
        return conv, False
    if current_user.is_staff and current_user.has_perm("conversations"):
        return conv, True
    abort(404)


@bp.route("/")
def inbox():
    if current_user.is_staff:
        abort(404)
    convs = (Conversation.query.filter(db.or_(Conversation.buyer_id == current_user.id,
                                              Conversation.vendor_user_id == current_user.id))
             .order_by(Conversation.last_message_at.desc()).all())
    unread = {}
    for c in convs:
        unread[c.id] = Message.query.filter(Message.conversation_id == c.id, Message.sender_id != current_user.id,
                                            Message.read_at.is_(None)).count()
    return render_template("messaging/inbox.html", convs=convs, unread=unread)


@bp.route("/<int:conv_id>", methods=["GET", "POST"])
def thread(conv_id):
    conv, as_staff = _conv_for(conv_id)
    if request.method == "POST":
        if as_staff:
            abort(403)
        body = (request.form.get("body") or "").strip()
        att = None
        f = request.files.get("attachment")
        if f and f.filename:
            try:
                att = files.save_private(f, files.ATTACH_EXT, subdir="messages")
            except files.UploadError as e:
                flash(str(e), "danger")
                return redirect(url_for("messaging.thread", conv_id=conv.id))
        if not body and not att:
            flash("Écrivez un message ou joignez un fichier.", "warning")
        else:
            messaging.post(conv, current_user, body, att)
            db.session.commit()
        return redirect(url_for("messaging.thread", conv_id=conv.id) + "#bas")
    if as_staff:
        audit.log("conversation.staff_access", conv, reason=request.args.get("motif", "support"))
        db.session.add(ConversationAccessLog(conversation_id=conv.id, staff_id=current_user.id,
                                             reason=(request.args.get("motif") or "traitement ticket")[:250]))
        db.session.commit()
    else:
        messaging.mark_read(conv, current_user)
        db.session.commit()
    templates = messaging.TEMPLATES["vendor"] if current_user.id == conv.vendor_user_id else []
    return render_template("messaging/thread.html", conv=conv, as_staff=as_staff, warning=messaging.WARNING,
                           templates=templates, ctx_url=messaging.context_url(conv, current_user) if not as_staff else None)


@bp.route("/<int:conv_id>/poll")
def poll(conv_id):
    conv, as_staff = _conv_for(conv_id)
    after = request.args.get("after", type=int, default=0)
    msgs = Message.query.filter(Message.conversation_id == conv.id, Message.id > after).order_by(Message.id).all()
    if not as_staff:
        messaging.mark_read(conv, current_user)
        db.session.commit()
    return jsonify(messages=[{"id": m.id, "mine": m.sender_id == current_user.id, "body": m.body or "",
                              "author": m.sender.display_name, "at": m.created_at.strftime("%d/%m %H:%M"),
                              "attachment": m.attachment_name,
                              "att_url": url_for("messaging.attachment", msg_id=m.id) if m.attachment_name else None}
                             for m in msgs])


@bp.route("/pieces/<int:msg_id>")
def attachment(msg_id):
    m = db.session.get(Message, msg_id) or abort(404)
    conv, _ = _conv_for(m.conversation_id)
    if not m.attachment_stored:
        abort(404)
    return send_file(io.BytesIO(files.read_private(m.attachment_stored)), mimetype=m.attachment_mime,
                     as_attachment=True, download_name=m.attachment_name)
