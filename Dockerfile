FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir "setuptools<82" \
    && pip install --no-cache-dir --no-build-isolation -r requirements.txt

COPY app.py .
COPY templates ./templates

ENV DATA_DIR=/app/data
ENV WHISPER_MODEL=base
ENV ATEN_CPU_CAPABILITY=default
ENV OMP_NUM_THREADS=1
ENV MKL_NUM_THREADS=1
ENV PORT=8080
VOLUME ["/app/data"]
EXPOSE 8080

CMD ["python", "app.py"]
