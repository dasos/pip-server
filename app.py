import logging
import os
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from faster_whisper import WhisperModel
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
AUDIO_DIR = DATA_DIR / "audio"
MODEL_DIR = DATA_DIR / "models"
DATABASE = DATA_DIR / "notes.sqlite3"
MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")
API_TOKEN = os.environ.get("API_TOKEN")

app = Flask(__name__)
app.logger.setLevel(logging.INFO)
transcription_queue = queue.Queue()
model = None
model_lock = threading.Lock()


def get_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with get_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                filename TEXT NOT NULL,
                audio_path TEXT NOT NULL,
                transcript TEXT,
                status TEXT NOT NULL,
                error TEXT,
                received_at TEXT NOT NULL
            )
            """
        )


def get_model():
    global model
    if model is None:
        with model_lock:
            if model is None:
                app.logger.info("Loading Whisper model %s", MODEL_NAME)
                model = WhisperModel(
                    MODEL_NAME,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(MODEL_DIR),
                    cpu_threads=1,
                )
                app.logger.info("Whisper model %s loaded", MODEL_NAME)
    return model


def authorized():
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return scheme.lower() == "bearer" and token == API_TOKEN


def validate_created_at(value):
    if not value:
        raise ValueError("created_at is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("created_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def transcribe(note_id, audio_path):
    try:
        segments, _ = get_model().transcribe(str(audio_path))
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
        with get_db() as connection:
            connection.execute(
                "UPDATE notes SET transcript = ?, status = 'complete' WHERE id = ?",
                (transcript, note_id),
            )
    except Exception as error:
        app.logger.exception("Audio processing failed for %s", note_id)
        with get_db() as connection:
            connection.execute(
                "UPDATE notes SET status = 'failed', error = ? WHERE id = ?",
                (str(error), note_id),
            )


def recover_processing_jobs():
    with get_db() as connection:
        notes = connection.execute(
            "SELECT id, audio_path FROM notes WHERE status = 'processing'"
        ).fetchall()
        for note in notes:
            audio_path = Path(note["audio_path"])
            if audio_path.is_file():
                transcription_queue.put((note["id"], audio_path))
                app.logger.info("Requeued transcription %s", note["id"])
            else:
                connection.execute(
                    "UPDATE notes SET status = 'failed', error = ? WHERE id = ?",
                    ("Audio file is missing", note["id"]),
                )
                app.logger.error("Cannot requeue %s: audio file is missing", note["id"])
        if notes:
            app.logger.info("Recovered %d transcription job(s)", len(notes))


def worker():
    app.logger.info("Transcription worker started")
    while True:
        note_id, audio_path = transcription_queue.get()
        app.logger.info("Starting transcription %s", note_id)
        try:
            transcribe(note_id, audio_path)
            app.logger.info("Finished transcription %s", note_id)
        finally:
            transcription_queue.task_done()


@app.template_filter("friendly_time")
def friendly_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.strftime("%Y %b %d %H:%M")


@app.before_request
def prepare():
    init_db()
    if request.path.startswith("/api/") and not authorized():
        return jsonify(error="Unauthorized"), 401


@app.get("/")
def index():
    with get_db() as connection:
        notes = connection.execute(
            "SELECT id, created_at, transcript, status, error FROM notes ORDER BY created_at DESC"
        ).fetchall()
    return render_template("index.html", notes=notes), {"Cache-Control": "no-store"}


@app.post("/api/audio")
def upload_audio():
    if not authorized():
        return jsonify(error="Unauthorized"), 401
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify(error="file is required"), 400
    try:
        created_at = validate_created_at(request.form.get("created_at"))
    except (TypeError, ValueError) as error:
        return jsonify(error=str(error)), 400

    original_name = secure_filename(uploaded.filename) or "recording.wav"
    note_id = str(uuid.uuid4())
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = AUDIO_DIR / f"{note_id}-{original_name}"
    uploaded.save(audio_path)
    with get_db() as connection:
        connection.execute(
            "INSERT INTO notes (id, created_at, filename, audio_path, status, received_at) "
            "VALUES (?, ?, ?, ?, 'processing', ?)",
            (note_id, created_at, original_name, str(audio_path), datetime.now(timezone.utc).isoformat()),
        )
    transcription_queue.put((note_id, audio_path))
    app.logger.info("Queued transcription %s for %s", note_id, original_name)
    return jsonify(id=note_id, created_at=created_at, status="processing"), 202


@app.get("/notes/<note_id>")
def note_status(note_id):
    with get_db() as connection:
        note = connection.execute(
            "SELECT id, created_at, transcript, status, error FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
    if note is None:
        return jsonify(error="not found"), 404
    return jsonify(dict(note))


@app.get("/audio/<note_id>")
def audio(note_id):
    with get_db() as connection:
        note = connection.execute("SELECT audio_path FROM notes WHERE id = ?", (note_id,)).fetchone()
    if note is None or not Path(note["audio_path"]).is_file():
        return jsonify(error="not found"), 404
    return send_file(note["audio_path"], mimetype="audio/wav")


@app.delete("/notes/<note_id>")
def delete_note(note_id):
    with get_db() as connection:
        note = connection.execute(
            "SELECT audio_path FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        if note is None:
            return jsonify(error="not found"), 404
        connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    audio_path = Path(note["audio_path"])
    if audio_path.is_file():
        audio_path.unlink()
    app.logger.info("Deleted note %s", note_id)
    return "", 204


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/api/health/audio")
def audio_health():
    if not authorized():
        return "Unauthorized", 401
    return "Connected", 200


if not API_TOKEN:
    raise RuntimeError("API_TOKEN environment variable is required")

init_db()
MODEL_DIR.mkdir(parents=True, exist_ok=True)
recover_processing_jobs()
threading.Thread(target=worker, daemon=True, name="transcription-worker").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
