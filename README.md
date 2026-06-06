# Football Highlighter Live

`football-highlighter-live` is a Python service that watches a live football stream, detects goal moments, and writes highlight clips plus structured goal metadata.

It ships with a local dashboard and supports two operating modes:

- `API mode`: poll the football-data.org live matches API, detect newly reported goals, and align them to the on-screen timer before clipping.
- `Stream-only mode`: skip the external API and detect confirmed score-value changes directly from the broadcast scoreboard.

The dashboard is local-only and is served by `npm run dev`.

## What The Pipeline Does

1. `StreamRecorder` keeps a rolling disk buffer of short stream segments.
2. `FrameSampler` reads frames from the same stream at a low OCR-friendly FPS.
3. OCR reads the timer and scoreboard overlay.
4. Match resolution maps the current stream to the correct live match.
5. Goal detection creates a pending clip job.
6. `ClipExtractor` cuts a raw highlight window from buffered segments.
7. `ClipCropper` optionally creates a second cropped highlight using YOLO-based subject tracking.
8. JSON stores persist seen goals, processed clips, lock state, and per-match event history.

## Detection Modes

### API Mode

Use this when you have a football-data.org token and want official match metadata.

- Polls `GET /v4/matches?status=LIVE`
- Detects new goals from the API payload
- Falls back to score-delta inference if the API payload has updated scores but no goal event list
- Uses OCR of the on-screen timer and scoreboard to resolve which live match the stream belongs to
- Waits for the timer to match the expected goal minute before extracting the clip

### Stream-Only Mode

Use this when you only have the broadcast stream.

- Watches only the configured home and away score digit ROIs
- Reads the score values with OpenCV preprocessing plus local Tesseract digit OCR
- Requires several stable parsed frames before confirming a change
- Creates a confirmed highlight only when exactly one score increases by one
- Ignores timer changes, extra time, aggregate-score text, scoreboard disappearance, and scoreboard reappearance
- Saves uncertain candidates separately when OCR is weak or the score transition is not a normal `+1`
- Tracks confirmed goals for a configurable VAR window and creates a separate VAR reversal clip if the score reverts
- Emits synthetic match metadata such as `stream_home` / `stream_away` unless you override it in config

## Repository Layout

- `app/main.py`: CLI entry point
- `app/pipeline/orchestrator.py`: main control loop
- `app/stream/`: stream recording, frame sampling, rolling buffer
- `app/vision/`: timer OCR, scoreboard OCR, match resolution, score change detection, clip cropping
- `app/api/`: football-data client and goal detection
- `app/storage/`: JSON-backed runtime state and goal event stores
- `app/ui/`: local dashboard API, process supervisor, clip library, and static frontend
- `configs/config.example.yaml`: baseline configuration
- `models/soccer_yolov8s.pt`: object detector used for optional cropped highlights
- `tests/`: unit tests for timer parsing, goal dedup, clip windows, config loading, match resolution, cropper selection, and score-change detection

## Requirements

- Python 3.11 recommended
- `ffmpeg` installed and available on `PATH`
- A reachable HTTP video stream in `STREAM_URL`
- Optional: football-data.org API token for API mode
- Optional: a local Tesseract install if you want `pytesseract` fallback when EasyOCR is unavailable

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp configs/config.example.yaml configs/config.yaml
```

Then edit `.env` and `configs/config.yaml` for your stream and preferred mode.

## Environment Variables

```env
FOOTBALL_DATA_API_TOKEN=...
FOOTBALL_DATA_BASE_URL=https://api.football-data.org/v4
STREAM_URL=http://127.0.0.1:6878/ace/getstream?id=...
```

`FOOTBALL_DATA_API_TOKEN` is only needed for API mode. `STREAM_URL` is always required.

## Configuration

The config file is loaded from `configs/config.yaml` by default.

Important sections:

- `api`: football-data polling, timeout, and retry settings
- `stream`: sampling FPS, reconnect delay, rolling buffer size, segment length
- `highlight`: seconds before and after a detected goal plus timer tolerance
- `vision`: OCR ROIs for timer and scoreboard
- `stream_only`: score ROIs and synthetic match labels
- `score_ocr`: score digit stability, confidence, Tesseract command, and temporary OCR folder
- `var`: VAR/no-goal watch window and reversal clip length
- `crop`: cropped highlight behavior, YOLO model path, target classes, worker count
- `output`: raw clip, cropped clip, custom categories, state, goal, and temp directories

## Running The Local UI

Start the local engine, UI, and highlighter in dry-run mode:

```bash
npm run dev
```

Open:

```text
http://127.0.0.1:5174
```

The dashboard shows AceStream/highlighter status, recent JSON logs, a stream preview/open action, a local channel catalog, and a clip library for raw, cropped, uncertain, VAR, and custom categories. You can add a stream by pasting a bare AceStream ID, an `acestream://...` link, or an existing local `getstream?id=...` URL into the Stream panel. Rename, delete, and move actions change files directly on disk and are guarded by backend path checks.

### Local Channel Catalog

Channels are stored locally in `data/state/channels.json`. The dashboard can add, delete, search, filter by language, sort higher-quality entries first, and set the active stream from a channel row.

Manual entries accept the same stream input formats as the Stream panel. Optional automatic refresh imports only configured JSON catalogs; it does not scrape arbitrary web pages. In the Channels panel, paste a comma-separated JSON catalog URL or file path into the catalog source field and click `Check`; the browser remembers those sources and re-checks them every minute while the page is open.

You can also configure lawful catalog sources with comma-separated URLs or file paths before starting the server:

```bash
CHANNEL_CATALOG_URLS="https://example.test/channels.json,/absolute/path/to/channels.json" npm run dev
```

Catalog JSON can be either a list or an object with a `channels` list:

```json
{
  "channels": [
    {
      "name": "Example Channel",
      "stream": "acestream://e38b33c56332de27ff25df223cdf488b1ec6051f/",
      "language": "en",
      "quality": "1080p",
      "source": "local-catalog"
    }
  ]
}
```

The UI refreshes configured catalogs every minute while open, and the server also refreshes configured sources every minute while running.

To create real clips instead of dry-run events:

```bash
npm run dev:clips
```

Useful UI environment variables:

- `UI_HOST`: dashboard bind host, default `127.0.0.1`
- `UI_PORT`: dashboard port, default `5174`
- `DRY_RUN`: set `0` to enable clipping
- `CHANNEL_CATALOG_URLS`: comma-separated lawful JSON catalog URLs or file paths
- `CHANNEL_REFRESH_SECONDS`: server-side catalog refresh interval, default `60`

## Running The CLI Directly

Default run:

```bash
python -m app.main --config configs/config.yaml
```

Useful CLI flags:

- `--stream-url`: override `STREAM_URL`
- `--manual-match-id`: lock to a specific live match id
- `--dry-run`: record goal events without clipping
- `--stream-only`: force stream-only detection
- `--input-video`: process a local video file without AceStream
- `--calibrate-score-rois`: write ROI preview images and exit
- `--log-level`: console log level, default `DEBUG`
- `--log-file`: JSON log file path, default `data/state/runtime.log`

Example:

```bash
python -m app.main \
  --config configs/config.yaml \
  --stream-only \
  --stream-url "http://127.0.0.1:8090/live.ts" \
  --dry-run \
  --log-level DEBUG \
  --log-file data/state/runtime.log
```

Calibrate score ROIs from a local sample or recorded clip:

```bash
python -m app.main \
  --config configs/config.yaml \
  --input-video data/clips_raw/sample.mp4 \
  --calibrate-score-rois data/tmp/score_roi_preview
```

Offline validation against a local video:

```bash
python -m app.main \
  --config configs/config.yaml \
  --input-video data/samples/match_sample.mp4 \
  --stream-only
```

## Output

Generated runtime files are intentionally ignored by Git.

- `data/clips_raw/`: extracted raw highlight windows
- `data/clips_cropped/`: cropped highlight clips
- `data/clips_uncertain/`: raw candidate clips where OCR/score transition was not strong enough to confirm a goal
- `data/clips_var/`: VAR/no-goal score-reversal clips
- `data/clips_categories/`: custom dashboard categories backed by folders
- `data/goals/<match_id>.json`: append-or-update event history per match
- `data/state/runtime_state.json`: dedup state, processed goals, score cache, match lock
- `data/state/runtime.log`: structured runtime logs
- `data/tmp/`: concat files and rolling stream segments

Raw clip names follow this pattern:

```text
YYYY-MM-DD_match<match_id>_<home>_vs_<away>_goal<index>_<minute>_<injury>_raw.mp4
```

Cropped clips reuse the same base name and append the configured suffix, default `_crop1x1`.

## Notes On Cropping

- Cropping is optional through `crop.enabled`
- The cropper uses `models/soccer_yolov8s.pt`
- It follows the ball when detected, otherwise follows a smoothed player/action cluster
- The square crop path is rendered dynamically across the clip instead of using one static crop box
- If YOLO is unavailable, the model is missing, or detection fails, the crop falls back to a centered square path
- If crop processing is disabled, the raw clip is copied to the cropped output path

## Rate Limiting

The football-data client is built to behave conservatively on the free plan:

- default poll interval is 6 seconds
- reads `X-RequestsAvailable` and `X-RequestCounter-Reset` headers
- backs off on `429` responses and retries with reset-aware timing when possible

## Development

Run the test suite with:

```bash
pytest -q
```

## Known Limitations

- Score OCR accuracy depends heavily on the score ROI calibration, stream quality, and scoreboard design
- Team-name OCR can be weak on unusual overlays, which reduces match-resolution confidence
- Stream-only mode confirms score increases, not semantic scorer metadata, so minute/scorer fields are synthetic unless API mode is used
- Bad OCR and unusual score transitions are saved to the uncertain folder instead of being exported as confirmed highlights
- Segment concatenation may need a re-encode fallback when the source stream changes codec parameters between chunks
- Generated media can grow quickly; keep `data/` and local recordings out of Git
