"""Bounded public HTTPS import and Twitch-only recording."""
import csv
import http.client
import ipaddress
import importlib.util
import math
import re
import socket
import subprocess
import sys
from contextlib import ExitStack
import time
import threading
from urllib.parse import urlsplit, urljoin, urlunsplit, parse_qs


def link_parts(url):
    if not url.isascii() or re.search(r'[\x00-\x20\x7f\\]', url):
        raise ValueError('Use an encoded public HTTPS video URL.')
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username is not None or p.password is not None or p.port not in (None, 443):
        raise ValueError('Use a public HTTPS video URL on port 443.')
    if not re.fullmatch(r'[a-z0-9.-]+', p.hostname) or '.' not in p.hostname:
        raise ValueError('Use a public DNS hostname.')
    return p


def twitch_channel(url):
    p = link_parts(url)
    if p.hostname not in ('twitch.tv', 'www.twitch.tv') or not re.fullmatch(r'/[A-Za-z0-9_]{2,25}/?', p.path):
        raise ValueError('Use a Twitch channel link, not a VOD or clip link.')
    return p.path.strip('/').lower()


YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'}


def youtube_id(url):
    p = link_parts(url)
    if p.hostname == 'youtu.be':
        value = p.path.strip('/')
    elif p.hostname in YOUTUBE_HOSTS and p.path == '/watch':
        values = parse_qs(p.query).get('v', [])
        value = values[0] if len(values) == 1 else ''
    elif p.hostname in YOUTUBE_HOSTS and re.fullmatch(r'/(?:live|shorts)/[A-Za-z0-9_-]{11}/?', p.path):
        value = p.path.rstrip('/').rsplit('/', 1)[1]
    else:
        value = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        raise ValueError('Use a single YouTube video or broadcast link, not a channel or playlist.')
    return value


def youtube_command(video_id, live=False):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Invalid YouTube video ID.')
    if importlib.util.find_spec('yt_dlp') is None:
        raise ValueError('YouTube import requires yt-dlp. Install requirements-youtube.txt in the app virtual environment.')
    return [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
            '--use-extractors', 'youtube', '--no-remote-components', '--no-progress',
            '--socket-timeout', '15', '--retries', '2', '--fragment-retries', '2',
            '--abort-on-unavailable-fragments', '--downloader', 'native',
            '--match-filter', 'is_live' if live else '!is_live & !is_upcoming & duration <= 3600',
            '-f', ('best[protocol^=m3u8][height<=720][vcodec^=avc1][acodec^=mp4a]'
                   if live else 'best[protocol=https][ext=mp4][height<=720][vcodec^=avc1][acodec^=mp4a]')]


def youtube_url(video_id):
    return 'https://www.youtube.com/watch?v=' + video_id


def youtube_live_command(video_id):
    return [*youtube_command(video_id, live=True), '--no-live-from-start', '--hls-use-mpegts',
            '-o', '-', '--', youtube_url(video_id)]


def download_youtube(video_id, out, limit, stop):
    cmd = [*youtube_command(video_id), '--no-part', '--max-filesize', str(limit),
           '-o', str(out), '--', youtube_url(video_id)]
    deadline = time.monotonic() + 600
    with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL) as process:
        try:
            while process.poll() is None:
                if stop.is_set():
                    raise ValueError('YouTube import interrupted. Submit the link again.')
                if time.monotonic() >= deadline:
                    raise ValueError('YouTube import reached the 10-minute download limit.')
                if out.exists() and out.stat().st_size > limit:
                    raise ValueError('The YouTube source exceeds MAX_UPLOAD_MB.')
                stop.wait(.2)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    if out.exists() and out.stat().st_size > limit:
        raise ValueError('The YouTube source exceeds MAX_UPLOAD_MB.')
    if process.returncode or not out.is_file() or not out.stat().st_size:
        raise ValueError('Could not import this YouTube video. Use an authorized accessible video or finalized replay up to 60 minutes and within MAX_UPLOAD_MB. Check yt-dlp and Deno, or upload an MP4.')


def public_ip(host):
    answers = [entry[4][0] for entry in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)]
    if not answers or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in answers):
        raise ValueError('Private or reserved network addresses are not allowed.')
    return answers[0]


def download(url, out, limit):
    deadline = time.monotonic() + 600
    for _ in range(5):
        p = link_parts(url)
        ip = public_ip(p.hostname)
        with_https = http.client.HTTPSConnection(p.hostname, 443, timeout=20)
        # Pin the validated DNS result while preserving TLS hostname verification.
        with_https._create_connection = lambda addr, timeout, source: socket.create_connection((ip, 443), timeout, source)
        try:
            with_https.request('GET', urlunsplit(('', '', p.path or '/', p.query, '')), headers={'Accept-Encoding': 'identity'})
            r = with_https.getresponse()
            if r.status in (301, 302, 303, 307, 308):
                target = r.getheader('Location')
                if not target:
                    raise ValueError('Video link redirect has no destination.')
                url = urljoin(url, target)
                continue
            if r.status != 200:
                raise ValueError(f'Video host returned HTTP {r.status}. Check the link and its expiration.')
            mime = r.getheader('Content-Type', '').split(';')[0].lower()
            if mime not in ('video/mp4', 'video/quicktime', 'application/octet-stream', 'binary/octet-stream', ''):
                raise ValueError('The link did not return an MP4 or MOV file. Watch pages and playlists are not supported.')
            expected = r.getheader('Content-Length')
            if expected is not None and int(expected) > limit:
                raise ValueError('The linked file exceeds MAX_UPLOAD_MB.')
            size = 0
            with out.open('wb') as f:
                while chunk := r.read1(256 * 1024):
                    size += len(chunk)
                    if size > limit or time.monotonic() > deadline:
                        raise ValueError('Video import exceeded the file size or 10-minute download limit.')
                    f.write(chunk)
            if not size or (expected is not None and size != int(expected)):
                raise ValueError('The linked file is empty or incomplete.')
            return
        finally:
            with_https.close()
    raise ValueError('Video link has too many redirects.')


LIVE_WINDOW_SECONDS = 120


def twitch_command(channel, config):
    if not re.fullmatch(r'[a-z0-9_]{2,25}', channel):
        raise ValueError('Invalid Twitch channel.')
    return [sys.executable, '-m', 'streamlink', '--config', str(config),
            '--retry-streams', '10', '--retry-max', '30', '--stream-timeout', '30',
            '--loglevel', 'error', '--stdout', 'https://www.twitch.tv/' + channel,
            '720p,480p,best']


LIVE_RECONNECT_SECONDS = 90
LIVE_ATTEMPT_STALL = 35


def record(channel, minutes, folder, limit, stop, on_window, on_progress=None, *, target_seconds=None, fixed=False, run_id=None, provider="twitch"):
    """Record across transient disconnects; emit closed windows and live mux time."""
    target = minutes * 60 if target_seconds is None else target_seconds
    config = folder / 'empty.conf'
    config.touch()
    producer = youtube_live_command(channel) if provider == "youtube" else twitch_command(channel, config)
    total = 0.0
    moved = 0
    number = 0
    attempt = 0
    first_deadline = time.monotonic() + 360
    hard_deadline = None
    retry_deadline = None
    last_reason = 'No usable stream was received.'

    def terminate(child):
        if child.poll() is None:
            child.kill()
        child.wait()

    while not stop.is_set():
        attempt += 1
        work = folder / (f'{run_id}-connection-{attempt:04d}' if run_id else f'connection-{attempt:04d}')
        work.mkdir()
        manifest = work / 'windows.csv'
        base = total
        cursor = 0
        mux_seconds = 0.0
        last_report = None
        exceeded = False
        cmd = ['ffmpeg', '-nostdin', '-nostats', '-loglevel', 'error', '-n',
               '-progress', 'pipe:1', '-stats_period', '1',
               '-protocol_whitelist', 'pipe', '-format_whitelist', 'mov,mpegts',
               '-fflags', '+genpts', '-i', 'pipe:0', '-t', str(target - base),
               '-map', '0:v:0', '-map', '0:a:0',
               *(['-vf', 'scale=trunc(iw*min(1\\,720/ih)/2)*2:trunc(ih*min(1\\,720/ih)/2)*2,fps=25',
                  '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '24', '-threads', '2',
                  '-pix_fmt', 'yuv420p', '-force_key_frames', 'expr:gte(t,n_forced*45)',
                  '-c:a', 'aac', '-b:a', '96k'] if fixed else ['-c', 'copy']),
               '-f', 'segment', '-segment_format', 'mpegts', '-reset_timestamps', '1',
               '-segment_time', str(45 if fixed else LIVE_WINDOW_SECONDS), '-segment_list_type', 'csv',
               '-segment_list', manifest.name, 'window-%05d.ts']

        def enqueue(final):
            nonlocal moved, number, cursor, total, exceeded
            if not manifest.is_file():
                return
            with manifest.open(encoding='ascii', newline='') as f:
                entries = list(csv.reader(f))
            for entry in entries[cursor:]:
                name = f'window-{cursor:05d}.ts'
                if len(entry) != 3 or entry[0] != name:
                    return
                if not final and not (work / f'window-{cursor+1:05d}.ts').exists():
                    return
                try:
                    start, end = map(float, entry[1:])
                except ValueError:
                    return
                if not math.isfinite(start) or not math.isfinite(end) or end <= start:
                    cursor += 1
                    continue
                path = work / name
                size = path.stat().st_size
                if size and moved + size > limit:
                    exceeded = True
                    return
                if size:
                    saved = min(float(target), total + end - start)
                    on_window(path, number + 1, saved)
                    number += 1
                    moved += size
                    total = saved
                cursor += 1

        def read_progress(pipe):
            nonlocal mux_seconds
            for line in pipe:
                if line.startswith('out_time_us='):
                    try:
                        value = int(line.partition('=')[2]) / 1_000_000
                    except ValueError:
                        continue
                    mux_seconds = max(mux_seconds, value)

        def report(state, seconds):
            nonlocal last_report
            item = (state, int(seconds))
            if on_progress is not None and item != last_report:
                on_progress(state, seconds)
                last_report = item

        reason = None
        last_size = 0
        last_activity = time.monotonic()
        with ExitStack() as stack:
            stream = subprocess.Popen(producer, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            stack.callback(terminate, stream)
            stack.callback(stream.stdout.close)
            try:
                mux = subprocess.Popen(cmd, cwd=work, stdin=stream.stdout, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, encoding='ascii', errors='replace')
            except FileNotFoundError as exc:
                raise ValueError('FFmpeg was not found. Add its bin folder to PATH and restart.') from exc
            stream.stdout.close()
            reader = threading.Thread(target=read_progress, args=(mux.stdout,), daemon=True)
            reader.start()
            stack.callback(mux.stdout.close)
            stack.callback(reader.join)
            stack.callback(terminate, mux)
            while True:
                now = time.monotonic()
                finished = mux.poll() is not None
                size = moved + sum(p.stat().st_size for p in work.glob('window-*.ts'))
                enqueue(finished)
                if size != last_size:
                    last_size = size
                    last_activity = now
                has_media = size > 0 and (size > moved or total > base)
                if has_media and hard_deadline is None:
                    hard_deadline = now + target + 240
                active = min(float(target), max(total, base + mux_seconds))
                report('recording' if has_media else ('reconnecting' if attempt > 1 else 'connecting'), active)
                if exceeded or size > limit:
                    reason = 'Reached MAX_LIVE_MB. Increase it only if you have sufficient disk space.'
                elif stop.is_set():
                    reason = getattr(stop, 'reason', 'Capture interrupted by app shutdown.')
                elif hard_deadline is not None and now >= hard_deadline:
                    reason = 'Capture reached its wall-clock safety limit.'
                if reason or finished:
                    break
                if (hard_deadline is None and now >= first_deadline or
                    retry_deadline is not None and not has_media and now >= retry_deadline or
                    (has_media or attempt > 1) and now - last_activity >= LIVE_ATTEMPT_STALL):
                    # Close input first; let FFmpeg write the final window's trailer.
                    terminate(stream)
                    try:
                        mux.wait(timeout=8)
                        enqueue(True)
                    except subprocess.TimeoutExpired:
                        pass
                    break
                stop.wait(.5)
            if reason and mux.poll() is None:
                terminate(stream)
                try:
                    mux.wait(timeout=5)
                    enqueue(True)
                except subprocess.TimeoutExpired:
                    pass
            last_reason = (f'FFmpeg exited with code {mux.returncode}.' if mux.returncode not in (None, 0)
                           else ('YouTube' if provider == 'youtube' else 'Twitch') + ' stopped supplying usable media.')
        if reason or exceeded:
            last_reason = reason or 'Reached MAX_LIVE_MB.'
            break
        if total >= target - 1:
            if on_progress is not None:
                on_progress('recording', total)
            return total, None
        now = time.monotonic()
        if total > base:
            retry_deadline = now + LIVE_RECONNECT_SECONDS
        elif retry_deadline is None:
            retry_deadline = first_deadline if not total else now + LIVE_RECONNECT_SECONDS
        if now >= retry_deadline:
            break
        report('reconnecting', total)
        stop.wait(min(3, max(0, retry_deadline - now)))
    if stop.is_set():
        last_reason = getattr(stop, 'reason', 'Capture interrupted by app shutdown.')
    if total:
        return total, f'{last_reason} Saved windows are kept.'
    raise ValueError(f'{last_reason} No playable live recording was saved.')
