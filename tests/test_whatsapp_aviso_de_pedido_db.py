"""Os avisos de status do pedido: aceito, saiu para entrega, entregue.

São sempre TEMPLATE, e isso não é conservadorismo: o cliente pediu pelo app e
nunca escreveu no WhatsApp da loja, então a janela de 24h está fechada em
praticamente 100% dos pedidos. Texto livre ali voltaria `131047` e o cliente
não seria avisado.

O que estes testes travam, e cada um custa de um jeito diferente:

- **o aviso não sai duas vezes.** O `UNIQUE (order_id, kind)` sozinho não
  resolve: ele barra a LINHA, e nesse ponto a mensagem já saiu. Quem impede o
  cliente de receber duas é a conferência ANTES do envio;
- **falha de envio não derruba a mudança de status.** O aceite já está
  gravado; virar 500 diria ao lojista que ele não aconteceu, e ele clicaria
  de novo;
- **retirada não recebe "foi entregue".** O texto mentiria — e
  `out_for_delivery` nem existe naquele fluxo;
- **a chave global desligada não manda nada.** É o freio que não depende de
  `UPDATE` de madrugada.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from src.core.config import settings
from src.integrations.whatsapp_client import WhatsAppRejectedError
from src.models.whatsapp_model import WhatsAppChannel, WhatsAppMessage
from src.services.whatsapp_notification_service import WhatsAppOrderNotifier
from src.utils.crypto import encrypt_whatsapp_token
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


def cenario(db: Session, status: str = "accepted", order_type: str = "delivery"):
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
        status=status,
        order_type=order_type,
        customer_name_snapshot="Maria Aparecida",
        customer_phone_snapshot="85988887777",
        order_number=5471,
    )
    db.flush()
    return restaurante, filial, pedido


def avisar(db: Session, pedido, restaurante) -> None:
    WhatsAppOrderNotifier(db).notify(order=pedido, restaurant_id=restaurante.id)


class TestOsQuatroAvisos:
    def test_aceito_manda_o_template_com_nome_numero_e_loja(
        self, db: Session, envio
    ) -> None:
        restaurante, _, pedido = cenario(db, status="accepted")

        avisar(db, pedido, restaurante)

        assert envio.call_count == 1
        argumentos = envio.call_args.kwargs
        assert argumentos["template_name"] == "pedido_aceito"
        assert argumentos["language"] == "pt_BR"
        assert argumentos["parameters"] == ("Maria", "5471", "Júnior da Picanha")
        assert argumentos["to"] == "5585988887777"

    def test_saiu_para_entrega(self, db: Session, envio) -> None:
        restaurante, _, pedido = cenario(db, status="out_for_delivery")

        avisar(db, pedido, restaurante)

        assert envio.call_args.kwargs["template_name"] == "pedido_saiu_para_entrega"

    def test_entregue(self, db: Session, envio) -> None:
        restaurante, _, pedido = cenario(db, status="completed")

        avisar(db, pedido, restaurante)

        assert envio.call_args.kwargs["template_name"] == "pedido_entregue"

    def test_pronto_para_retirada(self, db: Session, envio) -> None:
        """`ready` é o estado em que o lojista diz "acabei de fazer".

        Antes deste aviso, quem retirava não recebia NADA depois do
        aceite: `out_for_delivery` e `completed` não são dele, e a pessoa
        ficava esperando sem saber que já podia buscar. É o pior caso da
        frente inteira — o cliente esperando por informação que existe."""
        restaurante, _, pedido = cenario(db, status="ready", order_type="pickup")

        avisar(db, pedido, restaurante)

        assert (
            envio.call_args.kwargs["template_name"] == "pedido_pronto_para_retirada"
        )
        assert envio.call_args.kwargs["parameters"] == (
            "Maria",
            "5471",
            "Júnior da Picanha",
        )

    def test_o_envio_bem_sucedido_grava_a_linha_com_o_wamid(
        self, db: Session, envio
    ) -> None:
        restaurante, _, pedido = cenario(db, status="accepted")

        avisar(db, pedido, restaurante)

        linha = db.query(WhatsAppMessage).one()
        assert linha.order_id == pedido.id
        assert linha.kind == "order_accepted"
        assert linha.status == "sent"
        assert linha.wamid == "wamid.ENVIADA"


class TestOQueNaoGeraAviso:
    @pytest.mark.parametrize("status", ["pending", "preparing", "cancelled", "rejected"])
    def test_status_fora_dos_quatro(self, db: Session, envio, status: str) -> None:
        restaurante, _, pedido = cenario(db, status=status)

        avisar(db, pedido, restaurante)

        assert envio.call_count == 0
        assert db.query(WhatsAppMessage).count() == 0

    def test_retirada_concluida_nao_recebe_foi_entregue(self, db: Session, envio) -> None:
        """O texto do template diz "foi entregue". Numa retirada isso é
        mentira — e `out_for_delivery` nem existe naquele fluxo. O aviso que a
        retirada quer é "pronto para retirada", e ele não existe nesta rodada."""
        restaurante, _, pedido = cenario(db, status="completed", order_type="pickup")

        avisar(db, pedido, restaurante)

        assert envio.call_count == 0

    def test_entrega_pronta_nao_recebe_pronto_para_retirada(
        self, db: Session, envio
    ) -> None:
        """A simetria do "foi entregue": cada aviso vale no tipo de pedido
        em que a FRASE dele é verdadeira. Numa entrega, `ready` significa
        "pronto para SAIR" — e quem vai buscar é o motoboy. O cliente
        recebe o "saiu para entrega" no passo seguinte."""
        restaurante, _, pedido = cenario(db, status="ready", order_type="delivery")

        avisar(db, pedido, restaurante)

        assert envio.call_count == 0

    def test_retirada_aceita_recebe_o_aviso_de_aceite(self, db: Session, envio) -> None:
        """O aceite vale nos dois tipos: a frase é verdadeira nos dois."""
        restaurante, _, pedido = cenario(db, status="accepted", order_type="pickup")

        avisar(db, pedido, restaurante)

        assert envio.call_args.kwargs["template_name"] == "pedido_aceito"

    def test_a_chave_global_desligada_nao_manda_nada(
        self, db: Session, envio, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "WHATSAPP_NOTIFICATIONS_ENABLED", False)
        restaurante, _, pedido = cenario(db, status="accepted")

        avisar(db, pedido, restaurante)

        assert envio.call_count == 0
        assert db.query(WhatsAppMessage).count() == 0

    def test_restaurante_sem_canal_nao_manda_nada(self, db: Session, envio) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial, status="accepted")

        avisar(db, pedido, restaurante)

        assert envio.call_count == 0
        assert db.query(WhatsAppMessage).count() == 0


class TestOAvisoNaoSaiDuasVezes:
    def test_a_segunda_chamada_nao_manda_de_novo(self, db: Session, envio) -> None:
        """O `UNIQUE (order_id, kind)` barra a LINHA — e nesse ponto a mensagem
        já saiu. Quem impede o cliente de receber duas é a conferência ANTES
        do envio."""
        restaurante, _, pedido = cenario(db, status="accepted")

        avisar(db, pedido, restaurante)
        avisar(db, pedido, restaurante)

        assert envio.call_count == 1
        assert db.query(WhatsAppMessage).count() == 1


class TestQuandoOEnvioFalha:
    def test_a_recusa_da_meta_vira_linha_failed_com_o_codigo(
        self, db: Session, envio
    ) -> None:
        envio.side_effect = WhatsAppRejectedError(
            "template nao existe", error_code="132001"
        )
        restaurante, _, pedido = cenario(db, status="accepted")

        avisar(db, pedido, restaurante)

        linha = db.query(WhatsAppMessage).one()
        assert linha.status == "failed"
        assert linha.error_code == "132001"
        assert linha.wamid is None

    def test_telefone_impossivel_vira_linha_failed_sem_chamar_a_meta(
        self, db: Session, envio
    ) -> None:
        restaurante, filial, _ = cenario(db, status="accepted")
        pedido = fab.criar_pedido(
            db,
            restaurante,
            filial,
            status="accepted",
            customer_phone_snapshot="99999999",
            order_number=5472,
        )

        avisar(db, pedido, restaurante)

        assert envio.call_count == 0
        linha = db.query(WhatsAppMessage).filter_by(order_id=pedido.id).one()
        assert linha.status == "failed"
        assert linha.error_code == "refused:phone"


class TestALigacaoComAEscritaDeStatus:
    """O aviso sai de `OrderStatusChangeService.apply`, DEPOIS do commit.

    É o mesmo lugar e o mesmo motivo do estorno: falar com a Meta não pode
    fazer parte da transação do pedido — um timeout deles desfaria a mudança
    de status, e quem clicou veria um erro com o pedido já na cozinha.

    E é esse ponto único que faz o aviso valer para as QUATRO portas de
    escrita de status (painel, cancelamento, cliente, entregador) sem quatro
    cópias.
    """

    def _aceitar(self, db: Session, pedido, restaurante):
        from src.services.order_status_change_service import OrderStatusChangeService

        return OrderStatusChangeService(db).apply(
            order=pedido,
            restaurant_id=restaurante.id,
            new_status="accepted",
            note=None,
            changed_by="admin:teste",
            requester="admin:teste",
            route="PATCH /admin/orders/{id}/status",
            idempotency_key=None,
        )

    def test_aceitar_o_pedido_manda_o_aviso(self, db: Session, envio) -> None:
        restaurante, _, pedido = cenario(db, status="pending")

        self._aceitar(db, pedido, restaurante)

        assert envio.call_args.kwargs["template_name"] == "pedido_aceito"

    def test_o_aviso_explodindo_nao_derruba_a_mudanca_de_status(
        self, db: Session, envio
    ) -> None:
        """O aceite JÁ está gravado. Virar 500 diria ao lojista que ele não
        aconteceu, e ele clicaria de novo."""
        envio.side_effect = RuntimeError("qualquer coisa inesperada")
        restaurante, _, pedido = cenario(db, status="pending")

        resposta = self._aceitar(db, pedido, restaurante)

        assert resposta.status == "accepted"


class TestONumeroDoRestauranteAtendeAsFiliais:
    """O cenário do piloto: UM número na linha do restaurante, duas filiais.

    O número novo e dedicado é cadastrado sem `--branch-slug`, então
    `branch_id` fica nulo e as duas lojas herdam. Isto aqui é o que prova que
    o aviso de um pedido do Centro sai por ele — sem nenhuma linha de código
    escrita para esse caso.
    """

    def test_pedido_de_uma_filial_avisa_pelo_numero_do_restaurante(
        self, db: Session, envio
    ) -> None:
        restaurante = fab.criar_restaurante(db, nome="Júnior da Picanha")
        centro = fab.criar_filial(db, restaurante, nome="Centro")
        fab.criar_filial(db, restaurante, nome="Aldeota")
        db.add(
            WhatsAppChannel(
                restaurant_id=restaurante.id,
                branch_id=None,
                waba_id="waba-1",
                phone_number_id="pni-do-restaurante",
                display_phone_number="+55 85 99999-0000",
                access_token_encrypted=encrypt_whatsapp_token("EAAG-token"),
                is_active=True,
            )
        )
        pedido = fab.criar_pedido(
            db,
            restaurante,
            centro,
            status="accepted",
            customer_name_snapshot="Maria Aparecida",
            customer_phone_snapshot="85988887777",
            order_number=5471,
        )
        db.flush()

        avisar(db, pedido, restaurante)

        assert envio.call_count == 1
        linha = db.query(WhatsAppMessage).one()
        assert linha.order_id == pedido.id
