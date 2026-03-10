FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HELIOS_DATABASE_URL=sqlite:////app/data/helios_home.db

COPY pyproject.toml README.md /app/
COPY apps/edge-api /app/apps/edge-api

RUN pip install --no-cache-dir .
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--app-dir", "apps/edge-api", "--host", "0.0.0.0", "--port", "8000"]

