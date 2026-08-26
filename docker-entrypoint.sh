#!/bin/sh
# Migra antes de servir. Sem isto, `docker compose up --build` sobe a API
# contra o schema antigo: o codigo novo consulta coluna que ainda nao existe
# e a falha aparece na requisicao do cliente, nao no deploy.
set -e

# Sem `|| true`: se a migracao falhar, o container morre e o `restart: always`
# do compose tenta de novo. Um banco fora do ar vira loop de restart visivel
# no `docker ps`, o que e melhor que uma API de pe respondendo errado.

# DUAS REPLICAS NAO MIGRAM JUNTAS, e a trava NAO esta aqui: esta em
# `alembic/env.py`, que toma um `pg_advisory_xact_lock` antes de ler
# `alembic_version`. Mora la, e nao neste script, porque assim ela vale para
# TODO caminho que migra — o `alembic upgrade` rodado a mao numa janela de
# manutencao, o `stamp` do banco novo e a fixture da suite `db` inclusive —, e
# nao so para o container.
#
# A replica que chegar em segundo lugar ESPERA, sem timeout, e diz no log que
# esta esperando. Ver `src/db/advisory_lock.py` para por que esperar e o
# comportamento certo e o que o `set -e` acima ja cobrava antes.

# ALVO PADRAO E `head`, e mudar isso e para UMA situacao so: a migracao em
# duas etapas, onde a segunda e irreversivel e precisa de uma janela de
# conferencia antes.
#
# O caso concreto que criou esta variavel e o hash do tracking_token: a
# revisao 0016 acrescenta o hash e mantem a coluna em claro (da para voltar),
# a 0017 apaga a coluna em claro (nao da para voltar, sha-256 nao se desfaz).
# Entre as duas tem que existir tempo com a API NOVA rodando contra o banco no
# estado da 0016, e nao ha como conseguir isso quando as duas revisoes ja
# estao na mesma imagem — `upgrade head` aplicaria as duas no mesmo segundo.
#
# `ALEMBIC_TARGET=20260812_0016` no `.env` para a primeira etapa; TIRAR a
# variavel para a segunda. O roteiro esta em
# `docs/deploy-hash-do-tracking-token.md`.
ALVO="${ALEMBIC_TARGET:-head}"
echo "[entrypoint] alembic upgrade $ALVO"
alembic upgrade "$ALVO"

# Alvo diferente de `head` e estado TEMPORARIO, e o jeito de ele virar
# permanente e alguem esquecer a variavel no `.env`: dali em diante toda
# revisao nova deixa de ser aplicada, o deploy passa verde e a API sobe contra
# um schema velho — que e exatamente o que este entrypoint existe para
# impedir. O aviso e alto de proposito.
if [ "$ALVO" != "head" ]; then
  echo "[entrypoint] ATENCAO: ALEMBIC_TARGET=$ALVO — o banco NAO esta em head."
  echo "[entrypoint] Nenhuma revisao posterior a $ALVO sera aplicada enquanto"
  echo "[entrypoint] esta variavel existir no .env. Tire-a assim que a janela"
  echo "[entrypoint] de conferencia terminar."
fi

# Banco que ja tem o schema mas nunca passou pelo Alembic precisa de
# `alembic stamp 20260726_0001` UMA vez antes do primeiro up — senao o
# upgrade tenta aplicar da 0002 em diante sobre tabelas que ja existem.
# Veja o docstring de alembic/versions/20260726_0001_baseline_schema_existente.py.

echo "[entrypoint] iniciando: $*"
exec "$@"
