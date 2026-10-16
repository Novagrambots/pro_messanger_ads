from django.urls import include, path

from apps.ads.views import (AdBudgetHistoryView, AdBudgetTopUpView, AdDetailView, AdListCreateView,
                            AdPricingView)
from apps.directories.views import TelegramResolveView


urlpatterns = [
    path('', AdListCreateView.as_view(), name='ads-list'),
    path('pricing/', AdPricingView.as_view(), name='ads-pricing'),
    path('directories/', include('apps.directories.urls')),
    path('resolve/', TelegramResolveView.as_view(), name='ads-resolve'),
    path('<uuid:pk>/', AdDetailView.as_view(), name='ads-detail'),
    path('<uuid:pk>/budget/', AdBudgetTopUpView.as_view(), name='ads-budget'),
    path('<uuid:pk>/budget-history/', AdBudgetHistoryView.as_view(), name='ads-budget-history'),
]
