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


@lru_cache
def get_engine() -> Engine:
    return create_engine(settings.DATABASE_URL, pool_pre_ping=True)


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
