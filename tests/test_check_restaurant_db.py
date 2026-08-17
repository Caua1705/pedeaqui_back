"""`scripts/check_restaurant.py` — os cinco silenciosos do onboarding.

Este teste existe porque o script confere regras que **não estão no código
que ele lê**: elas estão espalhadas entre o cálculo de entrega, a máquina de
horários, a montagem da comanda, o webhook e o congelamento da comissão. Um
script de checklist erra do jeito mais caro possível — dizendo "pronto" para
um restaurante que não está —, e nada no próprio script denunciaria isso.

Cada teste monta o restaurante NO ESTADO EXATO do erro que descreve, e
confere a situação daquele passo. Um restaurante completo, com todos os cinco
resolvidos, fecha o arquivo: sem ele, um script que respondesse ERRO para
tudo passaria em todos os outros testes.

Marcado `db` porque tudo aqui é consulta contra o schema de verdade — a
maioria das cinco conferências é SQL cru, e um nome de coluna errado só
aparece contra um Postgres.
"""

from datetime import time
from decimal import Decimal

import pytest

from scripts.check_restaurant import (
    ATENCAO,
    ERRO,
    OK,
    Restaurante,
    conferir_comissao,
    conferir_horarios,
    conferir_setores_de_impressao,
    conferir_taxa_de_entrega,
    conferir_tudo,
    conferir_webhook_do_pagamento,
)
from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_payment_method_model import BranchPaymentMethod
from src.models.printing_sector_model import PrintingSector
from src.models.restaurant_payment_credential_model import RestaurantPaymentCredential
from tests.fabricas_db import (
    criar_categoria,
    criar_configuracoes,
    criar_filial,
    criar_produto,
    criar_restaurante,
)


pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Montagem
# ---------------------------------------------------------------------------


def _alvo(restaurante) -> Restaurante:
    return Restaurante(str(restaurante.id), restaurante.slug, restaurante.name)


def _abrir_a_semana(db, filial) -> None:
    """As sete faixas. `weekday` 0 = segunda (armadilha 1)."""
    for dia in range(7):
        db.add(BranchBusinessHour(
            branch_id=filial.id, weekday=dia, is_closed=False,
            opens_at=time(11, 0), closes_at=time(23, 0),
        ))
    db.flush()


def _cobrar_entrega(filial) -> None:
    filial.delivery_base_fee = Decimal("5.00")
    filial.delivery_fee_per_km = Decimal("1.50")


def _oferecer_pix_online(db, filial) -> None:
    db.add(BranchPaymentMethod(
        branch_id=filial.id, payment_flow="online", method_type="pix",
        label="Pix", enabled=True, requires_gateway=True,
    ))
    db.flush()


def _cadastrar_credencial(db, restaurante, com_segredo: bool) -> None:
    db.add(RestaurantPaymentCredential(
        restaurant_id=restaurante.id,
        environment="test",
        public_key="TEST-public-key",
        access_token_encrypted="cifrado",
        webhook_secret_encrypted="cifrado" if com_segredo else None,
    ))
    db.flush()


def _setor(db, filial, is_active: bool = True) -> PrintingSector:
    setor = PrintingSector(branch_id=filial.id, name="Cozinha", is_active=is_active)
    db.add(setor)
    db.flush()
    return setor


def restaurante_pronto(db):
    """Os cinco resolvidos. É o unico cenario que pode responder "pronto"."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    _abrir_a_semana(db, filial)
    _cobrar_entrega(filial)
    _oferecer_pix_online(db, filial)
    _cadastrar_credencial(db, restaurante, com_segredo=True)

    configuracoes = criar_configuracoes(db, restaurante)
    configuracoes.platform_commission_percent = Decimal("12.00")

    categoria = criar_categoria(db, restaurante)
    produto = criar_produto(db, restaurante, categoria)
    produto.printing_sector_id = _setor(db, filial).id
    db.flush()
    return restaurante, filial


# ---------------------------------------------------------------------------
# 3.1 Horarios
# ---------------------------------------------------------------------------


def test_filial_sem_nenhum_horario_e_erro(db):
    restaurante = criar_restaurante(db)
    criar_filial(db, restaurante)

    resultado = conferir_horarios(db, _alvo(restaurante))

    assert resultado.situacao == ERRO
    assert "NENHUM dia" in " ".join(resultado.linhas)


def test_dia_marcado_is_closed_nao_conta_como_configurado(db):
    """A linha `is_closed` com horario preenchido e o jeito comum de registrar
    "domingo fechado". Ela existe no banco e NAO abre a loja (armadilha 10)."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    _abrir_a_semana(db, filial)
    domingo = db.query(BranchBusinessHour).filter_by(branch_id=filial.id, weekday=6).one()
    domingo.is_closed = True
    db.flush()

    resultado = conferir_horarios(db, _alvo(restaurante))

    assert resultado.situacao == ATENCAO
    assert "domingo" in " ".join(resultado.linhas)


def test_semana_inteira_aberta_passa(db):
    restaurante = criar_restaurante(db)
    _abrir_a_semana(db, criar_filial(db, restaurante))

    assert conferir_horarios(db, _alvo(restaurante)).situacao == OK


# ---------------------------------------------------------------------------
# 3.2 Taxa de entrega
# ---------------------------------------------------------------------------


def test_sem_taxa_por_km_e_sem_contingencia_e_erro(db):
    """O caso do documento: endereco a 500 m recusado como "fora da area"."""
    restaurante = criar_restaurante(db)
    criar_filial(db, restaurante)
    criar_configuracoes(db, restaurante)

    resultado = conferir_taxa_de_entrega(db, _alvo(restaurante))

    assert resultado.situacao == ERRO
    assert "fora da area" in " ".join(resultado.linhas)


def test_contingencia_maior_que_zero_rebaixa_para_atencao(db):
    """`default_delivery_fee` > 0 salva a entrega, mas cobra o mesmo valor
    para 500 m e para 15 km — é contingencia, não é a taxa do dia a dia."""
    restaurante = criar_restaurante(db)
    criar_filial(db, restaurante)
    configuracoes = criar_configuracoes(db, restaurante)
    configuracoes.default_delivery_fee = Decimal("8.00")
    db.flush()

    assert conferir_taxa_de_entrega(db, _alvo(restaurante)).situacao == ATENCAO


def test_restaurante_que_nao_aceita_entrega_nao_e_cobrado(db):
    restaurante = criar_restaurante(db)
    criar_filial(db, restaurante)
    configuracoes = criar_configuracoes(db, restaurante)
    configuracoes.accepts_delivery = False
    db.flush()

    assert conferir_taxa_de_entrega(db, _alvo(restaurante)).situacao == OK


# ---------------------------------------------------------------------------
# 5.2 Setor de impressao
# ---------------------------------------------------------------------------


def test_cardapio_inteiro_sem_setor_e_erro(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    _setor(db, filial)
    categoria = criar_categoria(db, restaurante)
    criar_produto(db, restaurante, categoria)

    resultado = conferir_setores_de_impressao(db, _alvo(restaurante))

    assert resultado.situacao == ERRO
    assert "NENHUM produto" in " ".join(resultado.linhas)


def test_produto_apontando_para_setor_desativado_e_erro(db):
    """Ele nao some da comanda: vai para a via "SEM SETOR" (armadilha 13)."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    categoria = criar_categoria(db, restaurante)
    produto = criar_produto(db, restaurante, categoria)
    produto.printing_sector_id = _setor(db, filial, is_active=False).id
    db.flush()

    resultado = conferir_setores_de_impressao(db, _alvo(restaurante))

    assert resultado.situacao == ERRO
    assert "SEM SETOR" in " ".join(resultado.linhas)


def test_alguns_produtos_sem_setor_e_so_atencao(db):
    """Nulo e decisao legitima — a lata que sai da geladeira do balcao."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    categoria = criar_categoria(db, restaurante)
    com_setor = criar_produto(db, restaurante, categoria, nome="Picanha")
    com_setor.printing_sector_id = _setor(db, filial).id
    criar_produto(db, restaurante, categoria, nome="Refrigerante")
    db.flush()

    assert conferir_setores_de_impressao(db, _alvo(restaurante)).situacao == ATENCAO


# ---------------------------------------------------------------------------
# 6.4 Segredo do webhook
# ---------------------------------------------------------------------------


def test_pagamento_online_sem_segredo_de_webhook_e_erro(db):
    """O silencioso numero 1: o cliente paga, o dinheiro sai, e o pedido
    nunca chega ao painel."""
    restaurante = criar_restaurante(db)
    _oferecer_pix_online(db, criar_filial(db, restaurante))
    _cadastrar_credencial(db, restaurante, com_segredo=False)

    resultado = conferir_webhook_do_pagamento(db, _alvo(restaurante))

    assert resultado.situacao == ERRO
    assert "SEM segredo" in " ".join(resultado.linhas)


def test_pagamento_online_sem_credencial_nenhuma_e_erro(db):
    restaurante = criar_restaurante(db)
    _oferecer_pix_online(db, criar_filial(db, restaurante))

    assert conferir_webhook_do_pagamento(db, _alvo(restaurante)).situacao == ERRO


def test_sem_metodo_online_o_webhook_nao_e_cobrado(db):
    """Sem cobranca no gateway nao ha webhook para chegar."""
    restaurante = criar_restaurante(db)
    criar_filial(db, restaurante)

    assert conferir_webhook_do_pagamento(db, _alvo(restaurante)).situacao == OK


# ---------------------------------------------------------------------------
# 7 Comissao
# ---------------------------------------------------------------------------


def test_sem_restaurant_settings_a_comissao_fica_no_padrao(db):
    restaurante = criar_restaurante(db)

    resultado = conferir_comissao(db, _alvo(restaurante))

    assert resultado.situacao == ATENCAO
    assert "10.00%" in " ".join(resultado.linhas)


def test_comissao_negociada_diferente_do_padrao_passa(db):
    restaurante = criar_restaurante(db)
    configuracoes = criar_configuracoes(db, restaurante)
    configuracoes.platform_commission_percent = Decimal("12.00")
    db.flush()

    assert conferir_comissao(db, _alvo(restaurante)).situacao == OK


# ---------------------------------------------------------------------------
# O restaurante completo
# ---------------------------------------------------------------------------


def test_restaurante_completo_responde_pronto(db):
    """Sem este teste, um script que respondesse ERRO para tudo passaria em
    todos os outros — cada um deles so afirma que UM caso e detectado."""
    restaurante, _ = restaurante_pronto(db)

    conferencias = conferir_tudo(db, _alvo(restaurante))

    assert [c.situacao for c in conferencias] == [OK] * 5


def test_o_script_nao_escreve_nada(db):
    """So SELECT, por contrato. Se alguem acrescentar um "conserta sozinho",
    esta linha fica vermelha antes de o script rodar em producao."""
    restaurante, _ = restaurante_pronto(db)
    db.flush()

    conferir_tudo(db, _alvo(restaurante))

    assert not db.new and not db.dirty and not db.deleted
