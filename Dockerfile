# Job Engine: one container for the Telegram webhook, the scheduled tasks and /healthz.
# Secrets are never baked in: Cloud Run passes them from Secret Manager as env variables.
FROM python:3.12-slim

# WeasyPrint needs Pango and HarfBuzz; resumes are rendered only in the Lato font.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-lato libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libffi8 shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
# Outside prod the contact providers answer from these invented fixtures (Module 05).
COPY fixtures/contacts ./fixtures/contacts
RUN pip install --no-cache-dir . \
    && mkdir -p /app/out \
    && chown -R app:app /app/out

ENV PYTHONUNBUFFERED=1 BOT_MODE=webhook JOBENGINE_ROOT=/app
USER app
EXPOSE 8080
CMD ["python", "-m", "jobengine.web"]
