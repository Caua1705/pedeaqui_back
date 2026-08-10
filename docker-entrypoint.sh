#!/bin/sh
# Migra antes de servir. Sem isto, `docker compose up --build` sobe a API
# contra o schema antigo: o codigo novo consulta coluna que ainda nao existe
# e a falha aparece na requisicao do cliente, nao no deploy.
set -e

# Sem `|| true`: se a migracao falhar, o container morre e o `restart: always`
# do compose tenta de novo. Um banco fora do ar vira loop de restart visivel
# no `docker ps`, o que e melhor que uma API de pe respondendo errado.
echo "[entrypoint] alembic upgrade head"
alembic upgrade head

# Banco que ja tem o schema mas nunca passou pelo Alembic precisa de
# `alembic stamp 20260726_0001` UMA vez antes do primeiro up — senao o
# upgrade tenta aplicar da 0002 em diante sobre tabelas que ja existem.
# Veja o docstring de alembic/versions/20260726_0001_baseline_schema_existente.py.

echo "[entrypoint] iniciando: $*"
exec "$@"
