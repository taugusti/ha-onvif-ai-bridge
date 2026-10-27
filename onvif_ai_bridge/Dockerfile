FROM python:3.12-alpine
COPY app /app
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-u", "/app/bridge.py", "/data/options.json"]
