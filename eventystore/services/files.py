"""Gestion des fichiers.

- Images publiques : validées par Pillow, ré-encodées (supprime les métadonnées/EXIF), redimensionnées,
  miniature générée, nom aléatoire -> UPLOAD_DIR.
- Documents sensibles (pièces d'identité, registres…) : types restreints, chiffrés au repos (Fernet)
  dans PRIVATE_DIR, jamais servis directement ; accès via une route autorisée et journalisée.
"""
import base64
import hashlib
import io
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError

IMAGE_EXT = {"jpg", "jpeg", "png", "webp"}
DOC_EXT = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
ATTACH_EXT = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
              "webp": "image/webp", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


class UploadError(ValueError):
    pass


def _ext(filename):
    return filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""


def _fernet():
    key = current_app.config.get("DOCS_ENCRYPTION_KEY")
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)
    digest = hashlib.sha256(current_app.config["SECRET_KEY"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def generate_key():
    return Fernet.generate_key().decode()


def _read(file_storage, max_bytes):
    data = file_storage.read(max_bytes + 1)
    if not data:
        raise UploadError("Fichier vide.")
    if len(data) > max_bytes:
        raise UploadError(f"Fichier trop volumineux (max {max_bytes // (1024 * 1024)} Mo).")
    return data


def save_image(file_storage, subdir="offers", max_side=1600, thumb_side=480):
    """Retourne (chemin_relatif, chemin_miniature_relatif)."""
    if not file_storage or not file_storage.filename:
        raise UploadError("Aucun fichier.")
    if _ext(file_storage.filename) not in IMAGE_EXT:
        raise UploadError("Format d'image non accepté (JPG, PNG ou WebP).")
    data = _read(file_storage, current_app.config["MAX_IMAGE_BYTES"])
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise UploadError("Le fichier n'est pas une image valide.")
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    name = secrets.token_hex(12)
    base = Path(current_app.config["UPLOAD_DIR"]) / subdir
    base.mkdir(parents=True, exist_ok=True)

    def _save(im, side, suffix):
        im = im.copy()
        im.thumbnail((side, side), Image.LANCZOS)
        fmt, ext = ("PNG", "png") if im.mode == "RGBA" else ("JPEG", "jpg")
        rel = f"{subdir}/{name}{suffix}.{ext}"
        kwargs = {"optimize": True} if fmt == "PNG" else {"quality": 85, "optimize": True, "progressive": True}
        im.save(Path(current_app.config["UPLOAD_DIR"]) / rel, fmt, **kwargs)
        return rel

    return _save(img, max_side, ""), _save(img, thumb_side, "_t")


def delete_public(rel):
    if not rel:
        return
    p = (Path(current_app.config["UPLOAD_DIR"]) / rel).resolve()
    if str(p).startswith(str(Path(current_app.config["UPLOAD_DIR"]).resolve())) and p.exists():
        p.unlink()


def _check_magic(ext, data):
    if ext == "pdf" and not data.startswith(b"%PDF-"):
        raise UploadError("Le fichier n'est pas un PDF valide.")
    if ext in ("jpg", "jpeg") and not data.startswith(b"\xff\xd8"):
        raise UploadError("Le fichier n'est pas un JPEG valide.")
    if ext == "png" and not data.startswith(b"\x89PNG"):
        raise UploadError("Le fichier n'est pas un PNG valide.")
    if ext == "webp" and not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        raise UploadError("Le fichier n'est pas un WebP valide.")
    if ext in ("docx", "xlsx") and not data.startswith(b"PK"):
        raise UploadError("Le fichier n'est pas un document Office valide.")


def save_private(file_storage, allowed=None, subdir="docs", max_bytes=None):
    """Chiffre et enregistre un fichier privé. Retourne dict(stored_name, original_name, mime, size)."""
    allowed = allowed or DOC_EXT
    if not file_storage or not file_storage.filename:
        raise UploadError("Aucun fichier.")
    ext = _ext(file_storage.filename)
    if ext not in allowed:
        raise UploadError("Type de fichier non accepté (" + ", ".join(sorted(allowed)).upper() + ").")
    data = _read(file_storage, max_bytes or current_app.config["MAX_DOC_BYTES"])
    _check_magic(ext, data)
    stored = f"{subdir}/{secrets.token_hex(16)}.bin"
    path = Path(current_app.config["PRIVATE_DIR"]) / stored
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_fernet().encrypt(data))
    from werkzeug.utils import secure_filename
    return {"stored_name": stored, "original_name": secure_filename(file_storage.filename)[:200] or f"fichier.{ext}",
            "mime": allowed[ext], "size": len(data)}


def read_private(stored_name):
    root = Path(current_app.config["PRIVATE_DIR"]).resolve()
    path = (root / stored_name).resolve()
    if not str(path).startswith(str(root)) or not path.exists():
        raise FileNotFoundError(stored_name)
    return _fernet().decrypt(path.read_bytes())


def encrypt_text(text):
    return _fernet().encrypt(text.encode()).decode()


def decrypt_text(token):
    return _fernet().decrypt(token.encode()).decode()
