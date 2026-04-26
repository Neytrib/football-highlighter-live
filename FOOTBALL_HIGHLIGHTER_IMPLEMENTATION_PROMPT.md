# Goal Highlighter

Use this as a **copy-paste implementation prompt** for another AI coding assistant.

---

You are a senior Python engineer. Build a production-ready project called **Goal Highlighter**.

## 1) Objective
Create a Python system that:
1. Polls football-data.org live match data.
2. Detects new goals from API responses and writes structured goal events to JSON.
3. Continuously records an Ace Stream live feed with a rolling buffer (last 3-5 minutes, configurable) so no goal is missed.
4. Uses OpenCV + OCR to read the on-screen match timer (usually top-left) from the live stream.
5. When a goal event minute matches the stream timer, cuts a highlight clip with strict configurable windows:
   - `pre_goal_seconds` default `45`
   - `post_goal_seconds` default `30`
   - default clip length `75s`
6. Saves the raw clip (`.mp4`) and also a cropped clip (1:1 by default, configurable) using `models/soccer_yolov8s.pt`.
7. Handles live streams end-to-end with reconnect/retry logic.

The stream URL format is like:
`http://127.0.0.1:6878/ace/getstream?id=<acestream_content_id>`

Ace Engine setup is already handled externally (Docker already running), so do not implement Ace Engine provisioning.

## 2) Mandatory API Provider + Docs Requirements
Use:
- Quickstart: https://www.football-data.org/documentation/quickstart
- Python coding docs: https://docs.football-data.org/general/v4/coding/python.html
- Match resource docs: https://docs.football-data.org/general/v4/match.html
- API policies (throttling + folding): https://docs.football-data.org/general/v4/policies.html
- Lookup tables (headers/status/filters): https://docs.football-data.org/general/v4/lookup_tables.html

Implement according to docs:
- Auth header: `X-Auth-Token`
- Main live endpoint: `GET /v4/matches?status=LIVE`
- For goal arrays in match-list responses, send header: `X-Unfold-Goals: true` (because of automatic folding in v4 list resources)
- Free plan throttle: max 10 requests/minute
- Poll interval default: every 6 seconds (configurable)
- Respect response headers:
  - `X-RequestsAvailable`
  - `X-RequestCounter-Reset`
- Implement backoff and never exceed rate limits

## 3) Secrets + Environment
Create `.env` and load with `python-dotenv`:

```env
FOOTBALL_DATA_API_TOKEN=c3054115bf244c3d90b3567959ef6295
FOOTBALL_DATA_BASE_URL=https://api.football-data.org/v4
STREAM_URL=http://127.0.0.1:6878/ace/getstream?id=REPLACE_ME
```

Add `.env` to `.gitignore`.

## 4) Critical Note About Goal Time Precision
football-data v4 goal objects provide `minute` and `injuryTime`, but usually not exact goal seconds. Implement this strategy:
1. Use API goal minute as the scheduling anchor.
2. Use OCR stream timer + OCR scoreline change to infer exact second in-stream.
3. If scoreline detection is unreliable, trigger when timer enters target minute with tolerance.
4. Always still clip `pre_goal_seconds` before and `post_goal_seconds` after the detected goal timestamp.

## 5) Match Auto-Identification (No Manual Match ID Required)
Implement automatic stream-to-match mapping:
1. From stream frames, OCR scoreboard team labels/abbreviations (home left, away right).
2. Poll live matches from `/v4/matches?status=LIVE`.
3. Fuzzy-match OCR names against API fields:
   - `homeTeam.name`, `homeTeam.shortName`, `homeTeam.tla`
   - `awayTeam.name`, `awayTeam.shortName`, `awayTeam.tla`
4. Confirm with OCR scoreline when available.
5. Lock onto best candidate and keep confidence score.
6. Re-validate periodically in case of wrong lock.
7. Keep optional manual override config (`manual_match_id`) only as fallback.

Use `rapidfuzz` for fuzzy matching.

## 6) Tech Stack
Use Python 3.11+ and these libraries:
- `requests`
- `python-dotenv`
- `opencv-python`
- `ultralytics`
- `rapidfuzz`
- `pydantic` (or dataclasses)
- `ffmpeg` via subprocess/ffmpeg-python
- OCR library: `easyocr` preferred (or `pytesseract` fallback)

System dependency required: `ffmpeg` installed and available in PATH.

## 7) Project Structure (Implement Exactly)

```text
football-highlighter/
  app/
    __init__.py
    main.py
    config.py
    logging_setup.py
    models.py
    api/
      __init__.py
      football_data_client.py
      goal_detector.py
    stream/
      __init__.py
      recorder.py
      rolling_buffer.py
      frame_sampler.py
    vision/
      __init__.py
      timer_ocr.py
      scoreboard_ocr.py
      match_resolver.py
      cropper.py
    pipeline/
      __init__.py
      orchestrator.py
      clip_scheduler.py
      clip_extractor.py
    storage/
      __init__.py
      json_store.py
      state_store.py
  configs/
    config.example.yaml
  data/
    goals/
    state/
    clips_raw/
    clips_cropped/
    tmp/
  models/
    soccer_yolov8s.pt
  tests/
    test_goal_dedup.py
    test_timer_parser.py
    test_clip_windows.py
    test_match_resolver.py
  requirements.txt
  .env.example
  .gitignore
  README.md
```

## 8) Data Contracts

### `data/goals/<match_id>.json`
Store append-only goal events:

```json
{
  "match_id": 123456,
  "home_team": "Team A",
  "away_team": "Team B",
  "events": [
    {
      "goal_id": "123456_54_0_home_2_1",
      "source": "football-data-v4",
      "api_detected_at_utc": "2026-03-15T18:20:10Z",
      "minute": 54,
      "injury_time": 0,
      "target_minute_label": "54",
      "team": "Team A",
      "scorer": "Player X",
      "assist": "Player Y",
      "goal_type": "REGULAR",
      "score_after_goal": {"home": 2, "away": 1},
      "ocr_goal_time": "54:13",
      "stream_goal_ts_unix": 1773598813.42,
      "clip_status": "CREATED",
      "raw_clip_path": "data/clips_raw/...mp4",
      "cropped_clip_path": "data/clips_cropped/...mp4"
    }
  ]
}
```

`goal_id` must be deterministic and dedup-safe.

## 9) Runtime Configuration
Use YAML + env overrides:

```yaml
api:
  poll_interval_seconds: 6
  unfold_goals: true
  timeout_seconds: 10
  max_retries: 5
  backoff_base_seconds: 1.5

stream:
  read_fps_for_ocr: 2
  reconnect_delay_seconds: 3
  rolling_buffer_seconds: 300
  segment_seconds: 2

highlight:
  pre_goal_seconds: 45
  post_goal_seconds: 30
  timer_match_tolerance_seconds: 2
  require_score_change_confirmation: true

vision:
  timer_roi: [0.0, 0.0, 0.28, 0.12]
  scoreboard_roi: [0.0, 0.0, 1.0, 0.16]
  ocr_confidence_min: 0.55

crop:
  enabled: true
  aspect_ratio: "1:1"
  detector_model_path: "models/soccer_yolov8s.pt"
  detection_frame_stride: 3
  smoothing_alpha: 0.25
  output_suffix: "_crop1x1"

output:
  raw_dir: "data/clips_raw"
  cropped_dir: "data/clips_cropped"
  goals_dir: "data/goals"
  state_dir: "data/state"
```

## 10) Core Pipeline Behavior

### A. API Poller
- Every `poll_interval_seconds`, call `/v4/matches?status=LIVE` with headers:
  - `X-Auth-Token`
  - `X-Unfold-Goals: true`
- Parse `matches[].goals[]`.
- Deduplicate goals and create pending clip jobs.
- Handle `429` using reset header + exponential backoff.

### B. Continuous Stream Buffer
- Keep stream always active.
- Maintain rolling disk buffer using short segments (e.g., 2s each), pruning old segments older than `rolling_buffer_seconds`.
- Store segment metadata (start_ts, end_ts, file path).

### C. OCR Loop
- Sample frames at low FPS for efficiency.
- Read timer text (`MM:SS`, `45+2`, `90+3`, etc.) and parse into normalized timeline value.
- Read scoreline and team text if possible.

### D. Goal Trigger Logic
For each pending goal event:
1. Wait until OCR timer reaches target minute window.
2. Prefer exact trigger at moment OCR scoreline equals API `score_after_goal`.
3. If scoreline unavailable, trigger when timer reaches minute with tolerance.
4. Compute clip window:
   - `start_ts = goal_ts - pre_goal_seconds`
   - `end_ts = goal_ts + post_goal_seconds`

### E. Clip Extraction
- Assemble clip from rolling segments intersecting `[start_ts, end_ts]`.
- Output raw `.mp4`.
- Guarantee playable video (re-encode if needed).

### F. 1:1 Crop Using `soccer_yolov8s.pt`
- Load detector from `models/soccer_yolov8s.pt`.
- Run detections across sampled clip frames.
- Compute crop center trajectory from detections (smoothed).
- Produce cropped video with configurable aspect ratio (default 1:1).
- Save both raw and cropped outputs.

## 11) Naming Convention for Clips
Use deterministic names:

`<utcdate>_match<id>_<home>_vs_<away>_goal<index>_<minute>_<injury>.mp4`

Examples:
- raw: `2026-03-15_match123456_teama_vs_teamb_goal2_54_0_raw.mp4`
- crop: `2026-03-15_match123456_teama_vs_teamb_goal2_54_0_crop1x1.mp4`

## 12) Reliability + Recovery
- Full retry handling for API/network/stream disconnects.
- Persist state so restart does not duplicate clips.
- If app restarts mid-match, continue from saved goals and segment index.
- Log all key events as JSON logs.

## 13) CLI
Provide CLI entrypoint:

```bash
python -m app.main --config configs/config.yaml
```

Optional overrides:
- `--stream-url`
- `--manual-match-id`
- `--dry-run` (no clipping)

## 14) Tests (Required)
Implement tests for:
1. Goal dedup key generation.
2. Timer text parsing (`45+2`, `90+4`, malformed OCR).
3. Clip window math (45 before / 30 after and configurable values).
4. Match resolver fuzzy scoring.

## 15) README Requirements
README must include:
- Setup steps (Python + ffmpeg)
- `.env` + config setup
- How to run
- Output folder explanation
- Rate-limit behavior explanation
- Known limitations (OCR quality, scoreboard format variance)

## 16) Acceptance Criteria (Must Pass)
1. App runs continuously on Ace stream URL without manual match ID in normal cases.
2. API is polled roughly every 6s by default without violating 10 req/min cap.
3. New goals are stored in JSON with minute/injury and metadata.
4. For each goal, raw clip is generated with default 75s window (45+30).
5. Cropped 1:1 clip is also generated from `soccer_yolov8s.pt`.
6. On restart, app does not duplicate already-processed goals.

## 17) Implementation Notes
- Keep code modular and typed.
- Prefer explicit classes for `ApiClient`, `GoalDetector`, `Recorder`, `TimerOCR`, `ClipExtractor`, `Cropper`, `Orchestrator`.
- Use UTC timestamps internally.
- Keep all timing and ROI values configurable.

Build the complete codebase now.
