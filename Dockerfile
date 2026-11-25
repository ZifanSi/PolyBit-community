FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY polybit ./polybit
RUN pip install --no-cache-dir .

RUN useradd --create-home polybit
USER polybit

CMD ["polybit", "run"]
