"""
Описание горячего пути выдачи (serve, events, report) в Swagger. Вьюхи — простые Django (не DRF),
drf-spectacular их не видит, поэтому пути добавляет этот хук (SPECTACULAR_SETTINGS['POSTPROCESSING_HOOKS']).
"""
from apps.ads.models import AdReport, Placement

USER_ID = {'type': 'integer', 'format': 'int64', 'description': 'Числовой id пользователя Telegram. '
           'На сервере не хранится — только HMAC от него'}
ERROR = {'type': 'object', 'properties': {'error': {'type': 'string'}}}

SERVE_REQUEST = {
    'type': 'object',
    'required': ['placement', 'user_id'],
    'properties': {
        'placement': {'type': 'string', 'enum': list(Placement.values),
                      'description': 'Место показа. Работают SEARCH и BOT, остальные пока отдают пустой список'},
        'user_id': USER_ID,
        'context': {'type': 'object',
                    'description': 'SEARCH: {"query": "строка поиска"}; '
                                   'BOT: {"bot_username": "имя бота, в чате которого плашка"}',
                    'properties': {
                        'query': {'type': 'string', 'maxLength': 256},
                        'bot_username': {'type': 'string', 'description': 'Без @ или с @, регистр не важен'},
                    }},
        'lang': {'type': 'string', 'maxLength': 64},
        'platform': {'type': 'string', 'maxLength': 64},
        'app_version': {'type': 'string', 'maxLength': 64},
    },
}

AD = {
    'type': 'object',
    'properties': {
        'random_id': {'type': 'string', 'description': 'Подписанный токен показа (живёт 1 час) — '
                      'присылается в событиях view / click'},
        'placement': {'type': 'string'},
        'text': {'type': 'string', 'description': 'В поиске пустой'},
        'url': {'type': 'string', 'description': 'https://t.me/<username>'},
        'promoted': {
            'type': 'object',
            'properties': {
                'username': {'type': 'string'},
                'title': {'type': 'string'},
                'avatar_url': {'type': 'string', 'nullable': True},
                'telegram_id': {'type': 'integer', 'format': 'int64', 'nullable': True},
                'type': {'type': 'string', 'enum': ['CHANNEL', 'BOT']},
            },
        },
        'button_type': {'type': 'string', 'enum': ['CHANNEL', 'BOT'],
                        'description': 'Текст кнопки переводит клиент'},
        'media': {
            'type': 'object', 'nullable': True,
            'properties': {
                'type': {'type': 'string', 'enum': ['PHOTO', 'VIDEO']},
                'url': {'type': 'string'},
                'width': {'type': 'integer', 'nullable': True},
                'height': {'type': 'integer', 'nullable': True},
                'duration': {'type': 'string', 'format': 'decimal', 'nullable': True},
            },
        },
        'other_info': {'type': 'string'},
        'label': {'type': 'string', 'enum': ['Ad']},
    },
}

SERVE_RESPONSE = {
    'type': 'object',
    'properties': {
        'ads': {'type': 'array', 'items': AD, 'description': 'Поиск и боты — не больше одного объявления'},
        'ttl_seconds': {'type': 'integer', 'description': 'Сколько клиент кэширует ответ'},
    },
}

EVENTS_REQUEST = {
    'type': 'object',
    'required': ['user_id', 'events'],
    'properties': {
        'user_id': USER_ID,
        'events': {
            'type': 'array', 'minItems': 1, 'maxItems': 50,
            'items': {
                'type': 'object',
                'required': ['random_id', 'type'],
                'properties': {
                    'random_id': {'type': 'string'},
                    'type': {'type': 'string', 'enum': ['view', 'click']},
                    'duration_ms': {'type': 'integer', 'description': 'view: сколько видел, мс (считаем от 1000)'},
                    'visible_ratio': {'type': 'number', 'description': 'view: видимая доля 0–1 (считаем от 0.5)'},
                },
            },
        },
    },
}

EVENTS_RESPONSE = {
    'type': 'object',
    'properties': {'accepted': {'type': 'integer'}, 'rejected': {'type': 'integer'}},
}

REPORT_REQUEST = {
    'type': 'object',
    'required': ['user_id', 'random_id', 'reason'],
    'properties': {
        'user_id': USER_ID,
        'random_id': {'type': 'string', 'description': 'random_id показанного объявления (живёт 1 час)'},
        'reason': {'type': 'string', 'enum': list(AdReport.Reason.values)},
        'details': {'type': 'string', 'maxLength': 500},
    },
}

REPORT_RESPONSE = {'type': 'object', 'properties': {'reported': {'type': 'boolean'}}}


def _operation(operation_id, summary, description, request, ok, extra, ok_codes=('200',)):
    responses = {code: {'description': '', 'content': {'application/json': {'schema': ok}}} for code in ok_codes}
    for code, text in {'400': 'Неверное тело запроса', '403': 'Неверный или отсутствующий X-API-Key',
                       '429': 'Лимит запросов (на зрителя или IP) в минуту', **extra}.items():
        responses[code] = {'description': text, 'content': {'application/json': {'schema': ERROR}}}
    return {'post': {
        'operationId': operation_id,
        'summary': summary,
        'description': description,
        'tags': ['ads'],
        'security': [{'serverApiKey': []}],
        'requestBody': {'required': True, 'content': {'application/json': {'schema': request}}},
        'responses': responses,
    }}


PATHS = {
    '/api/ads/serve/': _operation(
        'ads_serve', 'Объявление для показа',
        'Запрос рекламы из приложения. Ничего не списывает — деньги списываются по событию view. '
        'Нет подходящего объявления — {"ads": []}. Сервис недоступен — {"ads": [], "ttl_seconds": 60}.',
        SERVE_REQUEST, SERVE_RESPONSE, {'503': 'Выдача не настроена'}),
    '/api/ads/events/': _operation(
        'ads_events', 'События показа и клика',
        'До 50 событий в пачке, каждое обрабатывается отдельно. Один view и один click на random_id; '
        'токен другого пользователя или просроченный — отклоняется. 503 — пришлите пачку позже.',
        EVENTS_REQUEST, EVENTS_RESPONSE, {'503': 'Приём событий временно недоступен'}),
    '/api/ads/report/': _operation(
        'ads_report', 'Жалоба на объявление',
        'Одна жалоба от пользователя на объявление: 201 — принята, 200 — уже была. '
        'random_id — из выдачи этому же пользователю, не старше часа.',
        REPORT_REQUEST, REPORT_RESPONSE, {'404': 'Объявление не найдено', '503': 'Жалобы временно не принимаются'},
        ok_codes=('201', '200')),
}


def add_serving_paths(result, generator, request, public):
    result.setdefault('paths', {}).update(PATHS)
    return result
