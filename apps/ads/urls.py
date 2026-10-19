from django.urls import include, path

from apps.ads.serving import views as serving
from apps.ads.views import (AdBudgetHistoryView, AdBudgetTopUpView, AdCopyToDraftView, AdDetailView, AdDraftView,
                            AdListCreateView, AdMediaDetailView, AdMediaUploadView, AdPricingView, AdStatsView,
                            AdTableSettingsView)
from apps.directories.views import SimilarBotsView, SimilarChannelsView, TelegramResolveView


urlpatterns = [
    path('', AdListCreateView.as_view(), name='ads-list'),
    path('pricing/', AdPricingView.as_view(), name='ads-pricing'),
    path('table-settings/', AdTableSettingsView.as_view(), name='ads-table-settings'),
    path('draft/', AdDraftView.as_view(), name='ads-draft'),
    # Горячий путь выдачи (простые Django-вьюхи, X-API-Key)
    path('serve/', serving.serve, name='ads-serve'),
    path('events/', serving.events, name='ads-events'),
    path('report/', serving.report, name='ads-report'),
    path('media/', AdMediaUploadView.as_view(), name='ads-media-upload'),
    path('media/<uuid:pk>/', AdMediaDetailView.as_view(), name='ads-media-detail'),
    path('directories/', include('apps.directories.urls')),
    path('resolve/', TelegramResolveView.as_view(), name='ads-resolve'),
    path('similar-channels/', SimilarChannelsView.as_view(), name='ads-similar-channels'),
    path('similar-bots/', SimilarBotsView.as_view(), name='ads-similar-bots'),
    path('<uuid:pk>/', AdDetailView.as_view(), name='ads-detail'),
    path('<uuid:pk>/budget/', AdBudgetTopUpView.as_view(), name='ads-budget'),
    path('<uuid:pk>/budget-history/', AdBudgetHistoryView.as_view(), name='ads-budget-history'),
    path('<uuid:pk>/stats/', AdStatsView.as_view(), name='ads-stats'),
    path('<uuid:pk>/copy-to-draft/', AdCopyToDraftView.as_view(), name='ads-copy-to-draft'),
]
