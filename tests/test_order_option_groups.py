"""Grupo obrigatorio sem opcao ativa continua RECUSANDO o pedido.

E o outro lado de `MenuService._blocking_required_group`, e os dois precisam
concordar: o produto sai do cardapio publico, e quem ja o tinha no carrinho
leva este 400 no checkout — o mesmo papel que o 400 de `is_available` cumpre
para o produto esgotado (armadilha 23).

POR QUE NAO VENDER SEM A ESCOLHA: grupo obrigatorio existe porque a cozinha
nao produz sem aquela informacao. Vender sem ela manda uma picanha sem ponto
para a chapa — e esconde o erro do lojista, porque os pedidos continuam
entrando e ninguem descobre que ele desativou tudo por engano.

O que ESTE arquivo trava e que a recusa continua de pe. Que o produto some da
vitrine esta em `test_menu_service.py::TestProductsOutOfSale`, e o sinal para
o lojista em `test_admin_product_availability.py`.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.services.order_service import OrderService


def make_option(is_active=True, additional_price="0.00"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Opcao",
        is_active=is_active,
        additional_price=Decimal(additional_price),
    )


def make_group(name="Escolha o tamanho", options=(), is_required=True, is_active=True, min_select=1, max_select=1):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        is_active=is_active,
        is_required=is_required,
        min_select=min_select,
        max_select=max_select,
        options=list(options),
    )


def make_product(groups=()):
    return SimpleNamespace(id=uuid.uuid4(), name="Pizza Calabresa", option_groups=list(groups))


def selecao(group, option):
    return SimpleNamespace(option_group_id=group.id, option_id=option.id)


def service():
    return OrderService.__new__(OrderService)


class TestRequiredGroupWithoutActiveOptions:
    def test_the_order_is_refused_and_there_is_no_way_around_it(self):
        """As DUAS saidas do cliente batem na parede, e e proposital.

        O produto ja nao esta no cardapio nesse estado, entao quem chega aqui
        e carrinho montado antes. Nao ha o que aceitar: mandar o pedido para a
        cozinha sem a escolha obrigatoria e o que esta correcao existe para
        impedir.
        """
        morta = make_option(is_active=False)
        grupo = make_group(options=[morta, make_option(is_active=False)])
        produto = make_product([grupo])

        # 1. nao selecionar nada
        with pytest.raises(HTTPException) as sem_escolha:
            service()._validate_selected_options(produto, [])
        assert sem_escolha.value.detail == "Opcao obrigatoria nao selecionada: Escolha o tamanho"

        # 2. mandar a opcao desativada
        with pytest.raises(HTTPException) as com_opcao_morta:
            service()._validate_selected_options(produto, [selecao(grupo, morta)])
        assert com_opcao_morta.value.detail == "Opcao invalida para este grupo"

    def test_an_inactive_group_does_not_block(self):
        """Grupo obrigatorio DESATIVADO nao exige nada — o lojista desligou o
        passo inteiro de proposito. E a mesma fronteira que mantem o produto
        no cardapio (`test_menu_service.py`)."""
        grupo = make_group(is_active=False, options=[make_option(is_active=False)])

        assert service()._validate_selected_options(make_product([grupo]), []) == []

    def test_an_empty_optional_group_does_not_block(self):
        """So o obrigatorio recusa. Uma pizza sem borda recheada disponivel
        continua vendavel — e continua no cardapio."""
        grupo = make_group("Bordas", [make_option(is_active=False)], is_required=False, min_select=0)

        assert service()._validate_selected_options(make_product([grupo]), []) == []

    def test_a_healthy_group_next_to_it_does_not_rescue_the_order(self):
        """Escolher o que da para escolher nao libera o pedido: o grupo vazio
        continua exigindo o que ninguem tem como dar."""
        vazio = make_group("Tamanho", [make_option(is_active=False)])
        com_opcao = make_group("Ponto da carne", [make_option()])
        produto = make_product([vazio, com_opcao])

        with pytest.raises(HTTPException) as exc:
            service()._validate_selected_options(produto, [selecao(com_opcao, com_opcao.options[0])])

        assert "Tamanho" in exc.value.detail


class TestTheChecksThatDidNotChange:
    def test_a_required_group_with_options_still_blocks_an_empty_selection(self):
        grupo = make_group(options=[make_option()])

        with pytest.raises(HTTPException) as exc:
            service()._validate_selected_options(make_product([grupo]), [])

        assert exc.value.detail == "Opcao obrigatoria nao selecionada: Escolha o tamanho"

    def test_an_inactive_option_is_still_refused(self):
        """A permissao do grupo vazio nao virou permissao para VENDER opcao
        desativada: o item esgotado continua recusado."""
        morta = make_option(is_active=False)
        grupo = make_group(options=[make_option(), morta])
        produto = make_product([grupo])

        with pytest.raises(HTTPException) as exc:
            service()._validate_selected_options(produto, [selecao(grupo, morta)])

        assert exc.value.detail == "Opcao invalida para este grupo"

    def test_the_max_select_ceiling_still_applies(self):
        a, b = make_option(), make_option()
        grupo = make_group(options=[a, b], max_select=1)
        produto = make_product([grupo])

        with pytest.raises(HTTPException) as exc:
            service()._validate_selected_options(produto, [selecao(grupo, a), selecao(grupo, b)])

        assert "no maximo" in exc.value.detail

    def test_a_duplicated_option_is_still_refused(self):
        a = make_option()
        grupo = make_group(options=[a], max_select=2)
        produto = make_product([grupo])

        with pytest.raises(HTTPException) as exc:
            service()._validate_selected_options(produto, [selecao(grupo, a), selecao(grupo, a)])

        assert exc.value.detail == "Opcao duplicada no mesmo produto"

    def test_an_option_from_another_product_is_still_refused(self):
        grupo = make_group(options=[make_option()])
        outro_grupo = make_group("De outro produto", [make_option()])
        produto = make_product([grupo])

        with pytest.raises(HTTPException) as exc:
            service()._validate_selected_options(
                produto, [selecao(outro_grupo, outro_grupo.options[0])]
            )

        assert exc.value.detail == "Grupo de opcao invalido para este produto"
