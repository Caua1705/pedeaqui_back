"""O que a revisão 0049 (identidade social do cliente) promete ao banco.

Nada disto aparece na suíte rápida: a instância transiente do model não tem
UNIQUE, não tem CHECK e não sabe o que é `NOT NULL`.

1. **Uma conta do provedor aponta para UM cliente, sempre.** É o
   `UNIQUE (provider, provider_user_id)`, e é ele que faz o vínculo pelo `sub`
   valer — sem ele, dois clientes poderiam reivindicar o mesmo Google.
2. **Vários provedores no mesmo cliente são LEGÍTIMOS.** Não há
   `UNIQUE (customer_id, provider)`, de propósito: o Gmail pessoal e o do
   trabalho, os dois confirmados por código, são a mesma pessoa. Um UNIQUE ali
   derrubaria a segunda ligação com `IntegrityError` no fim de um fluxo que
   deu certo.
3. **`provider` é conjunto fechado**, e o CHECK espelha
   `SOCIAL_AUTH_PROVIDERS` (armadilha 15). O par é cobrado de verdade por
   `test_espelhos_de_enum_db.py`; aqui o que se prova é que o CHECK existe e
   recusa.

O estado do schema aqui já é o de depois da revisão — a fixture `db` roda
`alembic upgrade head`.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.constants import SOCIAL_PROVIDER_GOOGLE
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _ligar(db: Session, customer_id, sub: str, provider: str = SOCIAL_PROVIDER_GOOGLE):
    """Por SQL cru, e não pelo model: um INSERT do ORM não prova nada sobre o
    DDL — o que está sendo exercitado aqui é a constraint do Postgres."""
    return db.execute(
        text(
            "INSERT INTO customer_social_identities "
            "(customer_id, provider, provider_user_id) "
            "VALUES (:customer_id, :provider, :sub) RETURNING id"
        ),
        {"customer_id": customer_id, "provider": provider, "sub": sub},
    ).scalar_one()


class TestOVinculoPeloSub:
    def test_a_mesma_conta_do_google_nao_serve_a_dois_clientes(self, db: Session) -> None:
        primeiro = fab.criar_cliente(db)
        segundo = fab.criar_cliente(db)
        sub = f"sub-{uuid.uuid4()}"
        _ligar(db, primeiro.id, sub)
        db.flush()

        with pytest.raises(IntegrityError):
            _ligar(db, segundo.id, sub)
            db.flush()

    def test_o_mesmo_cliente_pode_ter_dois_googles(self, db: Session) -> None:
        """O par do teste acima: a MESMA operação com o dado certo passa.

        Sem ele, o de cima ficaria verde contra um schema que recusa qualquer
        segunda linha — e a pessoa com o Gmail pessoal e o do trabalho, os dois
        já confirmados por código, levaria 500 na segunda ligação.
        """
        cliente = fab.criar_cliente(db)
        _ligar(db, cliente.id, f"sub-{uuid.uuid4()}")
        _ligar(db, cliente.id, f"sub-{uuid.uuid4()}")
        db.flush()

        quantas = db.execute(
            text(
                "SELECT count(*) FROM customer_social_identities "
                "WHERE customer_id = :customer_id"
            ),
            {"customer_id": cliente.id},
        ).scalar_one()
        assert quantas == 2


class TestOProvedorEConjuntoFechado:
    def test_provedor_fora_da_lista_e_recusado(self, db: Session) -> None:
        cliente = fab.criar_cliente(db)
        with pytest.raises(IntegrityError):
            _ligar(db, cliente.id, f"sub-{uuid.uuid4()}", provider="orkut")
            db.flush()

    def test_o_provedor_da_constante_passa(self, db: Session) -> None:
        cliente = fab.criar_cliente(db)
        _ligar(db, cliente.id, f"sub-{uuid.uuid4()}", provider=SOCIAL_PROVIDER_GOOGLE)
        db.flush()


class TestOQueANaoENulo:
    def test_o_sub_e_obrigatorio(self, db: Session) -> None:
        """Identidade sem `sub` não liga nada, e nulo não tem significado aqui.

        `NULL = NULL` é nulo no Postgres, então uma linha assim escaparia do
        UNIQUE e o vínculo deixaria de ser único sem erro nenhum.
        """
        cliente = fab.criar_cliente(db)
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO customer_social_identities "
                    "(customer_id, provider, provider_user_id) "
                    "VALUES (:customer_id, 'google', NULL)"
                ),
                {"customer_id": cliente.id},
            )
            db.flush()

    def test_o_created_at_nasce_preenchido(self, db: Session) -> None:
        """Vem do `server_default`, e não do `default=` do model: quem insere
        por SQL cru (importação, correção à mão) também tem data."""
        cliente = fab.criar_cliente(db)
        identidade = _ligar(db, cliente.id, f"sub-{uuid.uuid4()}")
        db.flush()

        created_at, last_login_at = db.execute(
            text(
                "SELECT created_at, last_login_at FROM customer_social_identities "
                "WHERE id = :id"
            ),
            {"id": identidade},
        ).one()
        assert created_at is not None
        # Nulo até o primeiro login por este provedor, e o nulo SIGNIFICA.
        assert last_login_at is None
