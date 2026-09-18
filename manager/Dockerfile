FROM python:3.12-alpine

# Standard library only: no packages beyond the base image are installed.
COPY manager.py index.html app.js app.css /app/
RUN chmod 0644 /app/* && chmod 0755 /app/manager.py

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5)"]

STOPSIGNAL SIGINT
ENTRYPOINT ["python3", "/app/manager.py"]
