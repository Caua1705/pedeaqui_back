"""O reenvio do aviso que falhou, e as três desistências.

Um timeout da Meta no instante do aceite custava o aviso inteiro daquele
pedido: virava linha `failed` e ninguém retentava.

O que estes testes travam, e cada um fecha uma porta diferente:

- **só o que se conserta repetindo é marcado.** A decisão sai do TIPO da
  exceção (armadilha 49) e é gravada em `next_attempt_at` no instante da
  falha. Um `132001` — template não aprovado — NÃO é remarcado: quem o
  conserta é uma pessoa no painel da Meta, e retentá-lo a cada dois minutos
  gastaria a validade inteira sem uma chance;
- **o reenvio ATUALIZA a linha, não insere outra.** O `UNIQUE (order_id,
  kind)` diz que um aviso tem uma linha; inserir seria o banco recusando o
  reenvio depois de a mensagem já ter saído;
- **as três desistências.** Validade vencida, pedido cancelado, canal sem
  número utilizável. As três são o mesmo erro se saírem: o cliente recebendo
  uma frase que virou mentira — e ele age com base nela.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from src.core.config import settings
from src.integrations.whatsapp_client import WhatsAppRejectedError, WhatsAppTransportError
from src.models.order_model import Order
from src.models.whatsapp_model import WhatsAppChannel, WhatsAppMessage
from src.repositories.whatsapp_repository import WhatsAppMessageRepository
from src.services.whatsapp_notification_service import (
    ESPERA_ENTRE_TENTATIVAS,
    REENVIO_DESISTIU,
    REENVIO_ENVIADO,
    REENVIO_FALHOU,
    VALIDADE_DO_AVISO,
    WhatsAppOrderNotifier,
)
from src.utils.crypto import encrypt_whatsapp_token
from src.utils.security import utcnow
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def ligado(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "WHATSAPP_NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(
        settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


@pytest.fixture
def envio():
    with patch("src.services.whatsapp_send_service.send_template_message") as template:
        template.return_value = "wamid.ENVIADA"
        yield template


def cenario(db: Session, status: str = "accepted"):
    restaurante = fab.criar_restaurante(db, nome="Júnior da Picanha")
    filial = fab.criar_filial(db, restaurante, nome="Centro")
    canal = WhatsAppChannel(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        waba_id="waba-1",
        phone_number_id="pni-1",
        display_phone_number="+55 85 99999-0000",
        access_token_encrypted=encrypt_whatsapp_token("EAAG-token"),
        is_active=True,
    )
    db.add(canal)
    pedido = fab.criar_pedido(
        db,
        restaurante,
        filial,
        status=status,
        customer_name_snapshot="Maria Aparecida",
        customer_phone_snapshot="85988887777",
        order_number=5471,
    )
    db.flush()
    return restaurante, canal, pedido


def falhar_o_primeiro_envio(db: Session, envio, erro: Exception) -> WhatsAppMessage:
    """Deixa o cenário no estado real: um aviso que já tentou sair e falhou."""
    restaurante, _, pedido = cenario(db)
    envio.side_effect = erro
    WhatsAppOrderNotifier(db).notify(order=pedido, restaurant_id=restaurante.id)
    envio.side_effect = None
    return db.query(WhatsAppMessage).one()


class TestOQueFicaMarcadoParaReenvio:
    def test_a_falha_de_transporte_marca_a_proxima_tentativa(
        self, db: Session, envio
    ) -> None:
        """Timeout, DNS, conexão: a chamada nem chegou a ter resposta.

        `WhatsAppTransportError.retryable` é `True`, e é ele — o TIPO — que
        responde. A linha guarda a decisão, e não o sintoma."""
        linha = falhar_o_primeiro_envio(db, envio, WhatsAppTransportError("timeout"))

        assert linha.status == "failed"
        assert linha.next_attempt_at is not None
        assert linha.attempts == 1

    def test_a_recusa_da_meta_NAO_marca(self, db: Session, envio) -> None:
        """`132001` é template não aprovado, e ele parece transitório.

        A aprovação chega em horas, e quem a faz é uma pessoa no painel da
        Meta. Retentar a cada dois minutos gastaria os 30 minutos de validade
        do aviso sem uma chance sequer."""
        linha = falhar_o_primeiro_envio(
            db, envio, WhatsAppRejectedError("template nao existe", error_code="132001")
        )

        assert linha.error_code == "132001"
        assert linha.next_attempt_at is None

    def test_o_telefone_torto_NAO_marca(self, db: Session, envio) -> None:
        """Recusa NOSSA: o telefone do pedido não vira E.164 sem chute.

        Ele não muda sozinho entre uma tentativa e a próxima — repetir é
        repetir a mesma recusa."""
        restaurante = fab.criar_restaurante(db, nome="Júnior da Picanha")
        filial = fab.criar_filial(db, restaurante, nome="Centro")
        db.add(
            WhatsAppChannel(
                restaurant_id=restaurante.id,
                branch_id=filial.id,
                waba_id="waba-1",
                phone_number_id="pni-1",
                display_phone_number="+55 85 99999-0000",
                access_token_encrypted=encrypt_whatsapp_token("EAAG-token"),
                is_active=True,
            )
        )
        pedido = fab.criar_pedido(
            db,
            restaurante,
            filial,
            status="accepted",
            customer_name_snapshot="Maria",
            customer_phone_snapshot="123",
            order_number=5471,
        )
        db.flush()

        WhatsAppOrderNotifier(db).notify(order=pedido, restaurant_id=restaurante.id)

        linha = db.query(WhatsAppMessage).one()
        assert linha.error_code == "refused:phone"
        assert linha.next_attempt_at is None

    def test_o_envio_que_deu_certo_nao_fica_marcado(self, db: Session, envio) -> None:
        restaurante, _, pedido = cenario(db)

        WhatsAppOrderNotifier(db).notify(order=pedido, restaurant_id=restaurante.id)

        linha = db.query(WhatsAppMessage).one()
        assert linha.status == "sent"
        assert linha.next_attempt_at is None


class TestAConsultaDaVarredura:
    def test_so_devolve_o_que_ja_venceu(self, db: Session, envio) -> None:
        """O filtro é `next_attempt_at`, e nunca `status='failed'`.

        Perguntar pelo status traria de volta o `132001`, que a linha já disse
        para não retentar."""
        linha = falhar_o_primeiro_envio(db, envio, WhatsAppTransportError("timeout"))
        repositorio = WhatsAppMessageRepository(db)

        antes_da_hora = repositorio.list_due_for_retry(
            now=linha.next_attempt_at - timedelta(seconds=1), limit=10
        )
        depois = repositorio.list_due_for_retry(now=linha.next_attempt_at, limit=10)

        assert antes_da_hora == []
        assert [m.id for m in depois] == [linha.id]

    def test_a_recusa_permanente_nao_aparece(self, db: Session, envio) -> None:
        falhar_o_primeiro_envio(
            db, envio, WhatsAppRejectedError("template nao existe", error_code="132001")
        )

        pendentes = WhatsAppMessageRepository(db).list_due_for_retry(
            now=utcnow() + timedelta(days=1), limit=10
        )

        assert pendentes == []


class TestOReenvioQueDaCerto:
    def test_atualiza_a_linha_em_vez_de_inserir_outra(self, db: Session, envio) -> None:
        """O `UNIQUE (order_id, kind)` diz que um aviso tem UMA linha.

        Inserir seria o banco recusando o reenvio depois de a mensagem já ter
        saído — a mensagem entregue e nenhum registro dela."""
        linha = falhar_o_primeiro_envio(db, envio, WhatsAppTransportError("timeout"))

        desfecho = WhatsAppOrderNotifier(db).retry(linha)

        assert desfecho == REENVIO_ENVIADO
        assert db.query(WhatsAppMessage).count() == 1
        assert linha.status == "sent"
        assert linha.wamid == "wamid.ENVIADA"
        assert linha.error_code is None
        assert linha.next_attempt_at is None
        assert linha.attempts == 2

    def test_a_segunda_falha_de_transporte_remarca_e_conta(
        self, db: Session, envio
    ) -> None:
        linha = falhar_o_primeiro_envio(db, envio, WhatsAppTransportError("timeout"))
        marcada_antes = linha.next_attempt_at
        envio.side_effect = WhatsAppTransportError("timeout de novo")

        notifier = WhatsAppOrderNotifier(db)
        agora = marcada_antes + timedelta(seconds=30)
        notifier.clock = lambda: agora
        desfecho = notifier.retry(linha)

        assert desfecho == REENVIO_FALHOU
        assert linha.attempts == 2
        assert linha.next_attempt_at == agora + ESPERA_ENTRE_TENTATIVAS


class TestAsTresDesistencias:
    def test_validade_vencida_desiste_sem_mandar(self, db: Session, envio) -> None:
        """"Seu pedido foi aceito" com o pedido já entregue não é um aviso
        atrasado — é um aviso ERRADO, e o cliente age com base nele."""
        linha = falhar_o_primeiro_envio(db, envio, WhatsAppTransportError("timeout"))
        envio.reset_mock()

        notifier = WhatsAppOrderNotifier(db)
        notifier.clock = lambda: linha.created_at + VALIDADE_DO_AVISO
        desfecho = notifier.retry(linha)

        assert desfecho == REENVIO_DESISTIU
        assert envio.call_count == 0
        assert linha.next_attempt_at is None
        # O motivo da falha original NÃO é sobrescrito: ele é a causa, e
        # "desisti" é o desfecho.
        assert linha.status == "failed"

    def test_pedido_cancelado_desiste_sem_mandar(self, db: Session, envio) -> None:
        """Mandar "foi aceito" para quem teve o pedido cancelado deixa o
        cliente esperando comida que não vem."""
        linha = falhar_o_primeiro_envio(db, envio, WhatsAppTransportError("timeout"))
        envio.reset_mock()
        pedido = db.get(Order, linha.order_id)
        pedido.status = "cancelled"
        db.commit()

        desfecho = WhatsAppOrderNotifier(db).retry(linha)

        assert desfecho == REENVIO_DESISTIU
        assert envio.call_count == 0
        assert linha.next_attempt_at is None

    def test_canal_desconectado_desiste_e_NAO_cai_no_do_restaurante(
        self, db: Session, envio
    ) -> None:
        """O canal é RESOLVIDO de novo, e não lido do `channel_id` da linha.

        Entre a falha e o reenvio o lojista pode ter desconectado a Cloud API;
        insistir seria mandar contra um acesso que não existe mais. E a queda
        no número do restaurante não acontece: o número da filial DESLIGADO
        significa que aquela loja para de mandar, não que ela passa a falar
        por outro número."""
        linha = falhar_o_primeiro_envio(db, envio, WhatsAppTransportError("timeout"))
        envio.reset_mock()
        canal = db.get(WhatsAppChannel, linha.channel_id)
        canal.disconnected_at = utcnow()
        canal.disconnect_reason = "PARTNER_REMOVED"
        db.add(
            WhatsAppChannel(
                restaurant_id=canal.restaurant_id,
                branch_id=None,
                waba_id="waba-1",
                phone_number_id="pni-do-restaurante",
                display_phone_number="+55 85 98888-0000",
                access_token_encrypted=encrypt_whatsapp_token("EAAG-outro"),
                is_active=True,
            )
        )
        db.commit()

        desfecho = WhatsAppOrderNotifier(db).retry(linha)

        assert desfecho == REENVIO_DESISTIU
        assert envio.call_count == 0
        assert linha.next_attempt_at is None
