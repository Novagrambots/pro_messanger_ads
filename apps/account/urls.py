from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (UserRegistrationView, UserProfileView, UserLoginView, UserTokenVerifyView,
                    AdminUserListView, UserRoleUpdateView, AdGramUserListView, AdGramUserDetailView,
                    AdGramUserRoleView)



urlpatterns = [
    # Регистрация и аутентификация
    path('register/', UserRegistrationView.as_view(), name='register'),
    # Кастомный логин: кроме токенов отдаёт user_id, username и role (нужно фронтенду)
    path('login/', UserLoginView.as_view(), name='login'),
    path('refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('verify/', UserTokenVerifyView.as_view(), name='token_verify'),

    # Профиль пользователя
    path('profile/', UserProfileView.as_view(), name='profile'),

    # Управление пользователями (только суперадмин)
    path('users/', AdminUserListView.as_view(), name='users-list'),
    path('users/<uuid:user_id>/role/', UserRoleUpdateView.as_view(), name='user-role'),

    # Роли юзеров AdGram по их id в AdGram (страница модераторов на сайте AdGram; только суперадмин)
    path('adgram-users/', AdGramUserListView.as_view(), name='adgram-users-list'),
    path('adgram-users/<str:adgram_user_id>/', AdGramUserDetailView.as_view(), name='adgram-user'),
    path('adgram-users/<str:adgram_user_id>/role/', AdGramUserRoleView.as_view(), name='adgram-user-role'),
]
