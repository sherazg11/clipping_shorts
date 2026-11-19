"""Offline route and export checks. Run with python studio_test.py (requires httpx)."""
import io
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


def main():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
        'DATA_DIR': directory, 'APP_PASSWORD': '', 'PUBLIC_BASE_URL': '',
        'MAKE_WEBHOOK_URL': '', 'MAKE_CALLBACK_SECRET': '',
    }):
        import app

        client = TestClient(app.app)
        assert client.get('/studio/unknown').status_code == 404
        for page, (title, route, _) in app.PAGES.items():
            response = client.get(route)
            assert response.status_code == 200
            assert response.text.count('data-view=') == 1
            assert f'data-view="{page}"' in response.text
            assert '<title>' + title in response.text
            assert 'local demo' not in response.text.lower()
            if page == 'overview':
                for tool in ('upload', 'import', 'live', 'fixed', 'recordings', 'queue'):
                    assert f'href="/studio/{tool}" target="_blank"' in response.text
            if page == 'fixed':
                assert response.text.count('value="fixed45"') == 2
        for asset in ('studio.css', 'studio.js'):
            assert client.get('/studio-assets/' + asset).status_code == 200
        assert client.get('/api/recordings/download-all').status_code == 404
        with app.db() as db:
            for i, (mode, state) in enumerate([('story', 'ready'), ('fixed45', 'uploaded'), ('story', 'ready')]):
                j, k = f'job{i}', f'clip{i}'
                db.execute('INSERT INTO jobs(id,name,state,created,mode) VALUES(?,?,?,?,?)', (j, j, 'ready', i, mode))
                db.execute('INSERT INTO clips(id,job,title,start,end,state) VALUES(?,?,?,?,?,?)',
                           (k, j, 'First: Clip' if i < 2 else 'Missing Clip', 0, 2, state))
                if i < 2:
                    folder = Path(directory) / j
                    folder.mkdir()
                    (folder / f'{k}.mp4').write_bytes(f'fixture{i}'.encode())
        assert [j['clips'][0]['download_available'] for j in client.get('/api/jobs').json()] == [False, True, True]
        response = client.get('/api/recordings/download-all')
        assert response.status_code == 200 and response.headers['content-type'] == 'application/zip'
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.namelist() == ['First - Clip.mp4', 'First - Clip (2).mp4', 'Missing files.txt']
            assert archive.read('First - Clip (2).mp4') == b'fixture1'
            assert b'Missing Clip' in archive.read('Missing files.txt')
        assert client.post('/api/clips/clip0/rename', json={'title': 'New title'}).json() == {'title': 'New title'}
        assert client.get('/clips/clip0?download=true').headers['content-disposition'].endswith('New%20title.mp4')
        with zipfile.ZipFile(io.BytesIO(client.get('/api/recordings/download-all').content)) as archive:
            assert archive.namelist()[0] == 'New title.mp4'
    print('Passed: seven page routes, new-tab links, assets, missing-file handling, saved titles, and ZIP export.')


if __name__ == '__main__':
    main()
