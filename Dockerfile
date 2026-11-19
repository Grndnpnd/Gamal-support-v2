# One image for all three processes; each service picks its own start command:
#   bot      python entrypoint.py bot.py              (no port)
#   api      python entrypoint.py api_server.py       (API_SERVER_PORT, default 8000; health at /health)
#   webhook  python entrypoint.py webhook_server.py   (WEBHOOK_PORT, default 8080; health at /health)
# entrypoint.py turns the secret store's values into the plain variables the code reads, then becomes the process.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/models ANONYMIZED_TELEMETRY=False

WORKDIR /app
COPY requirements.txt .
# The CPU build of torch: the default one carries GPU libraries several times the size of everything else here.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch \
 && pip install -r requirements.txt

# The embedding model is fetched once, at build, so a start never waits on (or fails with) the model host.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
ENV HF_HUB_OFFLINE=1

COPY . .
RUN useradd --create-home --uid 10001 app && chown -R app:app /app /opt/models
USER app

CMD ["python", "entrypoint.py", "bot.py"]
