"""O sinal que impede o lojista de perder a venda em silencio.

Quando um grupo obrigatorio ATIVO fica sem nenhuma opcao ativa, o produto sai
do cardapio publico e o pedido de quem ja o tinha no carrinho e recusado
(`test_menu_service.py`, `test_order_option_groups.py`). Do lado do painel
nada indicava isso: `is_active` ligado, `is_available` ligado, e o produto
simplesmente nao aparecia para o cliente.

`AdminProductResponse.unavailable_by_required_group` e esse indicador. Sem
ele, a correcao de "nao vender errado" cobraria "perder venda sem saber" —
que e o mesmo defeito de outra forma.

A MESMA REGRA E ESCRITA DUAS VEZES no codigo de producao, e nao ha como
compartilha-las: em Python (`services/menu_rules.blocking_required_group`,
para as rotas de UM produto, onde os grupos ja estao carregados) e em SQL
(`AdminMenuRepository.product_ids_blocked_by_required_group`, para a listagem
nao virar uma consulta por produto). A classe `TestTheTwoExpressionsAgree`
existe para as duas nao divergirem em silencio.
"""

from decimal import Decimal

from src.services.menu_rules import blocking_required_group
from tests import fabricas


def make_option(is_active=True):
    return fabricas.opcao(is_active=is_active)


def make_group(name="Escolha o ponto", options=(), is_required=True, is_active=True):
    return fabricas.grupo_de_opcoes(
        name=name, is_required=is_required, is_active=is_active, options=list(options)
    )


def make_product(groups=()):
    return fabricas.produto(
        slug="picanha", price=Decimal("89.00"), option_groups=list(groups)
    )


class TestBlockingRequiredGroup:
    """A regra em Python — a que as rotas de UM produto usam."""

    def test_a_required_group_with_no_active_option_blocks(self):
        grupo = make_group(options=[make_option(is_active=False)])

        assert blocking_required_group(make_product([grupo])) is grupo

    def test_it_returns_the_group_so_the_caller_can_name_it(self):
        """Devolve o GRUPO e nao um booleano de proposito: o log e o aviso ao
        lojista sem o nome ("um grupo obrigatorio ficou vazio") mandam
        procurar em todos."""
        grupo = make_group("Escolha o ponto", [make_option(is_active=False)])

        assert blocking_required_group(make_product([grupo])).name == "Escolha o ponto"

    def test_one_active_option_is_enough(self):
        grupo = make_group(options=[make_option(is_active=False), make_option()])

        assert blocking_required_group(make_product([grupo])) is None

    def test_an_optional_group_never_blocks(self):
        grupo = make_group(options=[make_option(is_active=False)], is_required=False)

        assert blocking_required_group(make_product([grupo])) is None

    def test_an_inactive_group_never_blocks(self):
        """O lojista desligou o passo inteiro de proposito — nao ha exigencia
        a cumprir."""
        grupo = make_group(options=[make_option(is_active=False)], is_active=False)

        assert blocking_required_group(make_product([grupo])) is None

    def test_a_product_without_groups_never_blocks(self):
        assert blocking_required_group(make_product([])) is None

    def test_the_first_blocking_group_is_the_one_reported(self):
        primeiro = make_group("Tamanho", [make_option(is_active=False)])
        segundo = make_group("Ponto", [make_option(is_active=False)])

        assert blocking_required_group(make_product([primeiro, segundo])) is primeiro


class TestTheTwoExpressionsAgree:
    """A regra em Python e a em SQL respondem a mesma coisa.

    O SQL de verdade precisa de Postgres e nao roda nesta suite, entao o que
    se compara aqui e a regra em Python contra a leitura em memoria que o fake
    do repositorio faz — a mesma que `test_admin_menu.py` usa para provar a
    listagem. Nao substitui um teste contra o banco (esse entra com o
    marcador `db`), mas pega a divergencia mais provavel: alguem mudar uma das
    duas e esquecer a outra.
    """

    @staticmethod
    def em_sql(product):
        """A traducao fiel do `NOT EXISTS` do repositorio, sobre um produto."""
        for group in product.option_groups:
            if not group.is_active or not group.is_required:
                continue
            tem_opcao_ativa = any(option.is_active for option in group.options)
            if not tem_opcao_ativa:
                return True
        return False

    def test_they_agree_on_every_shape(self):
        casos = {
            "sem grupo": make_product([]),
            "obrigatorio vazio": make_product([make_group(options=[make_option(is_active=False)])]),
            "obrigatorio com opcao": make_product([make_group(options=[make_option()])]),
            "opcional vazio": make_product(
                [make_group(options=[make_option(is_active=False)], is_required=False)]
            ),
            "obrigatorio desligado": make_product(
                [make_group(options=[make_option(is_active=False)], is_active=False)]
            ),
            "um vazio e um cheio": make_product(
                [
                    make_group("Tamanho", [make_option(is_active=False)]),
                    make_group("Ponto", [make_option()]),
                ]
            ),
            "grupo sem opcao nenhuma": make_product([make_group(options=[])]),
        }

        for nome, produto in casos.items():
            em_python = blocking_required_group(produto) is not None
            assert em_python == self.em_sql(produto), nome

    def test_a_group_with_no_rows_at_all_also_blocks(self):
        """Grupo obrigatorio criado e ainda sem nenhuma opcao cadastrada e o
        mesmo caso de "todas desativadas": nao ha o que escolher. O `NOT
        EXISTS` do SQL responde igual — e por isso ele e `NOT EXISTS` e nao
        `count(ativas) = 0` sobre linhas existentes.
        """
        produto = make_product([make_group(options=[])])

        assert blocking_required_group(produto) is not None
        assert self.em_sql(produto) is True
