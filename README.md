# Pip Server

A Flask server for receiving WAV voice notes, transcribing them with OpenAI Whisper, and displaying the resulting notes in a small web UI.

## Requirements

- Python 3.11+
- `ffmpeg` available on the system
- Docker, or a local Python environment

## Configuration

| Variable | Default | Description |
|---|---|---|
| `API_TOKEN` | required | Bearer token accepted by `POST /audio` |
| `WHISPER_MODEL` | `base` | Whisper model to use, such as `tiny`, `small`, or `base` |
| `DATA_DIR` | `./data` | Directory containing the SQLite database and audio files |
| `PORT` | `8080` | Flask listening port |

`API_TOKEN` must be set. The server refuses to start when it is missing.

Whisper downloads the configured model the first time an audio file is transcribed. Persist `DATA_DIR` so the SQLite database, recordings, and downloaded model cache can be retained as appropriate for the deployment.

## Run with Docker

Build the image:

```sh
docker build -t pip-server .
```

Run it with persistent note storage:

```sh
docker run --rm \
  -p 8080:8080 \
  -e API_TOKEN='replace-with-a-long-random-token' \
  -v pip-server-data:/app/data \
  pip-server
```

The web UI is available at `http://localhost:8080/`.

## Run locally

Install dependencies and start the server:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export API_TOKEN='replace-with-a-long-random-token'
python3 app.py
```

Install `ffmpeg` separately if it is not already installed.

## API

### `POST /audio`

Uploads one WAV recording. The request must use a Bearer token and include an ISO-8601 timestamp.

```sh
curl -X POST http://localhost:8080/audio \
  -H 'Authorization: Bearer replace-with-a-long-random-token' \
  -F 'created_at=2026-08-31T12:00:00Z' \
  -F 'file=@recording.wav;type=audio/wav'
```

A successful upload returns `202 Accepted` immediately while transcription runs in the background:

```json
{
  "created_at": "2026-08-31T12:00:00+00:00",
  "id": "note-id",
  "status": "processing"
}
```

The endpoint returns `401` for a missing or invalid token. Invalid form data returns `400`.

### `GET /notes/<id>`

Returns the current transcription status and transcript:

```json
{
  "created_at": "2026-08-31T12:00:00+00:00",
  "error": null,
  "id": "note-id",
  "status": "complete",
  "transcript": "Recognised note text"
}
```

`status` is one of `processing`, `complete`, or `failed`.

### `GET /audio/<id>`

Streams the stored WAV recording for the UI audio player.

### `GET /health`

Returns `{ "status": "ok" }`.

### `GET /health/audio`

Checks that the server is alive and the Bearer token is valid:

```sh
curl http://localhost:8080/health/audio \
  -H 'Authorization: Bearer replace-with-a-long-random-token'
```

The endpoint returns `200 Connected` for a valid token and `401 Unauthorized` for a missing or invalid token.

## Reverse proxy

Terminate HTTPS and apply any additional access controls at the reverse proxy. The upload API still requires the `Authorization: Bearer <API_TOKEN>` header even when the proxy itself does not authenticate the audio endpoint.

Do not expose the API token in client-side HTML or URLs. Keep the data volume private and use a long, randomly generated token.
