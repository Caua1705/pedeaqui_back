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

**A sexta conferência (canal de WhatsApp) é de outra natureza, e os testes
dela travam justamente isso: ela nunca sai ERRO.** Um restaurante sem
WhatsApp continua pronto para o primeiro pedido — o pedido entra, a comanda
sai, o dinheiro entra —, e fazer a falta dele derrubar o código de saída
transformaria uma frente opcional em porta de instalação.

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
    conferir_canal_de_whatsapp,
    conferir_comissao,
    conferir_horarios,
    conferir_setores_de_impressao,
    conferir_taxa_de_entrega,
    conferir_tudo,
    conferir_webhook_do_pagamento,
)
from src.core.config import settings
from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_payment_method_model import BranchPaymentMethod
from src.models.printing_sector_model import PrintingSector
from src.models.restaurant_payment_credential_model import RestaurantPaymentCredential
from src.models.whatsapp_model import WhatsAppChannel
from src.utils.crypto import encrypt_whatsapp_token
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


_numeros = iter(range(1000, 9999))


def _canal(db, restaurante, filial=None, *, is_active=True, disconnected_at=None,
           disconnect_reason=None) -> WhatsAppChannel:
    """Uma linha de canal. `filial=None` e a QUEDA DO RESTAURANTE.

    `branch_id` nulo nao e "canal sem dono": e a linha que toda filial sem a
    sua propria herda, e so nulo significa isso.
    """
    numero = next(_numeros)
    canal = WhatsAppChannel(
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial is not None else None,
        waba_id=f"waba-{numero}",
        phone_number_id=f"pni-{numero}",
        display_phone_number=f"+55 85 9{numero}-0000",
        access_token_encrypted=encrypt_whatsapp_token("EAAG-token"),
        is_active=is_active,
        disconnected_at=disconnected_at,
        disconnect_reason=disconnect_reason,
    )
    db.add(canal)
    db.flush()
    return canal


@pytest.fixture
def chave_de_cifra(monkeypatch):
    """A chave do Fernet do WhatsApp, so para `_canal` conseguir cifrar.

    O canal guarda o token do lojista cifrado, e a coluna e `NOT NULL`. A
    conferencia nunca decifra nada — mas gravar um texto qualquer ali
    descreveria uma linha que o cadastro nao produz.
    """
    from cryptography.fernet import Fernet

    monkeypatch.setattr(
        settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


@pytest.fixture
def avisos_ligados(monkeypatch, chave_de_cifra):
    """A chave da plataforma ligada.

    Ela nasce FALSA de proposito (quem liga envio para telefone de cliente de
    verdade liga de proposito), entao o cenario "tudo certo" precisa dize-lo.
    """
    monkeypatch.setattr(settings, "WHATSAPP_NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "segredo-de-teste")


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


def test_filial_que_nao_aceita_entrega_nao_e_cobrada(db):
    """`accepts_delivery` e da FILIAL desde a revisao 20260818_0025.

    Enquanto foi do restaurante, o quiosque de shopping que so faz retirada
    desligava a conferencia da rede inteira — e a loja de rua ao lado, sem
    taxa por km cadastrada, passava no check sem ninguem olhar.
    """
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    criar_configuracoes(db, restaurante)
    filial.accepts_delivery = False
    db.flush()

    assert conferir_taxa_de_entrega(db, _alvo(restaurante)).situacao == OK


def test_a_filial_que_entrega_e_conferida_mesmo_com_a_vizinha_desligada(db):
    restaurante = criar_restaurante(db)
    quiosque = criar_filial(db, restaurante, nome="Quiosque")
    criar_filial(db, restaurante, nome="Rua")
    criar_configuracoes(db, restaurante)
    quiosque.accepts_delivery = False
    db.flush()

    # A filial "Rua" nao tem base nem por-km, e o restaurante nao tem
    # contingencia: toda entrega dela vira "fora da area".
    assert conferir_taxa_de_entrega(db, _alvo(restaurante)).situacao == ERRO


def test_a_contingencia_da_filial_vence_a_do_restaurante(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    configuracoes = criar_configuracoes(db, restaurante)
    configuracoes.default_delivery_fee = Decimal("0.00")
    filial.default_delivery_fee = Decimal("8.00")
    db.flush()

    # O zero do restaurante desligaria a contingencia; a filial sobrescreveu.
    assert conferir_taxa_de_entrega(db, _alvo(restaurante)).situacao == ATENCAO


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
# WA. Canal de WhatsApp
# ---------------------------------------------------------------------------


def test_sem_canal_nenhum_e_atencao_e_nunca_erro(db):
    """A conferencia que nao bloqueia.

    Aviso de WhatsApp e opcional: sem ele o pedido entra, a comanda sai e o
    dinheiro entra. ERRO aqui derrubaria o codigo de saida de todo
    restaurante que ainda nao conectou o numero — e o script existe para
    dizer o que IMPEDE o primeiro pedido."""
    restaurante, _ = restaurante_pronto(db)

    conferencia = conferir_canal_de_whatsapp(db, _alvo(restaurante))

    assert conferencia.situacao == ATENCAO
    assert "nenhum numero cadastrado" in " ".join(conferencia.linhas)


def test_filial_que_herda_o_numero_do_restaurante_sai_OK(db, avisos_ligados):
    """O achado que motiva a conferencia inteira.

    A filial que HERDA e a filial que nao tem numero nenhum sao
    indistinguiveis de fora, e so uma delas avisa o cliente. Sem esta linha,
    o dono da loja que herda acha que ela esta muda."""
    restaurante, filial = restaurante_pronto(db)
    canal = _canal(db, restaurante)

    conferencia = conferir_canal_de_whatsapp(db, _alvo(restaurante))

    assert conferencia.situacao == OK
    texto = " ".join(conferencia.linhas)
    assert "herda o numero do restaurante" in texto
    assert canal.display_phone_number in texto
    assert filial.name in texto


def test_numero_proprio_no_ar_sai_OK(db, avisos_ligados):
    restaurante, filial = restaurante_pronto(db)
    canal = _canal(db, restaurante, filial)

    conferencia = conferir_canal_de_whatsapp(db, _alvo(restaurante))

    assert conferencia.situacao == OK
    assert f"numero proprio {canal.display_phone_number}" in " ".join(conferencia.linhas)


def test_numero_proprio_desligado_NAO_cai_no_do_restaurante(db, avisos_ligados):
    """A regra de `resolve_for_branch`, e o motivo de a ordem das perguntas
    nao poder ser invertida.

    Cair seria a loja passando a falar por outro numero sem ninguem ter
    pedido. O que se espera de um numero desligado e que a loja PARE de
    mandar — e e isso que a linha precisa dizer, senao quem le ve o numero
    do restaurante logo abaixo e conclui que esta coberto."""
    restaurante, filial = restaurante_pronto(db)
    _canal(db, restaurante)
    _canal(db, restaurante, filial, is_active=False)

    conferencia = conferir_canal_de_whatsapp(db, _alvo(restaurante))

    assert conferencia.situacao == ATENCAO
    texto = " ".join(conferencia.linhas)
    assert "DESLIGADO no painel" in texto
    assert "NAO cai no numero do restaurante" in texto


def test_desconectado_pela_meta_diz_que_o_conserto_nao_e_nosso(db, avisos_ligados):
    """O unico estado cujo conserto comeca FORA daqui.

    Confundi-lo com "desligado no painel" faz o dono clicar em conectar no
    nosso painel e continuar sem receber nada (docs/whatsapp.md, 9.3)."""
    from datetime import datetime, timezone

    restaurante, filial = restaurante_pronto(db)
    _canal(db, restaurante, filial,
           disconnected_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
           disconnect_reason="PARTNER_REMOVED")

    conferencia = conferir_canal_de_whatsapp(db, _alvo(restaurante))

    assert conferencia.situacao == ATENCAO
    texto = " ".join(conferencia.linhas)
    assert "DESCONECTADO PELA META em 01/09/2026" in texto
    assert "PARTNER_REMOVED" in texto
    assert "WhatsApp da loja" in conferencia.o_que_fazer


def test_a_chave_da_plataforma_desligada_com_numero_cadastrado_e_atencao(
    db, monkeypatch, chave_de_cifra
):
    """O numero esta la, o trabalho foi feito, e nada sai.

    E o estado mais caro de nao enxergar: tudo parece configurado, e a chave
    que cala tudo e uma variavel de ambiente que nao aparece em tela
    nenhuma."""
    monkeypatch.setattr(settings, "WHATSAPP_NOTIFICATIONS_ENABLED", False)
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "segredo-de-teste")
    restaurante, filial = restaurante_pronto(db)
    _canal(db, restaurante, filial)

    conferencia = conferir_canal_de_whatsapp(db, _alvo(restaurante))

    assert conferencia.situacao == ATENCAO
    assert "WHATSAPP_NOTIFICATIONS_ENABLED esta FALSA" in " ".join(conferencia.linhas)


def test_sem_app_secret_o_webhook_de_status_nunca_volta(db, monkeypatch, chave_de_cifra):
    monkeypatch.setattr(settings, "WHATSAPP_NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", None)
    restaurante, filial = restaurante_pronto(db)
    _canal(db, restaurante, filial)

    conferencia = conferir_canal_de_whatsapp(db, _alvo(restaurante))

    assert conferencia.situacao == ATENCAO
    assert "WHATSAPP_APP_SECRET ausente" in " ".join(conferencia.linhas)


# ---------------------------------------------------------------------------
# O restaurante completo
# ---------------------------------------------------------------------------


def test_restaurante_completo_responde_pronto(db):
    """Sem este teste, um script que respondesse ERRO para tudo passaria em
    todos os outros — cada um deles so afirma que UM caso e detectado.

    A sexta sai ATENCAO porque `restaurante_pronto` nao conecta WhatsApp — e
    e assim que tem que ser: os cinco silenciosos sao o que impede o primeiro
    pedido, e o aviso ao cliente nao esta entre eles."""
    restaurante, _ = restaurante_pronto(db)

    conferencias = conferir_tudo(db, _alvo(restaurante))

    assert [c.situacao for c in conferencias] == [OK] * 5 + [ATENCAO]
    assert not [c for c in conferencias if c.situacao == ERRO]


def test_restaurante_completo_com_whatsapp_sai_OK_nos_seis(db, avisos_ligados):
    restaurante, filial = restaurante_pronto(db)
    _canal(db, restaurante, filial)

    conferencias = conferir_tudo(db, _alvo(restaurante))

    assert [c.situacao for c in conferencias] == [OK] * 6


def test_o_script_nao_escreve_nada(db):
    """So SELECT, por contrato. Se alguem acrescentar um "conserta sozinho",
    esta linha fica vermelha antes de o script rodar em producao."""
    restaurante, _ = restaurante_pronto(db)
    db.flush()

    conferir_tudo(db, _alvo(restaurante))

    assert not db.new and not db.dirty and not db.deleted
