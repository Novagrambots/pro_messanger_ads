from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from django.contrib.auth import get_user_model

from apps.common.permissions import IsSuperAdmin
from apps.common.schema import DETAIL_RESPONSE, ERROR_RESPONSE
from apps.billing.serializers import BalanceSerializer, AdminDepositSerializer
from apps.billing.services import admin_deposit, format_money, wallet_amount_for_display


User = get_user_model()


class BalanceView(generics.GenericAPIView):
    """
    Баланс текущего пользователя.
    У юзера AdGram — баланс кошелька AdGram + дробный остаток возвратов у нас
    (AdGram недоступен → 503). У остальных — локальный баланс.
    """
    serializer_class = BalanceSerializer
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: BalanceSerializer, 503: DETAIL_RESPONSE})
    def get(self, request):
        user = request.user
        # Строкой — как раньше (Decimal в JSON иначе стал бы float)
        return Response({
            'username': user.username,
            'amount': format_money(wallet_amount_for_display(user)),
            'user_id': str(user.user_id),
        })


class AdminDepositView(generics.GenericAPIView):
    """
    Пополнение баланса пользователя.
    Доступно только суперадмину — больше никто пополнять балансы не вправе.
    """
    serializer_class = AdminDepositSerializer
    permission_classes = [IsSuperAdmin]

    @extend_schema(request=AdminDepositSerializer, responses={
        400: ERROR_RESPONSE,  # у юзера AdGram кошелёк в AdGram — пополнять только там
        200: {
            'type': 'object',
            'properties': {
                'message': {'type': 'string'},
                'user_info': {
                    'type': 'object',
                    'properties': {
                        'user_id': {'type': 'string', 'format': 'uuid'},
                        'username': {'type': 'string'},
                        'email': {'type': 'string'},
                    },
                },
                'balance_info': {
                    'type': 'object',
                    'properties': {
                        'old_balance': {'type': 'string', 'format': 'decimal'},
                        'new_balance': {'type': 'string', 'format': 'decimal'},
                        'added_amount': {'type': 'string', 'format': 'decimal'},
                    },
                },
            },
        },
    })
    def post(self, request):
        """
        Пополняет баланс указанного пользователя.
        Доступно только суперадмину — больше никто пополнять балансы не вправе.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data['user']
        amount = serializer.validated_data['amount']

        # У юзера AdGram деньги живут в AdGram — локальный баланс ему не пополняем
        if user.adgram_user_id:
            return Response({'error': 'Пополнение только в AdGram'}, status=status.HTTP_400_BAD_REQUEST)

        # Пополняем баланс (с записью в журнал операций)
        balance = admin_deposit(user, amount)

        return Response({
            'message': f'Баланс пользователя {user.username} успешно пополнен на {amount}',
            'user_info': {
                'user_id': str(user.user_id),
                'username': user.username,
                'email': user.email
            },
            'balance_info': {
                # строками — как amount в /api/balance/ (Decimal в JSON иначе стал бы float)
                'old_balance': str(balance.amount - amount),
                'new_balance': str(balance.amount),
                'added_amount': str(amount)
            }
        }, status=status.HTTP_200_OK)
