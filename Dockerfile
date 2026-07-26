FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Executa como usuario sem privilegios; o codigo da aplicacao fica read-only para ele.
RUN useradd --system --create-home --uid 10001 appuser \
    && chown -R root:root /app \
    && chmod -R a-w /app

USER appuser

EXPOSE 8000

# --proxy-headers para que o IP do cliente atras do Traefik chegue correto
# ao rate limiting e aos logs.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
