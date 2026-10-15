# Типовые кусочки для @extend_schema — чтобы не повторять одни и те же словари в каждом view

# Ответ вида {"message": "..."}
MESSAGE_RESPONSE = {
    'type': 'object',
    'properties': {'message': {'type': 'string'}},
}

# Ответ-ошибка вида {"error": "..."} (наш кастомный формат ошибок)
ERROR_RESPONSE = {
    'type': 'object',
    'properties': {'error': {'type': 'string'}},
}

# Ошибка DRF вида {"detail": "..."} (403, 503 и т.п.)
DETAIL_RESPONSE = {
    'type': 'object',
    'properties': {'detail': {'type': 'string'}},
}

# Ответы создания заказа, кроме успеха: 400 — валидация или нехватка денег
# (у AdGram ещё balance — текущий баланс), 403 — юзер заблокирован в AdGram,
# 503 — AdGram не ответил / не настроен (платёж не подтверждён, деньги не потеряются)
ORDER_CREATE_ERROR_RESPONSES = {
    400: {'type': 'object', 'additionalProperties': True},
    403: DETAIL_RESPONSE,
    503: DETAIL_RESPONSE,
}

# Авторизация server-to-server эндпоинтов (permission HasServerApiKey):
# заголовок X-API-Key, JWT там не работает. Сама схема securitySchemes
# объявлена в SPECTACULAR_SETTINGS['APPEND_COMPONENTS'].
SERVER_API_KEY_AUTH = [{'serverApiKey': []}]
