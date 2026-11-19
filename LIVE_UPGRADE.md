# Shorts Studio: 60-minute Twitch capture and progressive previews

## Install over your existing Windows project

1. Stop Uvicorn with Ctrl+C. Back up the existing code and entire active data directory (`shorts-data` by default). Back up `.env` securely. Keep the tunnel off for the first test; `PUBLIC_BASE_URL` should remain blank for a local-only test. Do not copy credentials from an old example.
2. Copy `app.py`, `sources.py`, and `index.html` from this ZIP over the existing files. Copy the new docs and tests if wanted. Keep `.env`, `.venv`, and `shorts-data` as they are. The new SQLite columns are added on startup. No new Python dependency is required.
3. From the project folder, verify and run with the same PowerShell window:

```powershell
ffprobe -version | Select-Object -First 1
ffmpeg -version | Select-Object -First 1
.\.venv\Scripts\python.exe -m streamlink --version
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

If FFmpeg is installed but not found, refresh the current session from saved Windows PATH entries:

```powershell
$env:Path += ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
```

Open `http://127.0.0.1:8000`. For a first test choose **5 minutes** and **Story highlights** for a live channel you can record and republish. Keep the PC awake. First previews appear when the first ~2-minute window finishes transcription and rendering, not exactly at the 2-minute mark. Then submit 60 minutes. A failed legacy job will not be retried.

## Behavior and limitations

- The app records the requested duration from a current live stream, not the prior hour of a broadcast. Stream interruptions, missing audio/video, disk limits, or machine shutdown can shorten the session. Completed, queued windows remain usable. Raw windows and rendered clips stay on disk. `MAX_LIVE_MB` defaults to `5000` (decimal MB per session, separate from `MAX_UPLOAD_MB` and `MAKE_MAX_FILE_BYTES`); no setting change is required to use the default. Leave extra disk space for other jobs and renders.
- FFmpeg reads MPEG-TS or fragmented MP4 from Streamlink, resets timestamps per output window, and bounds capture independently of the stream's original timeline. Windows are approximately two minutes, split at keyframes, with no overlap. Short tails remain recorded but may not yield clips. Clip times/trim ranges are relative to the window. New windows show up without stopping playback of a different window.
- **Story highlights** ranks spoken ideas, not visual gameplay events. It can miss stories crossing window boundaries or return no candidate. Optional local `STORY_MODEL` (Ollama) can improve context/payoff ranking; it is not required. CPU/AI backlog may delay previews and may continue after capture ends.
- Publishing and schedule behavior are unchanged. Clips are previews until explicitly queued or uploaded. Do not expose a tunnel with secrets that have been shared; rotate them first.

## Verification and rollback

Offline checks: `live_test.py` exercises 60-minute API intake, real FFmpeg with simulated fragmented MP4 and TS producers, preview and trim before simulated stream EOF, early end, and the media time cap. `source_test.py`, `smoke_test.py`, and `webhook_test.py` cover the existing paths. Actual Twitch connectivity, Windows runtime behavior, real Whisper/Ollama inference, and live YouTube delivery are not verified by these tests.

To roll back, stop Uvicorn and restore the matching pre-upgrade code and data snapshot. This discards jobs created after the snapshot. Reconcile any Make/YouTube attempts made since the snapshot before resuming the queue, to avoid duplicate uploads.
