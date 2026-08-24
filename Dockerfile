FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# O LOCK, e nao o `requirements.txt`: e ele que fixa as ~70 versoes, com as
# transitivas. Instalar a declaracao deixaria o pip resolver de novo a cada
# build, que e o sorteio que o lock veio acabar (a historia esta no cabecalho
# dos dois arquivos).
#
# Copiado sozinho, antes do `COPY . .`, para a camada de instalacao so
# invalidar quando as versoes mudarem — e nao a cada linha de codigo.
COPY requirements.lock.txt .

RUN pip install --no-cache-dir -r requirements.lock.txt

COPY . .

# O bit de execucao nao sobrevive ao checkout no Windows, entao e marcado
# aqui. Precisa vir antes do `chmod -R a-w` (que so tira escrita, nao +x).
RUN chmod +x /app/docker-entrypoint.sh

# Executa como usuario sem privilegios; o codigo da aplicacao fica read-only para ele.
RUN useradd --system --create-home --uid 10001 appuser \
    && chown -R root:root /app \
    && chmod -R a-w /app

USER appuser

EXPOSE 8000

# O entrypoint roda `alembic upgrade head` e so entao passa o CMD adiante.
# Fica como ENTRYPOINT, e nao embutido no CMD, para que sobrescrever o
# comando (`docker compose run api bash`) continue migrando primeiro — e
# para que o CMD continue sendo so "como servir".
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# --proxy-headers para que o IP do cliente atras do Traefik chegue correto
# ao rate limiting e aos logs.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
