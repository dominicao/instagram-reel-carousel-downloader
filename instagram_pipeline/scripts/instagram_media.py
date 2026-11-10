#!/usr/bin/env python3
"""Validated single-post Reel/carousel acquisition; run in .instagram-download-venv."""
import argparse
import contextlib
from datetime import datetime, timezone, timedelta
import http.cookiejar
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import urlsplit
import instagram_download as base

ROOT = base.ROOT
LIBRARY = ROOT / 'reference-library' / 'instagram-media'
MAX_ITEMS = 20
MAX_TOTAL = 1024 ** 3


class QuietLogger:
    def __init__(self):
        self.reason = None
    def debug(self, message, *args, **kwargs):
        pass
    info = debug
    def warning(self, message, *args, **kwargs):
        reason = classify(str(message))
        if self.reason != "browser_session_locked":
            self.reason = reason
    error = warning


def classify(message):
    s = message.lower()
    if any(x in s for x in ('find-generic-password', 'cannot decrypt', 'no key found', 'keyring', 'keychain')):
        return 'browser_session_locked'
    return base.classify(message)


def chrome_session(profile=None):
    """Read explicitly authorized local Chrome session; never write a cookie file."""
    from yt_dlp.cookies import extract_cookies_from_browser, YoutubeDLCookieJar
    log = QuietLogger()
    try:
        original = extract_cookies_from_browser('chrome', profile=profile, logger=log)
    except Exception as error:
        raise base.DownloadError('browser_session_unavailable_' + type(error).__name__) from None
    jar = YoutubeDLCookieJar()
    for cookie in original:
        domain = cookie.domain.lstrip('.')
        if (domain == 'instagram.com' or domain.endswith('.instagram.com')) and not cookie.is_expired():
            jar.set_cookie(cookie)
    if not any(c.name == 'sessionid' and c.value for c in jar):
        raise base.DownloadError(log.reason if log.reason == 'browser_session_locked' else 'instagram_session_missing')
    return jar


def safe_metadata(data):
    result = {}
    for key in ('caption', 'description', 'date', 'timestamp', 'username', 'shortcode', 'post_shortcode', 'post_url'):
        value = data.get(key)
        if key == 'date' and hasattr(value, 'isoformat'):
            value = value.isoformat()
        if isinstance(value, (str, int, float)):
            result[key] = value
    return result


def fetch_reel(url, work, cookies):
    from yt_dlp import YoutubeDL
    log = QuietLogger()
    options = {'logger': log, 'quiet': True, 'no_warnings': True, 'noplaylist': True,
               'cachedir': False, 'retries': 0, 'fragment_retries': 0, 'extractor_retries': 0,
               'socket_timeout': 20, 'max_filesize': base.MAX_BYTES,
               'format': 'best[ext=mp4]/best', 'outtmpl': str(work/'input.%(ext)s')}
    try:
        with YoutubeDL(options) as ydl:
            if cookies is not None:
                ydl.cookiejar = cookies
            info = ydl.extract_info(url, download=True)
    except Exception as error:
        raise base.DownloadError(log.reason or classify(str(error))) from None
    if not info or info.get('_type') in ('playlist', 'multi_video'):
        raise base.DownloadError('not_a_single_reel_use_carousel_mode')
    if str(info.get('id')) != base.canonicalize(url)[0]:
        raise base.DownloadError('source_shortcode_mismatch')
    candidates = list(work.glob('input.*'))
    if len(candidates) != 1 or candidates[0].suffix not in {'.mp4', '.webm', '.mkv', '.mov'}:
        raise base.DownloadError('incomplete_reel_download')
    return [(1, candidates[0], 'video')], 1, safe_metadata(info)


def cdn_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise base.DownloadError('invalid_media_destination')
    if not any(host == d or host.endswith('.'+d) for d in ('cdninstagram.com', 'fbcdn.net')):
        raise base.DownloadError('unexpected_media_destination')
    return url


def download_asset(session, url, path, total):
    for _ in range(5):
        response = session.get(cdn_url(url), stream=True, timeout=(20, 30), allow_redirects=False)
        with response:
            if response.status_code in (301, 302, 303, 307, 308):
                from urllib.parse import urljoin
                url = urljoin(url, response.headers.get('Location', ''))
                continue
            if response.status_code != 200:
                raise base.DownloadError(classify(str(response.status_code)))
            with path.open('xb') as output:
                size = 0
                for chunk in response.iter_content(1024*1024):
                    size += len(chunk)
                    if size > base.MAX_BYTES or total + size > MAX_TOTAL:
                        raise base.DownloadError('download_size_limit')
                    output.write(chunk)
            return size
    raise base.DownloadError('media_redirect_limit')


def fetch_carousel(url, work, cookies):
    from gallery_dl import config, extractor
    from gallery_dl.extractor.message import Message
    import requests
    config.clear()  # Never load personal configs, passwords or cookie files.
    for key, value in {'retries': 0, 'timeout': 20, 'sleep-request': 0,
                       'videos': 'merged', 'audio': False, 'previews': False,
                       'max-posts': 1}.items():
        config.set(('extractor',), key, value)
    config.set(('extractor', 'instagram'), 'cookies', {c.name: c.value for c in cookies} if cookies is not None else {})
    shortcode = base.canonicalize(url)[0]
    ext = extractor.find(url)
    if ext is None or ext.category != 'instagram' or ext.subcategory != 'post':
        raise base.DownloadError('not_a_single_instagram_post')
    parse_post = ext._parse_post
    def checked_parse(post):
        expected = len(post.get('carousel_media') or [post])
        parsed = parse_post(post)
        if len(parsed['_files']) != expected:
            raise base.DownloadError('incomplete_carousel_metadata')
        return parsed
    ext._parse_post = checked_parse
    assets, count, metadata, total = [], None, {}, 0
    previous_log = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with requests.Session() as session:
            for message, media_url, data in ext:
                if message == Message.Directory:
                    if count is not None:
                        raise base.DownloadError('multiple_posts_rejected')
                    if str(data.get('post_shortcode')) != shortcode:
                        raise base.DownloadError('source_shortcode_mismatch')
                    count = int(data['count'])
                    if not 1 <= count <= MAX_ITEMS:
                        raise base.DownloadError('unsupported_carousel_item_count')
                    metadata = safe_metadata(data)
                elif message == Message.Url:
                    index = int(data['num'])
                    if count is None or not 1 <= index <= count or index in [a[0] for a in assets]:
                        raise base.DownloadError('invalid_carousel_order')
                    extension = str(data['extension']).lower()
                    kind = 'video' if data.get('video_url') else 'image'
                    if extension not in {'jpg', 'jpeg', 'png', 'webp', 'mp4', 'mov', 'webm'}:
                        raise base.DownloadError('unsupported_asset_type')
                    path = work / f'{index:03d}.{extension}'
                    total += download_asset(session, media_url, path, total)
                    assets.append((index, path, kind))
                else:
                    raise base.DownloadError('unexpected_extractor_message')
    except base.DownloadError:
        raise
    except Exception as error:
        raise base.DownloadError(classify(str(error))) from None
    finally:
        logging.disable(previous_log)
        config.clear()
    if count is None or len(assets) != count or sorted(a[0] for a in assets) != list(range(1, count+1)):
        raise base.DownloadError('incomplete_carousel')
    return sorted(assets), count, metadata


def validate_asset(path, kind):
    if kind == 'video':
        return base.validate(path)
    from PIL import Image
    if not path.is_file() or not 0 < path.stat().st_size <= base.MAX_BYTES:
        raise base.DownloadError('invalid_image_size')
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            im.load()
            width, height = im.size
            if not width or not height:
                raise ValueError()
    except Exception:
        raise base.DownloadError('image_decode_failed') from None
    return {'width': width, 'height': height, 'bytes': path.stat().st_size,
            'sha256': base.digest(path), 'full_decode_passed': True}


def verify_manifest(folder, saved):
    assets = saved['assets']
    if len(assets) != saved['expected_count'] or [a['index'] for a in assets] != list(range(1,len(assets)+1)):
        raise base.DownloadError('cached_manifest_incomplete')
    for asset in assets:
        if Path(asset['file']).name != asset['file']:
            raise base.DownloadError('invalid_cached_path')
        current = validate_asset(folder/asset['file'], asset['kind'])
        if current['sha256'] != asset['media']['sha256']:
            raise base.DownloadError('cached_asset_changed')


def acquire(url, kind, library=LIBRARY, browser=None, profile=None):
    shortcode, canonical = base.canonicalize(url)
    folder = library.resolve()/shortcode
    folder.mkdir(parents=True, exist_ok=True)
    with base.locked(folder):
        manifest = folder/'manifest.json'
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            if saved['requested_kind'] != kind:
                raise base.DownloadError('cached_format_mismatch')
            verify_manifest(folder, saved)
            return saved, True
        failure = folder/'last-failure.json'
        now = datetime.now(timezone.utc)
        if failure.exists():
            old = json.loads(failure.read_text())
            # Session repair can be tested immediately; challenges/rate limits cannot.
            if old.get('retry_after') and now < datetime.fromisoformat(old['retry_after']):
                raise base.DownloadError('download_cooldown_active')
        try:
            cookies = chrome_session(profile) if browser == 'chrome' else None
            with tempfile.TemporaryDirectory(prefix='.incoming-', dir=folder) as tmp:
                work = Path(tmp)
                fetch = fetch_reel if kind == 'reel' else fetch_carousel
                found, count, metadata = fetch(canonical, work, cookies)
                if len(found) != count or [i[0] for i in found] != list(range(1,count+1)):
                    raise base.DownloadError('incomplete_download')
                assets = []
                for index, path, asset_kind in found:
                    assets.append({'index': index, 'file': f'{index:03d}{path.suffix.lower()}',
                                   'kind': asset_kind, 'media': validate_asset(path, asset_kind)})
                saved = {'schema_version': 2, 'shortcode': shortcode, 'source_url': canonical,
                         'requested_kind': kind, 'acquired_at': now.isoformat(),
                         'method': ('yt-dlp' if kind == 'reel' else 'gallery-dl') + ('-chrome' if browser else '-public'),
                         'expected_count': count, 'collected_count': len(assets), 'assets': assets,
                         'metadata': metadata, 'status': 'ready_for_content_review',
                         'source_identity_verified': False,
                         'completeness': 'All extractor-reported items validated; original post still requires visual comparison.'}
                for (_, path, _), asset in zip(found, assets):
                    path.replace(folder/asset['file'])
                base.write_json(manifest, saved)
                failure.unlink(missing_ok=True)
                return saved, False
        except Exception as error:
            reason = str(error) if isinstance(error, base.DownloadError) else 'internal_error'
            retry_after = None if (reason.startswith('browser_session_unavailable') or reason in ('browser_session_locked', 'instagram_session_missing')) else (now + timedelta(hours=24)).isoformat()
            base.write_json(failure, {'attempted_at': now.isoformat(), 'reason': reason, 'retry_after': retry_after})
            raise base.DownloadError(reason) from None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['fetch', 'doctor'])
    p.add_argument('url', nargs='?')
    p.add_argument('--kind', choices=['reel', 'carousel'])
    p.add_argument('--browser', choices=['chrome'], help='Explicitly authorized local Chrome session only.')
    p.add_argument('--profile', help='Optional authorized Chrome profile name.')
    p.add_argument('--library', type=Path, default=LIBRARY)
    a = p.parse_args()
    try:
        if a.command == 'doctor':
            from importlib.metadata import version
            result = {n: version(n) for n in ('yt-dlp', 'gallery-dl', 'Pillow')}
            for n in ('ffmpeg', 'ffprobe'):
                result[n] = base.binary(n)
            if a.browser:
                chrome_session(a.profile)
                result['chrome_instagram_session'] = 'available_locally_not_yet_verified_by_instagram'
            print(json.dumps(result, indent=2))
            return 0
        if not a.url or not a.kind:
            p.error('fetch requires a single Instagram URL and --kind reel or carousel')
        saved, cached = acquire(a.url, a.kind, a.library, a.browser, a.profile)
        folder = a.library.resolve()/saved['shortcode']
        print(json.dumps({'status': saved['status'], 'cached': cached, 'manifest': str(folder/'manifest.json'),
                          'files': [str(folder/x['file']) for x in saved['assets']]}, indent=2))
        return 0
    except (base.DownloadError, OSError, ValueError) as error:
        reason = str(error) if isinstance(error, base.DownloadError) else 'local_setup_error'
        print(json.dumps({'status': 'blocked', 'reason': reason}))
        return 2


if __name__ == '__main__':
    # Bound the entire worker, including a pending macOS Keychain child process.
    # stdout contains sanitized results only, never cookies/provider responses.
    if '--worker' in sys.argv:
        sys.argv.remove('--worker')
        raise SystemExit(main())
    try:
        completed = base.run([sys.executable, __file__, '--worker', *sys.argv[1:]],
                             timeout=90 if 'doctor' in sys.argv else 300)
        if completed.stdout:
            print(completed.stdout, end='')
        if completed.returncode and not completed.stdout:
            print(json.dumps({'status': 'blocked', 'reason': 'worker_failed'}))
        raise SystemExit(completed.returncode)
    except base.DownloadError:
        print(json.dumps({'status': 'blocked', 'reason': 'operation_timeout'}))
        raise SystemExit(2)
