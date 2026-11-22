"""Paiements : abstraction fournisseur + traitement serveur des confirmations (webhooks).

IMPORTANT — état de l'intégration
- `sandbox` : fournisseur de SIMULATION interne. Aucun argent réel ne circule. La page de paiement test
  affiche un bandeau explicite. La confirmation passe par le même pipeline serveur que les webhooks réels
  (`process_event`), de façon à valider tout le parcours (acompte, solde, commission, grand livre).
- `wave` / `orange_money` : adaptateurs PRÉPARÉS (interface, vérification de signature HMAC, route de webhook)
  mais l'appel d'API réel (création de session de paiement, remboursement) est à compléter avec les
  identifiants/contrats fournis par les opérateurs. Tant que les clés ne sont pas configurées, ils lèvent
  `ProviderNotConfigured` et l'interface le signale à l'utilisateur au lieu de simuler un paiement.
- Un paiement n'est JAMAIS considéré réussi sur la base d'une redirection navigateur (AC-07).
"""
import hashlib
import hmac
import json
import secrets
from datetime import datetime

from flask import current_app, url_for

from ..extensions import db
from ..models import LedgerEntry, Order, Payment, Refund, WebhookEvent, utcnow
from . import audit


class ProviderNotConfigured(Exception):
    pass


class PaymentProvider:
    code = "base"
    label = "Base"
    methods = ()

    def configured(self):
        return True

    def create_checkout(self, payment, order):
        """Retourne l'URL vers laquelle rediriger l'acheteur."""
        raise NotImplementedError

    def verify_signature(self, raw_body, headers):
        raise NotImplementedError

    def refund(self, payment, amount_xof):
        raise NotImplementedError


class SandboxProvider(PaymentProvider):
    code = "sandbox"
    label = "Paiement test (aucun argent réel)"
    methods = ("sandbox",)

    def create_checkout(self, payment, order):
        return url_for("payments.sandbox_checkout", ref=payment.external_ref)

    def sign(self, raw_body):
        secret = current_app.config["PAYMENT_WEBHOOK_SECRET"].encode()
        return hmac.new(secret, raw_body, hashlib.sha256).hexdigest()

    def verify_signature(self, raw_body, headers):
        sig = headers.get("X-Eventy-Signature", "")
        return hmac.compare_digest(sig, self.sign(raw_body))

    def refund(self, payment, amount_xof):
        return {"status": "processed", "ref": "SBX-RF-" + secrets.token_hex(4).upper()}


class ProviderError(ProviderNotConfigured):
    """Erreur d'appel API fournisseur (réseau, refus). Les remboursements retombent alors en traitement manuel."""


def _http_json(method, url, headers=None, body=None, timeout=15):
    """Appel HTTP JSON (stdlib). Isolé pour pouvoir être simulé dans les tests."""
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - URL issue de la configuration
            raw = r.read().decode() or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ProviderError(f"Fournisseur injoignable : {e}")


class WaveProvider(PaymentProvider):
    """Wave Checkout API (https://docs.wave.com/checkout) — implémentée d'après la documentation publique.

    NON testée contre l'API réelle (aucune clé disponible au développement) : valider en sandbox/production avec
    votre compte marchand. Points à confirmer au premier essai réel : nom de l'en-tête de signature et nom de
    l'événement de succès (les deux variantes connues sont acceptées), schéma exact de la signature.
    """
    code = "wave"
    label = "Wave"
    methods = ("wave",)

    def _base(self):
        return current_app.config["WAVE_API_BASE"].rstrip("/")

    def _auth(self):
        return {"Authorization": f"Bearer {current_app.config['WAVE_API_KEY']}"}

    def configured(self):
        return bool(current_app.config["WAVE_API_KEY"] and current_app.config["WAVE_WEBHOOK_SECRET"])

    def create_checkout(self, payment, order):
        if not self.configured():
            raise ProviderNotConfigured("Wave n'est pas configuré (WAVE_API_KEY / WAVE_WEBHOOK_SECRET).")
        back = current_app.config["SITE_URL"].rstrip("/") + url_for("payments.result", ref=payment.external_ref)
        status, data = _http_json("POST", self._base() + "/v1/checkout/sessions", self._auth(), {
            "amount": str(int(payment.amount_xof)), "currency": "XOF", "client_reference": payment.external_ref,
            "success_url": back, "error_url": back})
        if status >= 300 or not data.get("wave_launch_url"):
            current_app.logger.warning("Wave checkout refusé: %s %s", status, data)
            raise ProviderError("Wave a refusé la création du paiement. Réessayez ou choisissez un autre moyen.")
        payment.raw = {"wave_session_id": data.get("id")}
        return data["wave_launch_url"]

    # -- webhook
    def _signature_header(self, headers):
        return headers.get("Wave-Signature") or headers.get("X-Wave-Signature") or ""

    def verify_signature(self, raw_body, headers, now=None):
        import time
        secret = current_app.config["WAVE_WEBHOOK_SECRET"]
        header = self._signature_header(headers)
        if not secret or not header:
            return False
        parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
        ts = next((v for k, v in parts if k.strip() == "t"), "")
        sigs = [v for k, v in parts if k.strip() == "v1"]
        if not ts.isdigit() or abs((now or time.time()) - int(ts)) > 300:  # anti-rejeu : 5 minutes
            return False
        body = raw_body.decode() if isinstance(raw_body, bytes) else raw_body
        candidates = {hmac.new(secret.encode(), (ts + sep + body).encode(), hashlib.sha256).hexdigest() for sep in ("", ".")}
        return any(hmac.compare_digest(sig, c) for sig in sigs for c in candidates)

    def normalize_event(self, payload):
        """Convertit l'événement Wave vers le format interne de `process_event`."""
        d = payload.get("data") or {}
        typ = payload.get("type", "")
        if typ in ("checkout.session.completed", "checkout.paid") and d.get("payment_status", "succeeded") == "succeeded":
            kind = "payment.succeeded"
        elif typ in ("checkout.session.payment_failed", "checkout.failed", "checkout.session.expired"):
            kind = "payment.failed"
        else:
            kind = "ignored"
        try:
            amount = int(float(d.get("amount", -1)))
        except (TypeError, ValueError):
            amount = -1
        return {"id": payload.get("id"), "type": kind, "reference": d.get("client_reference"), "amount_xof": amount,
                "wave_session_id": d.get("id")}

    def confirm_server_side(self, event):
        """Double contrôle : on relit la session chez Wave avant de créditer (jamais confiance au seul webhook)."""
        sid = event.get("wave_session_id")
        if not sid:
            return False
        status, data = _http_json("GET", f"{self._base()}/v1/checkout/sessions/{sid}", self._auth())
        return (status == 200 and data.get("payment_status") == "succeeded"
                and data.get("client_reference") == event.get("reference")
                and int(float(data.get("amount", -1))) == int(event.get("amount_xof", -2))
                and data.get("currency", "XOF") == "XOF")

    def refund(self, payment, amount_xof):
        sid = (payment.raw or {}).get("wave_session_id")
        if not (self.configured() and sid):
            raise ProviderNotConfigured("Remboursement Wave à traiter manuellement.")
        # Wave rembourse la session entière ; un remboursement partiel doit donc être traité à la main.
        if int(amount_xof) != payment.amount_xof:
            raise ProviderNotConfigured("Remboursement partiel Wave : à traiter manuellement.")
        status, data = _http_json("POST", f"{self._base()}/v1/checkout/sessions/{sid}/refund", self._auth(), {})
        if status >= 300:
            raise ProviderError(f"Remboursement Wave refusé ({status}).")
        return {"status": "processed", "ref": sid}


class OrangeMoneyProvider(PaymentProvider):
    """Squelette Orange Money Web Payment (OAuth2 client_credentials + merchant key). À compléter."""
    code = "orange_money"
    label = "Orange Money"
    methods = ("orange_money",)

    def configured(self):
        c = current_app.config
        return bool(c["ORANGE_MONEY_CLIENT_ID"] and c["ORANGE_MONEY_CLIENT_SECRET"] and c["ORANGE_MONEY_MERCHANT_KEY"])

    def create_checkout(self, payment, order):
        if not self.configured():
            raise ProviderNotConfigured("Orange Money n'est pas configuré (ORANGE_MONEY_*).")
        raise ProviderNotConfigured("Appel API Orange Money à implémenter avec les identifiants du compte marchand.")

    def verify_signature(self, raw_body, headers):
        secret = current_app.config["ORANGE_MONEY_CLIENT_SECRET"]
        if not secret:
            return False
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(headers.get("X-Signature", ""), expected)

    def refund(self, payment, amount_xof):
        raise ProviderNotConfigured("Remboursement Orange Money à traiter via le portail marchand.")


PROVIDERS = {p.code: p for p in (SandboxProvider(), WaveProvider(), OrangeMoneyProvider())}


def active_provider():
    return PROVIDERS.get(current_app.config["PAYMENT_PROVIDER"], PROVIDERS["sandbox"])


def available_methods():
    """Moyens proposés à l'acheteur selon la configuration réelle (jamais de faux boutons)."""
    prov = active_provider()
    if prov.code == "sandbox":
        return [("sandbox", "Paiement test (simulation, aucun argent réel)")]
    from ..models import PAYMENT_METHODS
    return [(m, PAYMENT_METHODS[m]) for m in prov.methods]


# ------------------------------------------------------------------ cycle de vie d'un paiement
def create_payment(order, amount, kind, method=None):
    prov = active_provider()
    p = Payment(order_id=order.id, kind=kind, amount_xof=int(amount), method=method or prov.methods[0],
                provider=prov.code, external_ref=f"{prov.code[:3].upper()}-{secrets.token_hex(8).upper()}", status="pending")
    db.session.add(p)
    db.session.flush()
    return p


def _split(order, amount):
    """Ventilation d'un encaissement : commission (taux figé de la commande) et net prestataire."""
    commission = round(amount * (order.commission_bp or 0) / 10000)
    return commission, amount - commission


def process_event(provider_code, event, signature_ok):
    """Point d'entrée UNIQUE de confirmation serveur. `event` = {"id","type","reference","amount_xof"}.

    Idempotent (WebhookEvent.event_id unique). Types : payment.succeeded | payment.failed.
    """
    from . import orders as order_service  # import tardif (dépendance circulaire)
    ev_id = str(event.get("id") or "")
    if not ev_id:
        return False, "id manquant"
    existing = WebhookEvent.query.filter_by(event_id=ev_id).first()
    if existing and existing.processed:
        return True, "déjà traité"
    we = existing or WebhookEvent(provider=provider_code, event_id=ev_id, payload=event, signature_ok=signature_ok)
    db.session.add(we)
    if not signature_ok:
        we.error = "signature invalide"
        db.session.commit()
        return False, "signature invalide"
    pay = Payment.query.filter_by(external_ref=event.get("reference")).first()
    if pay is None:
        we.error = "paiement inconnu"
        db.session.commit()
        return False, "paiement inconnu"
    if pay.status != "pending":
        we.processed = True
        db.session.commit()
        return True, "paiement déjà finalisé"
    order = pay.order
    if event.get("type") == "payment.failed":
        pay.status = "failed"
        pay.raw = event
        we.processed = True
        order_service.on_payment_failed(order, pay)
        db.session.commit()
        return True, "échec enregistré"
    if event.get("type") != "payment.succeeded":
        we.error = "type inconnu"
        db.session.commit()
        return False, "type inconnu"
    if int(event.get("amount_xof", -1)) != pay.amount_xof:  # contrôle de rapprochement montant
        pay.status = "failed"
        pay.raw = event
        we.error = "montant incohérent"
        we.processed = True
        audit.log("payment.amount_mismatch", pay, actor=None, expected=pay.amount_xof, received=event.get("amount_xof"))
        db.session.commit()
        return False, "montant incohérent"
    pay.status = "succeeded"
    pay.paid_at = utcnow()
    pay.raw = event
    commission, net = _split(order, pay.amount_xof)
    pay.commission_xof, pay.net_vendor_xof = commission, net
    order.paid_xof = (order.paid_xof or 0) + pay.amount_xof
    db.session.add(LedgerEntry(vendor_id=order.vendor_id, order_id=order.id, payment_id=pay.id, kind="credit",
                               amount_xof=net, gross_xof=pay.amount_xof, commission_xof=commission,
                               available=bool(order.funds_released), note=f"Paiement {pay.kind} {order.reference}"))
    we.processed = True
    order_service.on_payment_succeeded(order, pay)
    db.session.commit()
    return True, "paiement confirmé"


def sandbox_event(payment, success=True):
    """Fabrique l'événement signé que le sandbox envoie à notre propre pipeline."""
    ev = {"id": f"evt_{secrets.token_hex(8)}", "type": "payment.succeeded" if success else "payment.failed",
          "reference": payment.external_ref, "amount_xof": payment.amount_xof}
    raw = json.dumps(ev, sort_keys=True).encode()
    prov = PROVIDERS["sandbox"]
    ok = prov.verify_signature(raw, {"X-Eventy-Signature": prov.sign(raw)})
    return ev, ok


def refund_payment(order, amount, reason, actor=None, days_before=None):
    """Rembourse jusqu'à `amount` en commençant par les paiements les plus récents. Retourne le total remboursé."""
    remaining = int(amount)
    done = 0
    for pay in sorted([p for p in order.payments if p.status in ("succeeded", "partially_refunded")],
                      key=lambda p: p.id, reverse=True):
        if remaining <= 0:
            break
        refundable = pay.amount_xof - (pay.refunded_xof or 0)
        part = min(refundable, remaining)
        if part <= 0:
            continue
        prov = PROVIDERS.get(pay.provider, PROVIDERS["sandbox"])
        status = "processed"
        try:
            prov.refund(pay, part)
        except ProviderNotConfigured:
            status = "manual_required"  # à traiter manuellement par la finance, tracé dans la liste des remboursements
        pay.refunded_xof = (pay.refunded_xof or 0) + part
        pay.status = "refunded" if pay.refunded_xof >= pay.amount_xof else "partially_refunded"
        db.session.add(Refund(order_id=order.id, payment_id=pay.id, amount_xof=part, reason=reason,
                              actor_id=getattr(actor, "id", None), days_before_event=days_before, status=status))
        # reprise du net prestataire au prorata
        ratio = part / pay.amount_xof
        net_back = round((pay.net_vendor_xof or 0) * ratio)
        if net_back:
            db.session.add(LedgerEntry(vendor_id=order.vendor_id, order_id=order.id, payment_id=pay.id,
                                       kind="refund_debit", amount_xof=-net_back, gross_xof=-part,
                                       commission_xof=-(part - net_back), available=bool(order.funds_released),
                                       note=f"Remboursement {order.reference}"))
        order.refunded_xof = (order.refunded_xof or 0) + part
        done += part
        remaining -= part
    order.commission_xof = max(0, round(max(0, (order.paid_xof or 0) - (order.refunded_xof or 0)) * (order.commission_bp or 0) / 10000))
    audit.log("refund", order, amount=done, reason=reason, actor=actor)
    return done
