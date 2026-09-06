"""Engine e sessões do banco.

O engine é criado **sob demanda**, não no import. Criado no import, ele
congelava `settings.DATABASE_URL` no instante em que qualquer módulo de `src`
fosse importado — e a suíte marcada `db` precisa apontar para outro Postgres
depois disso, sem ter que ser a primeira coisa a rodar no processo.

`create_engine` não abre conexão: quem conecta é a primeira sessão que executa
alguma coisa. Então importar este módulo continua não dependendo de banco de pé.

`lru_cache` e não um `global`: o pool de conexões só é pool se houver **um**
engine por processo, e é ele quem guarda as conexões abertas.
"""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from src.core.config import settings


# ---------------------------------------------------------------------------
# O tamanho do pool, e a conta que o produziu
# ---------------------------------------------------------------------------
#
# Antes disto o pool era o PADRÃO do SQLAlchemy — `pool_size=5`,
# `max_overflow=10`, `pool_timeout=30` —, e o padrão não sabe nada sobre este
# sistema. Os três números ficam explícitos porque o teto do banco é pequeno e
# porque a conta muda no dia em que houver mais de um worker.
#
# ## O que o banco aguenta (medido em produção, 05/09/2026)
#
#     max_connections = 60      8 abertas    1 ativa    0 presas em transação
#
# As 60 não são nossas. Descontando, por ordem de quem não dá para negociar:
#
#     60  teto do Postgres
#    -10  Supabase (pooler, painel, `superuser_reserved_connections`) — as 8
#         medidas com a API ociosa, com folga
#     -6  os quatro contêineres de manutenção (`limpeza`, `estorno`,
#         `whatsapp-reenvio`, `reindex`). Cada um roda um laço sequencial e
#         segura UMA conexão de cada vez, mas cada um constrói o próprio
#         engine com este mesmo módulo — se um dia paralelizarem, o teto
#         abaixo passa a valer para eles também
#     -4  a janela de deploy e o psql de emergência
#     ---
#     40  para a API
#
# ## Por que 20 por worker, e não 40
#
# A API roda hoje com **um** worker (`CMD` do Dockerfile, sem `--workers`),
# então 40 caberiam. O número é 20 porque a conta tem que sobreviver ao
# segundo worker sem ninguém refazê-la: `2 x 20 = 40`, que é exatamente o que
# sobrou. Dimensionar para o worker de hoje é escrever um limite que quebra em
# silêncio no dia do `--workers 2` — e o sintoma seria
# `FATAL: too many connections`, no boot, com a API fora do ar.
#
# ## O que 20 compra, e o que ele NÃO resolve
#
# Os endpoints são `def`, então rodam no threadpool do AnyIO — **40 threads**.
# Com o pool em 5+10 eram 40 threads disputando 15 conexões; agora são 40
# disputando 20. **A folga continua sendo menor que o paralelismo**, e isso é
# deliberado: o gargalo real é `create_order` segurar uma conexão durante a
# chamada ao Google Maps (5 s de teto), e o conserto disso é tirar o I/O de
# dentro da transação — não abrir mais conexões contra um banco que tem 60.
# Ver o PR que trouxe estes números.
#
# `pool_size` 10 e não 20 com overflow 0: as 10 permanentes cobrem o regime
# normal sem abrir e fechar conexão a cada pico, e as 10 de overflow são
# devolvidas ao banco quando o pico passa, em vez de ficarem paradas ocupando
# vaga que os contêineres de manutenção podem precisar.
POOL_SIZE = 10
MAX_OVERFLOW = 10

# 30 s (o padrão) é tempo que ninguém tem: o app e o painel já desistiram, e a
# thread fica presa esperando uma conexão para uma resposta que ninguém vai
# ler. 10 s absorve uma chamada lenta ao Google (5 s de teto) sem virar fila.
POOL_TIMEOUT = 10

# O pooler do Supabase derruba conexão ociosa, e `pool_pre_ping` já cobre isso
# — ao custo de um SELECT 1 por checkout que pega uma conexão morta. Reciclar
# a cada 30 min faz o pre-ping quase nunca precisar agir. Não substitui o
# pre-ping: reciclagem é por IDADE, e a queda pode acontecer antes.
POOL_RECYCLE_SECONDS = 1800


@lru_cache
def get_engine() -> Engine:
    return create_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
        pool_recycle=POOL_RECYCLE_SECONDS,
    )


def SessionLocal() -> Session:
    """Uma sessão nova, ligada ao engine do processo.

    Continua sendo chamada de `SessionLocal()` porque é assim que os scripts
    de manutenção e o stream do painel já a chamam — e porque no vocabulário
    do SQLAlchemy esse nome já significa "me dê uma sessão".

    `autoflush=False` é a única diferença para o padrão do SQLAlchemy: com ele
    ligado, uma leitura no meio da montagem do pedido dispararia o INSERT dos
    itens antes da hora.
    """
    return Session(bind=get_engine(), autoflush=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
