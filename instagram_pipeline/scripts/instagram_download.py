#!/usr/bin/env python3
"""Download public Instagram video references, or ingest a browser-saved video.

No passwords, cookies, browser profile access, generation or publication.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / 'reference-library' / 'instagram'
MAX_BYTES = 1024 ** 3


class DownloadError(Exception):
    pass


def canonicalize(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'instagram.com', 'www.instagram.com'}
            or parsed.username or parsed.password or parsed.port):
        raise DownloadError('Use an HTTPS Instagram post or Reel URL.')
    match = re.fullmatch(r'/(?:[A-Za-z0-9._]+/)?(?:reel|p|tv)/([A-Za-z0-9_-]{5,64})/?', parsed.path)
    if not match:
        raise DownloadError('A single post/Reel URL is required, not a profile or feed.')
    shortcode = match.group(1)
    return shortcode, f'https://www.instagram.com/p/{shortcode}/'


def binary(name):
    result = shutil.which(name)
    if not result:
        raise DownloadError(f'Missing dependency: {name}. See instagram_pipeline/README.md.')
    return result


def run(args, timeout=90):
    # Kill the whole downloader/ffmpeg group on timeout; no orphan downloads.
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            returncode_output = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise DownloadError('operation_timeout') from None
        return subprocess.CompletedProcess(args, process.returncode, *returncode_output)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def validate(path):
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise DownloadError('missing_empty_or_oversized_video')
    probe = run([binary('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file,pipe',
                 '-show_entries', 'format=duration:stream=codec_type,width,height,codec_name',
                 '-of', 'json', str(path)])
    if probe.returncode:
        raise DownloadError('invalid_media')
    try:
        data = json.loads(probe.stdout)
        video = next(s for s in data['streams'] if s['codec_type'] == 'video')
        duration = float(data['format']['duration'])
        if not math.isfinite(duration) or not 0 < duration <= 1800:
            raise ValueError()
        if video.get('width', 0) <= 0 or video.get('height', 0) <= 0:
            raise ValueError()
    except (ValueError, KeyError, StopIteration):
        raise DownloadError('no_valid_video_stream_or_duration') from None
    decode = run([binary('ffmpeg'), '-nostdin', '-v', 'error', '-xerror',
                  '-protocol_whitelist', 'file,pipe', '-i', str(path),
                  '-map', '0:v:0', '-map', '0:a?', '-f', 'null', '-'], timeout=180)
    if decode.returncode:
        raise DownloadError('video_decode_failed')
    return {'duration_seconds': duration, 'width': video['width'], 'height': video['height'],
            'has_audio': any(s['codec_type'] == 'audio' for s in data['streams']),
            'bytes': path.stat().st_size, 'sha256': digest(path), 'full_decode_passed': True}


def classify(message):
    text = message.lower()
    if any(x in text for x in ('429', 'too many requests', 'rate limit')):
        return 'rate_limited'
    if any(x in text for x in ('captcha', 'checkpoint', 'challenge')):
        return 'verification_required'
    if any(x in text for x in ("isn't available to everyone", 'login required', 'log in',
                                'private', 'certain audiences', '403')):
        return 'access_restricted'
    return 'download_failed'


def write_json(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    temp.replace(path)


@contextmanager
def locked(folder):
    import fcntl
    with (folder / '.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DownloadError('Another process is handling this Reel.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def acquire(url, library=LIBRARY, local=None, method='browser-export'):
    shortcode, canonical = canonicalize(url)
    folder = library.resolve() / shortcode
    folder.mkdir(parents=True, exist_ok=True)
    with locked(folder):
        manifest = folder / 'manifest.json'
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            media = folder / Path(saved['file']).name
            verified = validate(media)
            if verified['sha256'] != saved['media']['sha256']:
                raise DownloadError('Cached video changed; inspect it before continuing.')
            if local and digest(local.resolve()) != verified['sha256']:
                raise DownloadError('A different source is already archived; use a separate library.')
            return saved, media, True
        failure = folder / 'last-failure.json'
        now = datetime.now(timezone.utc)
        if local is None and failure.exists():
            previous = json.loads(failure.read_text())
            if now < datetime.fromisoformat(previous['retry_after']):
                raise DownloadError('Download cooldown active; import a saved file or wait until ' + previous['retry_after'])
        try:
            with tempfile.TemporaryDirectory(prefix='.incoming-', dir=folder) as temp:
                work = Path(temp)
                if local:
                    source = local.resolve()
                    # Validate before copying to avoid importing playlists/invalid files.
                    validate(source)
                    candidate = work / 'input.media'
                    shutil.copyfile(source, candidate)
                else:
                    method = 'yt-dlp-public'
                    result = run([binary('yt-dlp'), '--ignore-config', '--no-playlist',
                                  '--no-progress', '--no-cache-dir', '--no-write-comments',
                                  '--retries', '0', '--fragment-retries', '0', '--extractor-retries', '0',
                                  '--socket-timeout', '20', '--max-filesize', str(MAX_BYTES),
                                  '--format', 'best[ext=mp4]/best', '--restrict-filenames',
                                  '--output', str(work / 'source.%(ext)s'), canonical], timeout=180)
                    if result.returncode:
                        raise DownloadError(classify(result.stderr + result.stdout))
                    candidates = [p for p in work.iterdir() if p.suffix.lower() in {'.mp4', '.mov', '.webm', '.mkv'}]
                    if len(candidates) != 1:
                        raise DownloadError('Expected one complete video; no video or ambiguous carousel.')
                    candidate = candidates[0]
                validate(candidate)
                normalized = work / 'normalized.mp4'
                remux = run([binary('ffmpeg'), '-nostdin', '-v', 'error', '-xerror',
                             '-protocol_whitelist', 'file,pipe', '-i', str(candidate),
                             '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
                             '-map_metadata', '-1', '-movflags', '+faststart', str(normalized)])
                if remux.returncode:
                    raise DownloadError('mp4_remux_failed')
                info = validate(normalized)
                target = folder / 'source.mp4'
                normalized.replace(target)
                saved = {'schema_version': 1, 'shortcode': shortcode, 'source_url': canonical,
                         'acquired_at': now.isoformat(), 'method': method, 'file': 'source.mp4',
                         'media': info, 'status': 'ready_for_content_review',
                         'source_identity_verified': False,
                         'review_note': 'Technical validation only; compare to the original Reel before generation. No performance or identity approval implied.'}
                write_json(manifest, saved)
                return saved, target, False
        except DownloadError as error:
            # Never retain raw provider output, signed CDN URLs or session information.
            write_json(failure, {'attempted_at': now.isoformat(), 'method': method,
                                'reason': str(error), 'retry_after': (now + timedelta(hours=24)).isoformat()})
            raise


def attach(job_path, manifest, media):
    job = json.loads(job_path.read_text())
    if canonicalize(job.get('source_url', ''))[0] != manifest['shortcode']:
        raise DownloadError('Job source URL does not match this reference.')
    if job.get('generation_submitted') or job.get('source_file'):
        raise DownloadError('Job already submitted or has a source; refusing to overwrite.')
    job['source_file'] = str(media)
    job['source_media'] = manifest
    job['status'] = 'source_ready_requires_review'
    job['next_step'] = 'Review complete source motion/audio and Mollie fit; prepare settings and obtain any required spending approval.'
    write_json(job_path, job)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['get', 'import', 'doctor'])
    parser.add_argument('url', nargs='?')
    parser.add_argument('--file', type=Path)
    parser.add_argument('--method', choices=['browser-export', 'phone-download', 'screen-recording'], default='browser-export')
    parser.add_argument('--library', type=Path, default=LIBRARY)
    parser.add_argument('--job', type=Path, help='Attach to an unsubmitted job with the same source URL.')
    args = parser.parse_args()
    try:
        if args.command == 'doctor':
            for name in ('yt-dlp', 'ffprobe', 'ffmpeg'):
                path = binary(name)
                version = run([path, '--version' if name == 'yt-dlp' else '-version'])
                print(name + ': ' + version.stdout.splitlines()[0])
            return 0
        if not args.url or (args.command == 'import' and not args.file):
            parser.error('URL is required; import also requires --file.')
        if args.command == 'get' and args.file:
            parser.error('Use import with --file.')
        saved, media, cached = acquire(args.url, args.library, args.file, args.method)
        if args.job:
            attach(args.job, saved, media)
        print(json.dumps({'status': saved['status'], 'file': str(media), 'cached': cached,
                          'media': saved['media']}, indent=2))
        return 0
    except (DownloadError, OSError, ValueError) as error:
        print(json.dumps({'status': 'blocked', 'reason': str(error),
                          'next_step': 'Use a supported signed-in browser download, then import the saved file. No automatic access workaround.'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
