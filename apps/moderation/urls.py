from django.urls import path

from .ad_views import (AdReportsListView, AllAdsListView, ApproveAdView, ApproveRevisionView, BlockAdView,
                       DeclineAdView, DeclineRevisionView, ModerationAdDetailView, PendingAdsListView,
                       PendingRevisionsListView, ReviewAdReportView)
from .views import (PendingSearchAdsListView, PendingChatAdsListView, PendingCountView,
                    ApproveOrderView, RejectOrderView, BlockOrderView,
                    AllSearchAdsListView, AllChatAdsListView)


urlpatterns = [
    # Объявления v2. ВАЖНО: стоят раньше старых '<order_type>/<uuid>/approve/' —
    # иначе 'ads/<uuid>/approve/' перехватил бы старый маршрут с order_type='ads'
    path('ads/pending/', PendingAdsListView.as_view(), name='moderation-ads-pending'),
    path('ads/all/', AllAdsListView.as_view(), name='moderation-ads-all'),
    path('ads/revisions/pending/', PendingRevisionsListView.as_view(), name='moderation-ad-revisions-pending'),
    path('ads/revisions/<uuid:pk>/approve/', ApproveRevisionView.as_view(), name='moderation-ad-revision-approve'),
    path('ads/revisions/<uuid:pk>/decline/', DeclineRevisionView.as_view(), name='moderation-ad-revision-decline'),
    path('ads/reports/', AdReportsListView.as_view(), name='moderation-ad-reports'),
    path('ads/reports/<uuid:pk>/review/', ReviewAdReportView.as_view(), name='moderation-ad-report-review'),
    path('ads/<uuid:pk>/', ModerationAdDetailView.as_view(), name='moderation-ads-detail'),
    path('ads/<uuid:pk>/approve/', ApproveAdView.as_view(), name='moderation-ads-approve'),
    path('ads/<uuid:pk>/decline/', DeclineAdView.as_view(), name='moderation-ads-decline'),
    path('ads/<uuid:pk>/block/', BlockAdView.as_view(), name='moderation-ads-block'),

    # Очереди на модерацию (по типам рекламы)
    path('search_ads/pending/', PendingSearchAdsListView.as_view(), name='moderation-search-pending'),
    path('chat_ads/pending/', PendingChatAdsListView.as_view(), name='moderation-chat-pending'),

    # Все заказы всех юзеров для админ-панели (только суперадмин)
    path('search_ads/all/', AllSearchAdsListView.as_view(), name='moderation-search-all'),
    path('chat_ads/all/', AllChatAdsListView.as_view(), name='moderation-chat-all'),

    # Счётчик для бейджа (фронт поллит)
    path('pending_count/', PendingCountView.as_view(), name='moderation-pending-count'),

    # Решения модератора; order_type — search_ads или chat_ads
    path('<str:order_type>/<uuid:order_id>/approve/', ApproveOrderView.as_view(), name='moderation-approve'),
    path('<str:order_type>/<uuid:order_id>/reject/', RejectOrderView.as_view(), name='moderation-reject'),
    path('<str:order_type>/<uuid:order_id>/block/', BlockOrderView.as_view(), name='moderation-block'),
]
