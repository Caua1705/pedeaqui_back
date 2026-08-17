"""`BranchRepository.get_default_branch` — a filial padrao, agora com UMA regra.

Antes desta funcao existiam duas respostas para a mesma pergunta:

- `GET /restaurants/{slug}/info` sem `branch_id` pegava
  `list_active_by_restaurant()[0]`;
- `POST /restaurants/{slug}/delivery/estimate` sem `branch_id` exigia
  `is_main` e recusava com **400** quem nao tivesse a flag marcada.

Como aquela listagem ja ordena por `is_main DESC NULLS LAST, name ASC`, as
duas concordavam quando havia filial principal — e discordavam exatamente
quando ela faltava, que e o caso em que uma rota respondia e a outra falhava
para o mesmo restaurante no mesmo minuto.

Marcado `db` porque a regra mora no ORDER BY, e ORDER BY com `NULLS LAST` so
se prova contra o Postgres: em memoria, `None` e `False` ordenam de outro
jeito.
"""

import pytest

from src.repositories.branch_repository import BranchRepository
from tests.fabricas_db import criar_filial, criar_restaurante


pytestmark = pytest.mark.db


def test_a_principal_ganha_de_quem_vem_antes_no_alfabeto(db):
    restaurante = criar_restaurante(db)
    criar_filial(db, restaurante, nome="Aldeota")
    principal = criar_filial(db, restaurante, nome="Zona Norte")
    principal.is_main = True
    db.flush()

    assert BranchRepository(db).get_default_branch(restaurante.id).id == principal.id


def test_sem_principal_vale_a_primeira_em_ordem_alfabetica(db):
    """O caso que separava as duas regras antigas: aqui a estimativa de
    entrega respondia 400 e o /info respondia normalmente."""
    restaurante = criar_restaurante(db)
    primeira = criar_filial(db, restaurante, nome="Aldeota")
    criar_filial(db, restaurante, nome="Centro")

    assert BranchRepository(db).get_default_branch(restaurante.id).id == primeira.id


def test_is_main_nulo_nao_ganha_de_is_main_verdadeiro(db):
    """`NULLS LAST` no ORDER BY. A coluna e nullable e linhas antigas de
    producao podem ter `None` em vez de `False`."""
    restaurante = criar_restaurante(db)
    sem_flag = criar_filial(db, restaurante, nome="Aldeota")
    sem_flag.is_main = None
    principal = criar_filial(db, restaurante, nome="Centro")
    principal.is_main = True
    db.flush()

    assert BranchRepository(db).get_default_branch(restaurante.id).id == principal.id


def test_filial_inativa_nao_e_escolhida(db):
    restaurante = criar_restaurante(db)
    inativa = criar_filial(db, restaurante, nome="Aldeota")
    inativa.is_active = False
    inativa.is_main = True
    ativa = criar_filial(db, restaurante, nome="Centro")
    db.flush()

    assert BranchRepository(db).get_default_branch(restaurante.id).id == ativa.id


def test_restaurante_sem_filial_ativa_devolve_none(db):
    """Devolve `None` em vez de levantar: cada rota tem o proprio codigo de
    erro para este caso (404 no /info, 400 na estimativa), e centralizar a
    excecao aqui mudaria contrato ja publicado."""
    restaurante = criar_restaurante(db)

    assert BranchRepository(db).get_default_branch(restaurante.id) is None
