"""A redação de credencial do relato de erro, e o que ela deliberadamente NÃO faz.

Sem banco: o que está sob teste é uma função de texto. O que ela decide, no
entanto, não é cosmético — é o que separa "uma tabela com relatos" de "uma
tabela com tokens de painel em texto puro que ninguém audita".

**A metade negativa deste arquivo é tão importante quanto a positiva.** Os
testes que provam que nome, telefone e endereço NÃO são mascarados existem
para que a decisão fique escrita: regex de nome e endereço acerta metade e dá
confiança falsa, e o relato sem o contexto que o lojista escreveu não
reproduz nada. Quem os apaga é a retenção de 90 dias.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.schemas.admin_error_report_schema import CreateErrorReportRequest
from src.services.admin_error_report_service import (
    ERROR_REPORT_RETENTION_DAYS,
    MARCA,
    error_report_retention_cutoff,
    redigir_credenciais,
)


# ---------------------------------------------------------------------------
# O que SEMPRE é mascarado
# ---------------------------------------------------------------------------


JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGd1ZW0ifQ.T3g4bF9hc3NpbmF0dXJhX2FxdWk"


def test_o_cabecalho_authorization_perde_o_valor_inteiro():
    """A linha fica, o token sai. Quem lê precisa saber que havia um ali."""
    texto = f"POST /admin/orders\nAuthorization: Bearer {JWT}\nAccept: */*"

    redigido = redigir_credenciais(texto)

    assert JWT not in redigido
    assert "Authorization" in redigido
    assert MARCA in redigido
    assert "Accept: */*" in redigido


def test_a_idempotency_key_do_cabecalho_sai():
    texto = "Idempotency-Key: 3f2b1c9e-0000-4444-8888-abcdefabcdef"

    redigido = redigir_credenciais(texto)

    assert "3f2b1c9e" not in redigido


def test_o_jwt_solto_sai_mesmo_sem_cabecalho():
    """O token do painel é reconhecível pelo formato, colado sozinho."""
    redigido = redigir_credenciais(f"o token que estava no localStorage: {JWT}")

    assert JWT not in redigido
    assert MARCA in redigido


def test_a_linha_de_traceback_nao_e_confundida_com_jwt():
    """O falso positivo que quase entrou, e que teria comido o log inteiro.

    "tres blocos base64url separados por ponto" tambem descreve
    `payment_gateway.mercadopago.create_payment`. O `eyJ` obrigatorio no
    comeco e o que separa um JWT de um caminho de modulo — e traceback e
    exatamente o que o lojista tem a colar aqui.
    """
    linha = 'File "src/services/order_service.py", in payment_gateway.mercadopago.create_payment'

    assert redigir_credenciais(linha) == linha


def test_o_tracking_token_sai_da_url_do_acompanhamento():
    """Ele abre o pedido de um cliente, e não há rota que o reemita.

    Guardá-lo aqui seria dar acesso permanente ao pedido de alguém a quem
    lesse a tabela — e a armadilha 19 já registra que o token só sai da API
    uma vez.
    """
    token = "wZ8-kQ3xN1pL7yR2tV5bC0dF4gH6jK9mA1sD3fG5hJ8"
    texto = f"deu 404 em /restaurants/junior/orders/track/{token}"

    redigido = redigir_credenciais(texto)

    assert token not in redigido
    assert "/orders/track/" in redigido


@pytest.mark.parametrize(
    "campo",
    ["password", "senha", "tracking_token", "access_token", "webhook_secret", "signature"],
)
def test_campo_de_json_com_nome_de_segredo_perde_o_valor(campo):
    """O caso mais provável de todos: o painel cola a resposta inteira.

    `CreateOrderResponse` traz o `tracking_token` em claro, e é ela que o
    lojista tem na mão quando o pedido dá errado.
    """
    redigido = redigir_credenciais(f'{{"{campo}": "valor-secreto-aqui", "total": 52.9}}')

    assert "valor-secreto-aqui" not in redigido
    assert "52.9" in redigido


def test_o_resto_do_json_sobrevive_a_redacao():
    """Mascarar demais deixaria o log sem o que se precisa ler nele."""
    texto = '{"order_id": "abc-123", "tracking_token": "segredo", "status": "rejected"}'

    redigido = redigir_credenciais(texto)

    assert "abc-123" in redigido
    assert "rejected" in redigido
    assert "segredo" not in redigido


# ---------------------------------------------------------------------------
# O que NÃO é mascarado, e por quê
# ---------------------------------------------------------------------------


def test_o_nome_e_o_telefone_do_cliente_ficam_como_o_lojista_escreveu():
    """A decisão, escrita como teste.

    Não é esquecimento: o relato é o contexto, e um relato sem contexto não
    reproduz nada. Quem apaga isto é a retenção — ver
    `error_report_retention_cutoff`.
    """
    relato = "o pedido do Joao Silva, telefone 91988887777, nao imprimiu na cozinha"

    assert redigir_credenciais(relato) == relato


def test_o_endereco_fica():
    relato = "cliente da Travessa Quintino Bocaiuva 1200, apto 302, nao recebeu"

    assert redigir_credenciais(relato) == relato


def test_o_texto_sem_credencial_nenhuma_atravessa_intacto():
    relato = "cliquei em salvar no cardapio e a tela ficou branca"

    assert redigir_credenciais(relato) == relato


# ---------------------------------------------------------------------------
# A retenção É o mecanismo de exclusão (armadilha 38)
# ---------------------------------------------------------------------------


def test_o_corte_da_retencao_e_de_noventa_dias():
    agora = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)

    corte = error_report_retention_cutoff(agora)

    assert ERROR_REPORT_RETENTION_DAYS == 90
    assert (agora - corte).days == 90


# ---------------------------------------------------------------------------
# O corpo: só o que o lojista sabe
# ---------------------------------------------------------------------------


def test_o_corpo_recusa_a_filial():
    """Escopo de lojista sai do TOKEN. `extra="forbid"` fecha a porta antes
    de qualquer service ter a chance de obedecer o corpo."""
    with pytest.raises(ValidationError):
        CreateErrorReportRequest(description="deu erro", branch_id="qualquer-coisa")


def test_o_corpo_recusa_o_restaurante():
    with pytest.raises(ValidationError):
        CreateErrorReportRequest(description="deu erro", restaurant_id="qualquer-coisa")


def test_descricao_so_de_espacos_e_recusada_no_schema():
    """`min_length=1` confere o valor CRU: "   " tem três caracteres e passa.

    Sem o validator, o branco chegava ao banco e voltava como violação de
    CHECK — erro de servidor no lugar de erro de campo.
    """
    with pytest.raises(ValidationError):
        CreateErrorReportRequest(description="   ")


def test_os_campos_opcionais_em_branco_viram_nulo():
    corpo = CreateErrorReportRequest(description="deu erro", error_log="  ", screen="")

    assert corpo.error_log is None
    assert corpo.screen is None
