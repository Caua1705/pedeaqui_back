"""O que a revisão 0047 (trava por falhas do entregador) promete ao banco.

Três garantias, e nenhuma delas aparece na suíte rápida — a instância
transiente do model não aplica `default=` de coluna, não tem CHECK e não sabe
o que é `NOT NULL`:

1. **O deploy não trava ninguém.** Entregador existente nasce com zero
   falhas e sem bloqueio, e é o `server_default` da migração que responde
   isso — não o `default=` do model, que só vale quando o INSERT sai do ORM.
2. **O contador é `NOT NULL`.** É a armadilha 50 evitada de propósito: nulo
   aqui não tem significado nenhum de produto ("nunca errou" é zero), e
   `NULL >= 5` não é falso — é nulo, e a trava nunca fecharia.
3. **Os dois instantes são nulláveis**, e neles o nulo SIGNIFICA: "nunca
   falhou" e "não está travado".

O estado do schema aqui já é o de depois da revisão — a fixture `db` roda
`alembic upgrade head`.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _inserir_entregador(db: Session, restaurante, filial_id, telefone="85999990000") -> uuid.UUID:
    """Por SQL cru, e não pelo model: um INSERT do ORM carregaria o
    `default=0` do lado do Python e passaria verde sobre uma migração que
    tivesse esquecido o `server_default`."""
    return db.execute(
        text(
            "INSERT INTO couriers (restaurant_id, branch_id, name, phone) "
            "VALUES (:restaurant_id, :branch_id, 'Zé', :phone) "
            "RETURNING id"
        ),
        {"restaurant_id": restaurante.id, "branch_id": filial_id, "phone": telefone},
    ).scalar_one()


def _trava(db: Session, courier_id) -> tuple:
    return db.execute(
        text(
            "SELECT access_failed_attempts, access_failed_at, access_blocked_until "
            "FROM couriers WHERE id = :id"
        ),
        {"id": courier_id},
    ).one()


class TestODeployNaoTravaNinguem:
    def test_entregador_nasce_sem_falhas_e_sem_trava(self, db: Session):
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)

        courier_id = _inserir_entregador(db, restaurante, filial.id)

        assert _trava(db, courier_id) == (0, None, None)


class TestOContadorNaoAceitaNulo:
    def test_gravar_nulo_no_contador_e_recusado(self, db: Session):
        """`NULL >= 5` não é falso, é nulo — e a trava nunca fecharia. O
        schema é quem fecha essa porta, não o código."""
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        courier_id = _inserir_entregador(db, restaurante, filial.id)

        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE couriers SET access_failed_attempts = NULL WHERE id = :id"),
                {"id": courier_id},
            )
            db.flush()

    def test_contador_negativo_e_recusado(self, db: Session):
        """Contador negativo só chegaria por escrita feita por fora
        (armadilha 33), e com ele a trava nunca fecharia."""
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        courier_id = _inserir_entregador(db, restaurante, filial.id)

        with pytest.raises((IntegrityError, DataError)):
            db.execute(
                text("UPDATE couriers SET access_failed_attempts = -1 WHERE id = :id"),
                {"id": courier_id},
            )
            db.flush()


class TestOsDoisInstantesAceitamNulo:
    def test_voltar_a_zero_apaga_os_dois(self, db: Session):
        """É o que o acerto e a regeneração do acesso fazem: zerar as três
        colunas. Se algum dos dois instantes fosse `NOT NULL`, "nunca falhou"
        precisaria de um valor inventado."""
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        courier_id = _inserir_entregador(db, restaurante, filial.id)
        db.execute(
            text(
                "UPDATE couriers SET access_failed_attempts = 5, "
                "access_failed_at = now(), access_blocked_until = now() + interval '15 min' "
                "WHERE id = :id"
            ),
            {"id": courier_id},
        )

        db.execute(
            text(
                "UPDATE couriers SET access_failed_attempts = 0, "
                "access_failed_at = NULL, access_blocked_until = NULL WHERE id = :id"
            ),
            {"id": courier_id},
        )

        assert _trava(db, courier_id) == (0, None, None)
