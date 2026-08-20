"""`AdminMenuRepository` contra o Postgres — o SQL de verdade.

Testes de **caracterização**: descrevem o que a consulta faz hoje. Uma falha
aqui não é "o teste está errado", é um bug encontrado.

A prioridade é `product_ids_blocked_by_required_group`. Ela decide se o produto
aparece como "fora de venda" para o lojista e é a mesma regra escrita duas
vezes — em Python (`services/menu_rules.blocking_required_group`) e em SQL aqui.
`test_admin_product_availability.py::TestTheTwoExpressionsAgree` já compara as
duas, mas contra uma **tradução** do `NOT EXISTS` para Python: aquilo pega quem
mudar uma e esquecer a outra, e não prova que o SQL faz o que a tradução diz.
Isto aqui prova.
"""

import unicodedata
import uuid

import pytest

from src.repositories.admin_menu_repository import AdminMenuRepository
from tests.fabricas_db import (
    criar_categoria,
    criar_grupo_de_opcoes,
    criar_opcao,
    criar_produto,
    criar_restaurante,
    filial_padrao,
)


pytestmark = pytest.mark.db


class TestProdutoBloqueadoPorGrupoObrigatorio:
    def test_grupo_obrigatorio_sem_nenhuma_opcao_ativa_marca_o_produto(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Pizza")
        grupo = criar_grupo_de_opcoes(db, produto, nome="Tamanho", is_required=True)
        criar_opcao(db, grupo, nome="Grande", is_active=False)

        bloqueados = AdminMenuRepository(db).product_ids_blocked_by_required_group([produto.id])

        assert bloqueados == {produto.id}

    def test_grupo_obrigatorio_sem_nenhuma_LINHA_de_opcao_tambem_marca(self, db):
        """O motivo de a consulta ser `NOT EXISTS` e não `count(ativas) = 0`.

        Grupo recém-criado, antes de o lojista cadastrar a primeira opção: não
        há linha nenhuma para contar, e o produto está tão invendável quanto o
        do caso anterior — o cliente não tem como escolher o tamanho.
        """
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Pizza")
        criar_grupo_de_opcoes(db, produto, nome="Tamanho", is_required=True)

        bloqueados = AdminMenuRepository(db).product_ids_blocked_by_required_group([produto.id])

        assert bloqueados == {produto.id}

    def test_grupo_opcional_vazio_e_grupo_obrigatorio_desativado_nao_marcam(self, db):
        """Os dois jeitos de um grupo vazio ser inofensivo, no mesmo teste
        porque é o par que importa: se a consulta esquecer `is_required` ou
        `is_active`, um dos dois produtos passa a ser marcado sem motivo e o
        lojista vê "fora de venda" num produto que está vendendo."""
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)

        com_grupo_opcional = criar_produto(db, restaurante, categoria, nome="Suco")
        criar_grupo_de_opcoes(db, com_grupo_opcional, nome="Gelo", is_required=False)

        com_grupo_desativado = criar_produto(db, restaurante, categoria, nome="Água")
        criar_grupo_de_opcoes(
            db,
            com_grupo_desativado,
            nome="Tamanho antigo",
            is_required=True,
            is_active=False,
        )

        bloqueados = AdminMenuRepository(db).product_ids_blocked_by_required_group(
            [com_grupo_opcional.id, com_grupo_desativado.id]
        )

        assert bloqueados == set()

    def test_uma_opcao_ativa_entre_varias_inativas_basta(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Pizza")
        grupo = criar_grupo_de_opcoes(db, produto, nome="Tamanho", is_required=True)
        criar_opcao(db, grupo, nome="Broto", is_active=False)
        criar_opcao(db, grupo, nome="Média", is_active=False)
        criar_opcao(db, grupo, nome="Grande", is_active=True)

        bloqueados = AdminMenuRepository(db).product_ids_blocked_by_required_group([produto.id])

        assert bloqueados == set()

    def test_o_recorte_da_pagina_e_respeitado(self, db):
        """UMA consulta para a página inteira, e o `IN (...)` é o recorte dela.

        O produto bloqueado que ficou de fora da lista não pode aparecer no
        resultado: a listagem do painel é paginada, e marcar produto de outra
        página é marcar produto que não está na tela.
        """
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)

        na_pagina = criar_produto(db, restaurante, categoria, nome="Pizza")
        criar_grupo_de_opcoes(db, na_pagina, nome="Tamanho", is_required=True)

        fora_da_pagina = criar_produto(db, restaurante, categoria, nome="Lasanha")
        criar_grupo_de_opcoes(db, fora_da_pagina, nome="Tamanho", is_required=True)

        bloqueados = AdminMenuRepository(db).product_ids_blocked_by_required_group([na_pagina.id])

        assert bloqueados == {na_pagina.id}

    def test_lista_vazia_nao_consulta_o_banco(self, db):
        """Retorno cedo. Sem ele, `IN ()` sai como SQL inválido em alguns
        dialetos e como varredura completa em outros."""
        assert AdminMenuRepository(db).product_ids_blocked_by_required_group([]) == set()

    def test_produto_com_dois_grupos_obrigatorios_vazios_aparece_uma_vez(self, db):
        """O `distinct` da consulta. Sem ele o `set()` esconderia a
        duplicidade aqui, mas a consulta traria duas linhas por produto — e o
        próximo a reaproveitá-la para contar receberia o dobro."""
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Pizza")
        criar_grupo_de_opcoes(db, produto, nome="Tamanho", is_required=True)
        criar_grupo_de_opcoes(db, produto, nome="Massa", is_required=True)

        bloqueados = AdminMenuRepository(db).product_ids_blocked_by_required_group([produto.id])

        assert bloqueados == {produto.id}

    def test_opcao_ativa_de_OUTRO_grupo_nao_desbloqueia(self, db):
        """A correlação do `NOT EXISTS` é com o grupo, não com o produto.

        Sem o `option_group_id == ProductOptionGroup.id` de dentro do EXISTS,
        qualquer opção ativa em qualquer grupo do banco desbloquearia todo
        mundo — e a regra viraria "o produto tem alguma opção ativa".
        """
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Pizza")

        obrigatorio_vazio = criar_grupo_de_opcoes(db, produto, nome="Tamanho", is_required=True)
        assert obrigatorio_vazio.options == []

        opcional_cheio = criar_grupo_de_opcoes(db, produto, nome="Adicionais", is_required=False)
        criar_opcao(db, opcional_cheio, nome="Bacon", is_active=True)

        bloqueados = AdminMenuRepository(db).product_ids_blocked_by_required_group([produto.id])

        assert bloqueados == {produto.id}

    def test_produto_inexistente_na_lista_nao_quebra(self, db):
        assert AdminMenuRepository(db).product_ids_blocked_by_required_group([uuid.uuid4()]) == set()


class TestListagemDeProdutos:
    """`list_products` e `count_products` dividem o mesmo WHERE. O que se
    testa aqui é que eles continuam concordando — divergindo, o painel mostra
    12 produtos e diz que são 40."""

    def test_a_busca_encontra_por_nome_e_por_codigo(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        criar_produto(db, restaurante, categoria, nome="Picanha na Chapa", code="P100")
        criar_produto(db, restaurante, categoria, nome="Frango Grelhado", code="F200")

        repositorio = AdminMenuRepository(db)
        por_nome = repositorio.list_products(restaurante.id, search="picanha")
        por_codigo = repositorio.list_products(restaurante.id, search="F200")

        assert [produto.name for produto in por_nome] == ["Picanha na Chapa"]
        assert [produto.name for produto in por_codigo] == ["Frango Grelhado"]

    def test_o_porcento_digitado_nao_lista_o_cardapio_inteiro(self, db):
        """O escape do `%`. Sem ele o lojista digita "%" e recebe tudo, o que
        parece funcionar até ele digitar "50%" e receber tudo também."""
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        criar_produto(db, restaurante, categoria, nome="Picanha")
        criar_produto(db, restaurante, categoria, nome="Frango")

        encontrados = AdminMenuRepository(db).list_products(restaurante.id, search="%")

        assert encontrados == []

    def test_o_underline_digitado_nao_casa_com_qualquer_letra(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        combo = criar_produto(db, restaurante, categoria, nome="Combo_1")
        criar_produto(db, restaurante, categoria, nome="Combo 2")

        encontrados = AdminMenuRepository(db).list_products(restaurante.id, search="Combo_")

        assert [produto.id for produto in encontrados] == [combo.id]

    def test_o_termo_decomposto_encontra_o_nome_gravado_em_nfc(self, db):
        """Armadilha 31, o lado que este repositorio resolve.

        O ILIKE compara BYTES, e o "e" com acento tem duas formas Unicode. O
        `normalize_text` poe o termo da busca em NFC; o nome ja esta em NFC
        porque o schema de escrita normaliza. Este teste e o par: nome
        composto no banco, termo decomposto na busca — que e o que o teclado
        de iPhone/macOS produz.
        """
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        composto = unicodedata.normalize("NFC", "Fil\u00e9 Mignon")
        criar_produto(db, restaurante, categoria, nome=composto)

        decomposto = unicodedata.normalize("NFD", "Fil\u00e9")
        assert decomposto != unicodedata.normalize("NFC", "Fil\u00e9")

        encontrados = AdminMenuRepository(db).list_products(restaurante.id, search=decomposto)

        assert [produto.name for produto in encontrados] == [composto]

    def test_a_pagina_e_o_total_concordam(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        for indice in range(5):
            criar_produto(db, restaurante, categoria, nome=f"Produto {indice}")

        repositorio = AdminMenuRepository(db)
        primeira_pagina = repositorio.list_products(restaurante.id, limit=2, offset=0)

        assert len(primeira_pagina) == 2
        assert repositorio.count_products(restaurante.id) == 5

    def test_produto_de_outro_restaurante_nao_aparece(self, db):
        meu = criar_restaurante(db, nome="O Meu")
        minha_categoria = criar_categoria(db, meu)
        criar_produto(db, meu, minha_categoria, nome="Meu Produto")

        vizinho = criar_restaurante(db, nome="O Vizinho")
        categoria_do_vizinho = criar_categoria(db, vizinho)
        criar_produto(db, vizinho, categoria_do_vizinho, nome="Produto do Vizinho")

        repositorio = AdminMenuRepository(db)

        assert [produto.name for produto in repositorio.list_products(meu.id)] == ["Meu Produto"]
        assert repositorio.count_products(meu.id) == 1

    def test_a_ordem_e_sort_order_e_o_nome_desempata(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        criar_produto(db, restaurante, categoria, nome="Zebra", sort_order=1)
        criar_produto(db, restaurante, categoria, nome="Abacaxi", sort_order=2)
        criar_produto(db, restaurante, categoria, nome="Banana", sort_order=1)

        encontrados = AdminMenuRepository(db).list_products(restaurante.id)

        assert [produto.name for produto in encontrados] == ["Banana", "Zebra", "Abacaxi"]

    def test_o_filtro_is_active_separa_os_dois_lados(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        criar_produto(db, restaurante, categoria, nome="Ligado", is_active=True)
        criar_produto(db, restaurante, categoria, nome="Desligado", is_active=False)

        repositorio = AdminMenuRepository(db)

        ativos = repositorio.list_products(restaurante.id, is_active=True)
        inativos = repositorio.list_products(restaurante.id, is_active=False)
        todos = repositorio.list_products(restaurante.id)

        assert [produto.name for produto in ativos] == ["Ligado"]
        assert [produto.name for produto in inativos] == ["Desligado"]
        assert len(todos) == 2


class TestAsCategorias:
    def test_a_ordem_e_sort_order_e_o_nome_desempata(self, db):
        restaurante = criar_restaurante(db)
        criar_categoria(db, restaurante, nome="Zebra")
        criar_categoria(db, restaurante, nome="Abacaxi")
        criar_categoria(db, restaurante, nome="Banana")

        encontradas = AdminMenuRepository(db).list_categories(restaurante.id)

        assert [categoria.name for categoria in encontradas] == ["Abacaxi", "Banana", "Zebra"]

    def test_categoria_de_outro_restaurante_nao_e_encontrada(self, db):
        vizinho = criar_restaurante(db, nome="O Vizinho")
        categoria = criar_categoria(db, vizinho)
        meu = criar_restaurante(db, nome="O Meu")

        repositorio = AdminMenuRepository(db)

        assert repositorio.list_categories(meu.id) == []
        assert repositorio.get_category(categoria.id, meu.id) is None
        assert repositorio.get_category(categoria.id, vizinho.id).id == categoria.id
        # As duas consultas por slug espelham o indice unico, que e
            # `(branch_id, slug)` desde a revisao 20260820_0026: elas recebem
            # FILIAL, e nao restaurante. A filial do vizinho serve de
            # "restaurante alheio" aqui porque uma filial pertence a um
            # restaurante so.
        assert repositorio.get_category_by_slug(categoria.slug, filial_padrao(db, meu).id) is None
        assert (
            repositorio.get_category_by_slug(categoria.slug, categoria.branch_id).id
            == categoria.id
        )

    def test_listar_por_ids_recorta_e_confere_o_restaurante(self, db):
        meu = criar_restaurante(db, nome="O Meu")
        minha = criar_categoria(db, meu, nome="Minha")
        vizinho = criar_restaurante(db, nome="O Vizinho")
        do_vizinho = criar_categoria(db, vizinho, nome="Do Vizinho")

        encontradas = AdminMenuRepository(db).list_categories_by_ids(
            [minha.id, do_vizinho.id],
            meu.id,
        )

        assert [categoria.id for categoria in encontradas] == [minha.id]

    def test_o_filtro_de_categoria_vale_na_pagina_e_no_total(self, db):
        restaurante = criar_restaurante(db)
        bebidas = criar_categoria(db, restaurante, nome="Bebidas")
        carnes = criar_categoria(db, restaurante, nome="Carnes")
        suco = criar_produto(db, restaurante, bebidas, nome="Suco")
        criar_produto(db, restaurante, carnes, nome="Picanha")

        repositorio = AdminMenuRepository(db)
        encontrados = repositorio.list_products(restaurante.id, category_id=bebidas.id)

        assert [produto.id for produto in encontrados] == [suco.id]
        assert repositorio.count_products(restaurante.id, category_id=bebidas.id) == 1
        assert repositorio.count_products(restaurante.id) == 2

    def test_listar_por_categoria_ignora_a_paginacao(self, db):
        """Existe separado de `list_products` porque a reordenação precisa do
        conjunto INTEIRO: com `limit`, a partir do 101º produto a conferência
        aprovaria uma lista incompleta e renumeraria por cima do resto."""
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        for indice in range(7):
            criar_produto(db, restaurante, categoria, nome=f"Produto {indice}")

        encontrados = AdminMenuRepository(db).list_products_by_category(
            categoria.id,
            restaurante.id,
        )

        assert len(encontrados) == 7


class TestEscopoPorRestaurante:
    """Grupo e opção não têm `restaurant_id`: chegam nele pela junção com
    `products`. É o que impede um UUID descoberto de outro restaurante de ser
    editado — e junção é justamente o que fake em memória não exercita."""

    def test_grupo_de_outro_restaurante_nao_e_encontrado(self, db):
        vizinho = criar_restaurante(db, nome="O Vizinho")
        categoria = criar_categoria(db, vizinho)
        produto = criar_produto(db, vizinho, categoria)
        grupo = criar_grupo_de_opcoes(db, produto)

        meu = criar_restaurante(db, nome="O Meu")

        repositorio = AdminMenuRepository(db)

        assert repositorio.get_option_group(grupo.id, meu.id) is None
        assert repositorio.get_option_group(grupo.id, vizinho.id) is not None

    def test_opcao_de_outro_restaurante_nao_e_encontrada(self, db):
        vizinho = criar_restaurante(db, nome="O Vizinho")
        categoria = criar_categoria(db, vizinho)
        produto = criar_produto(db, vizinho, categoria)
        grupo = criar_grupo_de_opcoes(db, produto)
        opcao = criar_opcao(db, grupo)

        meu = criar_restaurante(db, nome="O Meu")

        repositorio = AdminMenuRepository(db)

        assert repositorio.get_option(opcao.id, meu.id) is None
        assert repositorio.get_option(opcao.id, vizinho.id) is not None

    def test_produto_de_outro_restaurante_nao_e_encontrado_por_id_nem_por_slug(self, db):
        vizinho = criar_restaurante(db, nome="O Vizinho")
        categoria = criar_categoria(db, vizinho)
        produto = criar_produto(db, vizinho, categoria)
        meu = criar_restaurante(db, nome="O Meu")

        repositorio = AdminMenuRepository(db)

        assert repositorio.get_product(produto.id, meu.id) is None
        assert repositorio.get_product(produto.id, vizinho.id).id == produto.id
        assert repositorio.get_product_by_slug(produto.slug, filial_padrao(db, meu).id) is None
        assert (
            repositorio.get_product_by_slug(produto.slug, produto.branch_id).id == produto.id
        )
        assert repositorio.get_product_with_options(produto.id, meu.id) is None

    def test_o_produto_com_opcoes_traz_grupos_e_opcoes_carregados(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria)
        grupo = criar_grupo_de_opcoes(db, produto, nome="Tamanho")
        criar_opcao(db, grupo, nome="Grande")

        encontrado = AdminMenuRepository(db).get_product_with_options(produto.id, restaurante.id)

        assert [g.name for g in encontrado.option_groups] == ["Tamanho"]
        assert [o.name for o in encontrado.option_groups[0].options] == ["Grande"]

    def test_os_grupos_do_produto_vem_na_ordem_do_cardapio(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria)
        criar_grupo_de_opcoes(db, produto, nome="Zebra")
        criar_grupo_de_opcoes(db, produto, nome="Abacaxi")

        encontrados = AdminMenuRepository(db).list_option_groups(produto.id)

        assert [grupo.name for grupo in encontrados] == ["Abacaxi", "Zebra"]

    def test_os_grupos_desativados_tambem_aparecem_para_o_lojista(self, db):
        """O oposto do cardápio público: o lojista precisa enxergar o que está
        desligado, senão não tem como religar."""
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria)
        criar_grupo_de_opcoes(db, produto, nome="Ligado", is_active=True)
        criar_grupo_de_opcoes(db, produto, nome="Desligado", is_active=False)

        encontrados = AdminMenuRepository(db).list_option_groups(produto.id)

        assert len(encontrados) == 2
