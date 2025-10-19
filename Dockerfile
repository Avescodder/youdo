FROM python:3.11-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev libffi-dev

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN apk del gcc musl-dev libffi-dev

RUN adduser -D -u 1000 botuser && chown -R botuser:botuser /app

USER botuser

COPY --chown=botuser:botuser bot/main.py .

CMD ["python", "-u", "main.py"]