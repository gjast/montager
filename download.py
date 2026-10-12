"""Скачивание видео целиком или отрезком. Откуда ссылка — определяется само (source_of),
для каждого источника свои способы, они пробуются по очереди (ROUTES).

YouTube:
1. pytubefix (поддерживаемое продолжение pytube; сама pytube с YouTube не работает)
   достаёт ссылки на потоки, а данные качаем сами, кусками в несколько потоков:
   так YouTube не режет скорость, а для отрезка берутся только нужные фрагменты.
2. yt-dlp — запасной: подхватывает, когда первый способ упал.

Instagram (рилсы и посты с видео из открытых аккаунтов), всё без входа в аккаунт:
1. yt-dlp — единственный берёт потоки 1080×1920; остальные получают готовый файл 720×1280.
2. parth-dl — прямая ссылка на файл; быстрый, подхватывает, когда yt-dlp просят войти.
3. instaloader — без входа Instagram отвечает ему отказом чаще остальных.
4–5. eeinstagram, kkinstagram — сервисы-посредники (см. ниже).

TikTok:
1. yt-dlp.
2. gallery-dl — свой, независимый разбор страницы TikTok: выручает, когда yt-dlp сломан.
3. fxtiktok (tnktok.com) — сервис-посредник.

Сервисы-посредники — сторонние сайты, которые сами ходят за роликом и отвечают
перенаправлением на файл. К сайту обращаются они, а не мы, поэтому выручают, когда
Instagram или TikTok ограничил запросы с нашего адреса. Им уходит только ссылка на ролик;
сам файл качаем не у них, а с серверов Instagram/TikTok — другие адреса не принимаются.
Стоят последними: чужой сервис может закрыться в любой день.

Остальные сайты — yt-dlp.
"""
import html
import json
import os
import re
import shutil
from datetime import datetime, timezone
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MAX_HEIGHT = 1080
CHUNK = 4 * 1024 * 1024
THREADS = 4

# MONTAGE_HOME — постоянная папка при запуске через Montager.exe (см. montage.py)
DOWNLOAD_DIR = Path(os.environ.get("MONTAGE_HOME") or Path(__file__).resolve().parent) / "downloads"


class DownloadError(Exception):
    pass


class BotBlocked(DownloadError):
    """YouTube принял запросы с этого адреса за бота и ничего не отдаёт."""


SOURCE_NAMES = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok"}
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def with_scheme(url):
    url = url.strip()
    return url if re.match(r"https?://", url, re.I) else "https://" + url


def source_of(url):
    """По адресу ссылки: 'youtube', 'instagram', 'tiktok' или 'other'."""
    host = (urllib.parse.urlparse(with_scheme(url)).hostname or "").lower()
    if host == "youtu.be" or re.search(r"(^|\.)(youtube|youtube-nocookie)\.com$", host):
        return "youtube"
    if host == "instagr.am" or re.search(r"(^|\.)instagram\.com$", host):
        return "instagram"
    if re.search(r"(^|\.)tiktok\.com$", host):
        return "tiktok"
    return "other"


def probe_url(url):
    """(короткая сторона кадра, длина в секундах) файла по ссылке; что не узнали — 0.
    Нужно там, где сайт этого не сообщает или сообщает про оригинал, а не про сам файл."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-user_agent", USER_AGENT, "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration", "-of", "json", url],
            capture_output=True, text=True, timeout=40)
        info = json.loads(r.stdout)
        stream = (info.get("streams") or [{}])[0]
        side = min(int(stream.get("width") or 0), int(stream.get("height") or 0))
        return side, float(info.get("format", {}).get("duration") or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0, 0.0


def parse_time(text):
    """'90', '1:30', '1:02:30' → секунды."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(p.strip().isdigit() for p in parts):
        raise ValueError(text)
    sec = 0
    for p in parts:
        sec = sec * 60 + int(p)
    return sec


def parse_range(text, length):
    """'12:30-15:00' → (750, 900). Конец можно не писать: '12:30-' — до конца видео."""
    m = re.fullmatch(r"\s*([\d:]+)\s*[-–—]\s*([\d:]*)\s*", text)
    if not m:
        raise ValueError(text)
    start = parse_time(m.group(1))
    end = parse_time(m.group(2)) if m.group(2) else length
    end = min(end, length)
    if start >= end:
        raise ValueError(text)
    return start, end


def fmt_clock(sec):
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fetch(url, start, end):
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(f"{url}&range={start}-{end}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
            if len(data) == end - start + 1:
                return data
            last = f"получено {len(data)} байт вместо {end - start + 1}"
        except (urllib.error.URLError, OSError) as e:
            last = e
        time.sleep(1 + attempt)
    raise DownloadError(f"не удалось скачать кусок видео: {last}")


def read_index(url):
    """Читает начало mp4-потока: заголовок и таблицу фрагментов (sidx).
    Возвращает (заголовок, [(время начала, время конца, первый байт, последний байт)])."""
    head = fetch(url, 0, 65535)
    pos = 0
    while True:
        if pos + 8 > len(head):
            raise DownloadError("у потока нет таблицы фрагментов")
        size, kind = struct.unpack(">I4s", head[pos:pos + 8])
        if kind == b"sidx":
            break
        if size < 8:
            raise DownloadError("непонятный формат потока")
        pos += size
    if pos + size > len(head):
        head += fetch(url, len(head), pos + size - 1)
    box = head[pos:pos + size]
    version = box[8]
    timescale = struct.unpack(">I", box[16:20])[0]
    if version == 0:
        earliest, first_offset = struct.unpack(">II", box[20:28])
        at = 28
    else:
        earliest, first_offset = struct.unpack(">QQ", box[20:36])
        at = 36
    count = struct.unpack(">H", box[at + 2:at + 4])[0]
    at += 4
    offset = pos + size + first_offset
    clock = earliest
    fragments = []
    for i in range(count):
        ref, duration = struct.unpack(">II", box[at + i * 12:at + i * 12 + 8])
        length = ref & 0x7FFFFFFF
        fragments.append((clock / timescale, (clock + duration) / timescale,
                          offset, offset + length - 1))
        offset += length
        clock += duration
    return head[:pos + size], fragments


def plan_stream(stream, start, end):
    """Что качать из потока, чтобы покрыть отрезок [start, end]."""
    header, fragments = read_index(stream.url)
    need = [f for f in fragments if f[1] > start and f[0] < end] or fragments[-1:]
    return {"url": stream.url, "header": header, "time0": need[0][0],
            "first": need[0][2], "last": need[-1][3]}


def download_plan(plan, path, on_bytes, pool):
    chunks = [(a, min(a + CHUNK, plan["last"] + 1) - 1)
              for a in range(plan["first"], plan["last"] + 1, CHUNK)]
    with open(path, "wb") as f:
        f.write(plan["header"])
        # map сохраняет порядок кусков, качаются они при этом параллельно
        for data in pool.map(lambda c: fetch(plan["url"], *c), chunks):
            f.write(data)
            on_bytes(len(data))


BOT_MESSAGE = ("YouTube принял запросы с этого интернет-адреса за бота и не отдаёт видео "
               "без входа в аккаунт.\nЧаще всего так бывает с адресами VPN и хостингов: "
               "смена одного VPN на другой не помогает.\nПомогает обычное подключение "
               "без VPN — домашний или мобильный интернет.")


# ---------- способ 1: pytubefix + своя загрузка кусками (быстрый, только YouTube) ----------
def pytube_info(url):
    from pytubefix import YouTube
    from pytubefix.exceptions import BotDetection, LoginRequired, PytubeFixError
    try:
        yt = YouTube(url)
        length = yt.length
        videos = [s for s in yt.streams.filter(only_video=True, file_extension="mp4")
                  if s.resolution and int(s.resolution[:-1]) <= MAX_HEIGHT]
        audio = yt.streams.filter(only_audio=True, file_extension="mp4").order_by("abr").last()
    except (BotDetection, LoginRequired) as e:
        # вход через pytubefix (use_oauth) проверен и не работает: после подтверждения
        # YouTube отвечает «The page needs to be reloaded» и ошибкой 400
        raise BotBlocked(BOT_MESSAGE) from e
    except PytubeFixError as e:
        raise DownloadError(f"YouTube не отдал видео: {e}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DownloadError(f"нет связи с YouTube: {e}") from e
    if not videos or audio is None or not length:
        raise DownloadError("у этого видео нет подходящих потоков (возможно, это трансляция)")
    # лучшее разрешение; при равном — H.264 (его читает всё), затем больше кадров/с
    videos.sort(key=lambda s: (int(s.resolution[:-1]), s.video_codec.startswith("avc1"), s.fps),
                reverse=True)
    return {"title": yt.title, "length": length, "resolution": videos[0].resolution,
            "handle": (videos[0], audio)}


# ---------- способ 2: yt-dlp (медленнее на отрезках, зато знает и другие сайты) ----------
def ytdlp_error(e):
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()
    if "Sign in to confirm" in text:
        return BotBlocked(BOT_MESSAGE)
    return DownloadError(text.split(". Use --cookies")[0][:300])


def ytdlp_base():
    """Общие настройки yt-dlp. Для YouTube ему нужен движок JavaScript; из коробки он ищет
    только deno, поэтому подключаем Node.js, если он установлен."""
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": Silent()}
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
    return opts


class Silent:
    """yt-dlp пишет ошибки прямо в терминал; мы показываем их сами."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def ytdlp_info(url):
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL(ytdlp_base()) as y:
            info = y.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ytdlp_error(e) from e
    if info.get("is_live"):
        raise DownloadError("это прямая трансляция — её не скачать")
    # «1080p» — это короткая сторона: у вертикального ролика 1080×1920 высота 1920
    videos = [f for f in info.get("formats") or [info]
              if f.get("height") and f.get("vcodec") != "none"]
    sides = [min(f["height"], f.get("width") or f["height"]) for f in videos]
    sides = [x for x in sides if x <= MAX_HEIGHT] or sides
    length = info.get("duration") or 0
    if not length and videos and videos[-1].get("url"):
        # Instagram длину не сообщает, а без неё не выбрать отрезок — смотрим в самом файле
        length = probe_url(videos[-1]["url"])[1]
    title = info.get("title") or "video"
    if source_of(url) == "instagram":
        # у yt-dlp название рилса — «Video by nasa»; берём подпись и номер, как у остальных способов
        title = post_title(info.get("channel") or info.get("uploader_id"),
                            info.get("description"), info.get("id"))
    return {"title": title, "length": int(length),
            "resolution": f"{max(sides)}p" if sides else "?", "handle": url}


def ytdlp_download(video, out, start, end, whole, on_progress):
    import yt_dlp
    best = f"[height<={MAX_HEIGHT}]"
    opts = {
        **ytdlp_base(), "noprogress": True,
        # сначала H.264 + AAC в mp4 (читается везде), потом что найдётся
        "format": (f"bv*{best}[vcodec^=avc1]+ba[ext=m4a]/bv*{best}[ext=mp4]+ba[ext=m4a]/"
                   f"bv*{best}+ba/b{best}/b"),
        "merge_output_format": "mp4",
        "outtmpl": str(out.with_suffix("")) + ".%(ext)s",
        "overwrites": True,
    }
    if video.get("source") != "youtube":
        # рилсы и тиктоки вертикальные: у 1080×1920 высота 1920, и отбор «высота до 1080»
        # выкидывал лучшее качество. res в yt-dlp — короткая сторона, ей и ограничиваем
        opts.update(format="bv*+ba/b",
                    format_sort=[f"res:{MAX_HEIGHT}", "vcodec:h264", "acodec:aac"])
    if not whole:
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start, end)])
    if on_progress:
        def hook(d):
            if d.get("status") == "downloading":
                on_progress(d.get("downloaded_bytes") or 0,
                            d.get("total_bytes") or d.get("total_bytes_estimate"))
        opts["progress_hooks"] = [hook]
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([video["handle"]])
    except yt_dlp.utils.DownloadError as e:
        raise ytdlp_error(e) from e
    if not out.is_file():
        made = sorted(out.parent.glob(out.stem + ".*"))
        if not made:
            raise DownloadError("yt-dlp не создал файл")
        return made[0]
    return out


# ---------- Instagram ----------
INSTA_PRIVATE = ("Instagram не отдаёт это видео без входа в аккаунт: закрытый профиль, "
                 "удалённый пост или ограничение по возрасту.")
INSTA_LIMIT = ("Instagram временно ограничил запросы с этого интернет-адреса. "
               "Подожди несколько минут; с VPN такое бывает чаще.")


def insta_code(url):
    """Номер поста из ссылки: /reel/XXXX/, /reels/XXXX/, /p/XXXX/, /tv/XXXX/."""
    m = re.search(r"/(?:reels?|p|tv)/([A-Za-z0-9_-]+)", urllib.parse.urlparse(with_scheme(url)).path)
    return m.group(1) if m else None


def insta_url(url):
    """Приводит ссылку к виду https://www.instagram.com/reel/XXXX/ — без меток отслеживания.
    Короткие ссылки «Поделиться» (/share/...) сначала раскрываются."""
    url = with_scheme(url)
    if "/share/" in urllib.parse.urlparse(url).path:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                url = r.geturl()
        except (urllib.error.URLError, OSError):
            pass
    code = insta_code(url)
    if not code:
        raise DownloadError("по этой ссылке нет видео: нужна ссылка на рилс или пост "
                            "(instagram.com/reel/... или instagram.com/p/...)")
    return f"https://www.instagram.com/reel/{code}/"


def post_title(author, caption, code):
    """Имя файла: автор, начало подписи и номер поста (чтобы ролики не затирали друг друга).
    Подпись ужимается так, чтобы номер не обрезался: имя файла ограничено 80 знаками."""
    author = f"@{author}" if author else ""
    code = str(code or "")
    lines = (caption or "").strip().splitlines()
    room = max(0, 78 - len(author) - len(code))
    parts = [author, lines[0].strip()[:min(50, room)].strip() if lines else "", code]
    return " ".join(x for x in parts if x) or "video"


def parth_info(url):
    import parth_dl
    from parth_dl.utils import NetworkError, RateLimitError
    page = insta_url(url)
    try:
        info = parth_dl.InstagramDownloader(quiet=True).get_info(page)
    except RateLimitError as e:
        raise DownloadError(INSTA_LIMIT) from e
    except NetworkError as e:
        raise DownloadError(f"нет связи с Instagram: {e}") from e
    except parth_dl.DownloadError as e:
        text = str(e)
        if re.search(r"private|login|not available|unavailable|deleted|removed", text, re.I):
            raise DownloadError(INSTA_PRIVATE) from e
        raise DownloadError(f"Instagram не отдал видео: {text[:300]}") from e
    clips = [e for e in info.get("entries") or [] if e.get("kind") == "video" and e.get("formats")]
    if not clips:
        raise DownloadError("в этом посте нет видео — только фото")
    # в посте-карусели видео может быть несколько — берём первое
    formats = [f for f in clips[0]["formats"] if f.get("url")]
    formats = [f for f in formats if f.get("has_audio")] or formats
    best = max(formats, key=lambda f: (f.get("width") or 0) * (f.get("height") or 0))
    # размеры в ответе Instagram — от оригинала, сам файл обычно меньше; смотрим в файле
    side, length = probe_url(best["url"])
    return {"title": post_title(info.get("uploader"), info.get("title"), insta_code(page)),
            "length": int(info.get("duration") or length),
            "resolution": f"{side}p" if side else "?", "handle": best["url"]}


def instaloader_info(url):
    import instaloader
    page = insta_url(url)
    try:
        loader = instaloader.Instaloader(quiet=True, max_connection_attempts=1)
        post = instaloader.Post.from_shortcode(loader.context, insta_code(page))
        clips = ([post.video_url] if post.is_video else
                 [n.video_url for n in post.get_sidecar_nodes() if n.is_video]
                 if post.typename == "GraphSidecar" else [])
        author, caption, length = post.owner_username, post.caption, post.video_duration
    except instaloader.exceptions.LoginRequiredException as e:
        raise DownloadError(INSTA_PRIVATE) from e
    except instaloader.exceptions.TooManyRequestsException as e:
        raise DownloadError(INSTA_LIMIT) from e
    except instaloader.exceptions.InstaloaderException as e:
        text = str(e)
        if "403" in text or "401" in text:
            raise DownloadError("Instagram отказал в доступе без входа в аккаунт (403)") from e
        raise DownloadError(f"Instagram не отдал видео: {text[:300]}") from e
    clips = [c for c in clips if c]
    if not clips:
        raise DownloadError("в этом посте нет видео — только фото")
    side, probed = probe_url(clips[0])
    return {"title": post_title(author, caption, insta_code(page)),
            "length": int(length or probed), "resolution": f"{side}p" if side else "?",
            "handle": clips[0]}


def direct_download(video, out, start, end, whole, on_progress):
    """Файл по прямой ссылке качается целиком (рилсы короткие), отрезок вырезается из него."""
    with tempfile.TemporaryDirectory(prefix="igdl_") as tmp:
        raw = Path(tmp) / "video.mp4"
        last = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(video["handle"], headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=40) as r, open(raw, "wb") as f:
                    total = int(r.headers.get("Content-Length") or 0) or None
                    got = 0
                    while True:
                        data = r.read(256 * 1024)
                        if not data:
                            break
                        f.write(data)
                        got += len(data)
                        if on_progress:
                            on_progress(got, total)
                if total and got != total:
                    last = f"получено {got} байт вместо {total}"
                    continue
                break
            except urllib.error.HTTPError as e:
                # ссылка на файл живёт недолго; после отказа её надо получать заново
                raise DownloadError(f"сервер не отдал файл: HTTP {e.code}") from e
            except (urllib.error.URLError, OSError) as e:
                last = e
            time.sleep(1 + attempt)
        else:
            raise DownloadError(f"не удалось скачать видео: {last}")
        return cut_file(raw, out, start, end, whole)


def cut_file(raw, out, start, end, whole):
    """Перекладывает скачанный целиком файл в out, вырезая отрезок, если он задан."""
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if not whole:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(raw), "-map", "0:v", "-map", "0:a?", "-c", "copy"]
    if not whole:
        cmd += ["-t", str(end - start)]
    cmd += ["-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise DownloadError("не удалось собрать файл:\n" + r.stderr.strip()[-300:])
    return out


# ---------- сервисы-посредники ----------
# так представляются боты мессенджеров; посредники отдают им страницу с адресом файла
BOT_AGENT = "TelegramBot (like TwitterBot)"
# принимаем файл только с серверов самих Instagram и TikTok
CDN = {"instagram": r"(^|\.)(cdninstagram\.com|fbcdn\.net)$",
       "tiktok": r"(^|\.)(tiktokcdn(-[a-z]+)?\.com|tiktokv\.(com|us|eu)|tiktok\.com|"
                 r"byteoversea\.com|muscdn\.com)$"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def relay_page(url):
    """Страница посредника в том виде, в каком он отдаёт её ботам мессенджеров."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BOT_AGENT})
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.read(600_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise DownloadError(f"посредник ответил отказом: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DownloadError(f"посредник недоступен: {e}") from e


def relay_meta(page, name):
    """Значение тега <meta property|name="..."> со страницы посредника."""
    m = re.search(rf'<meta (?:property|name)="{re.escape(name)}" content="([^"]*)"', page)
    return html.unescape(m.group(1)).strip() if m else ""


def relay_target(url, site, agent=BOT_AGENT):
    """Куда посредник перенаправляет за файлом. Адрес должен быть сервером самого сайта:
    файл, который посредник предлагает взять у себя или где-то ещё, не берём."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": agent})
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=25):
            raise DownloadError("посредник не дал ссылку на файл")
    except urllib.error.HTTPError as e:
        target = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
        if not target:
            raise DownloadError(f"посредник ответил отказом: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DownloadError(f"посредник недоступен: {e}") from e
    parts = urllib.parse.urlparse(target)
    if parts.scheme != "https" or not re.search(CDN[site], (parts.hostname or "").lower()):
        raise DownloadError(f"посредник дал ссылку на посторонний сервер ({parts.hostname})")
    return target


def relay_video(media, title):
    side, length = probe_url(media)
    if not side:
        raise DownloadError("по ссылке посредника нет видео")
    return {"title": title, "length": int(length), "resolution": f"{side}p", "handle": media}


def eeinstagram_info(url):
    code = insta_code(insta_url(url))
    page = relay_page(f"https://www.eeinstagram.com/reel/{code}/")
    author = relay_meta(page, "twitter:title").lstrip("@")
    m = re.search(r'href="[^"]*/oembed\?text=([^&"]*)', page)
    caption = urllib.parse.unquote_plus(html.unescape(m.group(1))) if m else ""
    media = relay_target(f"https://www.eeinstagram.com/videos/{code}/1", "instagram")
    return relay_video(media, post_title(author, caption, code))


def kkinstagram_info(url):
    code = insta_code(insta_url(url))
    media = relay_target(f"https://www.kkinstagram.com/reel/{code}/", "instagram")
    return relay_video(media, post_title(None, None, code))


def fxtiktok_info(url):
    """fxTikTok (github.com/okdargy/fxTikTok): та же ссылка, только на tnktok.com."""
    path = urllib.parse.urlparse(with_scheme(url)).path
    page = relay_page("https://tnktok.com" + (path if path.startswith("/") else "/" + path))
    m = re.search(r"/generate/video/(\d+)", relay_meta(page, "og:video"))
    if not m:
        raise DownloadError("посредник не нашёл видео по этой ссылке")
    title = post_title(relay_meta(page, "twitter:creator").lstrip("@"),
                       relay_meta(page, "og:description"), m.group(1))
    base = f"https://offload.tnktok.com/generate/video/{m.group(1)}.mp4"
    try:
        # лучшее качество (1080p, HEVC) fxTikTok отдаёт только браузерам: ботам мессенджеров
        # достаётся 576p, которое те точно смогут показать
        media = relay_target(base + "?hq=true", "tiktok", USER_AGENT)
    except DownloadError:
        media = relay_target(base, "tiktok")
    return relay_video(media, title)


# ---------- gallery-dl (TikTok) ----------
def gallery_run(args, timeout):
    """gallery-dl отдельным процессом: у него лицензия GPL, и так он остаётся самостоятельной
    программой, а не частью нашей. Заодно зависший запрос обрывается по времени.
    Пути к библиотекам передаём сами — Python из набора для Windows читает их только так."""
    code = ("import sys; sys.path[:0] = " + repr([p for p in sys.path if p])
            + "; import gallery_dl; sys.exit(gallery_dl.main())")
    try:
        return subprocess.run([sys.executable, "-c", code, "--retries", "1", "--http-timeout", "25",
                               *args], capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise DownloadError("gallery-dl не ответил вовремя") from e


def gallery_error(result):
    text = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return DownloadError("gallery-dl: " + (text[-1][:300] if text else "не удалось получить видео"))


def gallery_info(url):
    r = gallery_run(["-j", with_scheme(url)], 90)
    try:
        items = json.loads(r.stdout.decode("utf-8", "replace"))
    except ValueError:
        raise gallery_error(r) from None
    # в выдаче gallery-dl запись с адресом файла — [3, адрес, сведения]
    clips = [x for x in items if isinstance(x, list) and len(x) == 3 and x[0] == 3
             and isinstance(x[2], dict) and x[2].get("extension") == "mp4"]
    if not clips:
        failed = [x[1].get("message") for x in items
                  if isinstance(x, list) and x and x[0] == -1 and isinstance(x[1], dict)]
        raise DownloadError("gallery-dl: " + (str(failed[0])[:300] if failed else "в посте нет видео"))
    info = clips[0][2]
    author = info.get("author")
    # width/height в записи — от самого слабого варианта; gallery-dl качает лучший из bitrateInfo
    video = info.get("video") if isinstance(info.get("video"), dict) else {}
    sizes = [x.get("PlayAddr") for x in video.get("bitrateInfo") or [] if isinstance(x, dict)]
    sizes = [(a.get("Width"), a.get("Height")) for a in sizes if isinstance(a, dict)]
    sizes = [min(w, h) for w, h in sizes if isinstance(w, int) and isinstance(h, int)]
    side = max(sizes) if sizes else min(int(info.get("width") or 0), int(info.get("height") or 0))
    return {"title": post_title(author.get("uniqueId") if isinstance(author, dict) else author,
                                info.get("desc"), info.get("id")),
            "length": int(info.get("duration") or 0),
            "resolution": f"{side}p" if side else "?", "handle": with_scheme(url)}


def gallery_download(video, out, start, end, whole, on_progress):
    """Файл качает сам gallery-dl: ссылка TikTok работает только с его же куками."""
    with tempfile.TemporaryDirectory(prefix="gdl_") as tmp:
        r = gallery_run(["-D", tmp, "-f", "video.{extension}", "--no-mtime", video["handle"]], 600)
        raw = Path(tmp) / "video.mp4"
        if r.returncode != 0 or not raw.is_file():
            raise gallery_error(r)
        if on_progress:
            size = raw.stat().st_size
            on_progress(size, size)
        return cut_file(raw, out, start, end, whole)


# ---------- общий вход: способы пробуются по очереди ----------
ENGINES = {"pytubefix": pytube_info, "yt-dlp": ytdlp_info,
           "parth-dl": parth_info, "instaloader": instaloader_info,
           "eeinstagram": eeinstagram_info, "kkinstagram": kkinstagram_info,
           "gallery-dl": gallery_info, "fxtiktok": fxtiktok_info}
# какими способами и в каком порядке качать с каждого источника
ROUTES = {"youtube": ["pytubefix", "yt-dlp"],
          "instagram": ["yt-dlp", "parth-dl", "instaloader", "eeinstagram", "kkinstagram"],
          "tiktok": ["yt-dlp", "gallery-dl", "fxtiktok"],
          "other": ["yt-dlp"]}


def give_up(errors):
    if errors and all(isinstance(e, BotBlocked) for _, e in errors):
        return BotBlocked(BOT_MESSAGE)
    return DownloadError("ни один способ не сработал:\n"
                         + "\n".join(f"• {name}: {e}" for name, e in errors))


def get_video(url, skip=(), on_note=None):
    """Данные о видео от первого сработавшего способа:
    {"engine", "source", "title", "length", "resolution", "handle", "url"}."""
    errors = []
    source = source_of(url)
    if source == "instagram":
        # ссылку на профиль или сторис отсекаем сразу, не гоняя по всем способам
        url = insta_url(url)
    todo = [(name, ENGINES[name]) for name in ROUTES[source] if name not in skip]
    for i, (name, info) in enumerate(todo):
        try:
            video = info(url)
            video.update(engine=name, url=url, source=source)
            return video
        except DownloadError as e:
            errors.append((name, e))
        except Exception as e:      # чужая библиотека может упасть как угодно
            errors.append((name, DownloadError(f"{type(e).__name__}: {e}")))
        if on_note and i + 1 < len(todo):
            on_note(f"{name} не справился, пробую {todo[i + 1][0]}")
    raise give_up(errors)


def safe_name(title):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title)
    return re.sub(r"\s+", " ", name).strip()[:80] or "video"


def clean_file(path):
    """Пересобирает файл без перекодирования, выбрасывая метаданные источника
    и ставя свою дату создания. Картинка и звук не меняются."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    tmp = path.with_name(path.stem + ".clean" + path.suffix)
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-map", "0:v", "-map", "0:a?",
         "-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1",
         "-metadata", f"creation_time={stamp}", "-movflags", "+faststart", str(tmp)],
        capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise DownloadError("не удалось очистить метаданные:\n" + r.stderr.strip()[-300:])
    tmp.replace(path)
    return path


def out_file(video, start, end, whole):
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    name = safe_name(video["title"])
    if not whole:
        name += f" {fmt_clock(start).replace(':', '.')}-{fmt_clock(end).replace(':', '.')}"
    return DOWNLOAD_DIR / f"{name}.mp4"


def download(video, start, end, on_progress=None, on_note=None):
    """Скачивает отрезок [start, end] секунд (или всё видео) в downloads/. Возвращает путь.
    Если способ, которым получены данные, не смог скачать — пробует остальные."""
    whole = start <= 0 and (not video["length"] or end >= video["length"])
    errors, tried = [], []
    while True:
        tried.append(video["engine"])
        out = out_file(video, start, end, whole)
        try:
            if video["engine"] == "pytubefix":
                made = pytube_download(video, out, start, end, whole, on_progress)
            elif video["engine"] == "yt-dlp":
                made = ytdlp_download(video, out, start, end, whole, on_progress)
            elif video["engine"] == "gallery-dl":
                made = gallery_download(video, out, start, end, whole, on_progress)
            else:
                made = direct_download(video, out, start, end, whole, on_progress)
            return clean_file(made)
        except DownloadError as e:
            errors.append((video["engine"], e))
        except Exception as e:
            errors.append((video["engine"], DownloadError(f"{type(e).__name__}: {e}")))
        failed = video["engine"]
        try:
            video = get_video(video["url"], skip=tried, on_note=on_note)
        except DownloadError:
            raise give_up(errors) from None
        if on_note:
            on_note(f"{failed} не справился, качаю через {video['engine']}")


def pytube_download(video, out, start, end, whole, on_progress):
    stream, audio = video["handle"]
    with tempfile.TemporaryDirectory(prefix="ytdl_") as tmp, \
            ThreadPoolExecutor(max_workers=THREADS) as pool:
        plans = [plan_stream(stream, start, end), plan_stream(audio, start, end)]
        total = sum(p["last"] - p["first"] + 1 for p in plans)
        got = [0]

        def on_bytes(n):
            got[0] += n
            if on_progress:
                on_progress(got[0], total)

        if on_progress:
            on_progress(0, total)
        files = [Path(tmp) / "video.mp4", Path(tmp) / "audio.m4a"]
        for plan, path in zip(plans, files):
            download_plan(plan, path, on_bytes, pool)

        # склейка без перекодирования; время в скачанных кусках идёт с начала их первого фрагмента
        cmd = ["ffmpeg", "-v", "error", "-y"]
        for plan, path in zip(plans, files):
            if not whole:
                cmd += ["-ss", f"{max(0.0, start - plan['time0']):.3f}"]
            cmd += ["-i", str(path)]
        cmd += ["-map", "0:v", "-map", "1:a", "-c", "copy"]
        if not whole:
            cmd += ["-t", str(end - start)]
        cmd += ["-movflags", "+faststart", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise DownloadError("не удалось собрать файл:\n" + r.stderr.strip()[-300:])
    return out
