import os
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import whisper
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
AUDIO_DIR = DATA_DIR / "audio"
DATABASE = DATA_DIR / "notes.sqlite3"
MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")
API_TOKEN = os.environ.get("API_TOKEN")

app = Flask(__name__)
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
                model = whisper.load_model(MODEL_NAME)
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
        result = get_model().transcribe(str(audio_path), fp16=False)
        transcript = result.get("text", "").strip()
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


def worker():
    while True:
        note_id, audio_path = transcription_queue.get()
        try:
            transcribe(note_id, audio_path)
        finally:
            transcription_queue.task_done()


@app.before_request
def prepare():
    init_db()


@app.get("/")
def index():
    with get_db() as connection:
        notes = connection.execute(
            "SELECT id, created_at, transcript, status, error FROM notes ORDER BY created_at DESC"
        ).fetchall()
    return render_template("index.html", notes=notes)


@app.post("/audio")
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


@app.get("/health")
def health():
    return jsonify(status="ok")


if not API_TOKEN:
    raise RuntimeError("API_TOKEN environment variable is required")

init_db()
threading.Thread(target=worker, daemon=True, name="transcription-worker").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
