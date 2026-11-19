"""
Перенос ВСЕХ старых заказов поиска (search_ads.Order) в объявления v2 (место показа SEARCH) — с их статусом,
статистикой и деньгами. После переноса старые заказы — архив, правда — в v2: живые объявления видят и новые
версии приложения, и старые (через мост apps/ads/serving/legacy.py).

Без --apply — только отчёт: что перенесётся, в каком статусе, что нет и почему. Ничего не меняется.
С --apply — каждый заказ в своей транзакции, под локом:
- объявление v2: budget, spent, refunded и итоги (показы, клики) — как у заказа, дата создания — та же;
  одна строка дневной статистики на день создания заказа (по дням старый заказ не считал);
- остаток (budget − потрачено − возвращено) переезжает в объявление БЕЗ движения кошелька: журнал
  MoneyOperation — REFUND старого заказа и DEBIT объявления с причиной MIGRATE и wallet_amount = 0;
- старый заказ — в архив: cancelled, выключен, remaining_views = 0, остаток помечен возвращённым
  (refunded) — старые отмена, модерация и возвраты больше ничего по нему не вернут, деньги вернёт только v2;
- связь: Ad.legacy_order_id = id заказа (уникально) — повторный запуск его не тронет, а отчёты по нему
  отличают перенесённые заказы от живых (иначе показы и деньги посчитаются дважды).

Статус: REJECTED → DECLINED, BLOCKED → BLOCKED, отменён → DELETED, PENDING → IN_REVIEW (очередь модерации v2),
APPROVED: показы кончились → STOPPED, включён → ACTIVE, выключен → ON_HOLD.
Теги → фразы поиска (4–32 символа, без дублей); spm → cpm (целый, .5 вверх, не меньше 1);
max_views_per_user (за всё время) → views_per_user_per_day = min(…, 4); ссылка — t.me/<username канала>.
Объявление, которое может крутиться (ACTIVE / ON_HOLD / IN_REVIEW), переносим только с фразами и с каналом
или ботом, найденным в Telegram (у суперадмина — и с аккаунтом пользователя; в отчёте такие — отдельной строкой), — и если старый id канала (-100…) совпадает с id канала по username:
иначе по ссылке рекламировался бы чужой канал. Не подходит — заказ остаётся как был (в отчёте с причиной).
У архивных расхождение не мешает — канал просто не привязываем. Старые заказы в чатах не переносим.
"""
import re
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from apps.ads.models import Ad, AdDailyStat, AdPricing, AdStatus, InitialStatus, Placement, default_schedule
from apps.ads.validators import PROMOTABLE, SUPERADMIN_MAX_QUERIES, promoted_url
from apps.billing.models import MoneyOperation
from apps.billing.services import wallet_of
from apps.chat_ads.models import ChatAdOrder
from apps.common.models import ModerationStatus
from apps.directories.models import TelegramPeer
from apps.directories.telegram import PeerBatch, TelegramUnavailable, normalize_username
from apps.search_ads.models import Order

MAX_VIEWS_PER_DAY = 4
LEGACY_TYPE = 'search_ads'
# Объявления, которые могут крутиться: им нужны фразы и настоящий канал
SERVABLE = (AdStatus.ACTIVE, AdStatus.ON_HOLD, AdStatus.IN_REVIEW)

NO_QUERIES = "4–32 belgili teg yo'q"
NO_USERNAME = "kanalning username'i yo'q"
PEER_BAD = "kanal Telegram'da topilmadi yoki kanal/bot emas"
ID_MISMATCH = "eski kanal id'si username'dagi kanalga mos emas (boshqa kanal reklama qilinib qoladi)"
TELEGRAM_DOWN = 'Telegram javob bermadi'
CHANNEL_ID_RE = re.compile(r'-100\d{10,13}')
# Перенос — по правилу, с которым его запускали: аккаунт пользователя — только у заказов суперадмина
# (через API аккаунт теперь рекламирует любой рекламодатель — validators.PROMOTABLE)
CHANNEL_OR_BOT = (TelegramPeer.PeerType.CHANNEL, TelegramPeer.PeerType.BOT)


def queries_from(tags):
    """Теги → фразы поиска v2: 4–32 символа, без дублей (без учёта регистра), не больше потолка суперадмина"""
    seen, result = set(), []
    for tag in tags:
        tag = tag.strip()
        if 4 <= len(tag) <= 32 and tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
    return result[:SUPERADMIN_MAX_QUERIES]


def username_of(channel):
    """Username канала: channel_name, иначе channel_id (у старых каналов там бывает username). Нет — None"""
    for value in (channel.channel_name, channel.channel_id):
        try:
            return normalize_username(value)
        except ValueError:
            continue
    return None


def cpm_from(spm):
    """spm (сумы за 1000, 2 знака) → целый cpm v2: .5 — вверх, не меньше 1"""
    return max(1, int(spm.quantize(Decimal('1'), rounding=ROUND_HALF_UP)))


def status_of(order):
    """Статус объявления v2 по состоянию старого заказа"""
    if order.status == ModerationStatus.REJECTED:
        return AdStatus.DECLINED
    if order.status == ModerationStatus.BLOCKED:
        return AdStatus.BLOCKED
    if order.cancelled:
        return AdStatus.DELETED
    if order.status == ModerationStatus.PENDING:
        return AdStatus.IN_REVIEW
    if order.completed or order.remaining_views <= 0:
        return AdStatus.STOPPED
    return AdStatus.ACTIVE if order.is_active else AdStatus.ON_HOLD


class Plan:
    """Что сделаем с одним заказом: skip — причина пропуска, иначе данные будущего объявления"""

    def __init__(self, order):
        self.order = order
        self.status = status_of(order)
        self.username = username_of(order.channel_id)
        self.queries = queries_from(tag.name for tag in order.tags.all())
        self.cpm = cpm_from(order.spm)
        self.remaining = order.unrefunded_amount()
        self.peer = None
        self.skip = None
        if self.username is None:
            self.skip = NO_USERNAME
        elif self.status in SERVABLE and not self.queries:
            self.skip = NO_QUERIES

    def legacy_channel_id(self):
        """Числовой id канала из старого заказа (-100…) или None, если там username или мусор"""
        value = str(self.order.channel_id.channel_id).strip()
        return int(value) if CHANNEL_ID_RE.fullmatch(value) else None

    def attach_peer(self, peers, telegram_down):
        if self.skip:
            return
        peer = peers.get(self.username)
        # Канал или бот, суперадмину — ещё и аккаунт пользователя (CHANNEL_OR_BOT)
        allowed = PROMOTABLE if self.order.user.is_superuser else CHANNEL_OR_BOT
        bad = peer is not None and (peer.not_found or peer.peer_type not in allowed)
        # Старый заказ хранил и id, и имя канала. Имя ведёт на другой канал — значит, его набрали неверно:
        # по ссылке t.me/<имя> рекламировался бы чужой канал. У аккаунта id из Telegram неизвестен — не сверяем
        legacy_id = self.legacy_channel_id()
        mismatch = (peer is not None and not bad and legacy_id is not None and peer.telegram_id is not None
                    and peer.telegram_id != legacy_id)
        if self.status in SERVABLE:
            # Может крутиться — нужен настоящий канал, тот самый; у архивных хватит ссылки
            if peer is None:
                self.skip = TELEGRAM_DOWN if telegram_down else PEER_BAD
            elif bad:
                self.skip = PEER_BAD
            elif mismatch:
                self.skip = ID_MISMATCH
        self.peer = None if peer is None or bad or mismatch else peer

    @property
    def views_per_day(self):
        return min(max(self.order.max_views_per_user, 1), MAX_VIEWS_PER_DAY)


class Command(BaseCommand):
    help = ('Переносит все старые заказы поиска в объявления v2 со статусом, статистикой и деньгами. '
            'Без --apply — только отчёт, ничего не меняет')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Перенести (без флага — только отчёт)')
        parser.add_argument('--limit', type=int, default=None, help='Перенести не больше N заказов (для пробы)')
        parser.add_argument('--order', default=None, help='Только этот заказ (order_id)')

    def handle(self, *args, apply=False, limit=None, order=None, verbosity=1, **options):
        migrated = Ad.objects.filter(legacy_order_type=LEGACY_TYPE, legacy_order_id=OuterRef('pk'))
        orders = (Order.objects.annotate(migrated=Exists(migrated)).filter(migrated=False)
                  .select_related('user', 'channel_id').prefetch_related('tags').order_by('created_at'))
        if order:
            orders = orders.filter(pk=order)
        plans = [Plan(item) for item in orders]
        self._resolve_peers(plans)

        ready = [plan for plan in plans if not plan.skip]
        if limit is not None:
            ready = ready[:limit]
        self._report(plans, ready, verbose=verbosity >= 2 or len(plans) <= 30)

        if not apply:
            self.stdout.write(self.style.WARNING("Bu faqat hisobot: hech narsa o'zgarmadi. Ko'chirish — --apply bilan"))
            return

        done, failed = 0, 0
        for plan in ready:
            try:
                ad = self._migrate(plan)
            except Exception as exc:  # один заказ не должен остановить остальные: его транзакция откатилась
                failed += 1
                self.stderr.write(f'XATO {plan.order.pk}: {exc}')
                continue
            if ad is None:
                self.stdout.write(f"o'tkazildi {plan.order.pk}: allaqachon ko'chirilgan")
                continue
            done += 1
            if verbosity >= 2:
                self.stdout.write(f"ko'chirildi {plan.order.pk} → {ad.pk} ({ad.status})")
        style = self.style.SUCCESS if not failed else self.style.ERROR
        self.stdout.write(style(f"Ko'chirildi: {done}, xato: {failed}"))

    def _resolve_peers(self, plans):
        """Каналы проверяем в Telegram одним пакетом (уже проверенные — из базы)"""
        names = sorted({plan.username for plan in plans if not plan.skip})
        peers, down = {}, False
        if names:
            try:
                peers = PeerBatch(seconds=60).resolve(names)
            except TelegramUnavailable as exc:
                down = True
                self.stderr.write(f"Telegram: {exc} — ishlay oladigan buyurtmalar o'tkazib yuboriladi")
        for plan in plans:
            plan.attach_peer(peers, down)

    def _report(self, plans, ready, verbose):
        out = self.stdout.write
        statuses = Counter(plan.status for plan in ready)
        skipped = Counter(plan.skip for plan in plans if plan.skip)
        out(f"Ko'chirilmagan eski qidiruv buyurtmalari: {len(plans)}")
        out(f"Ko'chiriladi: {len(ready)} ta — " + ', '.join(f'{status} {count}' for status, count in statuses.most_common()))
        out(f"  korishlar: {sum(plan.order.shown_views for plan in ready)}, "
            f"kliklar: {sum(plan.order.clicks for plan in ready)}, "
            f"sarflangan: {sum((plan.order.spent_amount() for plan in ready), Decimal('0'))} so'm, "
            f"qoldiq (reklamaga o'tadi): {sum((plan.remaining for plan in ready), Decimal('0'))} so'm")
        accounts = Counter(plan.username for plan in ready
                           if plan.peer is not None and plan.peer.peer_type == TelegramPeer.PeerType.USER)
        for username, count in accounts.most_common():
            out(f"  akkaunt reklama qilinadi (kanal emas): @{username} — {count} ta")
        for reason, count in skipped.most_common():
            out(f"O'tkazib yuboriladi — {reason}: {count} ta")
        chats = ChatAdOrder.objects.count()
        out(f"Eski chat buyurtmalari: {chats} (ko'chirilmaydi)")
        if verbose:
            for plan in plans:
                out(f'  {plan.order.pk} | @{plan.username} | {plan.status} | spm {plan.order.spm} → cpm {plan.cpm} | '
                    f'korish {plan.order.shown_views} | qoldiq {plan.remaining} | {plan.skip or "kochiriladi"}')

    def _migrate(self, plan):
        """Один заказ под локом → объявление v2 + перенос остатка в журнале + заказ в архив. None — уже перенесён"""
        with transaction.atomic():
            # Лочим только строку заказа (of=self): строку пользователя из select_related не трогаем
            order = Order.objects.select_for_update(of=('self',)).select_related('user').get(pk=plan.order.pk)
            if Ad.objects.filter(legacy_order_type=LEGACY_TYPE, legacy_order_id=order.pk).exists():
                return None
            status = status_of(order)  # под локом заново: заказ мог измениться после отчёта
            spent, remaining = order.spent_amount(), order.unrefunded_amount()
            ad = Ad.objects.create(
                user=order.user, placement=Placement.SEARCH, status=status, initial_status=InitialStatus.ACTIVE,
                title=f'{order.order_name} · eski {str(order.pk)[:8]}'[:255],
                text='', url=promoted_url(plan.username), promoted_peer=plan.peer,
                cpm=plan.cpm, budget=order.budget, spent=spent, refunded=order.refunded,
                views_total=order.shown_views, clicks_total=order.clicks,
                views_per_user_per_day=plan.views_per_day, schedule=default_schedule(),
                targeting={'queries': plan.queries},
                terms_version=AdPricing.load().terms_version, terms_accepted_at=order.created_at,
                moderation_reason=order.reject_reason, moderated_by=order.moderated_by,
                moderated_at=order.moderated_at,
                legacy_order_type=LEGACY_TYPE, legacy_order_id=order.pk,
            )
            # Дата создания — как у заказа (auto_now_add её не принимает)
            Ad.objects.filter(pk=ad.pk).update(created_at=order.created_at)
            if order.shown_views or order.clicks:
                AdDailyStat.objects.create(ad=ad, date=timezone.localdate(order.created_at),
                                           views=order.shown_views, clicks=order.clicks, spent=spent)
            if remaining > 0:
                self._move_money(order, ad, remaining)
            # Старый заказ — в архив: не крутится, отменить / вернуть по нему больше нечего
            Order.objects.filter(pk=order.pk).update(
                cancelled=True, is_active=False, remaining_views=0,
                refunded=F('refunded') + max(remaining, Decimal('0')), updated_at=timezone.now())
            return ad

    @staticmethod
    def _move_money(order, ad, remaining):
        """Остаток переезжает из заказа в объявление: две записи журнала, кошелёк не двигается"""
        wallet = wallet_of(order.user)
        common = {'user': order.user, 'reason': MoneyOperation.Reason.MIGRATE, 'wallet': wallet,
                  'exact_amount': remaining, 'wallet_amount': Decimal('0'), 'status': MoneyOperation.Status.DONE}
        MoneyOperation.objects.bulk_create([
            MoneyOperation(order_type=LEGACY_TYPE, order_id=order.pk, kind=MoneyOperation.Kind.REFUND, **common),
            MoneyOperation(order_type=MoneyOperation.OrderType.ADS, order_id=ad.pk, kind=MoneyOperation.Kind.DEBIT,
                           **common),
        ])
