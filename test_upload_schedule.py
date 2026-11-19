"""Run in a test copy. Uses a temporary DB and never makes network requests."""
import os
import tempfile
import time
from datetime import datetime, timezone
from unittest.mock import patch

with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
    'DATA_DIR': folder, 'SUPABASE_URL': '', 'SUPABASE_SECRET_KEY': '', 'SUPABASE_SERVICE_ROLE_KEY': '',
    'PUBLIC_BASE_URL': 'https://test.invalid', 'APP_PASSWORD': 'x'*20, 'MAKE_CALLBACK_SECRET': 'x'*30,
    'MAKE_WEBHOOK_URL': 'https://make.invalid', 'MAKE_WEBHOOK_API_KEY': 'test', 'SCHEDULE_TIMEZONE': 'UTC',
}):
    import app
    now = time.time()
    with app.db() as db:
        db.execute("INSERT INTO jobs (id,name,state,created) VALUES ('j','Test','ready',?)", (now,))
        for name in ['exact','delay','now','cancel','invalid','legacy']:
            db.execute("INSERT INTO clips (id,job,title,start,end,state) VALUES (?,'j','Test',0,1,'ready')", (name,))
    (app.DATA/'j').mkdir()
    for name in ['exact','delay','now','cancel','invalid','legacy']:
        (app.DATA/'j'/f'{name}.mp4').write_bytes(b'test')
    with patch.object(app.requests,'post') as post:
        post.return_value.status_code = 200
        app.publish('now',app.Publish(title='Escape',privacy='public'))
        assert post.call_args.kwargs['json']['privacy'] == 'public'
        assert post.call_args.kwargs['json']['title'] == 'GTA 6 - Escape'
        assert app.youtube_title('GTA 6: Escape') == 'GTA 6 - Escape'
        assert len(app.youtube_title('x'*100)) == 100
        exact = now+600
        app.queue_clip('exact',app.Publish(title='Escape',privacy='public',upload_at=datetime.fromtimestamp(exact,timezone.utc)))
        app.queue_clip('delay',app.Publish(title='Drive',privacy='private',delay_minutes=1))
        app.queue_clip('cancel',app.Publish(title='Cancel',delay_minutes=1)); app.unqueue('cancel')
        app.queue_clip('legacy',app.Publish(title='Legacy'))
        for opts in [{'upload_at':datetime.now()}, {'upload_at':datetime.fromtimestamp(now-100,timezone.utc)}, {'privacy':'public','upload_at':datetime.fromtimestamp(exact,timezone.utc),'delay_minutes':1}]:
            try:
                app.queue_clip('invalid',app.Publish(title='Invalid',**opts))
            except app.HTTPException as exc:
                assert exc.status_code == 422
            else:
                raise AssertionError('Invalid schedule accepted')
        clips = {c['id']:c for c in app.jobs()[0]['clips']}
        assert clips['exact']['upload_at'] == exact and clips['exact']['upload_privacy'] == 'public'
        assert not app.dispatch_due(now+10)
        assert app.dispatch_due(now+75) and post.call_args.kwargs['json']['privacy'] == 'private'
        assert app.dispatch_due(exact) and post.call_args.kwargs['json']['privacy'] == 'public'
        assert not app.dispatch_due(exact+1)
        assert app.get('clips','cancel')['state'] == 'ready'
        assert app.dispatch_slot(datetime(2026,11,19,9,1,tzinfo=timezone.utc))
        assert post.call_args.kwargs['json']['clip_id'] == 'legacy'
print('Immediate, delayed, exact, cancellation, validation, prefix, and legacy queue checks passed.')
