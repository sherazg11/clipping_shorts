# Shorts Studio — video links, Twitch capture and story highlights

Upload an MP4/MOV → local Whisper transcription → transcript-based highlight ranking → FFmpeg vertical clips with burned-in captions → preview and review → Make → private YouTube upload → video ID saved in the dashboard.

## Workspace and downloads

The overview links to six dedicated pages that open in new browser tabs: Upload video, Import a video, Twitch Live, 45-second clips, Recordings, and YouTube queue.

In **Recordings**, choose **Download all** to save one ZIP of every available rendered MP4, regardless of the current filter. Each filename uses the saved clip title, with invalid filename characters replaced and duplicates numbered. On a ready clip, choose **Title & publishing → Save title** to change its filename before exporting. The ZIP does not include raw footage or checkpoint JSON. Keep enough free space in the server’s temporary directory for the archive.

## Choose a clip mode

- **Speech highlights (captioned):** Finds up to five distinct speech windows, choosing the best set at the highest feasible count. Sparse speech may still yield fewer clips.
- **Split by time (no captions):** For music or videos with little speech. Samples up to five separate 20–35 second excerpts across the recording; a source at least 1:40 long yields five. It does not cut or export every second of the original.

For the 60-minute progressive Twitch upgrade and rollback, see [LIVE_UPGRADE.md](LIVE_UPGRADE.md). The existing jobs and queue are preserved and new clips are never scheduled automatically.

## Upgrade your existing Windows install

1. Stop Uvicorn with Ctrl+C. Back up your `shorts-data` folder. Extract the ZIP and copy **app.py, sources.py, story.py, index.html, requirements.txt, and the entire ui/ folder** into `D:\shorts-studio`. Keep your existing `.env`, `.venv`, and `shorts-data`; the app adds columns to the existing database at startup. If you run a different installation path, use that path instead.
2. In PowerShell, run:

```powershell
cd D:\shorts-studio
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlink --version
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

3. Open `http://127.0.0.1:8000`. Use **Upload video** or **Import a video** first. Use a public HTTPS MP4/MOV link for direct imports, a Twitch channel for live capture, or a supported YouTube link with the optional YouTube import dependencies configured. Twitch replay pages and raw HLS links are not direct MP4/MOV imports. Standard signed storage URLs are supported if they are still valid and accessible without browser cookies. Only import or record material you have permission to republish. The original limits of 20 seconds–60 minutes, audio track required, and `MAX_UPLOAD_MB` still apply.
4. To try **Twitch Live**, enter an authorized live Twitch channel URL, enter 1–60 minutes, and choose **Story highlights**. Start with 5 minutes for a progressive preview test: clips can appear after the first roughly 2-minute window is saved and analyzed, while later windows are still recording. A one-minute capture is useful for basic connectivity, but ends before a full window can be previewed. A single Twitch capture runs at a time; CPU load can delay processing. Streams that end early keep their completed windows. This is not an always-on watcher or action-aware gameplay selector.
5. Preview each clip, use **Adjust start and end** to change the source timestamps and render a replacement, then queue the approved clip(s). Trimming is only enabled on newly processed sources and on clips that aren't queued or published. Existing clips and the Make webhook mappings are unchanged. The schedule still sends one queued clip at 9 AM, 10 AM and 4 PM in your configured timezone. Keep the PC, app, tunnel and Make scenario online for publishing. Existing queued clips remain active after upgrade.

### Better clip endings

**Story highlights** is the default on the dashboard. It uses Faster-Whisper word timestamps and sentence boundaries, prefers standalone openings and pauses, skips unfinished transcript tails, and selects up to five non-overlapping cuts. The default uses rules, not semantic reasoning; it can finish a sentence without capturing the full payoff. It works best with clear speech. For gameplay or music, **Split by time** gives distinct excerpts without interpreting the action. No mode guarantees publishable highlights; review before queueing.

For actual hook-to-payoff ranking, optionally install Ollama for Windows, run it locally, and pull a model, for example:

```powershell
ollama pull qwen2.5:3b
```

Add `STORY_MODEL=qwen2.5:3b` to `.env` and restart Uvicorn. The model receives nearby transcript sentences and picks inclusive sentence IDs. The app validates them against word timestamps and renders the cuts locally. No transcript is sent to Make or Ollama Cloud. Clear `STORY_MODEL` and restart to go back to rule-based selection. Ollama is optional and not installed by `pip`.

## 1. Run locally

Install Python 3.11 or 3.12 and a full FFmpeg build with `ffmpeg`, `ffprobe`, `libx264`, and the `subtitles` filter (libass). Put the binaries on PATH. SQLite must support `RETURNING` (3.35+). From this directory in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

macOS/Linux: run `python3 -m venv .venv`, then `.venv/bin/python -m pip install -r requirements.txt`, `cp .env.example .env`, and `.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000`.

Open `http://127.0.0.1:8000`. For the first test use a 2–4 minute talking-head video. The first transcription downloads the configured Whisper model; after it is cached, transcription runs locally on CPU without a paid AI API. Keep the app and your computer running; you can close the browser after upload. Use only one Uvicorn process. The dashboard stores files and SQLite in `shorts-data/`; back up or remove that folder yourself when appropriate. The classic selector and default Story mode are heuristic; either can return fewer than five clips. No speech or no eligible window produces a visible error.

## 2. Configure Make

Create a scenario: **Webhooks → Custom webhook → HTTP → Download a file → YouTube → Upload a Video → HTTP → Make a request**. Connect the correct YouTube channel. In the custom webhook add an API key and use that same key in `MAKE_WEBHOOK_API_KEY`. Copy the webhook URL into `MAKE_WEBHOOK_URL`. Use an HTTPS tunnel or a publicly reachable HTTPS reverse proxy in front of the local service; set its origin, without a trailing path, as `PUBLIC_BASE_URL`. Continue opening the dashboard on localhost so your original video upload does not go through the tunnel. Keep the tunnel online for both the download and callback. Set `APP_PASSWORD` and a different random `MAKE_CALLBACK_SECRET` in `.env`, then restart. Browser Basic Auth username is `admin`. Never commit `.env` or share your scenario logs without redacting the temporary file URLs and secrets. This is a single-user workspace and does not support public signup.

Use **Run once** to capture a sample payload from the app before mapping fields. For a dry capture, you can submit a clip from a disposable test job; that initial capture may leave its app status as `upload_requested` until a real successful callback. Use a fresh clip for the live test.

| Make module | Mapping |
| --- | --- |
| Webhooks — Custom webhook | Receives `clip_id`, `attempt_token`, `file_url`, `title`, `description`, `privacy`, `callback_url`. |
| HTTP — Download a file | URL = `file_url`. The link expires after one hour and stops working after the successful callback. |
| YouTube — Upload a Video | Use the file output (filename + binary data), not the URL. Map title and description. For the first test, set privacy to **Private**. To have Make honor `SCHEDULE_PRIVACY` and keep manual uploads private, map the incoming webhook field `privacy` to the module’s privacy field (enable mapping if needed). If you keep a fixed value in Make, that value controls every upload. Set the audience field truthfully for your channel/content if prompted. |
| HTTP — Make a request | Method POST. URL = your fixed `https://YOUR-HOST/make/callback` (or map `callback_url` only from the authenticated webhook). Set `Authorization: Bearer YOUR_MAKE_CALLBACK_SECRET`. Use a JSON request body with the three fields below. |

```json
{"clip_id":"MAP_WEBHOOK_CLIP_ID","attempt_token":"MAP_WEBHOOK_ATTEMPT_TOKEN","video_id":"MAP_YOUTUBE_VIDEO_ID"}
```

Don't add a Webhook Response module: the default acknowledgement indicates that the request was queued, not that the upload finished. Enable the scenario. Review a clip and click **Upload to YouTube (private)**. Confirm the returned video ID appears in the dashboard and that YouTube Studio shows the upload as private on the intended channel. Change visibility manually in YouTube Studio when ready. This app doesn't monitor YouTube's final transcoding status.

If the app remains on `upload_requested`, inspect the scenario history. If YouTube succeeded but the callback failed, retry only the callback (with the original video ID and attempt token); don't replay the upload. If nothing uploaded, use a fresh test clip/job after confirming there is no pending execution. Automatic retries and an administrative reset interface are intentionally not included. Keep each video sent to Make below 5 MB on Free (Core allows 100 MB). The app defaults to conservative 540×960, capped-size output for Free; `MAKE_MAX_FILE_BYTES` changes the byte budget for new renders.

## 3. Deployment boundary

This package runs locally and uses SQLite and the filesystem. Do not deploy this exact version as a Render Free web service: files are not durable there and the service sleeps. A hosted revision would split the dashboard/API from the worker, replace local files with private object storage (e.g. R2, uploaded directly from the browser), and replace SQLite with a persistent Postgres queue. Render can host the dashboard/API; the FFmpeg/Whisper worker stays on your computer or moves to a paid worker. Make remains the YouTube uploader in both setups.

Vendor documentation: https://apps.make.com/youtube · https://help.make.com/working-with-files · https://help.make.com/webhooks · https://render.com/docs/free


## 4. Upgrade an existing Windows install and use the daily queue

1. Stop Uvicorn with Ctrl+C. Back up `shorts-data/`. Extract the ZIP elsewhere and copy `app.py`, `index.html`, and `requirements.txt` into your existing `D:\shorts-studio` folder. Keep `.env`, `.venv`, and `shorts-data/`. The app adds the scheduling columns to the existing SQLite database on startup.
2. From `D:\shorts-studio`, run `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`. This installs timezone data for Windows and pins PyAV to fix the Whisper `metadata_errors` compatibility problem.
3. Optionally add `SCHEDULE_TIMEZONE=America/New_York` and `SCHEDULE_PRIVACY=private` to `.env` (these are the defaults). Use `Asia/Karachi` for Pakistan time. Use `SCHEDULE_PRIVACY=public` only after a successful private test, with Make configured to map the webhook's `privacy` field.
4. Start the app as above. Upload a source video, preview the extracted clips, then choose **Add to daily queue** per clip or **Queue all ready clips** for the project. The latter uses saved/generated titles; it does not submit unsaved text typed into individual cards. **Remove from schedule** returns a waiting clip to ready. The first failed transcription is not replayed automatically; re-upload that source after the dependency fix.
5. Keep your computer awake, the app running, and the Cloudflare tunnel and Make scenario active. The scheduler runs in Shorts Studio; keep the Make scenario on its incoming webhook, not a second three-times-a-day timer. Verify the dashboard shows the intended timezone and that Make's callback works before relying on scheduled delivery.

At 09:00, 10:00 and 16:00 in the configured timezone, the app sends **one** queued clip, oldest first. Each slot can be claimed once, even across restarts. The app checks every two seconds and allows a five-minute dispatch window; after that a missed slot is skipped, and unsent clips remain queued for the next slot. The clock controls the handoff to Make, not when YouTube finishes processing. If Make's acceptance is uncertain, check scenario history; do not automatically replay or manually reupload before verifying whether the original attempt succeeded. A running app does not probe whether the configured tunnel URL is still live, so after restarting a Quick Tunnel, update the URL and restart the app. A public visibility setting does not bypass YouTube processing or any channel/API restrictions.
