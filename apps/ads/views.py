from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ads.models import Ad, AdPricing, AdStateError, AdStatus
from apps.ads.serializers import (AdBudgetTopUpSerializer, AdCreateSerializer, AdPricingSerializer, AdSerializer,
                                  AdUpdateSerializer, BudgetOperationSerializer)
from apps.billing.models import MoneyOperation
from apps.billing.services import charge, format_money
from apps.common.pagination import StandardResultsSetPagination
from apps.common.schema import ORDER_CREATE_ERROR_RESPONSES

# Ответ на недопустимое действие: {"error": "..."} или {"<поле>": "..."}
STATE_ERROR_RESPONSE = {'type': 'object', 'additionalProperties': {'type': 'string'}}
DELETE_RESPONSE = {
    'type': 'object',
    'properties': {'message': {'type': 'string'}, 'refund_amount': {'type': 'string', 'format': 'decimal'}},
}


def own_ads(user):
    """Свои объявления владельца; удалённые в API не видны"""
    return Ad.objects.filter(user=user).exclude(status=AdStatus.DELETED)


def state_error(exc):
    return Response({exc.field or 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class AdListCreateView(generics.ListCreateAPIView):
    """Список своих объявлений (новые сверху) и создание нового"""
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    serializer_class = AdSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Ad.objects.none()
        return own_ads(self.request.user)

    @extend_schema(request=AdCreateSerializer, responses={201: AdSerializer, **ORDER_CREATE_ERROR_RESPONSES})
    def post(self, request, *args, **kwargs):
        serializer = AdCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        ad = serializer.save()
        return Response(AdSerializer(ad, context={'request': request}).data, status=status.HTTP_201_CREATED)


class AdDetailView(generics.GenericAPIView):
    """Детали, правка без модерации (PATCH) и мягкое удаление с возвратом остатка"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AdSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Ad.objects.none()
        return own_ads(self.request.user)

    def get(self, request, pk):
        return Response(AdSerializer(self.get_object(), context={'request': request}).data)

    @extend_schema(request=AdUpdateSerializer, responses={
        200: AdSerializer,  # всё применено сразу
        202: AdSerializer,  # креатив ушёл на модерацию — смотри pending_revision
        400: STATE_ERROR_RESPONSE,
    })
    def patch(self, request, pk):
        serializer = AdUpdateSerializer(instance=self.get_object(), data=request.data, partial=True,
                                        context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        settings_changes, creative_changes = serializer.split()
        revision = None
        try:
            with transaction.atomic():
                ad = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
                ad.update_settings(settings_changes)
                if creative_changes:
                    revision = ad.update_creative(creative_changes, request.user)
        except AdStateError as exc:
            return state_error(exc)
        return Response(AdSerializer(ad, context={'request': request}).data,
                        status=status.HTTP_202_ACCEPTED if revision else status.HTTP_200_OK)

    @extend_schema(responses={200: DELETE_RESPONSE})
    def delete(self, request, pk):
        with transaction.atomic():
            ad = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            refund_amount = ad.soft_delete()
        return Response({
            'message': 'Объявление удалено, остаток бюджета возвращён',
            'refund_amount': format_money(refund_amount),
        })


class AdBudgetTopUpView(generics.GenericAPIView):
    """Пополнение бюджета (с кошелька AdGram или локального баланса). STOPPED снова станет ACTIVE"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AdBudgetTopUpSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Ad.objects.none()
        return own_ads(self.request.user)

    @extend_schema(responses={200: AdSerializer, **ORDER_CREATE_ERROR_RESPONSES})
    def post(self, request, pk):
        ad = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Проверяем статус ДО списания денег (под локом проверит ещё раз add_budget)
        if ad.status not in Ad.BUDGET_STATUSES:
            return Response({'error': f'Нельзя пополнить бюджет: объявление в статусе «{ad.get_status_display()}»'},
                            status=status.HTTP_400_BAD_REQUEST)

        def add_to_ad(amount):
            # amount = оплачено + дробный остаток прошлых возвратов (его добавляет billing)
            locked = Ad.objects.select_for_update().get(pk=ad.pk)
            locked.add_budget(amount)
            return locked

        try:
            ad = charge(request.user, serializer.validated_data['amount'], MoneyOperation.OrderType.ADS,
                        add_to_ad, reason=MoneyOperation.Reason.TOP_UP)
        except AdStateError as exc:
            # Статус успел смениться после проверки: у локального кошелька всё откатилось,
            # у AdGram списание без объявления вернёт компенсация (через 10 минут)
            return state_error(exc)
        return Response(AdSerializer(ad, context={'request': request}).data)


class AdBudgetHistoryView(generics.ListAPIView):
    """История операций бюджета объявления (из журнала MoneyOperation), новые сверху"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = BudgetOperationSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return MoneyOperation.objects.none()
        ad = get_object_or_404(own_ads(self.request.user), pk=self.kwargs['pk'])
        return MoneyOperation.objects.filter(
            order_type=MoneyOperation.OrderType.ADS, order_id=ad.pk,
        ).order_by('-created_at')


class AdPricingView(APIView):
    """Минимальные CPM, наценки, минимальный бюджет и версия правил — для формы создания"""
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: AdPricingSerializer})
    def get(self, request):
        return Response(AdPricingSerializer.from_pricing(AdPricing.load()).data)
