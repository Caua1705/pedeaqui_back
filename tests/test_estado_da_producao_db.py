"""`scripts/estado_da_producao.py` — as duas conferencias que consultam o banco.

Marcado `db` porque as duas sao SQL cru contra o schema de verdade: a leitura
de `alembic_version` e a varredura de quem oferece pagamento online. Nome de
coluna errado nas duas so aparece contra um Postgres — a suite rapida
passaria verde descrevendo uma consulta que nunca roda.

A fixture `db` reverte tudo no fim, inclusive o UPDATE em `alembic_version`
que alguns testes daqui precisam fazer para simular um banco fora de head.
"""

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from scripts.estado_da_producao import (
    ATENCAO,
    ERRO,
    OK,
    conferir_a_revisao_do_banco,
    conferir_o_mercado_pago,
)
from src.core.config import settings
from src.models.branch_payment_method_model import BranchPaymentMethod
from src.models.restaurant_payment_credential_model import RestaurantPaymentCredential
from src.utils.crypto import encrypt_secret
from tests.fabricas_db import criar_filial, criar_restaurante


pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Montagem
# ---------------------------------------------------------------------------


def _oferecer_pagamento_online(db, filial) -> None:
    db.add(BranchPaymentMethod(
        branch_id=filial.id,
        payment_flow="online",
        method_type="pix",
        label="Pix",
        enabled=True,
        requires_gateway=True,
    ))
    db.flush()


def _cadastrar_credencial(db, restaurante, *, com_webhook: bool) -> None:
    db.add(RestaurantPaymentCredential(
        restaurant_id=restaurante.id,
        environment=settings.MERCADOPAGO_ENVIRONMENT,
        public_key="TEST-public-key-nao-e-sigilosa",
        access_token_encrypted=encrypt_secret("TEST-access-token-de-teste"),
        webhook_secret_encrypted=(
            encrypt_secret("assinatura-secreta-de-teste") if com_webhook else None
        ),
    ))
    db.flush()


@pytest.fixture
def com_mercadopago(monkeypatch):
    """Plataforma no Mercado Pago, com uma chave de cifra valida e propria.

    A chave e gerada no teste e nao sai do processo: cifrar com a do `.env`
    faria o resultado depender de qual `.env` a maquina tem, e usar uma fixa
    no arquivo poria uma chave Fernet valida no repositorio.
    """
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "mercadopago")
    monkeypatch.setattr(
        settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


# ---------------------------------------------------------------------------
# A revisao do banco
# ---------------------------------------------------------------------------


class TestARevisaoDoBanco:
    """O banco e o codigo concordam? E quando nao, para que lado erraram?

    As duas direcoes sao problemas diferentes. Banco ATRAS e migracao que
    nao rodou. Banco A FRENTE e rollback de imagem sem `alembic downgrade`,
    que na revisao do cardapio por filial faz `/menu` responder 200 com o
    cardapio das duas lojas misturado (armadilha 36) — sem erro nenhum.
    """

    def test_banco_em_head_passa(self, db):
        """A fixture monta o schema com `upgrade head`: este e o caso normal."""
        assert conferir_a_revisao_do_banco(db).situacao == OK

    def test_banco_atras_do_codigo_e_ERRO(self, db):
        """Uma revisao antiga em `alembic_version` = migracao que nao rodou."""
        db.execute(text("UPDATE alembic_version SET version_num = '20260810_0012'"))
        conferencia = conferir_a_revisao_do_banco(db)
        assert conferencia.situacao == ERRO
        assert any("ATRAS" in linha for linha in conferencia.linhas)

    def test_banco_a_frente_do_codigo_e_ERRO(self, db):
        """Revisao que esta no banco e NAO existe nesta imagem.

        E o unico jeito de a tela distinguir "esqueceram de migrar" de
        "voltaram a imagem". Sem esta linha os dois sairiam como "nao bate",
        e o segundo pede downgrade urgente enquanto o primeiro so pede
        esperar o deploy terminar.
        """
        db.execute(text("UPDATE alembic_version SET version_num = '29990101_9999'"))
        conferencia = conferir_a_revisao_do_banco(db)
        assert conferencia.situacao == ERRO
        assert any("NAO existe nesta imagem" in linha for linha in conferencia.linhas)

    def test_tabela_de_versao_vazia_e_ERRO(self, db):
        """Banco com schema e sem `stamp`: o proximo upgrade tenta da 0002.

        E a 0002 roda `ALTER TABLE orders`, sobre uma tabela que ja existe.
        A saida e o `alembic stamp 20260726_0001`, e ela sai na tela.
        """
        db.execute(text("DELETE FROM alembic_version"))
        conferencia = conferir_a_revisao_do_banco(db)
        assert conferencia.situacao == ERRO
        assert "stamp" in conferencia.o_que_fazer


# ---------------------------------------------------------------------------
# As credenciais do Mercado Pago
# ---------------------------------------------------------------------------


class TestAsCredenciaisDoMercadoPago:
    """O silencioso que ja mordeu, conferido por DECIFRAR e nao por olhar."""

    def test_loja_sem_pagamento_online_nao_aparece(self, db, com_mercadopago):
        """Sem cobranca no gateway nao ha webhook para chegar.

        Listar essas lojas encheria a tela de linhas que nao pedem nada — e
        uma tela cheia de linhas verdes e uma tela que ninguem le.
        """
        restaurante = criar_restaurante(db, "So Dinheiro")
        criar_filial(db, restaurante)
        conferencia = conferir_o_mercado_pago(db)
        assert not any(restaurante.slug in linha for linha in conferencia.linhas)

    def test_credencial_completa_passa(self, db, com_mercadopago):
        restaurante = criar_restaurante(db, "Junior da Picanha")
        _oferecer_pagamento_online(db, criar_filial(db, restaurante))
        _cadastrar_credencial(db, restaurante, com_webhook=True)

        conferencia = conferir_o_mercado_pago(db)
        assert conferencia.situacao == OK
        assert any(restaurante.slug in linha for linha in conferencia.linhas)

    def test_sem_credencial_e_ERRO(self, db, com_mercadopago):
        restaurante = criar_restaurante(db, "Sem Credencial")
        _oferecer_pagamento_online(db, criar_filial(db, restaurante))

        conferencia = conferir_o_mercado_pago(db)
        assert conferencia.situacao == ERRO
        assert any("sem credencial" in linha for linha in conferencia.linhas)

    def test_sem_segredo_de_webhook_e_ERRO(self, db, com_mercadopago):
        """O caso que ja aconteceu: o cliente paga e o pedido nunca chega.

        A credencial esta la, a cobranca e criada, o dinheiro sai da conta do
        cliente — e o webhook daquele restaurante responde 503, entao o
        pedido nao sai de "aguardando pagamento". Nada nisso e visivel no
        painel do lojista.
        """
        restaurante = criar_restaurante(db, "Sem Webhook")
        _oferecer_pagamento_online(db, criar_filial(db, restaurante))
        _cadastrar_credencial(db, restaurante, com_webhook=False)

        conferencia = conferir_o_mercado_pago(db)
        assert conferencia.situacao == ERRO
        assert any("SEM segredo de webhook" in linha for linha in conferencia.linhas)

    def test_chave_de_cifra_trocada_e_ERRO(self, db, com_mercadopago, monkeypatch):
        """Conferir a coluna preenchida NAO responde a pergunta que importa.

        `PAYMENT_CREDENTIALS_ENCRYPTION_KEY` trocada deixa toda credencial
        cadastrada ilegivel — a coluna continua cheia e o sintoma e 503 em
        toda cobranca, descoberto no primeiro pedido de verdade. So decifrar
        de fato distingue os dois estados, e por isso a conferencia decifra.
        """
        restaurante = criar_restaurante(db, "Chave Trocada")
        _oferecer_pagamento_online(db, criar_filial(db, restaurante))
        _cadastrar_credencial(db, restaurante, com_webhook=True)

        monkeypatch.setattr(
            settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode()
        )
        conferencia = conferir_o_mercado_pago(db)
        assert conferencia.situacao == ERRO
        assert any("NAO DECIFRA" in linha for linha in conferencia.linhas)

    def test_sandbox_em_producao_e_ATENCAO(self, db, monkeypatch):
        """Nenhum dinheiro se move, e a tela nao pode deixar isso passar."""
        monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "sandbox")
        monkeypatch.setattr(settings, "APP_ENV", "production")
        assert conferir_o_mercado_pago(db).situacao == ATENCAO
