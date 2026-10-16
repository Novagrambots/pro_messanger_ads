"""
Общие функции видео для старых заказов (chat_ads) и объявлений v2 (ads):
перекодирование в H.264/AAC mp4 с +faststart и ffprobe.

Безопасность (для обоих путей): формат входа задаём явно — демуксер mov (он читает и mp4,
и mov, внешние ссылки dref в нём выключены по умолчанию), протоколы — только локальный файл.
Иначе «mp4», внутри которого HLS-плейлист или ffconcat, мог бы заставить ffmpeg ходить по
сети (SSRF) — свежий ffmpeg такое уже не распознаёт, а в docker-образе версия может быть старше.
Плюс таймауты: зависший ffmpeg/ffprobe не держит воркер вечно.
"""
import json
import subprocess

FFPROBE_TIMEOUT = 15
FFMPEG_TIMEOUT = 5 * 60

# Опции ВХОДА — стоят перед -i (у ffprobe — перед именем файла)
SAFE_INPUT = ['-f', 'mov', '-protocol_whitelist', 'file']

# Масштаб для объявлений v2: исходный размер, если ширина ≤ 1280, иначе 1280 по ширине
# с сохранением пропорций (высота — чётная, как требует H.264)
SCALE_KEEP_UP_TO_1280 = "min(1280\\,iw):-2"
# Старые заказы chat_ads — как было: всегда 1280×720
SCALE_CHAT_ADS = '1280:720'


def h264_command(input_path, output_path, scale):
    """Команда ffmpeg: H.264 + AAC, +faststart (видео стартует до полной загрузки), 1 Мбит/с"""
    return [
        'ffmpeg', *SAFE_INPUT, '-i', input_path,
        '-vcodec', 'libx264',
        '-acodec', 'aac',
        '-preset', 'ultrafast',  # скорость конвертации (не качество!)
        '-movflags', '+faststart',
        '-vf', f'scale={scale}',
        '-b:v', '1M',  # видеобитрейт 1 Мбит/с
        '-b:a', '128k',  # аудиобитрейт
        '-y', output_path,
    ]


def transcode_to_h264(input_path, output_path, scale):
    """Перекодирует видео. Ошибка ffmpeg — CalledProcessError, завис — TimeoutExpired"""
    subprocess.run(h264_command(input_path, output_path, scale), check=True, capture_output=True,
                   timeout=FFMPEG_TIMEOUT)


def probe_command(path):
    return ['ffprobe', '-v', 'error', *SAFE_INPUT, '-print_format', 'json', '-show_format', '-show_streams', path]


def probe_video(path):
    """Данные ffprobe (format + streams). Не mp4/mov или битый файл — CalledProcessError"""
    result = subprocess.run(probe_command(path), check=True, capture_output=True, timeout=FFPROBE_TIMEOUT)
    return json.loads(result.stdout or b'{}')
