"""Offline capture-to-preview regression using real FFmpeg and a fake live producer."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from fastapi.testclient import TestClient


def eventually(read, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(.2)
    raise AssertionError('Timed out waiting for live pipeline')


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    os.environ.update(DATA_DIR=str(root/'data'), PUBLIC_BASE_URL='', APP_PASSWORD='',
                      MAKE_WEBHOOK_URL='', STORY_MODEL='')
    import app
    media = root/'fragmented.mp4'
    transport = root/'transport.ts'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10',
                    '-f','lavfi','-i','sine=frequency=400','-t','78','-c:v','libx264','-g','10',
                    '-c:a','aac','-movflags','+frag_keyframe+empty_moov+default_base_moof',str(media)],check=True)
    subprocess.run(['ffmpeg','-v','error','-i',str(media),'-c','copy','-f','mpegts',str(transport)],check=True)
    release = root/'release'
    producer = '''import shutil, sys, time
from pathlib import Path
with open(sys.argv[1], 'rb') as f:
    shutil.copyfileobj(f, sys.stdout.buffer)
sys.stdout.buffer.flush()
while not Path(sys.argv[2]).exists():
    time.sleep(.05)
'''
    stream = [sys.executable,'-c',producer,str(media),str(release)]
    with patch.object(app.sources,'twitch_command',return_value=stream), \
            patch.object(app.sources,'LIVE_WINDOW_SECONDS',30), TestClient(app.app) as client:
        payload = {'kind':'live','url':'https://www.twitch.tv/example','authorized':True,'mode':'split'}
        assert client.post('/api/sources',json={**payload,'minutes':61}).status_code == 422
        response = client.post('/api/sources',json={**payload,'minutes':60})
        assert response.status_code == 202, response.text
        key = response.json()['id']
        def preview_while_recording():
            data = client.get('/api/jobs').json()
            parent = next(j for j in data if j['id']==key)
            if parent['state']=='awaiting_source': return None
            assert parent['state']=='recording', data
            return next((j for j in data if j['parent_id']==key and j['clips']),None)
        window = eventually(preview_while_recording)
        route = '/clips/'+window['clips'][0]['id']
        assert client.get(route,headers={'Range':'bytes=0-99'}).status_code == 206
        assert client.post('/api/clips/'+window['clips'][0]['id']+'/trim',json={'start':0,'end':20}).status_code == 200
        release.touch()
        ended = eventually(lambda: next((j for j in client.get('/api/jobs').json()
                                         if j['id']==key and j['state']=='capture_partial'),None))
        assert 75 < ended['recorded_seconds'] < 80, ended
        assert client.get(route).status_code == 200
        # An endless producer must not prevent the one-minute media cap from closing the job.
        never = root/'never'
        stream[:] = [sys.executable,'-c',producer,str(transport),str(never)]
        response = client.post('/api/sources',json={**payload,'minutes':1})
        key = response.json()['id']
        ended = eventually(lambda: next((j for j in client.get('/api/jobs').json()
                                         if j['id']==key and j['state']=='capture_complete'),None))
        assert 58 < ended['recorded_seconds'] < 62, ended
    print('PASS: 60-minute intake, fragmented MP4 live preview before EOF, trim, partial capture, and MPEG-TS duration cap')
