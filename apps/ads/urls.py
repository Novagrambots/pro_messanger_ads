from django.urls import include, path

from apps.ads.serving import views as serving
from apps.ads.views import (AdBudgetHistoryView, AdBudgetTopUpView, AdDetailView, AdListCreateView,
                            AdMediaDetailView, AdMediaUploadView, AdPricingView)
from apps.directories.views import SimilarBotsView, SimilarChannelsView, TelegramResolveView


urlpatterns = [
    path('', AdListCreateView.as_view(), name='ads-list'),
    path('pricing/', AdPricingView.as_view(), name='ads-pricing'),
    # Горячий путь выдачи (простые Django-вьюхи, X-API-Key)
    path('serve/', serving.serve, name='ads-serve'),
    path('events/', serving.events, name='ads-events'),
    path('media/', AdMediaUploadView.as_view(), name='ads-media-upload'),
    path('media/<uuid:pk>/', AdMediaDetailView.as_view(), name='ads-media-detail'),
    path('directories/', include('apps.directories.urls')),
    path('resolve/', TelegramResolveView.as_view(), name='ads-resolve'),
    path('similar-channels/', SimilarChannelsView.as_view(), name='ads-similar-channels'),
    path('similar-bots/', SimilarBotsView.as_view(), name='ads-similar-bots'),
    path('<uuid:pk>/', AdDetailView.as_view(), name='ads-detail'),
    path('<uuid:pk>/budget/', AdBudgetTopUpView.as_view(), name='ads-budget'),
    path('<uuid:pk>/budget-history/', AdBudgetHistoryView.as_view(), name='ads-budget-history'),
]
