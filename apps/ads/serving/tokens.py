"""
viewer_key и random_id.

viewer_key = HMAC-SHA256(VIEWER_HASH_SECRET, user_id), первые 16 байт в hex. Сырой user_id
нигде не хранится и не логируется — дальше вьюхи идёт только viewer_key.

random_id — подписанный токен показа (base64url, 94 символа):
версия (1) + ad (16) + viewer (16) + место показа (1) + хэш контекста (8) + время выдачи (4)
+ nonce (8) + HMAC-SHA256(ADS_TOKEN_SECRET)[:16]. Живёт ADS_TOKEN_TTL (1 час).
"""
import base64
import binascii
import hashlib
import hmac
import os
import struct
import time
import uuid
from dataclasses import dataclass

from django.conf import settings

from apps.ads.models import Placement

TOKEN_VERSION = 1
PLACEMENT_CODES = {
    Placement.SEARCH: 1,
    Placement.BOT: 2,
    Placement.USERS_CHANNEL_MESSAGE: 3,
    Placement.USERS_VIDEO_BANNER: 4,
    Placement.USERS_BOT_BANNER: 5,
    Placement.CHANNEL_MESSAGE: 6,
}
CODE_PLACEMENTS = {code: placement for placement, code in PLACEMENT_CODES.items()}

_BODY = struct.Struct('>B16s16sB8sI8s')  # 54 байта
_MAC_SIZE = 16
_TOKEN_SIZE = _BODY.size + _MAC_SIZE
MAX_CLOCK_SKEW = 60  # токен «из будущего» больше чем на минуту — подделка
MAX_USER_ID = 2 ** 53  # id Telegram — до 52 бит


def is_configured():
    return bool(settings.VIEWER_HASH_SECRET and settings.ADS_TOKEN_SECRET)


def parse_user_id(value):
    """Числовой id Telegram (число или строка из цифр) → int; иначе None"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isdigit() and len(value) <= 20:
        value = int(value)
    if isinstance(value, int) and 0 < value < MAX_USER_ID:
        return value
    return None


def viewer_key(user_id):
    """HMAC от числового id — 32 hex-символа"""
    secret = settings.VIEWER_HASH_SECRET.encode()
    return hmac.new(secret, str(user_id).encode(), hashlib.sha256).hexdigest()[:32]


def context_hash(placement, context):
    """8 байт хэша контекста выдачи (у поиска — нормализованная фраза)"""
    return hashlib.sha256(f'{placement}|{context}'.encode()).digest()[:8]


@dataclass(frozen=True)
class Token:
    ad_id: str
    viewer: str
    placement: str
    context: bytes
    issued: int
    nonce: str

    def expires_in(self, now=None):
        """Сколько секунд токену ещё жить (не меньше 1)"""
        now = int(now if now is not None else time.time())
        return max(self.issued + settings.ADS_TOKEN_TTL - now, 1)


def _mac(body):
    return hmac.new(settings.ADS_TOKEN_SECRET.encode(), body, hashlib.sha256).digest()[:_MAC_SIZE]


def issue(ad_id, viewer, placement, ctx_hash, now=None):
    """Новый random_id для показа объявления ad_id зрителю viewer"""
    issued = int(now if now is not None else time.time())
    body = _BODY.pack(TOKEN_VERSION, uuid.UUID(ad_id).bytes, bytes.fromhex(viewer),
                      PLACEMENT_CODES[placement], ctx_hash, issued, os.urandom(8))
    return base64.urlsafe_b64encode(body + _mac(body)).rstrip(b'=').decode()


def decode(value, now=None):
    """random_id → Token; None, если формат, подпись или срок не те"""
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) != _TOKEN_SIZE:
        return None
    body, mac = raw[:_BODY.size], raw[_BODY.size:]
    if not hmac.compare_digest(mac, _mac(body)):
        return None
    version, ad_bytes, viewer_bytes, code, ctx, issued, nonce = _BODY.unpack(body)
    if version != TOKEN_VERSION or code not in CODE_PLACEMENTS:
        return None
    now = int(now if now is not None else time.time())
    if issued + settings.ADS_TOKEN_TTL < now or issued > now + MAX_CLOCK_SKEW:
        return None
    return Token(str(uuid.UUID(bytes=ad_bytes)), viewer_bytes.hex(), CODE_PLACEMENTS[code], ctx, issued, nonce.hex())
