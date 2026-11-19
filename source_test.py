"""Offline source tests: real MP4/MPEG-TS processing, mocked source transfer and speech."""
import os, tempfile, time, subprocess, shutil
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

with tempfile.TemporaryDirectory() as directory:
    os.environ.update(DATA_DIR=directory, PUBLIC_BASE_URL='', APP_PASSWORD='', STORY_MODEL='')
    import app
    root=Path(directory)
    mp4=root/'sample.mp4'; ts=root/'sample.ts'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=15','-f','lavfi','-i','sine=frequency=400','-t','49','-c:v','libx264','-c:a','aac',str(mp4)],check=True)
    subprocess.run(['ffmpeg','-v','error','-i',str(mp4),'-c','copy','-f','mpegts',str(ts)],check=True)
    lines=['Why does this matter?', 'It keeps the beginning easy to follow.', 'Then explain the real problem.', 'The best fix is to answer it directly.', 'That is how you make the idea useful.', 'An unfinished last thought']
    segments=[]
    for i,line in enumerate(lines):
        words=line.split(); t=i*8
        segments.append({'text':line,'start':t,'end':t+len(words)*.7,'words':[(t+j*.7,t+j*.7+.6,w) for j,w in enumerate(words)]})
    assert len(app.story.sentences(segments))==5
    picks=app.story.select(segments,49)
    assert picks and all(end < 40 for _,end,_,_ in picks)
    def wait(client,key):
        for _ in range(140):
            item=next(x for x in client.get('/api/jobs').json() if x['id']==key)
            if item['state'] in ('ready','failed'):
                assert item['state']=='ready',item
                return item
            time.sleep(.25)
        raise AssertionError('job timed out')
    with patch.object(app,'transcribe',return_value=segments), patch.object(app.sources,'download',side_effect=lambda u,o,l:shutil.copyfile(mp4,o)), patch.object(app.sources,'record',side_effect=lambda c,m,o,l,s,emit,progress:(emit(Path(shutil.copyfile(ts,o/'window-00000.ts')),1,49.0) or (49.0, 'Test stream ended early'))), TestClient(app.app) as client:
        link={'kind':'link','url':'https://files.example.org/sample.mp4?signature=SECRET','authorized':True}
        assert client.post('/api/sources',json={**link,'authorized':False}).status_code==400
        assert client.post('/api/sources',json={**link,'url':'https://www.youtube.com/watch?v=test'}).status_code==400
        r=client.post('/api/sources',json=link); assert r.status_code==202,r.text
        job=wait(client,r.json()['id']); assert job['clips'] and 'source_ref' not in job
        clip=job['clips'][0]; key=clip['id']; route=f'/api/clips/{key}/trim'
        assert client.post(route,json={'start':0,'end':999}).status_code==400
        r=client.post(route,json={'start':0,'end':22}); assert r.status_code==200,r.text
        assert wait(client,job['id'])['clips'][0]['end']==22
        r=client.post('/api/sources',json={'kind':'live','url':'https://www.twitch.tv/example','mode':'split','authorized':True}); assert r.status_code==202,r.text
        session=r.json()['id']
        for _ in range(100):
            entries=client.get('/api/jobs').json()
            windows=[item for item in entries if item['parent_id']==session]
            if windows:
                break
            time.sleep(.1)
        assert windows and len(wait(client,windows[0]['id'])['clips'])==2
        with patch.object(app.sources.socket,'getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',443))]):
            try: app.sources.public_ip('example.org')
            except ValueError: pass
            else: raise AssertionError('private IP allowed')
    print('PASS: link intake, Twitch intake, real MP4/TS rendering, story boundaries, trim, private IP guard')
