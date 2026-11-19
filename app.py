import json, math, os, re, secrets, subprocess, tempfile, threading, unicodedata, uuid, zipfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from shutil import copyfileobj
from urllib.parse import quote
from bisect import bisect_right
from typing import Literal
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import logging
import sqlite3, time
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile, Request, HTTPException, Depends
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

import sources, story
from cloud_storage import SupabaseStorage, StorageError, ObjectMissing

load_dotenv()
CLOUD_URL = os.getenv('SUPABASE_URL', '').strip()
CLOUD_KEY = (os.getenv('SUPABASE_SECRET_KEY') or os.getenv('SUPABASE_SERVICE_ROLE_KEY') or '').strip()
if bool(CLOUD_URL) != bool(CLOUD_KEY):
    raise RuntimeError('Configure both SUPABASE_URL and SUPABASE_SECRET_KEY, or leave both blank.')
STORAGE = SupabaseStorage(CLOUD_URL, CLOUD_KEY, os.getenv('SUPABASE_BUCKET') or 'recordings') if CLOUD_URL else None
DATA = Path(os.getenv('DATA_DIR', 'shorts-data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
PASSWORD = os.getenv('APP_PASSWORD', '')
BASE = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
WEBHOOK = os.getenv('MAKE_WEBHOOK_URL', '')
MAKE_KEY = os.getenv('MAKE_WEBHOOK_API_KEY', '')
CALLBACK = os.getenv('MAKE_CALLBACK_SECRET', '')
FILE_LIMIT = int(os.getenv('MAKE_MAX_FILE_BYTES', '5000000'))
SCHEDULE_TZ = ZoneInfo(os.getenv('SCHEDULE_TIMEZONE') or 'America/New_York')
SLOTS = (9, 10, 16)
SCHEDULE_PRIVACY = (os.getenv('SCHEDULE_PRIVACY') or 'private').lower()
if SCHEDULE_PRIVACY not in ('private', 'unlisted', 'public'):
    raise RuntimeError('SCHEDULE_PRIVACY must be private, unlisted or public.')
LOG = logging.getLogger('uvicorn.error')
LIVE_LIMIT = int(os.getenv('MAX_LIVE_MB', '5000')) * 1000000
UPLOAD_LIMIT = int(os.getenv('MAX_UPLOAD_MB', '500')) * 1000000
if BASE and (not BASE.startswith('https://') or len(PASSWORD) < 16 or len(CALLBACK) < 24):
    raise RuntimeError('Public mode requires HTTPS, APP_PASSWORD (16+ chars) and MAKE_CALLBACK_SECRET (24+ chars).')

@contextmanager
def db():
    c = sqlite3.connect(DATA / 'jobs.sqlite3', timeout=15)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()

with db() as c:
    c.executescript('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, name TEXT, state TEXT, error TEXT, created REAL);
    CREATE TABLE IF NOT EXISTS clips(id TEXT PRIMARY KEY, job TEXT, title TEXT, start REAL, end REAL, state TEXT, video_id TEXT, token TEXT, expires REAL);''')
    c.execute('CREATE TABLE IF NOT EXISTS capture_events (job TEXT, event TEXT, at REAL, seconds REAL)')
    c.execute('CREATE TABLE IF NOT EXISTS schedule_slots (slot TEXT PRIMARY KEY, clip_id TEXT NOT NULL)')
    if 'mode' not in {r['name'] for r in c.execute('PRAGMA table_info(jobs)')}:
        c.execute("ALTER TABLE jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'highlights'")
    fields = {r['name'] for r in c.execute('PRAGMA table_info(jobs)')}
    for col, kind in [('source_kind', "TEXT NOT NULL DEFAULT 'file'"), ('source_ref', 'TEXT'), ('capture_minutes','INTEGER'), ('duration','REAL'),
                      ('parent_id','TEXT'), ('window_no','INTEGER'), ('recorded_seconds','REAL'), ('live_seconds','REAL'), ('capture_phase','TEXT'), ('channel','TEXT'), ('origin_id','TEXT'), ('checkpoint_start','REAL'),
                      ('window_start','REAL'), ('visible_from','INTEGER'), ('stop_at','REAL')]:
        if col not in fields:
            c.execute(f'ALTER TABLE jobs ADD COLUMN {col} {kind}')
    for row in c.execute("SELECT id,name FROM jobs WHERE source_kind='live' AND parent_id IS NULL AND channel IS NULL").fetchall():
        slug = re.fullmatch(r'Twitch capture · ([a-z0-9_]{2,25})', row['name'] or '')
        if slug:
            c.execute('UPDATE jobs SET channel=? WHERE id=?', (slug[1],row['id']))
    existing = {r['name'] for r in c.execute('PRAGMA table_info(clips)')}
    for col, kind in [('queued_at','REAL'), ('description','TEXT'), ('slot','TEXT'), ('storage_key','TEXT'), ('storage_size','INTEGER'), ('cloud_cleanup','TEXT'), ('upload_at','REAL'), ('upload_privacy','TEXT')]:
        if col not in existing:
            c.execute(f'ALTER TABLE clips ADD COLUMN {col} {kind}')


def get(table, key):
    with db() as c:
        row = c.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Not found')
    return dict(row)

def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=3600)
    if result.returncode:
        raise RuntimeError('Media processing failed: ' + result.stderr[-550:])
    return result.stdout

whisper_lock = threading.Lock()

def transcribe(audio):
    with whisper_lock:
        return _transcribe(audio)

def _transcribe(audio):
    from faster_whisper import WhisperModel
    global whisper
    if whisper is None:
        whisper = WhisperModel(os.getenv('WHISPER_MODEL', 'base'), device='cpu', compute_type='int8')
    segments, _ = whisper.transcribe(str(audio), word_timestamps=True, vad_filter=True)
    return [{'start': s.start, 'end': s.end, 'text': s.text.strip(),
             'words': [(w.start, w.end, w.word.strip()) for w in (s.words or []) if w.start is not None and w.end is not None]}
            for s in segments if s.text.strip()]

whisper = None

def select_clips(segments):
    # Use word timestamps to recover more cut points from long Whisper segments.
    units = []
    for s in segments:
        words = s['words']
        if s['end'] - s['start'] <= 10 or not words:
            units.append(s)
            continue
        group = []
        for index, w in enumerate(words):
            group.append(w)
            if w[1] - group[0][0] >= 5 or index == len(words)-1:
                units.append({'start': group[0][0], 'end': group[-1][1],
                              'text': ' '.join(x[2] for x in group), 'words': group})
                group = []
    segments = sorted(units, key=lambda s: s['start'])
    # Rank non-overlapping transcript windows; timestamps come from Whisper.
    candidates = []
    for i, first in enumerate(segments):
        for j in range(i, min(i + 24, len(segments))):
            last = segments[j]
            duration = last['end'] - first['start']
            if duration > 55:
                continue
            if duration < 20:
                continue
            part = segments[i:j+1]
            words = re.findall(r"\b\w+\b", ' '.join(s['text'] for s in part).lower())
            hook = bool(re.search(r'\b(how|why|mistake|first|imagine|secret|important|never|because)\b', first['text'], re.I))
            score = 3*hook + 2*last['text'].endswith(('.', '?', '!')) + min(len(set(words))/25, 4) - abs(duration-34)/16
            candidates.append((score, first['start'], last['end'], part))
    # Find the maximum feasible number first; rank sets of equal size by quality.
    candidates.sort(key=lambda item: item[2])
    ends = [item[2] for item in candidates]
    dp = [[(0.0, ())] + [None]*5]
    for i, (score, start, end, part) in enumerate(candidates):
        prev = dp[bisect_right(ends, start, 0, i)]
        row = dp[-1].copy()
        for count in range(1, 6):
            if prev[count-1] is None:
                continue
            subtotal, indexes = prev[count-1]
            if row[count] is None or subtotal + score > row[count][0]:
                row[count] = (subtotal + score, indexes + (i,))
        dp.append(row)
    _, chosen = next(entry for entry in reversed(dp[-1]) if entry is not None)
    return [(candidates[i][1], candidates[i][2], candidates[i][3]) for i in chosen]


def split_clips(duration):
    # Sample distinct portions across the recording; no speech or captions needed.
    count = min(5, int(duration // 20))
    if count < 1:
        return []
    length = min(35.0, duration / count)
    if count == 1:
        return [(0.0, length, [])]
    gap = max(0.0, (duration - count*length) / (count-1))
    return [(i*(length+gap), min(duration, i*(length+gap)+length), [])
            for i in range(count)]

def stamp(t):
    ms = max(0, round(t * 1000)); seconds, ms = divmod(ms, 1000); minutes, seconds = divmod(seconds, 60); hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02},{ms:03}'

def input_flags(kind):
    # Do not let untrusted uploads trigger nested network protocols or playlists.
    if kind == 'live':
        # Streamlink can return MPEG-TS or fragmented MP4.
        return ['-protocol_whitelist', 'file', '-format_whitelist', 'mov,mpegts']
    return ['-protocol_whitelist', 'file', '-f', 'mov']


def render(source, out, start, end, part, kind='file'):
    captions = out.with_suffix('.srt')
    cues = []
    for s in part:
        words = [w for w in s['words'] if w[1] > start and w[0] < end]
        if s['words'] and not words:
            continue
        groups = [words[i:i+5] for i in range(0,len(words),5)] if words else [[(s['start'],s['end'],s['text'])]]
        for g in groups:
            a, b = max(0, g[0][0]-start), min(end-start,g[-1][1]-start)
            if b > a:
                cues.append(f'{len(cues)+1}\n{stamp(a)} --> {stamp(b)}\n' + ' '.join(w[2] for w in g))
    captions.write_text('\n\n'.join(cues)+'\n', encoding='utf-8')
    # out.name is server generated. Use cwd to keep subtitle filter paths portable on Windows.
    duration = end-start
    for factor in (.87,.63):
        total = int(FILE_LIMIT * 8 * factor / duration / 1000)
        bitrate = max(100, min(2200, total-64))
        filters = 'scale=540:960:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=540:960:(ow-iw)/2:(oh-ih)/2,setsar=1'
        if cues:
            filters += f",subtitles={captions.name}:force_style='FontSize=19,Outline=2,Alignment=2,MarginV=37'"
        cmd = ['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-ss',str(start),*input_flags(kind),'-i',str(source),'-t',str(duration),'-map','0:v:0','-map','0:a:0','-vf',filters,'-r','25','-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p','-b:v',f'{bitrate}k','-maxrate',f'{bitrate}k','-bufsize',f'{bitrate}k','-c:a','aac','-b:a','64k','-movflags','+faststart',out.name]
        p = subprocess.run(cmd,cwd=out.parent,capture_output=True,text=True,timeout=1800)
        if p.returncode:
            raise RuntimeError('FFmpeg render failed: '+p.stderr[-550:])
        if out.stat().st_size < FILE_LIMIT:
            return
    raise RuntimeError('Rendered clip exceeds the configured Make file limit.')

def media_duration(source, kind, minimum=20, maximum=3600):
    info = json.loads(run('ffprobe','-v','error',*input_flags(kind),'-show_streams','-show_format','-of','json',str(source)))
    if not {'video', 'audio'} <= {stream.get('codec_type') for stream in info.get('streams', [])}:
        raise ValueError('The source does not contain readable video and audio streams.')
    try:
        duration = float(info.get('format', {}).get('duration'))
    except (TypeError, ValueError):
        raise ValueError('Could not determine the source duration. Try a fresh recording or upload.') from None
    if not math.isfinite(duration) or duration < minimum or (maximum is not None and duration > maximum):
        raise ValueError('Source duration is outside the supported range (up to 60 minutes).')
    return duration

def process(job):
    folder = DATA / job
    source = folder/'source'
    job_info = get('jobs', job)
    kind, mode = job_info['source_kind'], job_info['mode']
    duration = media_duration(source, kind, 1 if mode == 'fixed45' else 20)
    if job_info['parent_id'] and mode == 'fixed45':
        # AAC/container tails can extend slightly beyond the committed video window.
        duration = min(duration, 45.0)
    audio = folder/'audio.wav'
    transcript = []
    mode = job_info['mode']
    if mode == 'fixed45':
        parts = [(start, min(start + 45, duration), [], '')
                 for start in range(0, math.ceil(duration), 45) if duration - start >= 1]
    elif mode == 'split':
        parts = [(*part, '') for part in split_clips(duration)]
    else:
        run('ffmpeg','-nostdin','-loglevel','error','-y',*input_flags(kind),'-i',str(source),'-vn','-ac','1','-ar','16000',str(audio))
        transcript = transcribe(audio)
        audio.unlink(missing_ok=True)
        parts = story.select(transcript, duration) if mode == 'story' else [(*part, '') for part in select_clips(transcript)]
    (folder/'transcript.json').write_text(json.dumps(transcript), encoding='utf-8')
    if not parts:
        raise ValueError('No complete speech clips found. Try a recording with clearer speech or use Split by time for uncaptioned excerpts.')
    results, uploaded, rendered = [], [], []
    try:
        for number, (start,end,part,title) in enumerate(parts, 1):
            clip = uuid.uuid4().hex
            path = folder/f'{clip}.mp4'
            rendered.append(path)
            render(source,path,start,end,part,kind)
            title = story.clip_title(part, start, end, title) if part else (
                f'Timed cut · {clock((job_info["window_start"] or 0) + start)}' if mode == 'fixed45' else f'Clip {number}')
            size, remote = path.stat().st_size, None
            if STORAGE:
                remote = storage_path(job, clip)
                uploaded.append(remote)
                STORAGE.upload(remote, path)
                remove_local(path)
            results.append((clip,job,title or f'Clip {number}',start,end,remote,size if remote else None))
        with db() as c:
            c.executemany("INSERT INTO clips(id,job,title,start,end,storage_key,storage_size,state) VALUES(?,?,?,?,?,?,?,'ready')",results)
            c.execute("UPDATE jobs SET state='ready', duration=? WHERE id=?",(duration,job))
    except Exception:
        for remote in uploaded:
            remove_remote(remote)
        for path in rendered:
            remove_local(path)
        raise
    audio.unlink(missing_ok=True)

def worker(stop):
    while not stop.is_set():
        with db() as c:
            row = c.execute("UPDATE jobs SET state='processing' WHERE id=(SELECT id FROM jobs WHERE state='queued' ORDER BY created LIMIT 1) RETURNING id").fetchone()
        if not row:
            stop.wait(1)
            continue
        try:
            process(row['id'])
        except Exception as e:
            with db() as c:
                c.execute("UPDATE jobs SET state='failed',error=? WHERE id=?",(str(e)[:700],row['id']))

def clock(seconds):
    seconds = max(0, int(seconds or 0))
    return f'{seconds // 60:02}:{seconds % 60:02}'


CHECKPOINT_LOCK = threading.Lock()
STREAM_KINDS = ('live', 'youtube_live')
SESSION_KINDS = (*STREAM_KINDS, 'recorded_file')


def youtube_enabled():
    return not BASE and os.getenv('ENABLE_YOUTUBE_IMPORT') == '1'
ACTIVE_CAPTURE = ('awaiting_source', 'recording')


def checkpoint(key):
    # SQLite is authoritative; JSON is an atomic, human-readable export.
    with CHECKPOINT_LOCK:
        row = get('jobs', key)
        saved = row['recorded_seconds'] or 0
        data = {'job_id': key, 'source_type': row['source_kind'], 'channel': row['channel'],
                'mode': row['mode'], 'state': row['state'], 'target_minutes': row['capture_minutes'],
                'saved_seconds': saved, 'saved_time': clock(saved),
                'run_start_seconds': row['checkpoint_start'] or 0,
                'stop_requested_at_utc': (datetime.fromtimestamp(row['stop_at'], timezone.utc).isoformat()
                                          if row['stop_at'] else None),
                'updated_at_utc': datetime.now(timezone.utc).isoformat()}
        with db() as c:
            data['windows'] = [dict(r) for r in c.execute(
                'SELECT id, window_no, window_start AS start_seconds FROM jobs WHERE parent_id=? ORDER BY window_no', (key,))]
            data['history'] = [dict(r) for r in c.execute(
                'SELECT event, at AS unix_time, seconds FROM capture_events WHERE job=? ORDER BY rowid', (key,))]
        text = json.dumps(data, indent=2) + '\n'
        path = DATA / key / 'checkpoint.json'
        try:
            temp = path.with_suffix('.tmp')
            temp.write_text(text, encoding='utf-8')
            os.replace(temp, path)
        except OSError:
            LOG.warning('Could not export checkpoint for job %s; SQLite checkpoint is retained.', key)
        return text


def queue_window(session, path, number, end_time):
    key = uuid.uuid4().hex
    folder = DATA / key
    folder.mkdir()
    dest = folder / 'source'
    os.replace(path, dest)
    with db() as c:
        start = c.execute('SELECT recorded_seconds FROM jobs WHERE id=?', (session['id'],)).fetchone()[0] or 0
        c.execute("""INSERT INTO jobs(id,name,state,created,mode,source_kind,parent_id,window_no,window_start)
                     VALUES(?,?,'queued',?,?,'live',?,?,?)""",
                  (key, f'Window {number}', time.time(), session['mode'], session['id'], number, start))
        c.execute('UPDATE jobs SET recorded_seconds=? WHERE id=?', (end_time, session['id']))
    checkpoint(session['id'])


def capture_progress(key, phase, seconds):
    with db() as c:
        c.execute("UPDATE jobs SET capture_phase=?,live_seconds=? WHERE id=? AND state='recording'",
                  (phase, seconds, key))


class CaptureStop:
    def __init__(self, shutdown, key):
        self.shutdown, self.key = shutdown, key
        self.reason = 'Capture interrupted by app shutdown.'

    def is_set(self):
        with db() as c:
            stopped = c.execute('SELECT stop_at FROM jobs WHERE id=?', (self.key,)).fetchone()[0]
        if stopped:
            self.reason = 'Stopped by operator.'
        return bool(stopped or self.shutdown.is_set())

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.shutdown.wait(min(.25, remaining))
        return True


def seekable_windows(row, offset, emit, stopped, folder):
    source = DATA / (row['origin_id'] or row['id']) / 'source'
    target = min(media_duration(source, 'file', 1, maximum=None), row['capture_minutes'] * 60)
    if offset >= target:
        return offset, None
    step = 45 if row['mode'] == 'fixed45' else 120
    number = 0
    capture_progress(row['id'], 'processing_video', offset)
    while offset < target and not stopped.is_set():
        end = min(target, offset + step)
        path = folder / 'seekable-window.ts'
        run('ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-ss', str(offset),
            *input_flags('file'), '-i', str(source), '-t', str(end - offset),
            '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'libx264', '-preset', 'ultrafast',
            '-pix_fmt', 'yuv420p', '-threads', '2', '-c:a', 'aac', '-f', 'mpegts', str(path))
        # Finish the current cut before acknowledging Stop; seek from its end on resume.
        number += 1
        emit(path, number, end)
        offset = end
    return offset, None if offset >= target else stopped.reason


def acquire(stop, kind):
    while not stop.is_set():
        state = 'recording' if kind in SESSION_KINDS else 'importing'
        with db() as c:
            row = c.execute("UPDATE jobs SET state=? WHERE id=(SELECT id FROM jobs WHERE state='awaiting_source' AND source_kind=? ORDER BY created LIMIT 1) RETURNING *", (state, kind)).fetchone()
        if not row:
            stop.wait(1)
            continue
        dest = DATA / row['id'] / 'source'
        try:
            if kind in ('youtube', 'youtube_live') and not youtube_enabled():
                raise ValueError('YouTube import is disabled. Enable it only for local use with the tunnel off.')
            if kind in SESSION_KINDS:
                key = row['id']
                offset = row['recorded_seconds'] or 0
                stopped = CaptureStop(stop, key)
                with db() as c:
                    previous = c.execute('SELECT COALESCE(MAX(window_no),0) FROM jobs WHERE parent_id=?', (key,)).fetchone()[0]
                def emit(path, number, seconds):
                    queue_window(row, path, previous + number, seconds)
                checkpoint(key)
                if stopped.is_set():
                    duration, warning = offset, stopped.reason
                elif kind in STREAM_KINDS:
                    new_seconds, warning = sources.record(
                        row['channel'] or row['source_ref'], row['capture_minutes'], dest.parent,
                        LIVE_LIMIT, stopped,
                        lambda path, number, seconds: emit(path, number, offset + seconds),
                        lambda phase, seconds: capture_progress(key, phase, offset + seconds),
                        target_seconds=row['capture_minutes'] * 60 - offset,
                        fixed=row['mode'] == 'fixed45', run_id=uuid.uuid4().hex,
                        **({'provider': 'youtube'} if kind == 'youtube_live' else {}))
                    duration = offset + new_seconds
                else:
                    duration, warning = seekable_windows(row, offset, emit, stopped, dest.parent)
                with db() as c:
                    c.execute("""UPDATE jobs SET state=CASE WHEN stop_at IS NOT NULL THEN 'capture_stopped' ELSE ? END,
                              source_ref=NULL, recorded_seconds=?,error=?,live_seconds=NULL,capture_phase=NULL WHERE id=?""",
                              ('capture_partial' if warning else 'capture_complete', duration, warning, key))
                    c.execute("INSERT INTO capture_events SELECT id,state,?,recorded_seconds FROM jobs WHERE id=?", (time.time(),key))
                checkpoint(key)
                continue
            elif kind == 'youtube':
                sources.download_youtube(row['source_ref'], dest, UPLOAD_LIMIT, stop)
            else:
                sources.download(row['source_ref'],dest,UPLOAD_LIMIT)
            with db() as c:
                c.execute("UPDATE jobs SET state='queued',source_ref=NULL WHERE id=?", (row['id'],))
        except Exception as exc:
            try:
                if kind not in SESSION_KINDS:
                    dest.unlink(missing_ok=True)
            except OSError:
                # A locked partial recording must not stop the acquisition worker.
                LOG.warning('Could not remove partial source for job %s.', row['id'])
            message = str(exc)[:500] if isinstance(exc, ValueError) else 'Could not acquire the recording. Check the connection and try again.'
            with db() as c:
                c.execute("""UPDATE jobs SET state=CASE WHEN stop_at IS NOT NULL THEN 'capture_stopped'
                          WHEN COALESCE(recorded_seconds,0)>0 THEN 'capture_partial' ELSE 'failed' END,
                          source_ref=NULL,error=?,live_seconds=NULL,capture_phase=NULL WHERE id=?""", (message,row['id']))
            if kind in SESSION_KINDS:
                checkpoint(row['id'])


@asynccontextmanager
async def lifespan(app):
    # Run exactly one Uvicorn worker for this local prototype.
    with db() as c:
        c.execute("UPDATE jobs SET state='queued' WHERE state='processing'")
        interrupted = [r[0] for r in c.execute("SELECT id FROM jobs WHERE state='recording'")]
        c.execute("""UPDATE jobs SET state=CASE WHEN stop_at IS NOT NULL THEN 'capture_stopped'
                  WHEN COALESCE(recorded_seconds,0)>0 THEN 'capture_partial' ELSE 'failed' END,
                  source_ref=NULL,error='App interrupted. Only saved windows are retained.',
                  live_seconds=NULL,capture_phase=NULL WHERE state IN ('importing','recording')""")
        c.execute("UPDATE clips SET state='ready' WHERE state='trimming'")
    for key in interrupted:
        checkpoint(key)
    stop = threading.Event()
    captures = [threading.Thread(target=acquire,args=(stop,kind),daemon=True) for kind in ('link','live','recorded_file','youtube','youtube_live')]
    for thread in captures:
        thread.start()
    threading.Thread(target=worker,args=(stop,),daemon=True).start()
    threading.Thread(target=scheduler,args=(stop,),daemon=True).start()
    try:
        yield
    finally:
        stop.set()
        captures[1].join(timeout=7)
        captures[2].join(timeout=3)
        captures[3].join(timeout=2)
        captures[4].join(timeout=7)

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

def auth(req: Request):
    import base64
    origin = req.headers.get('origin')
    local = {'http://127.0.0.1:8000', 'http://localhost:8000'}
    if req.method == 'POST' and origin and origin not in local | {BASE}:
        raise HTTPException(403, 'Origin not permitted.')
    if not PASSWORD:
        return
    try:
        scheme, value = req.headers.get('authorization','').split(' ',1)
        raw = base64.b64decode(value,validate=True)
        if scheme.lower() == 'basic' and secrets.compare_digest(raw, ('admin:'+PASSWORD).encode()):
            return
    except (ValueError, UnicodeError):
        pass
    raise HTTPException(401,'Sign in',headers={'WWW-Authenticate':'Basic realm="Shorts Studio"'})

PAGES = {
    'overview': ('Overview', '/', '01'),
    'upload': ('Upload video', '/studio/upload', '02'),
    'import': ('Import a video', '/studio/import', '03'),
    'live': ('Twitch Live', '/studio/live', '04'),
    'fixed': ('45-second clips', '/studio/fixed', '05'),
    'recordings': ('Recordings', '/studio/recordings', '06'),
    'queue': ('YouTube queue', '/studio/queue', '07'),
}
ASSETS = Path(__file__).with_name('ui')


@app.get('/studio-assets/{name}', dependencies=[Depends(auth)])
def studio_asset(name: str):
    types = {'studio.css': 'text/css', 'studio.js': 'text/javascript'}
    if name not in types:
        raise HTTPException(404, 'Not found')
    return FileResponse(ASSETS / name, media_type=types[name])


def studio_page(page):
    title, _, _ = PAGES[page]
    links = []
    for key, (label, url, number) in PAGES.items():
        attrs = 'aria-current="page"' if key == page else ''
        arrow = ''
        links.append(f'<a href="{url}" {attrs}><span class="nav-number">{number}</span>{label}{arrow}</a>')
    template = Path(__file__).with_name('index.html').read_text(encoding='utf-8')
    content = (ASSETS / 'pages' / f'{page}.html').read_text(encoding='utf-8')
    return HTMLResponse(template.replace('__PAGE__', page).replace('__TITLE__', title)
                        .replace('__NAV__', ''.join(links)).replace('__CONTENT__', content))


@app.get('/', response_class=HTMLResponse, dependencies=[Depends(auth)])
def home():
    return studio_page('overview')


@app.get('/studio/{page}', response_class=HTMLResponse, dependencies=[Depends(auth)])
def feature_page(page: str):
    if page not in PAGES or page == 'overview':
        raise HTTPException(404, 'Workspace not found')
    return studio_page(page)


@app.get('/api/jobs',dependencies=[Depends(auth)])
def jobs():
    with db() as c:
        result = [dict(r) for r in c.execute('SELECT * FROM jobs ORDER BY created DESC')]
        for job in result:
            job.pop('source_ref', None)
            job['clips'] = [dict(r) for r in c.execute('SELECT id,title,start,end,state,video_id,queued_at,slot,storage_key,cloud_cleanup,upload_at,upload_privacy FROM clips WHERE job=? ORDER BY start',(job['id'],))]
    for job in result:
        for clip in job['clips']:
            remote = clip.pop('storage_key')
            clip['download_available'] = clip['cloud_cleanup'] != 'removed' and (bool(remote and STORAGE) or (not remote and (DATA / job['id'] / f"{clip['id']}.mp4").is_file()))
    return result

@app.post('/api/jobs',status_code=202,dependencies=[Depends(auth)])
def upload(file: UploadFile = File(...), mode: Literal['highlights', 'story', 'split', 'fixed45'] = Form('highlights'),
           minutes: int | None = Form(None, ge=1, le=60)):
    if mode == 'fixed45' and minutes is None:
        minutes = 60
    name = (file.filename or 'video.mp4')[:240]
    if Path(name).suffix.lower() not in ('.mp4','.mov'):
        raise HTTPException(400,'Choose an MP4 or MOV.')
    key = uuid.uuid4().hex
    folder = DATA/key
    folder.mkdir()
    source = folder/'source'
    size = 0
    try:
        with source.open('wb') as out:
            while chunk := file.file.read(1024*1024):
                size += len(chunk)
                if size > UPLOAD_LIMIT:
                    raise HTTPException(413,'Video exceeds the upload limit.')
                out.write(chunk)
        if not size:
            raise HTTPException(400,'Empty file.')
    except Exception:
        source.unlink(missing_ok=True)
        folder.rmdir()
        raise
    with db() as c:
        if minutes is None:
            c.execute("INSERT INTO jobs(id,name,state,error,created,mode) VALUES(?,?,'queued',NULL,?,?)",(key,name,time.time(),mode))
        else:
            c.execute("""INSERT INTO jobs(id,name,state,created,mode,source_kind,capture_minutes,recorded_seconds,visible_from)
                         VALUES(?,?,'awaiting_source',?,?,'recorded_file',?,0,1)""", (key,name,time.time(),mode,minutes))
    if minutes is not None:
        checkpoint(key)
    return {'id':key}

class SourceRequest(BaseModel):
    kind: Literal['link','live']
    url: str = Field(min_length=10,max_length=4096)
    minutes: int = Field(default=2,ge=1,le=60)
    mode: Literal['highlights','story','split','fixed45'] = 'story'
    authorized: bool = False
    youtube_live: bool = False


@app.post('/api/sources',status_code=202,dependencies=[Depends(auth)])
def add_source(data: SourceRequest):
    if not data.authorized:
        raise HTTPException(400,'Confirm that you can record or download and republish this content.')
    url = data.url.strip()
    kind = data.kind
    try:
        p = sources.link_parts(url)
        if kind == 'live' or p.hostname in ('www.twitch.tv', 'twitch.tv'):
            kind, ref = 'live', sources.twitch_channel(url)
        elif p.hostname in sources.YOUTUBE_HOSTS:
            ref = sources.youtube_id(url)
            if not youtube_enabled():
                raise HTTPException(403, 'Enable YouTube imports for localhost in .env. Keep the tunnel off.')
            kind = 'youtube_live' if data.youtube_live else 'youtube'
        else:
            ref = url
            if p.path.lower().endswith(('.m3u8','.mpd')):
                raise ValueError('Paste a direct MP4/MOV or a supported video page, not a playlist file.')
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    key = uuid.uuid4().hex
    labels = {'live': 'Twitch capture · ' + ref, 'youtube_live': 'YouTube Live capture · ' + ref,
              'youtube': 'YouTube video · ' + ref, 'link': 'Video link · ' + p.hostname}
    label = labels[kind]
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('awaiting_source','importing','recording') AND source_kind=?",(kind,)).fetchone()[0] >= 3:
            raise HTTPException(409,'Too many source jobs are waiting. Try again after one finishes.')
        (DATA/key).mkdir()
        c.execute("""INSERT INTO jobs(id,name,state,created,mode,source_kind,source_ref,capture_minutes,channel,visible_from)
                     VALUES(?,?,'awaiting_source',?,?,?,?,?,?,?)""",
                  (key,label,time.time(),data.mode,kind,ref,data.minutes,ref if kind in STREAM_KINDS else None,1))
    if kind in STREAM_KINDS:
        checkpoint(key)
    return {'id':key}


def session_row(key):
    row = get('jobs', key)
    if row['source_kind'] not in SESSION_KINDS or row['parent_id']:
        raise HTTPException(404, 'Recording session not found.')
    return row


@app.get('/api/jobs/{key}/checkpoint', dependencies=[Depends(auth)])
def download_checkpoint(key: str):
    session_row(key)
    return Response(checkpoint(key), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{key}-checkpoint.json"'})


@app.post('/api/jobs/{key}/stop', status_code=202, dependencies=[Depends(auth)])
def stop_capture(key: str):
    session_row(key)
    with db() as c:
        if not c.execute("""UPDATE jobs SET stop_at=?,
                         state=CASE WHEN state='awaiting_source' THEN 'capture_stopped' ELSE state END
                         WHERE id=? AND stop_at IS NULL AND state IN ('awaiting_source','recording')""", (time.time(), key)).rowcount:
            raise HTTPException(409, 'This capture is not running.')
        c.execute("INSERT INTO capture_events SELECT id,'stop_requested',stop_at,COALESCE(recorded_seconds,0) FROM jobs WHERE id=?", (key,))
    checkpoint(key)
    return {'ok': True}


class ContinueCapture(BaseModel):
    minutes: int = Field(ge=1, le=60)
    restart: bool = False


@app.post('/api/jobs/{key}/continue', status_code=202, dependencies=[Depends(auth)])
def continue_capture(key: str, data: ContinueCapture):
    session_row(key)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone()
        if row['state'] in ACTIVE_CAPTURE:
            raise HTTPException(409, 'Stop this capture before continuing or restarting.')
        channel = row['channel']
        if row['source_kind'] == 'youtube_live' and not youtube_enabled():
            raise HTTPException(403, 'Enable YouTube imports for localhost in .env before continuing.')
        if row['source_kind'] in STREAM_KINDS and not channel:
            raise HTTPException(409, 'This older capture has no saved channel. Start a new Twitch capture.')
        if data.restart:
            new = uuid.uuid4().hex
            origin = (row['origin_id'] or key) if row['source_kind'] == 'recorded_file' else None
            (DATA/new).mkdir()
            c.execute("""INSERT INTO jobs(id,name,state,created,mode,source_kind,capture_minutes,
                         channel,origin_id,recorded_seconds,visible_from) VALUES(?,?,'awaiting_source',?,?,?,?,?,?,0,1)""",
                      (new,row['name'],time.time(),row['mode'],row['source_kind'],data.minutes,channel,origin))
        else:
            saved = row['recorded_seconds'] or 0
            if saved >= data.minutes * 60 - .05:
                raise HTTPException(400, 'Choose a total target longer than the saved timestamp, or Start again.')
            if row['source_kind'] == 'recorded_file' and row['state'] == 'capture_complete' and saved < row['capture_minutes'] * 60 - .05:
                raise HTTPException(400, 'This video has reached its end. Use Start again to process it from 00:00.')
            next_no = c.execute('SELECT COALESCE(MAX(window_no),0)+1 FROM jobs WHERE parent_id=?', (key,)).fetchone()[0]
            new = key
            c.execute("""UPDATE jobs SET state='awaiting_source',capture_minutes=?,checkpoint_start=?,
                         visible_from=?,stop_at=NULL,error=NULL,live_seconds=NULL,capture_phase=NULL WHERE id=?""",
                      (data.minutes,saved,next_no,key))
        c.execute('INSERT INTO capture_events VALUES(?,?,?,?)', (new, 'restart' if data.restart else 'continue', time.time(), 0 if data.restart else saved))
    checkpoint(new)
    return {'id': new}


class TrimRequest(BaseModel):
    start: float = Field(ge=0,allow_inf_nan=False)
    end: float = Field(gt=0,allow_inf_nan=False)


@app.post('/api/clips/{key}/trim',dependencies=[Depends(auth)])
def trim(key: str, data: TrimRequest):
    clip = get('clips', key)
    job = get('jobs', clip['job'])
    folder = DATA / clip['job']
    transcript_path = folder/'transcript.json'
    if not job['duration'] or not transcript_path.exists() or not (folder/'source').is_file():
        raise HTTPException(409,'This clip predates trim support. Re-import the source to enable trimming.')
    if data.end > job['duration'] or not 3 <= data.end-data.start <= 90:
        raise HTTPException(400,'Choose a 3–90 second range within the source duration.')
    with db() as c:
        if not c.execute("UPDATE clips SET state='trimming' WHERE id=? AND state='ready'",(key,)).rowcount:
            raise HTTPException(409,'Only an unqueued, ready clip can be trimmed.')
    temp = folder/f'{key}-trim.mp4'
    remote = None
    try:
        transcript = json.loads(transcript_path.read_text(encoding='utf-8'))
        render(folder/'source',temp,data.start,data.end,[x for x in transcript if x['end'] > data.start and x['start'] < data.end],job['source_kind'])
        size = temp.stat().st_size
        if STORAGE:
            remote = storage_path(clip['job'], f'{key}-{uuid.uuid4().hex}')
            STORAGE.upload(remote, temp)
        elif clip['storage_key']:
            raise StorageError('Configure Supabase before editing a cloud clip.')
        with db() as c:
            if not remote:
                os.replace(temp, clip_file(clip))
            c.execute("UPDATE clips SET state='ready',start=?,end=?,storage_key=?,storage_size=? WHERE id=?",
                      (data.start,data.end,remote,size if remote else None,key))
    except Exception as exc:
        if remote:
            remove_remote(remote)
        with db() as c:
            c.execute("UPDATE clips SET state='ready' WHERE id=? AND state='trimming'",(key,))
        if isinstance(exc, StorageError):
            raise HTTPException(502, str(exc)) from exc
        raise HTTPException(422,'Could not render the trim. Try a different range.') from exc
    finally:
        temp.unlink(missing_ok=True)
        temp.with_suffix('.srt').unlink(missing_ok=True)
    if remote:
        remove_local(clip_file(clip))
        if clip['storage_key']:
            remove_remote(clip['storage_key'])
    return {'ok':True}


def clip_file(row):
    return DATA/row['job']/f"{row['id']}.mp4"

def storage_path(job, clip):
    return f'clips/{job}/{clip}.mp4'


def remove_local(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOG.warning('Could not remove local clip file %s.', path.name)


def remove_remote(key):
    try:
        STORAGE.delete(key)
    except StorageError:
        LOG.warning('Could not clean up Supabase object %s.', key)


def open_remote(row, byte_range=None):
    if not STORAGE:
        raise StorageError('Configure Supabase Storage to access this clip.')
    return STORAGE.open(row['storage_key'], byte_range)


@contextmanager
def clip_input(row):
    if not row['storage_key']:
        yield clip_file(row)
        return
    with tempfile.TemporaryDirectory(prefix='shorts-clip-') as directory:
        path = Path(directory) / 'clip.mp4'
        with open_remote(row) as response, path.open('wb') as out:
            copyfileobj(response.raw, out, 256 * 1024)
        yield path


def clip_response(row, req, name=None):
    if row['cloud_cleanup'] == 'removed':
        raise HTTPException(410, 'The Supabase MP4 was removed after the YouTube upload.')
    if not row['storage_key']:
        path = clip_file(row)
        if not path.is_file():
            raise HTTPException(404, 'The rendered MP4 could not be found.')
        return FileResponse(path, media_type='video/mp4', filename=name)
    try:
        upstream = open_remote(row, req.headers.get('range'))
    except ObjectMissing as exc:
        raise HTTPException(404, str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(502, str(exc)) from exc
    headers = {k: upstream.headers[k] for k in ('content-length', 'content-range', 'accept-ranges') if k in upstream.headers}
    headers['cache-control'] = 'private, no-store'
    if name:
        headers['content-disposition'] = "attachment; filename*=UTF-8''" + quote(name, safe='')
    if upstream.status_code == 416:
        upstream.close()
        headers.pop('content-length', None)
        return Response(status_code=416, headers=headers)
    def chunks():
        try:
            yield from upstream.iter_content(chunk_size=256 * 1024)
        finally:
            upstream.close()
    return StreamingResponse(chunks(), status_code=upstream.status_code, media_type='video/mp4',
                             headers=headers, background=BackgroundTask(upstream.close))


@app.post('/api/clips/{key}/title', dependencies=[Depends(auth)])
def generate_clip_title(key: str):
    clip = get('clips', key)
    if clip['state'] != 'ready':
        raise HTTPException(409, 'Only a ready, unqueued clip can be retitled.')
    try:
        with tempfile.TemporaryDirectory(prefix='shorts-title-') as directory, clip_input(clip) as path:
            audio = Path(directory) / 'title.wav'
            run('ffmpeg', '-nostdin', '-loglevel', 'error', *input_flags('file'), '-i', str(path),
                '-vn', '-ac', '1', '-ar', '16000', str(audio))
            transcript = transcribe(audio)
            title = story.clip_title(transcript, 0, clip['end'] - clip['start'])
    except StorageError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        LOG.warning('Could not transcribe clip %s for a title.', key)
        raise HTTPException(422, 'Could not generate a transcript title. Check FFmpeg and Whisper setup.') from exc
    if not title:
        raise HTTPException(422, 'No usable speech was found. Enter a title manually.')
    with db() as c:
        if not c.execute("UPDATE clips SET title=? WHERE id=? AND state='ready' AND start=? AND end=?",
                         (title,key,clip['start'],clip['end'])).rowcount:
            raise HTTPException(409, 'The clip changed. Refresh and try again.')
    return {'title': title}


def clip_download_name(title, key):
    """Keep the saved title while making the name safe on Windows and in ZIPs."""
    stem = unicodedata.normalize('NFC', title or '')
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', ' - ', stem)
    stem = re.sub(r'\s+', ' ', stem).strip(' .-')
    if stem.lower().endswith('.mp4'):
        stem = stem[:-4].rstrip(' .')
    stem = stem.encode('utf-8')[:180].decode('utf-8', 'ignore').rstrip(' .')
    if not stem:
        stem = f'Clip {key}'
    if re.fullmatch(r'CON|PRN|AUX|NUL|(?:COM|LPT)[0-9¹²³]', stem.split('.')[0], re.I):
        stem = '_' + stem
    return stem + '.mp4'


class ClipTitle(BaseModel):
    title: str = Field(min_length=1, max_length=100)


@app.post('/api/clips/{key}/rename', dependencies=[Depends(auth)])
def rename_clip(key: str, data: ClipTitle):
    get('clips', key)
    title = data.title.strip()
    if not title:
        raise HTTPException(422, 'Enter a title.')
    with db() as c:
        if not c.execute("UPDATE clips SET title=? WHERE id=? AND state='ready'", (title, key)).rowcount:
            raise HTTPException(409, 'Only an unqueued, ready clip can be renamed.')
    return {'title': title}


@app.get('/api/recordings/download-all', dependencies=[Depends(auth)])
def download_recordings():
    # Snapshot the titles, then write already-compressed MP4s to disk rather
    # than buffering the archive in memory or relying on browser multi-downloads.
    with db() as c:
        clips = c.execute('SELECT clips.* FROM clips JOIN jobs ON jobs.id=clips.job '
                          "WHERE clips.cloud_cleanup IS NULL OR clips.cloud_cleanup != 'removed' "
                          'ORDER BY jobs.created, clips.start, clips.id').fetchall()
    used, missing = set(), []
    temp = tempfile.NamedTemporaryFile(prefix='shorts-recordings-', suffix='.zip', delete=False)
    path = Path(temp.name)
    temp.close()
    try:
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
            for clip in clips:
                base = clip_download_name(clip['title'], clip['id'])[:-4]
                name, n = base + '.mp4', 2
                while name.casefold() in used:
                    name = f'{base} ({n}).mp4'
                    n += 1
                try:
                    if clip['storage_key']:
                        with open_remote(clip) as response, archive.open(name, 'w', force_zip64=True) as entry:
                            received = 0
                            for chunk in response.iter_content(chunk_size=256 * 1024):
                                entry.write(chunk)
                                received += len(chunk)
                            if received != clip['storage_size']:
                                raise StorageError('The cloud MP4 size did not match the library. Retry the export.')
                    else:
                        archive.write(clip_file(clip), name)
                except (FileNotFoundError, ObjectMissing):
                    missing.append(f"{base} [{clip['id']}]")
                    continue
                used.add(name.casefold())
            if not used:
                raise HTTPException(404, 'No rendered clips are available to download yet.')
            if missing:
                archive.writestr('Missing files.txt', 'These clips are in the library but their MP4s were not found:\n' +
                                 '\n'.join(missing) + '\n')
    except (StorageError, requests.RequestException) as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(502, 'Could not complete the Supabase download. Check storage connectivity and retry.') from exc
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return FileResponse(path, media_type='application/zip', filename='Shorts Studio Recordings.zip',
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.get('/clips/{key}', dependencies=[Depends(auth)])
def preview(key: str, req: Request, download: bool = False):
    row = get('clips', key)
    name = clip_download_name(row['title'], key) if download else None
    return clip_response(row, req, name)


@app.get('/make/file/{key}')
def make_file(key: str, req: Request, token: str = ''):
    row = get('clips',key)
    if row['state'] not in ('upload_requested','delivery_unconfirmed') or not row['token'] or not secrets.compare_digest(token.encode(),row['token'].encode()) or time.time() > row['expires']:
        raise HTTPException(403,'Invalid or expired download link.')
    return clip_response(row, req, f'{key}.mp4')

class Publish(BaseModel):
    title: str = Field(min_length=1,max_length=100)
    description: str = Field(default='',max_length=4000)

    privacy: Literal['private', 'public'] | None = None
    upload_at: datetime | None = None
    delay_minutes: int | None = Field(default=None, ge=1, le=525600)


def youtube_title(title):
    title = title.strip()
    title = re.sub(r'^GTA 6(?=$|[\s:–—-])[\s:–—-]*', '', title, flags=re.I)
    return (f'GTA 6 - {title}' if title else 'GTA 6')[:100].rstrip()


def scheduled_time(data, now):
    if data.upload_at is not None and data.delay_minutes is not None:
        raise HTTPException(422, 'Choose an exact time or a delay, not both.')
    if data.delay_minutes is not None:
        return now + data.delay_minutes * 60
    if data.upload_at is not None:
        if data.upload_at.utcoffset() is None:
            raise HTTPException(422, 'An exact upload time must include a time zone offset.')
        due = data.upload_at.timestamp()
        if due <= now:
            raise HTTPException(422, 'Choose an upload time in the future.')
        return due
    return None


def make_ready():
    return bool(BASE and WEBHOOK and MAKE_KEY and CALLBACK)

def deliver(key, token, title, description, privacy):
    payload = {'clip_id':key,'attempt_token':token,'file_url':f'{BASE}/make/file/{key}?token={token}',
               'title':youtube_title(title),'description':description,'privacy':privacy,'callback_url':f'{BASE}/make/callback'}
    try:
        res = requests.post(WEBHOOK,json=payload,headers={'x-make-apikey':MAKE_KEY},timeout=15,allow_redirects=False)
        if not 200 <= res.status_code < 300:
            raise RuntimeError('Webhook rejected the request.')
    except (requests.RequestException, RuntimeError):
        # Never auto-replay an uncertain delivery. A late callback may still succeed.
        raise HTTPException(502,'Make acceptance not confirmed. Check Make run history before any retry.') from None

def validate_clip(row):
    if row['storage_key']:
        if not STORAGE or row['storage_size'] is None:
            raise HTTPException(503,'Supabase Storage is not configured for this clip.')
        size = row['storage_size']
    else:
        path = clip_file(row)
        if not path.is_file():
            raise HTTPException(422,'Clip file is missing.')
        size = path.stat().st_size
    if size >= FILE_LIMIT:
        raise HTTPException(422,'Clip exceeds the Make plan file cap.')

@app.get('/api/schedule',dependencies=[Depends(auth)])
def schedule_info():
    with db() as c:
        count = c.execute("SELECT COUNT(*) FROM clips WHERE state='scheduled'").fetchone()[0]
    return {'timezone':SCHEDULE_TZ.key,'count':count,'make_ready':make_ready(),'privacy':SCHEDULE_PRIVACY}

@app.post('/api/clips/{key}/schedule',status_code=202,dependencies=[Depends(auth)])
def queue_clip(key: str, data: Publish):
    row = get('clips',key)
    validate_clip(row)
    if not data.title.strip():
        raise HTTPException(422,'Title cannot be blank.')
    now = time.time()
    due = scheduled_time(data, now)
    if due is not None and not make_ready():
        raise HTTPException(503, 'Configure the public HTTPS URL and Make secrets first.')
    with db() as c:
        if not c.execute("UPDATE clips SET state='scheduled', queued_at=?, title=?, description=?, upload_at=?, upload_privacy=? WHERE id=? AND state='ready'",
                         (now,youtube_title(data.title),data.description,due,data.privacy,key)).rowcount:
            raise HTTPException(409,'Only ready clips can be scheduled.')
    return {'queued':1}

@app.post('/api/jobs/{key}/schedule',status_code=202,dependencies=[Depends(auth)])
def queue_job(key: str):
    get('jobs',key)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute("SELECT * FROM clips WHERE job=? AND state='ready' ORDER BY start",(key,)).fetchall()
        for row in rows:
            validate_clip(row)
        count = c.execute("UPDATE clips SET state='scheduled', queued_at=?, description='' WHERE job=? AND state='ready'",(time.time(),key)).rowcount
    return {'queued':count}

@app.post('/api/clips/{key}/unschedule',dependencies=[Depends(auth)])
def unqueue(key: str):
    with db() as c:
        if not c.execute("UPDATE clips SET state='ready',queued_at=NULL,upload_at=NULL,upload_privacy=NULL WHERE id=? AND state='scheduled'",(key,)).rowcount:
            raise HTTPException(409,'This clip is no longer waiting in the queue.')
    return {'ok':True}

def dispatch_slot(now=None):
    if not make_ready():
        return False
    now = now or datetime.now(SCHEDULE_TZ)
    now = now.astimezone(SCHEDULE_TZ)
    if now.hour not in SLOTS or now.minute >= 5:
        return False
    slot = now.strftime('%Y-%m-%d %H:00')
    token = secrets.token_urlsafe(32)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM schedule_slots WHERE slot=?',(slot,)).fetchone():
            return False
        rows = c.execute("SELECT * FROM clips WHERE state='scheduled' AND upload_at IS NULL ORDER BY queued_at,start,id").fetchall()
        row = None
        for candidate in rows:
            try:
                validate_clip(candidate)
            except HTTPException:
                c.execute("UPDATE clips SET state='schedule_file_error' WHERE id=?",(candidate['id'],))
                continue
            row = candidate
            break
        if row is None:
            return False
        c.execute('INSERT INTO schedule_slots VALUES(?,?)',(slot,row['id']))
        c.execute("UPDATE clips SET state='upload_requested',token=?,expires=?,slot=? WHERE id=?",
                  (token,time.time()+3600,slot,row['id']))
    try:
        deliver(row['id'],token,row['title'],row['description'] or '',row['upload_privacy'] or SCHEDULE_PRIVACY)
    except HTTPException:
        LOG.warning('Scheduled clip %s: Make delivery not confirmed. Check Make history.',row['id'])
        with db() as c:
            c.execute("UPDATE clips SET state='delivery_unconfirmed' WHERE id=? AND state='upload_requested'",(row['id'],))
    return True

def dispatch_due(now=None):
    if not make_ready():
        return False
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT * FROM clips WHERE state='scheduled' AND upload_at<=? ORDER BY upload_at,queued_at,id LIMIT 1", (now,)).fetchone()
        if row is None:
            return False
        try:
            validate_clip(row)
        except HTTPException as exc:
            if exc.status_code == 503:
                return False  # Keep the clip queued if storage configuration is unavailable.
            c.execute("UPDATE clips SET state='schedule_file_error' WHERE id=?", (row['id'],))
            return True
        c.execute("UPDATE clips SET state='upload_requested', token=?, expires=? WHERE id=?",
                  (token,time.time()+3600,row['id']))
    try:
        deliver(row['id'], token, row['title'], row['description'] or '', row['upload_privacy'] or 'private')
    except HTTPException:
        LOG.warning('Timed clip %s: Make delivery not confirmed. Check Make history.',row['id'])
        with db() as c:
            c.execute("UPDATE clips SET state='delivery_unconfirmed' WHERE id=? AND state='upload_requested'", (row['id'],))
    return True


def scheduler(stop):
    while not stop.is_set():
        try:
            dispatch_due()
            dispatch_slot()
        except Exception:
            LOG.exception('Could not check the Shorts Studio schedule.')
        stop.wait(2)

@app.post('/api/clips/{key}/publish',status_code=202,dependencies=[Depends(auth)])
def publish(key: str, data: Publish):
    if not make_ready():
        raise HTTPException(503,'Configure the public HTTPS URL and Make secrets first.')
    row = get('clips',key)
    validate_clip(row)
    if not data.title.strip():
        raise HTTPException(422,'Title cannot be blank.')
    if data.upload_at is not None or data.delay_minutes is not None:
        raise HTTPException(422, 'Use the schedule endpoint for future uploads.')
    title = youtube_title(data.title)
    privacy = data.privacy or 'private'
    token = secrets.token_urlsafe(32)
    with db() as c:
        changed = c.execute("UPDATE clips SET state='upload_requested', token=?, expires=?, title=?, description=?, upload_privacy=? WHERE id=? AND state='ready'",(token,time.time()+3600,title,data.description,privacy,key)).rowcount
    if not changed:
        raise HTTPException(409,'Already queued, requested, or uploaded. Check Make history before retrying.')
    deliver(key,token,title,data.description,privacy)
    return {'state':'upload_requested'}

class Completion(BaseModel):
    clip_id: str
    attempt_token: str
    video_id: str = Field(pattern=r'^[A-Za-z0-9_-]{11}$')

@app.post('/make/callback')
def complete(data: Completion, req: Request):
    if not CALLBACK or not secrets.compare_digest(req.headers.get('authorization','').encode(),('Bearer '+CALLBACK).encode()):
        raise HTTPException(401,'Unauthorized')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT state,video_id,token,storage_key,cloud_cleanup FROM clips WHERE id=?',(data.clip_id,)).fetchone()
        if not row or not secrets.compare_digest((row['token'] or '').encode(),data.attempt_token.encode()):
            raise HTTPException(409,'Unknown upload attempt')
        if row['state'] == 'uploaded':
            if row['video_id'] != data.video_id:
                raise HTTPException(409,'YouTube video ID mismatch')
            key = row['storage_key'] if row['cloud_cleanup'] == 'pending' else None
        elif row['state'] in ('upload_requested', 'delivery_unconfirmed'):
            key = row['storage_key']
            c.execute("UPDATE clips SET state='uploaded',video_id=?,cloud_cleanup=? WHERE id=?",
                      (data.video_id, 'pending' if key else None, data.clip_id))
        else:
            raise HTTPException(409,'Upload state mismatch')
    # Persist YouTube success before contacting Storage. If deletion fails, retry
    # this exact callback, never the YouTube upload. The clip cannot be requeued.
    if key:
        try:
            if not STORAGE:
                raise StorageError('Supabase Storage is not configured.')
            STORAGE.delete(key)
        except StorageError as exc:
            LOG.warning('Supabase cleanup pending for clip %s: %s', data.clip_id, exc)
            raise HTTPException(502, 'YouTube upload saved. Supabase cleanup pending. Retry only this callback.') from exc
        with db() as c:
            c.execute("UPDATE clips SET storage_key=NULL,storage_size=NULL,cloud_cleanup='removed' "
                      "WHERE id=? AND state='uploaded' AND token=? AND cloud_cleanup='pending' AND storage_key=?",
                      (data.clip_id, data.attempt_token, key))
    return {'ok':True}
