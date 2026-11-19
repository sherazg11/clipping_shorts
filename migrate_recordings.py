"""Move existing library MP4s to configured Supabase Storage with Uvicorn stopped."""
import argparse
import hashlib

import requests

import app
from cloud_storage import StorageError


def migrate(remove_local=False):
    if not app.STORAGE:
        raise SystemExit('Set SUPABASE_URL and SUPABASE_SECRET_KEY in .env before migrating.')
    with app.db() as db:
        rows = db.execute('SELECT * FROM clips ORDER BY job, start, id').fetchall()
    failed = moved = 0
    for row in rows:
        if row['cloud_cleanup'] in ('pending', 'removed'):
            continue
        path = app.clip_file(row)
        if row['storage_key'] and (not remove_local or not path.is_file()):
            continue
        if not path.is_file():
            print(f"Skipped {row['id']}: local MP4 missing.")
            failed += 1
            continue
        key = row['storage_key'] or app.storage_path(row['job'], row['id'])
        try:
            with path.open('rb') as local:
                expected = hashlib.file_digest(local, 'sha256').digest()
            size = path.stat().st_size
            if not row['storage_key']:
                app.STORAGE.upload(key, path)
            actual = hashlib.sha256()
            with app.STORAGE.open(key) as response:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    actual.update(chunk)
            if actual.digest() != expected:
                raise StorageError('Cloud and local bytes differ; kept the local file.')
            if not row['storage_key']:
                with app.db() as db:
                    db.execute('UPDATE clips SET storage_key=?, storage_size=? WHERE id=?', (key, size, row['id']))
                moved += 1
            if remove_local:
                path.unlink()
            print(f"Verified {row['id']}. " + ('Local MP4 removed.' if remove_local else 'Local backup retained.'))
        except (StorageError, requests.RequestException, OSError) as exc:
            message = str(exc) if isinstance(exc, StorageError) else 'Could not read, write, or verify the file.'
            print(f"Could not migrate or clean up {row['id']}: {message}")
            failed += 1
    print(f'{moved} newly migrated; {failed} skipped or failed.')
    return int(bool(failed))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remove-local', action='store_true', help='Remove an MP4 only after verifying the cloud copy.')
    raise SystemExit(migrate(parser.parse_args().remove_local))
