"""Server-only Supabase Storage adapter for rendered MP4s."""
import re
from urllib.parse import quote, urlsplit

import requests


class StorageError(RuntimeError):
    pass


class ObjectMissing(StorageError):
    pass


class SupabaseStorage:
    def __init__(self, url, key, bucket):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.path not in ('', '/') or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError('SUPABASE_URL must be the HTTPS project origin.')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', bucket):
            raise RuntimeError('Invalid SUPABASE_BUCKET name.')
        if not (key.startswith('sb_secret_') or key.count('.') == 2):
            raise RuntimeError('Use a Supabase secret key or legacy service_role JWT, not a publishable key.')
        self.base = url.rstrip('/') + '/storage/v1'
        self.bucket = bucket
        self.headers = {'apikey': key}
        if not key.startswith('sb_secret_'):
            self.headers['Authorization'] = 'Bearer ' + key

    def request(self, method, endpoint, *, headers=None, allowed=(200,), **kwargs):
        try:
            response = requests.request(method, self.base + endpoint,
                                        headers={**self.headers, **(headers or {})},
                                        timeout=(10, 120), allow_redirects=False, **kwargs)
        except requests.RequestException as exc:
            raise StorageError('Could not reach Supabase Storage.') from exc
        if response.status_code not in allowed:
            code = response.status_code
            response.close()
            if code == 404 and method == 'GET':
                raise ObjectMissing('The MP4 was not found in Supabase Storage.')
            raise StorageError(f'Supabase Storage returned HTTP {code}. Check the bucket, server key and file size limit.')
        return response

    def path(self, key):
        return f'{self.bucket}/{quote(key, safe="/")}'

    def upload(self, key, path):
        with path.open('rb') as data:
            self.request('POST', '/object/' + self.path(key), data=data,
                         headers={'Content-Type': 'video/mp4', 'Cache-Control': 'no-store', 'x-upsert': 'true'},
                         allowed=(200, 201)).close()

    def open(self, key, byte_range=None):
        headers = {'Accept-Encoding': 'identity'}
        if byte_range:
            headers['Range'] = byte_range
        return self.request('GET', '/object/authenticated/' + self.path(key), headers=headers,
                            stream=True, allowed=(200, 206, 416))

    def delete(self, key):
        self.request('DELETE', '/object/' + self.bucket, json={'prefixes': [key]}, allowed=(200, 204)).close()
