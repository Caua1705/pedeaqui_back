"""Os termos de resgate que o `/menu` publica, por filial.

Contra banco de verdade porque o que está sob teste **é** a herança: qual das
duas linhas de `cashback_rules` vale nesta filial, e o que sai quando não vale
nenhuma. Dublar `resolve_cashback_terms` testaria o dublê, e a herança por
linha é exatamente onde uma segunda implementação discordaria sem erro.

O que isto fecha: o app não conseguia explicar por que o cashback não
descontou. Saldo abaixo do mínimo devolve zero sem erro, e a única pista era
`cashback_redeemed_amount: 0` na resposta do pedido — depois de fechado.
"""

from decimal import Decimal

import pytest

from src.models.cashback_rule_model import CashbackRule
from src.services.menu_service import MenuService
from tests.fabricas_db import criar_filial, criar_restaurante


pytestmark = pytest.mark.db


def criar_regra(
    db,
    restaurante,
    *,
    filial=None,
    enabled: bool = True,
    min_redeem_balance: str = "5.00",
) -> CashbackRule:
    regra = CashbackRule(
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial else None,
        enabled=enabled,
        default_percent=Decimal("5.00"),
        min_redeem_balance=Decimal(min_redeem_balance),
        expiry_days=60,
    )
    db.add(regra)
    db.flush()
    return regra


def cardapio(db, restaurante, filial):
    return MenuService(db).get_restaurant_menu(restaurante.slug, filial.id)


def test_a_filial_publica_o_piso_da_regra_da_rede(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    criar_regra(db, restaurante, min_redeem_balance="10.00")

    termos = cardapio(db, restaurante, filial).settings.cashback

    assert termos.enabled is True
    assert termos.min_redeem_balance == 10.0


def test_a_regra_propria_da_filial_ganha_da_rede(db):
    """A herança é por LINHA: a filial tem a regra inteira ou herda a inteira.

    É o caso que `by_restaurant[]` não conseguia responder — lá a chave é o
    restaurante, e o piso da rede não vale na loja que tem regra própria.
    """
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    criar_regra(db, restaurante, min_redeem_balance="10.00")
    criar_regra(db, restaurante, filial=filial, min_redeem_balance="30.00")

    termos = cardapio(db, restaurante, filial).settings.cashback

    assert termos.min_redeem_balance == 30.0


def test_filial_fora_da_campanha_responde_desligado_com_a_rede_ligada(db):
    """`enabled = false` na filial é como uma loja sai da campanha.

    E é o caso mais caro de não publicar: o cliente tem saldo naquele
    restaurante, a tela de saldo o mostra, e nesta loja ele não vale.
    """
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    criar_regra(db, restaurante)
    criar_regra(db, restaurante, filial=filial, enabled=False)

    termos = cardapio(db, restaurante, filial).settings.cashback

    assert termos.enabled is False


def test_duas_filiais_do_mesmo_restaurante_respondem_pisos_diferentes(db):
    """O motivo inteiro de isto morar no cardápio e não na tela de saldo."""
    restaurante = criar_restaurante(db)
    centro = criar_filial(db, restaurante, nome="Centro")
    aldeota = criar_filial(db, restaurante, nome="Aldeota")
    criar_regra(db, restaurante, min_redeem_balance="10.00")
    criar_regra(db, restaurante, filial=aldeota, min_redeem_balance="50.00")

    assert cardapio(db, restaurante, centro).settings.cashback.min_redeem_balance == 10.0
    assert cardapio(db, restaurante, aldeota).settings.cashback.min_redeem_balance == 50.0


def test_sem_regra_nenhuma_responde_desligado_e_piso_zero(db):
    """O estado em que todo restaurante nasce, e a resposta que o app recebe hoje."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)

    termos = cardapio(db, restaurante, filial).settings.cashback

    assert termos.enabled is False
    assert termos.min_redeem_balance == 0.0


def test_regra_desligada_responde_igual_a_regra_ausente(db):
    """Os dois caem em `SEM_CASHBACK`, e para a tela do cliente são a mesma frase.

    A distinção entre "ninguém configurou" e "configurado e desligado" existe
    no painel do lojista (`source: "none"`), e é lá que ela serve para
    alguma coisa.
    """
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    criar_regra(db, restaurante, enabled=False, min_redeem_balance="10.00")

    termos = cardapio(db, restaurante, filial).settings.cashback

    assert termos.enabled is False
    assert termos.min_redeem_balance == 0.0


def test_o_percentual_nao_e_publicado(db):
    """Quem resolve o percentual do dia é o checkout, com `order.created_at`.

    Publicá-lo na abertura do cardápio criaria a segunda resposta para
    "quanto gera", e as duas discordariam sempre que a meia-noite caísse
    entre abrir o cardápio e fechar o pedido.
    """
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    criar_regra(db, restaurante)

    termos = cardapio(db, restaurante, filial).settings.cashback

    assert "percent" not in termos.model_dump()
