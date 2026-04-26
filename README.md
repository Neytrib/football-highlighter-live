# Football Highlighter Live

`football-highlighter-live` is a Python service that watches a live football stream, detects goal moments, and writes highlight clips plus structured goal metadata.

It supports two operating modes:

- `API mode`: poll the football-data.org live matches API, detect newly reported goals, and align them to the on-screen timer before clipping.
- `Stream-only mode`: skip the external API and detect score changes directly from the broadcast scoreboard using OpenCV frame differencing.

The project is a backend CLI service. It does not ship a web UI.

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

- Watches the score digits inside configured ROIs
- Can auto-locate home and away score boxes at startup
- Requires several stable frames before confirming a change
- Applies a cooldown window to reduce duplicate triggers
- Emits synthetic match metadata such as `stream_home` / `stream_away` unless you override it in config

## Repository Layout

- `app/main.py`: CLI entry point
- `app/pipeline/orchestrator.py`: main control loop
- `app/stream/`: stream recording, frame sampling, rolling buffer
- `app/vision/`: timer OCR, scoreboard OCR, match resolution, score change detection, clip cropping
- `app/api/`: football-data client and goal detection
- `app/storage/`: JSON-backed runtime state and goal event stores
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
- `stream_only`: score ROIs, auto-locate settings, thresholds, cooldown, synthetic match labels
- `crop`: cropped highlight behavior, YOLO model path, target classes, worker count
- `output`: raw clip, cropped clip, state, goal, and temp directories

## Running

Default run:

```bash
python -m app.main --config configs/config.yaml
```

Useful CLI flags:

- `--stream-url`: override `STREAM_URL`
- `--manual-match-id`: lock to a specific live match id
- `--dry-run`: record goal events without clipping
- `--stream-only`: force stream-only detection
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

## Output

Generated runtime files are intentionally ignored by Git.

- `data/clips_raw/`: extracted raw highlight windows
- `data/clips_cropped/`: cropped highlight clips
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
- The cropper prefers detections for the `ball` class from `models/soccer_yolov8s.pt`
- If YOLO is unavailable, the model is missing, or detection fails, the crop falls back to the frame center
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

- OCR accuracy depends heavily on stream quality and scoreboard design
- Team-name OCR can be weak on unusual overlays, which reduces match-resolution confidence
- Stream-only mode detects score changes, not semantic goal events, so minute metadata is approximate
- Segment concatenation may need a re-encode fallback when the source stream changes codec parameters between chunks
- Generated media can grow quickly; keep `data/` and local recordings out of Git
