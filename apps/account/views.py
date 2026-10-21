from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import filters, generics, permissions, serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenVerifyView
from django.contrib.auth import get_user_model

from apps.common.pagination import StandardResultsSetPagination
from apps.common.permissions import IsSuperAdmin
from apps.account.serializers import (UserRegistrationSerializer, UserLoginSerializer, UserProfileSerializer,
                                      AdminUserSerializer, UserRoleUpdateSerializer, AdGramUserRoleSerializer)
from apps.account.services import (ADGRAM_USER_ID_RE, ADGRAM_USERNAME_PREFIX, AdGramUsernameTaken,
                                   get_or_create_adgram_user)



User = get_user_model()



class UserRegistrationView(generics.CreateAPIView):
    """Регистрация нового пользователя"""
    queryset = User.objects.all()
    serializer_class = UserRegistrationSerializer
    permission_classes = [permissions.AllowAny]


@extend_schema(
    responses={
        200: {
            "type": "object",
            "properties": {
                "refresh": {"type": "string"},
                "access": {"type": "string"},
                "user_id": {"type": "string", "format": "uuid"},
                "username": {"type": "string"},
                "role": {"type": "string"},
            }
        }
    }
)
class UserLoginView(TokenObtainPairView):
    """Вход пользователя"""
    serializer_class = UserLoginSerializer
    permission_classes = [permissions.AllowAny]


class UserProfileView(generics.RetrieveAPIView):
    """Получение профиля текущего пользователя"""
    serializer_class = UserProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


@extend_schema(responses={
    200: {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
        }
    }
})
class UserTokenVerifyView(TokenVerifyView):
    def post(self, request: Request, *args, **kwargs) -> Response:
        response = super().post(request, *args, **kwargs)

        if response.status_code == 200:
            response.data['status'] = 'success'

        return response


class AdminUserListView(generics.ListAPIView):
    """
    Список пользователей для суперадмина.
    Поиск по username/email через ?search=, нужен для назначения ролей и пополнения баланса.
    """
    serializer_class = AdminUserSerializer
    permission_classes = [IsSuperAdmin]
    pagination_class = StandardResultsSetPagination
    queryset = User.objects.select_related('balance').order_by('username')
    filter_backends = [filters.SearchFilter]
    search_fields = ['username', 'email']


class UserRoleUpdateView(generics.GenericAPIView):
    """
    Смена роли пользователя (рекламодатель/модератор).
    Доступно только суперадмину.
    """
    serializer_class = UserRoleUpdateSerializer
    permission_classes = [IsSuperAdmin]

    @extend_schema(
        request=UserRoleUpdateSerializer,
        responses={
            200: {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "user_id": {"type": "string", "format": "uuid"},
                    "role": {"type": "string"},
                }
            },
            404: {
                "type": "object",
                "properties": {"error": {"type": "string"}},
            },
        }
    )
    def post(self, request, user_id):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            user = User.objects.get(user_id=user_id)
        except User.DoesNotExist:
            return Response({'error': 'Пользователь не найден'}, status=status.HTTP_404_NOT_FOUND)

        user.set_role(serializer.validated_data['role'])

        return Response({
            'message': f'Роль пользователя {user.username} изменена на {user.get_role_display()}',
            'user_id': str(user.user_id),
            'role': user.role,
        }, status=status.HTTP_200_OK)


# ======================== Роли юзеров AdGram (страница модераторов на сайте AdGram) ========================
# Админ AdGram знает только id пользователя в AdGram — поэтому API по adgram_user_id, а не по нашему uuid

ADGRAM_ERROR_RESPONSE = {'type': 'object', 'additionalProperties': {'type': 'string'}}


def bad_adgram_user_id(adgram_user_id):
    if not ADGRAM_USER_ID_RE.fullmatch(adgram_user_id):
        return Response({'adgram_user_id': 'Некорректный id пользователя AdGram'},
                        status=status.HTTP_400_BAD_REQUEST)
    return None


def adgram_user_payload(adgram_user_id, user):
    """Тени ещё нет (человек ни разу не входил в NovaGram) — обычный рекламодатель"""
    if user is None:
        return {'adgram_user_id': adgram_user_id, 'adgram_username': '', 'role': User.Role.ADVERTISER,
                'user_id': None}
    return AdGramUserRoleSerializer(user).data


class AdGramUserListView(generics.ListAPIView):
    """Юзеры AdGram с ролью в new_dvs (?role=MODERATOR — модераторы). Только суперадмин"""
    serializer_class = AdGramUserRoleSerializer
    permission_classes = [IsSuperAdmin]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        queryset = User.objects.exclude(adgram_user_id=None).order_by('adgram_username', 'username')
        role = self.request.query_params.get('role')
        if role:
            if role not in User.Role.values:
                raise serializers.ValidationError({'role': f'Роль — одна из: {", ".join(User.Role.values)}'})
            queryset = queryset.filter(role=role)
        return queryset

    @extend_schema(parameters=[OpenApiParameter('role', str, enum=User.Role.values,
                                                description='Только с этой ролью')])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class AdGramUserDetailView(APIView):
    """Роль юзера AdGram в new_dvs. Тени нет — 200 с ADVERTISER (тень не создаём)"""
    permission_classes = [IsSuperAdmin]

    @extend_schema(responses={200: AdGramUserRoleSerializer, 400: ADGRAM_ERROR_RESPONSE})
    def get(self, request, adgram_user_id):
        error = bad_adgram_user_id(adgram_user_id)
        if error:
            return error
        user = User.objects.filter(adgram_user_id=adgram_user_id).first()
        return Response(adgram_user_payload(adgram_user_id, user))


class AdGramUserRoleView(APIView):
    """
    Назначить или снять модератора юзеру AdGram. Тени нет — создаём, как при первом входе
    (имя заполнится при входе). ADMIN так не ставится — он приходит только из токена AdGram
    """
    permission_classes = [IsSuperAdmin]

    @extend_schema(request=UserRoleUpdateSerializer,
                   responses={200: AdGramUserRoleSerializer, 400: ADGRAM_ERROR_RESPONSE, 409: ADGRAM_ERROR_RESPONSE})
    def put(self, request, adgram_user_id):
        error = bad_adgram_user_id(adgram_user_id)
        if error:
            return error
        serializer = UserRoleUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = get_or_create_adgram_user(adgram_user_id)
        except AdGramUsernameTaken:
            return Response({'error': f'Имя {ADGRAM_USERNAME_PREFIX}{adgram_user_id} уже занято другим пользователем'},
                            status=status.HTTP_409_CONFLICT)
        user.set_role(serializer.validated_data['role'])
        return Response(adgram_user_payload(adgram_user_id, user))
