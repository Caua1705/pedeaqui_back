"""Grupo de adicionais sem nenhuma opcao ativa nao bloqueia o pedido.

O outro lado de `MenuService._answerable_option_groups`, e os dois precisam
concordar: escondendo o grupo so no cardapio, o cliente fecharia o pedido e
levaria um 400 sobre um passo que ele nunca viu — mais dificil de diagnosticar
que o problema original.

O PROBLEMA ORIGINAL: um grupo `is_required` cujas opcoes foram todas
desativadas deixava o produto IMPOSSIVEL DE VENDER. O cliente nao tinha o que
selecionar, entao:

    nao seleciona nada        -> 400 "Opcao obrigatoria nao selecionada"
    manda a opcao inativa     -> 400 "Opcao invalida para este grupo"

Nenhum caminho de compra, nada no log, e acontecendo sozinho — o lojista
desativa opcao todo dia, e desativar a ultima de um grupo obrigatorio nao
avisava nada.
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
    def test_the_order_goes_through_without_the_choice(self):
        """O caso que travava a venda. Hoje o pedido passa sem aquele grupo."""
        grupo = make_group(options=[make_option(is_active=False), make_option(is_active=False)])
        produto = make_product([grupo])

        assert service()._validate_selected_options(produto, []) == []

    def test_an_inactive_group_never_blocked_anyway(self):
        """Grupo desativado inteiro ja era ignorado antes — nao e o caso que
        mudou. Fica registrado para a fronteira ficar visivel."""
        grupo = make_group(is_active=False, options=[make_option(is_active=False)])

        assert service()._validate_selected_options(make_product([grupo]), []) == []

    def test_a_second_group_that_still_has_options_keeps_being_required(self):
        """A permissao vale SO para o grupo vazio. Um grupo obrigatorio ao
        lado, com opcao ativa, continua obrigatorio — senao um grupo vazio
        desligaria a exigencia do produto inteiro."""
        vazio = make_group("Tamanho", [make_option(is_active=False)])
        com_opcao = make_group("Ponto da carne", [make_option()])
        produto = make_product([vazio, com_opcao])

        with pytest.raises(HTTPException) as exc:
            service()._validate_selected_options(produto, [])

        assert exc.value.status_code == 400
        assert "Ponto da carne" in exc.value.detail

    def test_choosing_the_option_of_the_healthy_group_is_enough(self):
        vazio = make_group("Tamanho", [make_option(is_active=False)])
        com_opcao = make_group("Ponto da carne", [make_option()])
        produto = make_product([vazio, com_opcao])

        resultado = service()._validate_selected_options(
            produto, [selecao(com_opcao, com_opcao.options[0])]
        )

        assert len(resultado) == 1


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
