from django.urls import path, re_path

from .views import (PublicEarningView, ClaimChannelView, MyEarningsView,
                    WithdrawEarningView, AdminEarningsListView, ChannelMetaView)

urlpatterns = [
    path('earnings/', PublicEarningView.as_view(), name='partner-public-earnings'),
    path('admin/earnings/', AdminEarningsListView.as_view(), name='partner-admin-earnings'),
    path('claim/', ClaimChannelView.as_view(), name='partner-claim'),
    path('my/', MyEarningsView.as_view(), name='partner-my-earnings'),
    path('withdraw/', WithdrawEarningView.as_view(), name='partner-withdraw'),
    # id канала в формате Bot API отрицательный (-100…) — <int:> его не пропустит
    re_path(r'^channels/(?P<channel_id>-?\d{1,20})/$', ChannelMetaView.as_view(), name='partner-channel-meta'),
]
