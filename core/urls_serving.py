"""
URL-ы сервиса serving (docker-compose): только горячий путь выдачи.
Включается переменной SERVING_ONLY=True (см. settings.ROOT_URLCONF). Те же пути отдаёт и основной web.
"""
from django.urls import path

from apps.ads.serving import views

urlpatterns = [
    path('api/ads/serve/', views.serve, name='ads-serve'),
    path('api/ads/events/', views.events, name='ads-events'),
    path('api/ads/report/', views.report, name='ads-report'),
]
