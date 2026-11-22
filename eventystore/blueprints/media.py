"""Service des médias publics et des visuels de substitution (placeholders) pour les offres sans photo."""
from flask import Blueprint, Response, abort, current_app, send_from_directory

from ..icons import ICONS
from ..models import Category

bp = Blueprint("media", __name__)

_PALETTES = [("#E8E1FF", "#4B00A5"), ("#F8F5FF", "#4B00A5"), ("#E8E1FF", "#6B2FC4"), ("#F3EEFF", "#3A0080")]


@bp.route("/<path:rel>")
def public_file(rel):
    if rel.startswith("ph/"):
        abort(404)
    resp = send_from_directory(current_app.config["UPLOAD_DIR"], rel, max_age=60 * 60 * 24 * 7)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.route("/ph/<icon>.svg")
def placeholder(icon):
    """Visuel de substitution aux couleurs de la marque. Il n'est PAS une photo de prestation : il est affiché
    tant que le prestataire n'a pas téléversé de vraie photo."""
    inner = ICONS.get(icon, ICONS["sparkles"])
    h = sum(map(ord, icon)) % len(_PALETTES)
    bg, fg = _PALETTES[h]
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300" role="img" aria-label="Visuel de substitution">
<rect width="400" height="300" fill="{bg}"/>
<path d="M-20 230 C 90 170 200 290 420 190 L420 320 L-20 320Z" fill="#F4B400" opacity=".28"/>
<path d="M-20 255 C 110 205 240 300 420 225 L420 320 L-20 320Z" fill="{fg}" opacity=".14"/>
<g transform="translate(160 100) scale(3.3)" fill="none" stroke="{fg}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{inner}</g>
</svg>'''
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})
