"""Verify only a 2xx response confirms Make acceptance; no network."""
import os
import tempfile
from unittest.mock import Mock, patch
from fastapi import HTTPException

with tempfile.TemporaryDirectory() as directory:
    os.environ.update(DATA_DIR=directory, PUBLIC_BASE_URL='', APP_PASSWORD='')
    import app
    for status in (200, 202, 204, 302, 304, 400, 500):
        with patch.object(app.requests, 'post', return_value=Mock(status_code=status)) as post:
            try:
                app.deliver('clip', 'token', 'Title', '', 'private')
            except HTTPException as exc:
                assert exc.status_code == 502 and status >= 300, status
            else:
                assert 200 <= status < 300, status
            post.assert_called_once()
            assert post.call_args.kwargs['allow_redirects'] is False
print('PASS: only 2xx confirms Make acceptance; no redirect or retry')
