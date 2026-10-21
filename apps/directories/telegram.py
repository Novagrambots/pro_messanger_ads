"""
Данные канала / бота по t.me-имени: публичная страница https://t.me/<username>
и (если задан TELEGRAM_RESOLVER_BOT_TOKEN) Bot API getChat — числовой id, точный тип, фото.

Безопасность:
- имя проверяется регуляркой ДО любого запроса, URL собираем сами — только хосты
  t.me, api.telegram.org и CDN аватаров cdn<цифры>.telesco.pe; редиректы не идём;
- таймаут 3 с, плюс общий лимит времени на запрос (Deadline);
- аватар — не больше 1 МБ, тип по сигнатуре файла, храним у себя;
- ссылки Bot API содержат токен бота: ни URL, ни текст исключений requests (в них URL)
  не попадают ни в логи, ни наружу.
"""
import hashlib
import html
import logging
import re
import time

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from apps.directories.models import TelegramPeer

logger = logging.getLogger(__name__)

TIMEOUT = 3
MAX_PAGE_BYTES = 512 * 1024
MAX_AVATAR_BYTES = 1024 * 1024
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; NovaGramAds/1.0)', 'Accept-Language': 'en'}

# Имя в Telegram: латиница, цифры, _, начинается с буквы, 4–32 символа
USERNAME_RE = re.compile(r'[A-Za-z][A-Za-z0-9_]{3,31}')
TME_LINK_RE = re.compile(r'(?:https?://)?t\.me/([A-Za-z0-9_]+)/?', re.IGNORECASE)
AVATAR_URL_RE = re.compile(r'https://cdn\d*\.telesco\.pe/[A-Za-z0-9_\-./%=]+')
FILE_PATH_RE = re.compile(r'[A-Za-z0-9_\-./]+')

PeerType = TelegramPeer.PeerType


class TelegramUnavailable(Exception):
    """Telegram не ответил или ответил непонятно — данных нет"""


class DeadlineExceeded(TelegramUnavailable):
    """Кончился общий лимит времени на запрос"""


class TooManyUnknown(Exception):
    """В одном запросе слишком много ещё не проверенных имён"""


class Deadline:
    """Общий лимит времени: каждый запрос получает таймаут не больше остатка"""

    def __init__(self, seconds):
        self.until = time.monotonic() + seconds

    def timeout(self, cap=TIMEOUT):
        """Таймаут очередного запроса: не больше cap и не больше остатка общего лимита"""
        left = self.until - time.monotonic()
        if left <= 0:
            raise DeadlineExceeded('Кончилось время на проверку в Telegram')
        return min(cap, left)


def normalize_username(value):
    """t.me/name, https://t.me/name, @name или name → name в нижнем регистре. Иначе ValueError"""
    if not isinstance(value, str):
        raise ValueError(value)
    raw = value.strip()
    match = TME_LINK_RE.fullmatch(raw)
    name = match.group(1) if match else raw.removeprefix('@')
    if not USERNAME_RE.fullmatch(name):
        raise ValueError(value)
    return name.lower()


def not_bots(usernames):
    """
    Какие из имён (уже нормализованных) точно не боты. Тип, который уже определил резолвер, важнее имени:
    у @BotFather оно не кончается на «bot». Данных резолвера нет — правило Telegram про суффикс «bot»
    """
    known = dict(TelegramPeer.objects.filter(username__in=usernames).exclude(peer_type=PeerType.UNKNOWN)
                 .values_list('username', 'peer_type'))
    return [name for name in usernames
            if known.get(name, PeerType.BOT if name.endswith('bot') else None) != PeerType.BOT]


def not_bot_error(username):
    hint = '' if username.endswith('bot') else ': имя бота кончается на «bot»'
    return f'@{username} — не бот{hint}'


# ========================================= Резолвинг =========================================

def resolve(username, deadline=None):
    """
    TelegramPeer по имени (уже нормализованному). Свежие данные — из базы без сети;
    иначе спрашиваем Telegram. not_found=True — такого имени нет.
    Telegram не ответил: есть старые данные — отдаём их, нет — TelegramUnavailable.
    """
    if not USERNAME_RE.fullmatch(username):
        raise ValueError(username)
    peer = TelegramPeer.objects.filter(username=username).first()
    if peer and peer.is_fresh():
        return peer
    try:
        info = _fetch(username, deadline or Deadline(TIMEOUT * 3))
    except TelegramUnavailable:
        if peer and not peer.not_found:
            return peer  # протухшие данные лучше, чем ничего
        raise
    return _save(username, info)


class PeerBatch:
    """
    Проверка нескольких имён за один запрос (создание объявления): свежие — из базы,
    остальные — по очереди, с общим лимитом времени и числа новых имён.
    """

    def __init__(self, max_unknown=20, seconds=20):
        self.deadline = Deadline(seconds)
        self.unknown_left = max_unknown

    def resolve(self, usernames):
        names = list(dict.fromkeys(usernames))
        known = {peer.username: peer for peer in TelegramPeer.objects.filter(username__in=names) if peer.is_fresh()}
        unknown = [name for name in names if name not in known]
        if len(unknown) > self.unknown_left:
            raise TooManyUnknown()
        self.unknown_left -= len(unknown)
        for name in unknown:
            known[name] = resolve(name, self.deadline)
        return known


def _fetch(username, deadline):
    info = parse_page(_read(_get(f'https://t.me/{username}', deadline), MAX_PAGE_BYTES).decode('utf-8', 'replace'))
    if info['not_found']:
        return info

    chat = None
    if settings.TELEGRAM_RESOLVER_BOT_TOKEN and info['peer_type'] in (PeerType.CHANNEL, PeerType.GROUP,
                                                                       PeerType.UNKNOWN):
        chat = _get_chat(username, deadline)  # ботов и людей getChat не находит — для них только страница
    if chat:
        info['telegram_id'] = chat.get('id')
        info['peer_type'] = {'channel': PeerType.CHANNEL, 'supergroup': PeerType.GROUP,
                             'group': PeerType.GROUP}.get(chat.get('type'), info['peer_type'])
        info['title'] = (chat.get('title') or info['title'])[:255]

    # Аватар не обязателен: не скачался — сохраняем остальное
    file_id = ((chat or {}).get('photo') or {}).get('small_file_id')
    info['avatar'] = (_bot_api_photo(file_id, deadline) if file_id else None) or (
        _download_image(info['avatar_url'], deadline) if info['avatar_url'] else None)
    return info


@transaction.atomic
def _save(username, info):
    fields = {'fetched_at': timezone.now(), 'not_found': info['not_found']}
    if info['not_found']:
        fields.update(peer_type=PeerType.UNKNOWN, telegram_id=None, subscribers=None, monthly_users=None)
    else:
        for field in ('peer_type', 'title', 'subscribers', 'monthly_users'):
            fields[field] = info[field]
        if info.get('telegram_id') is not None:
            fields['telegram_id'] = info['telegram_id']
    # update_or_create сам лочит строку и переживает гонку двух первых запросов по одному имени
    peer, _ = TelegramPeer.objects.update_or_create(username=username, defaults=fields)

    if info['not_found'] or info.get('avatar'):
        store_avatar(peer, info.get('avatar'))
    return peer


def store_avatar(peer, avatar):
    """
    Заменяет аватар: avatar = (байты, расширение) или None (убрать). Старый файл удаляем.
    В имени — хэш содержимого: новая картинка = новый URL, поэтому /media/ можно отдавать
    с Cache-Control immutable (Cloudflare и браузеры не покажут старый аватар).
    """
    if peer.avatar:
        peer.avatar.delete(save=False)
    if avatar:
        content, ext = avatar
        digest = hashlib.sha256(content).hexdigest()[:8]
        peer.avatar.save(f'{peer.username}_{digest}.{ext}', ContentFile(content), save=False)
    peer.save(update_fields=['avatar'])


def download_cdn_avatar(url, deadline):
    """
    Аватар по ссылке со стороны (например, photoUrl из AdGram): качаем только с CDN Telegram
    https://cdn<цифры>.telesco.pe, иначе None. (байты, расширение) или None
    """
    if not isinstance(url, str) or not AVATAR_URL_RE.fullmatch(url):
        return None
    return _download_image(url, deadline)


# ======================================= Страница t.me =======================================

TITLE_TAG_RE = re.compile(r'<title>([^<]*)</title>')
PAGE_TITLE_RE = re.compile(r'<div class="tgme_page_title"[^>]*>\s*<span[^>]*>(.*?)</span>', re.S)
OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')
OG_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]*)"')
EXTRA_RE = re.compile(r'<div class="tgme_page_extra">(.*?)</div>', re.S)
BUTTON_RE = re.compile(r'<a class="tgme_action_button_new[^"]*"[^>]*>(.*?)</a>', re.S)
PREVIEW_CHANNEL_RE = re.compile(r'class="tgme_page_context_link"[^>]*>\s*Preview channel')
# «70 529 subscribers», «9 362 members, 456 online», «7 586 988 monthly users»
COUNT_RE = re.compile(r'(\d[\d\s  ,.]*?)\s+(subscribers?|members?|monthly users?)\b')


def parse_page(text):
    """
    Разбор публичной страницы t.me. Признаки типа (проверено на живых страницах):
    - нет блока tgme_page_title — имени нет (не найден);
    - <title>Telegram: Launch @… или кнопка «Start Bot» — бот;
    - «N subscribers» или ссылка «Preview channel» — канал;
    - «N members» — группа;
    - <title>Telegram: Contact @… или кнопка «Send Message» — пользователь.
    """
    if 'class="tgme_page_title"' not in text:
        return {'not_found': True}

    def first(regex):
        match = regex.search(text)
        return html.unescape(match.group(1)).strip() if match else ''

    title = re.sub(r'<[^>]+>', '', first(PAGE_TITLE_RE)).strip() or first(OG_TITLE_RE)
    extras = ' | '.join(EXTRA_RE.findall(text))
    counts = {}
    for number, kind in COUNT_RE.findall(extras):
        key = 'monthly_users' if kind.startswith('monthly') else 'members' if kind.startswith('member') \
            else 'subscribers'
        counts[key] = int(re.sub(r'\D', '', number))
    page_title, button = first(TITLE_TAG_RE), first(BUTTON_RE)

    if page_title.startswith('Telegram: Launch @') or button == 'Start Bot':
        peer_type = PeerType.BOT
    elif 'subscribers' in counts or PREVIEW_CHANNEL_RE.search(text):
        peer_type = PeerType.CHANNEL
    elif 'members' in counts:
        peer_type = PeerType.GROUP
    elif page_title.startswith('Telegram: Contact @') or button == 'Send Message':
        peer_type = PeerType.USER
    else:
        peer_type = PeerType.UNKNOWN

    image = first(OG_IMAGE_RE)
    return {
        'not_found': False,
        'peer_type': peer_type,
        'title': title[:255],
        'subscribers': counts.get('subscribers', counts.get('members')),
        'monthly_users': counts.get('monthly_users'),
        # Только CDN Telegram: заглушки (логотип telegram.org, data:-картинка) и чужие хосты не качаем
        'avatar_url': image if AVATAR_URL_RE.fullmatch(image) else None,
    }


# ========================================== Bot API ==========================================

def _bot_api(method, deadline, **params):
    url = f'https://api.telegram.org/bot{settings.TELEGRAM_RESOLVER_BOT_TOKEN}/{method}'
    response = _get(url, deadline, params=params)
    try:
        data = response.json()
    except ValueError:
        raise TelegramUnavailable(f'Bot API {method}: ответ не JSON') from None
    return data['result'] if data.get('ok') else None  # ok=false — например, «chat not found»


def _get_chat(username, deadline):
    try:
        return _bot_api('getChat', deadline, chat_id=f'@{username}')
    except DeadlineExceeded:
        raise
    except TelegramUnavailable as exc:
        logger.warning('Bot API getChat @%s не ответил: %s', username, exc)  # без URL — в нём токен
        return None


def _bot_api_photo(file_id, deadline):
    try:
        file = _bot_api('getFile', deadline, file_id=file_id)
    except TelegramUnavailable:
        return None
    path = (file or {}).get('file_path') or ''
    if not FILE_PATH_RE.fullmatch(path) or '..' in path:
        return None
    return _download_image(f'https://api.telegram.org/file/bot{settings.TELEGRAM_RESOLVER_BOT_TOKEN}/{path}',
                           deadline)


# ========================================== HTTP ==========================================

def _get(url, deadline, **kwargs):
    try:
        response = requests.get(url, timeout=deadline.timeout(), allow_redirects=False,
                                headers=HEADERS, stream=True, **kwargs)
    except requests.RequestException as exc:
        # Текст исключения requests содержит URL (у Bot API — с токеном), поэтому только имя класса
        raise TelegramUnavailable(type(exc).__name__) from None
    if response.status_code != 200:
        response.close()
        raise TelegramUnavailable(f'HTTP {response.status_code}')
    return response


def _read(response, limit):
    """Читает тело, но не больше limit байт (иначе TelegramUnavailable)"""
    try:
        if int(response.headers.get('Content-Length') or 0) > limit:
            raise TelegramUnavailable('Слишком большой ответ')
        content = b''
        for chunk in response.iter_content(64 * 1024):
            content += chunk
            if len(content) > limit:
                raise TelegramUnavailable('Слишком большой ответ')
        return content
    except requests.RequestException as exc:
        raise TelegramUnavailable(type(exc).__name__) from None
    finally:
        response.close()


def _image_ext(content):
    """Тип картинки по сигнатуре файла (заголовкам сервера не верим)"""
    if content.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return 'webp'
    return None


def _download_image(url, deadline):
    """(байты, расширение) или None: не скачалось, больше 1 МБ или не картинка"""
    try:
        content = _read(_get(url, deadline), MAX_AVATAR_BYTES)
    except TelegramUnavailable:
        return None
    ext = _image_ext(content)
    return (content, ext) if ext else None
