"""Offline integration test: FFmpeg is real, speech timestamps and Make are mocked.
Requires httpx in addition to the app dependencies.
"""
import os, tempfile, time, subprocess, json
from pathlib import Path
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient

with tempfile.TemporaryDirectory() as directory:
    os.environ.update(DATA_DIR=directory, PUBLIC_BASE_URL='', APP_PASSWORD='', MAKE_MAX_FILE_BYTES='5000000')
    import app
    source = Path(directory) / 'sample.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=640x360:rate=25','-f','lavfi','-i','sine=frequency=400','-t','85','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)],check=True)
    def segment(i):
        words='Why this is an important example.'.split()
        return {'start':i,'end':i+4.8,'text':' '.join(words),'words':[(i+j*.55,i+j*.55+.5,x) for j,x in enumerate(words)]}
    with patch.object(app,'transcribe',return_value=[segment(i) for i in range(0,80,5)]), TestClient(app.app) as client:
        assert client.get('/').status_code==200
        with source.open('rb') as file:
            res=client.post('/api/jobs',files={'file':('sample.mp4',file,'video/mp4')})
            assert res.status_code==202,res.text
        for _ in range(160):
            job=client.get('/api/jobs').json()[0]
            if job['state'] in ('ready','failed'): break
            time.sleep(.25)
        assert job['state']=='ready' and len(job['clips'])>=2,job
        key=job['clips'][0]['id']; path=app.clip_file(app.get('clips',key))
        meta=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(path)]))
        assert (meta['streams'][0]['width'],meta['streams'][0]['height'])==(540,960)
        assert path.stat().st_size<5000000
        assert client.get('/clips/'+key,headers={'Range':'bytes=0-1023'}).status_code==206
        app.BASE='https://example.com'; app.WEBHOOK='https://example.com/mock'; app.MAKE_KEY='test'; app.CALLBACK='callback-secret'
        with patch.object(app.requests,'post',return_value=Mock(status_code=202)) as make:
            route='/api/clips/'+key+'/publish'
            res=client.post(route,json={'title':'Test clip'}); assert res.status_code==202,res.text
            payload=make.call_args.kwargs['json']
            file='/make/file/'+key+'?token='+payload['attempt_token']; assert client.get(file).status_code==200
            assert client.post(route,json={'title':'Test clip'}).status_code==409
            body={'clip_id':key,'attempt_token':payload['attempt_token'],'video_id':'Abc123-_xyz'}
            for _ in range(2):
                res=client.post('/make/callback',json=body,headers={'Authorization':'Bearer callback-secret'}); assert res.status_code==200,res.text
            assert client.get(file).status_code==403
            assert client.get('/api/jobs').json()[0]['clips'][0]['video_id']==body['video_id']
        others=[clip['id'] for clip in job['clips'][1:]]
        with patch.object(app,'SLOTS',()), patch.object(app.requests,'post',return_value=Mock(status_code=202)) as make:
            assert client.post('/api/jobs/'+job['id']+'/schedule').json()=={'queued':len(others)}
            assert client.post('/api/jobs/'+job['id']+'/schedule').json()=={'queued':0}
            other=others[0]; path='/api/clips/'+other
            assert client.post(path+'/publish',json={'title':'Wrong time'}).status_code==409
            assert client.post(path+'/unschedule').status_code==200
            assert client.post(path+'/schedule',json={'title':'Scheduled title'}).status_code==202
            t=app.datetime(2027,1,1,9,0,tzinfo=app.SCHEDULE_TZ)
            assert not app.dispatch_slot(t)
            with patch.object(app,'SLOTS',(9,10,16)):
                assert not app.dispatch_slot(t.replace(minute=5))
                assert app.dispatch_slot(t)
                assert not app.dispatch_slot(t)
                payload=make.call_args.kwargs['json']
                assert payload['clip_id']==(others[1] if len(others)>1 else other)
                assert payload['privacy']=='private'
                assert client.get('/api/schedule').json()['count']==len(others)-1
                assert make.call_count==1
        print('PASS: batch queue, unschedule, time window, FIFO and one dispatch per slot')
        print('PASS: upload, queue, multiple FFmpeg clips, preview, file cap, Make handoff, duplicate guard, callback')
