FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN useradd --create-home app && mkdir -p /tmp/llm-cache && chown -R app /app /tmp/llm-cache
USER app
# 1 worker: Cloud Run scales by instances; the LLM disk cache and in-process ADK runner stay per instance.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1
