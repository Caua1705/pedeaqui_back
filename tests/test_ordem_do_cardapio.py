"""A ordem dos grupos de opcao e das opcoes, e que ela e a MESMA nas duas telas.

Ate 02/09/2026 `Product.option_groups` e `ProductOptionGroup.options` eram
`relationship(...)` sem `order_by`, e `selectinload` **nao ordena**: ele emite
um segundo `SELECT ... WHERE fk IN (...)` sem `ORDER BY` nenhum, e o Postgres
devolve as linhas na ordem que for barata para ele. Consequencias, as duas
relatadas pelo front:

- o `sort_order` que o lojista arrasta no painel **nao tinha efeito** na
  vitrine;
- painel e vitrine podiam mostrar os mesmos adicionais em ordens **diferentes**
  na mesma hora, porque so o painel ordenava — e so os grupos, nao as opcoes.

Os testes com banco sao os que valem: sem `ORDER BY`, a ordem de um `SELECT` e
uma decisao do planejador, e planejador nao se dubla. Os rapidos guardam a
outra metade — que as duas expressoes continuam sendo a MESMA, e nao duas
copias parecidas.
"""

import pytest

from src.models.product_model import Product
from src.models.product_option_model import (
    ProductOptionGroup,
    ordem_das_opcoes,
    ordem_dos_grupos,
)
from src.repositories.admin_menu_repository import AdminMenuRepository
from src.repositories.product_repository import ProductRepository
from tests.fabricas_db import (
    criar_categoria,
    criar_grupo_de_opcoes,
    criar_opcao,
    criar_produto,
    criar_restaurante,
    filial_padrao,
)


class TestUmaDefinicaoSo:
    """A ordem mora numa funcao, e as duas pontas chamam ELA.

    Duas listas parecidas divergiriam no dia em que alguem ajustasse uma —
    que e o mesmo defeito consertado aqui, so que uma camada acima.
    """

    @staticmethod
    def _colunas(expressoes) -> list[str]:
        """`str()` cru nao serve para comparar os dois lados.

        O `relationship` guarda a `Column` ja resolvida
        (`product_option_groups.sort_order`) e a funcao devolve o atributo do
        mapper (`ProductOptionGroup.sort_order`). Sao a MESMA coluna com dois
        `repr` diferentes, e comparar o texto acusaria uma diferenca que nao
        existe. `.expression` traz os dois para a forma do SQL.
        """
        return [str(expressao.expression) for expressao in expressoes]

    def test_o_relationship_dos_grupos_usa_a_funcao(self):
        do_relationship = self._colunas(Product.option_groups.property.order_by)

        assert do_relationship == self._colunas(ordem_dos_grupos())

    def test_o_relationship_das_opcoes_usa_a_funcao(self):
        do_relationship = self._colunas(ProductOptionGroup.options.property.order_by)

        assert do_relationship == self._colunas(ordem_das_opcoes())

    def test_a_ordem_termina_em_chave_unica(self):
        """O desempate tem que ser TOTAL.

        `sort_order` nasce 0 para todo mundo (`DEFAULT 0` na coluna), entao ate
        o lojista arrastar a lista o desempate decide a ordem inteira. Sem uma
        chave unica no fim, duas linhas com o mesmo `sort_order` e o mesmo
        `name` podem sair trocadas entre duas requisicoes — o cardapio piscando
        sem nada ter mudado.
        """
        for ordem in (ordem_dos_grupos(), ordem_das_opcoes()):
            assert ordem[-1].primary_key, f"a ultima chave de {ordem} nao e unica"


@pytest.mark.db
class TestAVitrineRespeitaOSortOrder:
    """O defeito relatado pelo front, contra o Postgres."""

    def _produto_com_grupos_fora_de_ordem(self, db):
        """Cadastra na ordem TROCADA de proposito.

        Inserir ja na ordem certa faria o teste passar com ou sem `ORDER BY` —
        o Postgres costuma devolver na ordem de insercao numa tabela pequena e
        recem-escrita. Ai o teste ficaria verde pelo motivo errado, que e o
        modo de falhar mais caro que esta suite ja teve.
        """
        restaurante = criar_restaurante(db)
        filial = filial_padrao(db, restaurante)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Pizza")

        terceiro = criar_grupo_de_opcoes(db, produto, nome="Bebida", sort_order=3)
        primeiro = criar_grupo_de_opcoes(db, produto, nome="Tamanho", sort_order=1)
        segundo = criar_grupo_de_opcoes(db, produto, nome="Borda", sort_order=2)

        criar_opcao(db, primeiro, nome="Pequena", sort_order=2)
        criar_opcao(db, primeiro, nome="Grande", sort_order=1)
        for grupo in (segundo, terceiro):
            criar_opcao(db, grupo, nome="Alguma", sort_order=1)

        db.commit()
        return filial, categoria, produto, [primeiro, segundo, terceiro]

    def test_os_grupos_saem_na_ordem_do_painel(self, db):
        filial, categoria, _, esperados = self._produto_com_grupos_fora_de_ordem(db)

        produtos = ProductRepository(db).list_active_by_category_slug(filial.id, categoria.slug)

        nomes = [grupo.name for grupo in produtos[0].option_groups]
        assert nomes == [grupo.name for grupo in esperados]

    def test_as_opcoes_saem_na_ordem_do_painel(self, db):
        """A metade que nem o painel tinha: `list_option_groups` ordenava os
        grupos e deixava as opcoes de dentro soltas."""
        filial, categoria, _, _ = self._produto_com_grupos_fora_de_ordem(db)

        produtos = ProductRepository(db).list_active_by_category_slug(filial.id, categoria.slug)

        tamanho = produtos[0].option_groups[0]
        assert [opcao.name for opcao in tamanho.options] == ["Grande", "Pequena"]

    def test_a_vitrine_e_o_painel_concordam(self, db):
        """O pedido do front, dito como ele foi dito: a mesma ordem nas duas."""
        filial, categoria, produto, _ = self._produto_com_grupos_fora_de_ordem(db)

        da_vitrine = ProductRepository(db).list_active_by_category_slug(filial.id, categoria.slug)[0]
        do_painel = AdminMenuRepository(db).list_option_groups(produto.id)

        assert [grupo.name for grupo in da_vitrine.option_groups] == [
            grupo.name for grupo in do_painel
        ]
        assert [
            [opcao.name for opcao in grupo.options] for grupo in da_vitrine.option_groups
        ] == [[opcao.name for opcao in grupo.options] for grupo in do_painel]

    def test_o_produto_avulso_tambem_sai_ordenado(self, db):
        """`get_active_by_slug` e outro `selectinload`, e sao seis no total.

        Consertar no `relationship` e o que alcanca os seis de uma vez; um
        `.order_by` por consulta deixaria o proximo `selectinload` sem ordem de
        novo, calado.
        """
        filial, _, produto, esperados = self._produto_com_grupos_fora_de_ordem(db)

        achado = ProductRepository(db).get_active_by_slug(filial.id, produto.slug)

        assert [grupo.name for grupo in achado.option_groups] == [g.name for g in esperados]
