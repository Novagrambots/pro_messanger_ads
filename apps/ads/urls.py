from django.urls import path

from apps.ads.views import (AdBudgetHistoryView, AdBudgetTopUpView, AdDetailView, AdListCreateView,
                            AdPricingView)


urlpatterns = [
    path('', AdListCreateView.as_view(), name='ads-list'),
    path('pricing/', AdPricingView.as_view(), name='ads-pricing'),
    path('<uuid:pk>/', AdDetailView.as_view(), name='ads-detail'),
    path('<uuid:pk>/budget/', AdBudgetTopUpView.as_view(), name='ads-budget'),
    path('<uuid:pk>/budget-history/', AdBudgetHistoryView.as_view(), name='ads-budget-history'),
]
