"""O `/menu` não lê `branches` nem `restaurant_banners` duas vezes (auditoria §5.2).

São duas coisas, e a segunda é a que não estava na auditoria:

1. **a contagem.** Eram duas idas ao Postgres por abertura de cardápio, na rota
   pública mais chamada do sistema. Ganho pequeno e certo — e o teste existe
   porque um `+ 1` aqui não quebra nada, não aparece em log e só se paga na
   fatura do Supabase;

2. **a ordenação, que divergia.** `get_default_branch` é
   `list_active_by_restaurant()[0]`, ordenado por `is_main DESC NULLS LAST,
   name ASC`. A lista publicada saía de `menu_repository.get_active_branches`,
   que ordenava `is_main DESC` **sem** `NULLS LAST` — e no Postgres `DESC` é
   `NULLS FIRST`. Com uma filial de `is_main` nulo, **o primeiro item da lista
   publicada não era a filial que a própria resposta usava.**

O segundo só aparece contra um banco de verdade: é `NULLS FIRST` contra
`NULLS LAST`, que nenhum dublê em memória reproduz sem alguém já saber da
diferença — e quem já sabe não precisa do teste.

`is_main` é `Mapped[bool | None]`, então o caso não é hipotético: linha escrita
por SQL manual (armadilha 33) nasce com ele nulo.
"""

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from src.services.menu_service import MenuService
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _contar(engine, agulha: str):
    """Quantas vezes uma tabela apareceu num SELECT desta requisição."""
    contagem = {"n": 0}

    def _antes(conn, cursor, statement, parameters, context, executemany):
        if agulha in statement:
            contagem["n"] += 1

    event.listen(engine, "before_cursor_execute", _antes)
    return contagem, lambda: event.remove(engine, "before_cursor_execute", _antes)


@pytest.fixture
def rede(db: Session):
    restaurante = fab.criar_restaurante(db, nome="Júnior da Picanha")
    fab.criar_configuracoes(db, restaurante)
    centro = fab.criar_filial(db, restaurante, nome="Centro")
    fab.criar_filial(db, restaurante, nome="Aldeota")
    db.flush()
    return restaurante, centro


def test_branches_e_lido_UMA_vez_por_cardapio(db: Session, rede):
    restaurante, _ = rede
    contagem, parar = _contar(db.get_bind(), "FROM branches")
    try:
        MenuService(db).get_restaurant_menu(restaurante.slug)
    finally:
        parar()

    assert contagem["n"] == 1, "a lista publicada e a filial escolhida saem da MESMA consulta"


def test_os_banners_saem_de_UMA_consulta(db: Session, rede):
    restaurante, _ = rede
    contagem, parar = _contar(db.get_bind(), "FROM restaurant_banners")
    try:
        MenuService(db).get_restaurant_menu(restaurante.slug)
    finally:
        parar()

    assert contagem["n"] == 1, "hero e highlight vem num `banner_type IN (...)`"


def test_com_branch_id_explicito_tambem_e_UMA_consulta(db: Session, rede):
    """O caminho que a auditoria descreveu: a filial escolhida JÁ ESTÁ na lista."""
    restaurante, centro = rede
    contagem, parar = _contar(db.get_bind(), "FROM branches")
    try:
        menu = MenuService(db).get_restaurant_menu(restaurante.slug, branch_id=centro.id)
    finally:
        parar()

    assert contagem["n"] == 1
    assert menu.branch_id == centro.id


class TestAOrdemQueDivergia:
    """Com `is_main` nulo, a lista publicada e a filial padrão discordavam."""

    def _com_is_main_nulo(self, db: Session, restaurante, filial):
        """`is_main = NULL` por SQL cru, e não pelo ORM.

        A coluna tem `default=False` no model, então `Branch(...)` sem o campo
        grava `false` e não nulo — é a armadilha 55 pelo lado do default do
        ORM. O nulo que existe em produção veio de escrita feita por fora, e é
        só assim que ele se reproduz aqui.
        """
        db.execute(
            text("UPDATE branches SET is_main = NULL WHERE id = :id"), {"id": filial.id}
        )
        db.flush()

    def test_a_primeira_da_lista_E_a_filial_padrao(self, db: Session, rede):
        """A afirmação que estava falsa antes.

        `Aldeota` vem antes de `Centro` no alfabeto. Com `Centro` marcada como
        principal e `Aldeota` com `is_main` NULO:

        - `NULLS LAST` (o canônico) põe `Centro` primeiro — é a principal;
        - `NULLS FIRST` (o que a lista do cardápio usava) põe `Aldeota`.

        O cardápio publicava a segunda ordem e respondia com a primeira filial.
        """
        restaurante, centro = rede
        centro.is_main = True
        aldeota = next(
            f for f in db.query(type(centro)).all() if f.name == "Aldeota"
        )
        self._com_is_main_nulo(db, restaurante, aldeota)

        menu = MenuService(db).get_restaurant_menu(restaurante.slug)

        assert menu.branches[0].id == menu.branch_id
        assert menu.branches[0].name == "Centro"

    def test_a_contraprova_sem_nulo_nenhum_a_ordem_ja_concordava(
        self, db: Session, rede
    ):
        """Sem `is_main` nulo as duas ordenações davam o mesmo resultado — e é
        por isso que a divergência nunca apareceu em teste nenhum."""
        restaurante, centro = rede
        centro.is_main = True
        db.flush()

        menu = MenuService(db).get_restaurant_menu(restaurante.slug)

        assert menu.branches[0].id == menu.branch_id
        assert menu.branches[0].name == "Centro"


def test_filial_de_outro_restaurante_continua_404(db: Session, rede):
    """O 404 saiu do repositório e passou a sair da lista — mesma resposta.

    Sem ele, "não está na lista" poderia virar "cai na filial padrão calada",
    que é exatamente o que `_resolve_menu_branch` existe para não fazer.
    """
    from fastapi import HTTPException

    restaurante, _ = rede
    outro = fab.criar_restaurante(db, nome="Concorrente")
    alheia = fab.criar_filial(db, outro, nome="Alheia")
    db.flush()

    with pytest.raises(HTTPException) as erro:
        MenuService(db).get_restaurant_menu(restaurante.slug, branch_id=alheia.id)

    assert erro.value.status_code == 404
