"""A corrida do clique duplo: o índice pega, e agora ele fala.

O cardápio e a impressão usam confere-depois-insere. Entre a conferência e o
`INSERT` não há nada segurando a linha — dois cliques no mesmo botão passam os
dois pela consulta antes de qualquer commit, e o segundo `INSERT` bate no
índice. Até aqui **não havia um único `except IntegrityError` na aplicação**, e
o lojista recebia 500 sem saber se o produto tinha sido criado.

O que estes testes travam:

- **a tradução acontece**, e com a MESMA frase da conferência. Duas frases para
  a mesma colisão seria a diferença que o lojista não consegue enxergar, porque
  qual das duas pega depende de uma corrida;
- **restrição desconhecida NÃO vira 409.** Uma FK violada — defeito nosso —
  chegando como "já existe um produto com esse nome" faria alguém passar a
  tarde procurando um produto que não existe. O 500 é feio e é honesto;
- **os quatro nomes existem no banco.** É a trava que a corrida não dá: ela não
  acontece na suíte, então uma revisão que renomeasse um índice deixaria a
  tradução muda e tudo continuaria verde.

A corrida é encenada desligando a conferência (`patch` no método), que é
exatamente o estado em que a segunda requisição chega: ela já passou por lá.
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from src.api.dependencies.admin_scope import AdminScope
from src.schemas.admin_menu_schema import AdminCategoryCreate, AdminProductCreate
from src.schemas.admin_printing_schema import PrintingSectorCreate
from src.services.admin_menu_service import AdminMenuService
from src.services.admin_printing_service import AdminPrintingService
from src.services.unique_conflict import FRASE_POR_RESTRICAO, traduzir
from tests.fabricas_db import (
    criar_categoria,
    criar_produto,
    criar_restaurante,
    filial_padrao,
)


pytestmark = pytest.mark.db


def escopo(db, restaurante) -> AdminScope:
    from src.models.admin_user_model import AdminUser

    admin = AdminUser(
        restaurant_id=restaurante.id,
        name="Dono",
        email=f"dono-{restaurante.id.hex[:8]}@exemplo.com",
        password_hash="$2b$12$" + "x" * 53,
        role="owner",
        is_active=True,
    )
    db.add(admin)
    db.flush()
    return AdminScope(admin_user=admin, restaurant_id=restaurante.id, branch_id=None)


class TestACorridaVira409ENao500:
    def test_produto_com_o_mesmo_nome(self, db) -> None:
        """O primeiro produto nasce pelo SERVICE, e não pela fábrica.

        A fábrica gera um slug aleatório, e aí não há colisão nenhuma — o teste
        ficaria verde sem exercitar o índice. É o slug de verdade
        (`slugify("Picanha")`) que os dois precisam ter."""
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        alvo = escopo(db, restaurante)
        service = AdminMenuService(db)
        pedido = AdminProductCreate(category_id=categoria.id, name="Picanha", price=10)
        service.create_product(alvo, pedido)

        with patch.object(AdminMenuService, "_ensure_product_slug_is_free"):
            with pytest.raises(HTTPException) as erro:
                service.create_product(alvo, pedido)

        assert erro.value.status_code == 409
        assert erro.value.detail == FRASE_POR_RESTRICAO["uq_products_branch_slug"]

    def test_categoria_com_o_mesmo_nome(self, db) -> None:
        restaurante = criar_restaurante(db)
        filial = filial_padrao(db, restaurante)
        alvo = escopo(db, restaurante)
        service = AdminMenuService(db)
        pedido = AdminCategoryCreate(name="Carnes", branch_id=filial.id)
        service.create_category(alvo, pedido)

        with patch.object(AdminMenuService, "_ensure_category_slug_is_free"):
            with pytest.raises(HTTPException) as erro:
                service.create_category(alvo, pedido)

        assert erro.value.status_code == 409
        assert erro.value.detail == FRASE_POR_RESTRICAO["uq_categories_branch_slug"]

    def test_produto_com_a_mesma_chave_de_catalogo(self, db) -> None:
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")
        produto.catalog_key = "PICANHA"
        db.commit()
        service = AdminMenuService(db)

        with patch.object(AdminMenuService, "_ensure_catalog_key_is_free"):
            with pytest.raises(HTTPException) as erro:
                service.create_product(
                    escopo(db, restaurante),
                    AdminProductCreate(
                        category_id=categoria.id,
                        name="Maminha",
                        price=10,
                        catalog_key="PICANHA",
                    ),
                )

        assert erro.value.status_code == 409
        assert (
            erro.value.detail == FRASE_POR_RESTRICAO["uq_products_branch_catalog_key"]
        )

    def test_setor_de_impressao_com_o_mesmo_nome(self, db) -> None:
        restaurante = criar_restaurante(db)
        filial = filial_padrao(db, restaurante)
        alvo = escopo(db, restaurante)
        service = AdminPrintingService(db)
        pedido = PrintingSectorCreate(name="Cozinha")
        service.create_sector(alvo, filial.id, pedido)

        with patch.object(AdminPrintingService, "_ensure_name_is_free"):
            with pytest.raises(HTTPException) as erro:
                service.create_sector(alvo, filial.id, pedido)

        assert erro.value.status_code == 409
        assert erro.value.detail == FRASE_POR_RESTRICAO["uq_printing_sectors_branch_name"]


class TestOQueNaoEColisaoConhecidaNaoVira409:
    def test_uma_FK_violada_sobe_como_erro_de_verdade(self, db) -> None:
        """`traduzir` devolve `None`, e quem chama levanta o original.

        Traduzir tudo seria pior que não traduzir nada: uma FK violada é
        defeito NOSSO, e chegar ao lojista como "já existe um produto com esse
        nome" o manda procurar um produto que não existe."""
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError) as capturado:
            db.execute(
                text(
                    "INSERT INTO categories (restaurant_id, branch_id, name, slug,"
                    " sort_order) VALUES"
                    " ('00000000-0000-0000-0000-000000000001'::uuid,"
                    " '00000000-0000-0000-0000-000000000002'::uuid, 'X', 'x', 1)"
                )
            )
        db.rollback()

        assert traduzir(capturado.value) is None


class TestOsNomesExistemNoBanco:
    """A trava que a corrida não dá.

    Ela não acontece na suíte, então uma revisão que renomeasse um índice
    deixaria a tradução muda — a colisão voltaria a ser 500 — e todo o resto
    continuaria verde. Este teste é o que fica vermelho no lugar dela.
    """

    def test_toda_frase_aponta_para_uma_restricao_que_existe(self, db) -> None:
        nomes = {
            linha[0]
            for linha in db.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
        }

        faltando = sorted(set(FRASE_POR_RESTRICAO) - nomes)

        assert faltando == [], (
            f"restrições citadas em FRASE_POR_RESTRICAO que não existem no banco: "
            f"{faltando}. A tradução do IntegrityError fica muda para elas, e a "
            f"colisão volta a ser 500."
        )
