from django.urls import path

from apps.directories.views import CountriesView, LanguagesView, LocationsView, TopicsView


# Подключаются в apps/ads/urls.py под /api/ads/directories/
urlpatterns = [
    path('languages/', LanguagesView.as_view(), name='directories-languages'),
    path('topics/', TopicsView.as_view(), name='directories-topics'),
    path('countries/', CountriesView.as_view(), name='directories-countries'),
    path('locations/', LocationsView.as_view(), name='directories-locations'),
]
