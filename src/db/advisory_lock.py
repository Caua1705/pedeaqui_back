"""O lock que impede duas replicas de migrarem o mesmo banco ao mesmo tempo.

O CASO CONCRETO. `docker-entrypoint.sh` roda `alembic upgrade` antes do
Uvicorn, e ele roda em TODO container da API. Com uma replica isso e uma
migracao; com duas atras do Traefik, `docker compose up -d` sobe as duas no
mesmo segundo e as duas comecam a migrar o mesmo banco.

O que acontece dai nao e um erro limpo. As duas leem `alembic_version`, as
duas veem a mesma revisao atual, e as duas tentam aplicar a mesma revisao
seguinte. O Postgres serializa o que consegue sozinho (`ALTER TABLE` pega
lock de tabela), entao o desfecho depende do que a revisao faz:

- a que perder a corrida morre com "column already exists" — e o container
  entra em loop de restart por causa de uma migracao que JA FOI aplicada com
  sucesso pela outra;
- ou, pior, uma revisao com PASSO DE DADOS roda duas vezes. A copia do
  cardapio por filial da `20260820_0026` e o exemplo que existe neste
  repositorio: rodada duas vezes, ela duplica cardapio.

Nenhum dos dois aparece em banco vazio, entao a suite nunca os veria.

**`pg_advisory_xact_lock` e nao `pg_advisory_lock`**, e a escolha e sobre
producao. O banco e Supabase, e se a `DATABASE_URL` apontar para o pooler em
modo TRANSACAO a conexao so fica presa a um backend enquanto dura a
transacao. Um lock de SESSAO seria tomado num backend e conferido em outro,
valendo nada — sem erro, sem log, com as duas replicas migrando do mesmo
jeito. O lock de transacao vale nos dois modos.

E ele nao precisa ser solto: morre com a transacao. Isso cobre de graca o
caso que mais assusta, que e o migrador MORRER segurando o lock — o Postgres
derruba a transacao junto com a conexao e o proximo entra.

**Nao ha timeout, de proposito.** Quem espera aqui e um container que ainda
nao serve ninguem, e o que ele espera e a outra replica terminar de migrar:
esperar e a resposta certa. Um `lock_timeout` transformaria uma migracao
longa e BEM-SUCEDIDA numa replica morrendo e reiniciando em cima dela.

O preco disso e explicito: migrador pendurado (nao morto — pendurado) segura
os outros pelo mesmo tempo. E o mesmo preco que o `set -e` do entrypoint ja
cobra, e o sintoma tem o mesmo lugar de sempre — a saida do alembic no
`docker logs`.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection


#: A chave do lock. `zlib.crc32(b"pedeaqui:alembic")`, escrita por extenso
#: para que procurar por ela no codigo e no `pg_locks` seja a mesma busca.
#: Qualquer bigint serviria; o que nao pode e mudar depois de estar em
#: producao — duas replicas com chaves diferentes nao se veem.
CHAVE_DA_MIGRACAO = 1568756040


#: O aviso que so esta espera consegue dar.
#:
#: `collect_runtime_warnings` (startup_checks.py) avisa sobre o mesmo perigo
#: lendo `--workers` do `sys.argv` — e por isso enxerga um processo so: o
#: DESTE container. Duas replicas com `--workers 1` cada sao dois processos
#: servindo o mesmo `/chat`, e argv nenhum diz isso.
#:
#: Ter esperado o lock diz. E o unico momento em que uma replica tem prova de
#: que existe outra, e sai daqui em vez de do boot da API porque e aqui que a
#: prova existe.
#:
#: O QUE ELE NAO PEGA, e vale saber antes de confiar nele: so dispara quando
#: os boots se SOBREPOEM. Replica que sobe uma hora depois da outra pega o
#: lock de primeira e nao aprende nada. Isso cobre o caso do deploy — que e
#: quando as duas sobem juntas — e nao cobre o de escalar a quente.
AVISO_DE_MAIS_DE_UMA_REPLICA = (
    "[alembic] ATENCAO: havia OUTRA REPLICA subindo agora, entao mais de um "
    "processo vai servir esta API.\n"
    "[alembic] O historico de conversa do /chat vive em MEMORIA DO PROCESSO e "
    "nao tem caminho de Redis (ao contrario do rate limit e do cache de "
    "entrega, que REDIS_URL resolve).\n"
    "[alembic] Cada replica guarda as proprias sessoes: um turno so encontra o "
    "historico do anterior se cair na mesma. O Rapi responde sem contexto, SEM "
    "erro e SEM log.\n"
    "[alembic] Preco e cardapio nao sao afetados (saem do banco); o que se "
    "perde e referencia do tipo 'quanto custa esse?'."
)


def travar_para_migrar(connection: Connection) -> None:
    """Segura o lock da migracao ate o fim da transacao ja aberta em `connection`.

    Precisa ser chamada DENTRO da transacao da migracao, antes de
    `run_migrations`: a leitura de `alembic_version` tem que acontecer ja
    travada. Duas replicas lendo a mesma revisao atual e o comeco do
    problema, nao o fim.

    Nao devolve nada porque nao ha decisao para quem chama tomar: ou o lock
    veio de primeira, ou esta funcao espera. O que ela faz e FALAR, nas duas
    linhas abaixo, e por isso ela nao e muda.
    """
    de_primeira = connection.execute(
        text("SELECT pg_try_advisory_xact_lock(:chave)"),
        {"chave": CHAVE_DA_MIGRACAO},
    ).scalar()
    if de_primeira:
        return

    # Escrita ANTES de bloquear, e nao depois: esta linha existe para
    # explicar uma pausa que esta ACONTECENDO. Impressa depois do bloqueio,
    # ela chegaria junto com o fim da espera, que e exatamente quando
    # ninguem mais precisa dela.
    #
    # `print` e nao `logger`: quem le isto le pelo `docker logs`, no meio da
    # saida do `alembic` e dos `echo` do entrypoint. Uma linha que depende da
    # configuracao de logging do `alembic.ini` para aparecer e uma linha que
    # um dia nao aparece.
    print(
        "[alembic] outra replica esta migrando este banco. Esperando a vez "
        "(sem timeout: esperar e o certo).",
        flush=True,
    )
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:chave)"),
        {"chave": CHAVE_DA_MIGRACAO},
    )
    print("[alembic] a outra replica terminou; migrando agora.", flush=True)
    print(AVISO_DE_MAIS_DE_UMA_REPLICA, flush=True)
