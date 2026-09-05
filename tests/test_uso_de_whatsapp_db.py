"""A contagem de avisos de WhatsApp contra o Postgres.

A agregação É uma consulta — `GROUP BY` sobre duas junções, três `FILTER` e uma
janela meio-aberta — e dublar o repositório aqui seria testar o dublê: ele
"contaria" o que o teste mandou contar, e o que se quer provar é que o SQL
separa os três desfechos e respeita a borda do período.

Nada aqui precisou de migração: `whatsapp_messages` já tem `channel_id`, `kind`,
`wamid`, `error_code` e `created_at` desde a revisão `20260905_0053`. É por isso
que este item ficou fora da revisão preparada do custo de IA.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.models.whatsapp_model import WhatsAppChannel, WhatsAppMessage
from src.services.whatsapp_usage_service import WhatsAppUsageService
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


DENTRO = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
INICIO = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
FIM = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def _canal(db, restaurante, filial=None) -> WhatsAppChannel:
    sufixo = uuid.uuid4().hex[:10]
    canal = WhatsAppChannel(
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial else None,
        waba_id=f"waba-{sufixo}",
        phone_number_id=f"pni-{sufixo}",
        display_phone_number="+55 85 99999-0000",
        access_token_encrypted="cifrado-de-mentira",
        is_active=True,
    )
    db.add(canal)
    db.flush()
    return canal


def _aviso(
    db,
    canal: WhatsAppChannel,
    pedido,
    kind: str = "order_accepted",
    wamid: str | None = "wamid.SAIU",
    status: str = "sent",
    error_code: str | None = None,
    created_at: datetime = DENTRO,
) -> WhatsAppMessage:
    mensagem = WhatsAppMessage(
        order_id=pedido.id,
        channel_id=canal.id,
        kind=kind,
        status=status,
        wamid=f"{wamid}.{uuid.uuid4().hex[:8]}" if wamid else None,
        error_code=error_code,
    )
    mensagem.created_at = created_at
    db.add(mensagem)
    db.flush()
    return mensagem


def _cenario(db, nome="Júnior da Picanha"):
    restaurante = fab.criar_restaurante(db, nome=nome)
    filial = fab.criar_filial(db, restaurante, nome="Centro")
    return restaurante, filial, _canal(db, restaurante, filial)


class TestOsTresDesfechos:
    """Sair, ser recusado por nós e ser recusado pela Meta não são a mesma coisa.

    Um total que junta os três diz "mandei 30" quando 10 nunca tocaram a Meta —
    e a pergunta que este relatório existe para responder é quanto do cartão da
    plataforma cada loja consome.
    """

    def test_o_que_saiu_conta_por_wamid_e_nao_por_status(self, db):
        """Uma mensagem que a Meta aceitou e não entregou JÁ SAIU.

        O `status` continua andando depois do envio (`sent` → `delivered` →
        `read`, ou `failed` numa entrega que não completou), e contar por ele
        faria a conta do mês mudar sozinha, dias depois, por causa de um
        webhook. O `wamid` não muda: ele existe se e só se a mensagem existiu
        lá.
        """
        restaurante, filial, canal = _cenario(db)
        _aviso(db, canal, fab.criar_pedido(db, restaurante, filial), status="sent")
        _aviso(
            db,
            canal,
            fab.criar_pedido(db, restaurante, filial),
            kind="order_delivered",
            status="failed",
            error_code="131047",
        )

        relatorio = WhatsAppUsageService(db).templates_por_restaurante(INICIO, FIM)

        bloco = next(b for b in relatorio.restaurants if b.restaurant_id == restaurante.id)
        assert bloco.templates == 2
        assert bloco.sent == 2
        assert bloco.refused_here == 0
        assert bloco.refused_by_meta == 0

    def test_a_recusa_NOSSA_nao_entra_no_que_saiu(self, db):
        """`refused:phone` e `refused:window` nunca chegaram à Meta.

        Elas não custam nada e não indicam problema com ela — juntá-las às
        recusas dela mandaria abrir chamado por um telefone mal cadastrado.
        """
        restaurante, filial, canal = _cenario(db)
        _aviso(
            db,
            canal,
            fab.criar_pedido(db, restaurante, filial),
            wamid=None,
            status="failed",
            error_code="refused:phone",
        )

        bloco = WhatsAppUsageService(db).templates_por_restaurante(INICIO, FIM).restaurants[0]

        assert bloco.templates == 1
        assert bloco.sent == 0
        assert bloco.refused_here == 1
        assert bloco.refused_by_meta == 0

    def test_a_recusa_DELA_e_o_que_sobra(self, db):
        """Sem `wamid` e sem o prefixo `refused:` — a chamada saiu e ela negou.

        É derivado (`templates - sent - refused_here`) e não um quarto `FILTER`,
        de propósito: uma quarta condição no SQL poderia divergir da subtração
        sem nada acusar.
        """
        restaurante, filial, canal = _cenario(db)
        _aviso(
            db,
            canal,
            fab.criar_pedido(db, restaurante, filial),
            wamid=None,
            status="failed",
            error_code="132001",
        )

        bloco = WhatsAppUsageService(db).templates_por_restaurante(INICIO, FIM).restaurants[0]

        assert bloco.templates == 1
        assert bloco.sent == 0
        assert bloco.refused_here == 0
        assert bloco.refused_by_meta == 1


class TestAQuebraPorTipo:
    def test_os_quatro_avisos_nao_se_misturam(self, db):
        """Um total que dobrou sem dizer QUAL tipo dobrou não responde nada.

        `order_accepted` sai para todo pedido aceito; os outros três dependem de
        o pedido chegar aonde eles descrevem, então a proporção entre eles é o
        que denuncia um gatilho disparando duas vezes.
        """
        restaurante, filial, canal = _cenario(db)
        for kind in ("order_accepted", "order_accepted", "order_delivered"):
            _aviso(db, canal, fab.criar_pedido(db, restaurante, filial), kind=kind)

        bloco = WhatsAppUsageService(db).templates_por_restaurante(INICIO, FIM).restaurants[0]

        por_tipo = {linha.kind: linha.templates for linha in bloco.by_kind}
        assert por_tipo == {"order_accepted": 2, "order_delivered": 1}
        assert bloco.templates == 3


class TestOEixoEOPeriodo:
    def test_cada_restaurante_no_seu_bloco(self, db):
        """A pergunta é "quanto o restaurante X consumiu", e o eixo é o CANAL.

        `whatsapp_messages` também tem `order_id`, e o pedido também sabe de
        quem é — mas quem a Meta cobra é o NÚMERO, e o número é o canal.
        """
        junior, filial_junior, canal_junior = _cenario(db, nome="Júnior da Picanha")
        varjota, filial_varjota, canal_varjota = _cenario(db, nome="Varjota Grill")

        _aviso(db, canal_junior, fab.criar_pedido(db, junior, filial_junior))
        _aviso(db, canal_junior, fab.criar_pedido(db, junior, filial_junior))
        _aviso(db, canal_varjota, fab.criar_pedido(db, varjota, filial_varjota))

        relatorio = WhatsAppUsageService(db).templates_por_restaurante(INICIO, FIM)

        totais = {b.restaurant_name: b.templates for b in relatorio.restaurants}
        assert totais == {"Júnior da Picanha": 2, "Varjota Grill": 1}
        assert relatorio.total_templates == 3
        assert relatorio.total_sent == 3

    def test_filtrar_por_restaurante_deixa_so_ele(self, db):
        junior, filial_junior, canal_junior = _cenario(db, nome="Júnior da Picanha")
        varjota, filial_varjota, canal_varjota = _cenario(db, nome="Varjota Grill")
        _aviso(db, canal_junior, fab.criar_pedido(db, junior, filial_junior))
        _aviso(db, canal_varjota, fab.criar_pedido(db, varjota, filial_varjota))

        relatorio = WhatsAppUsageService(db).templates_por_restaurante(
            INICIO, FIM, restaurant_id=junior.id
        )

        assert [b.restaurant_name for b in relatorio.restaurants] == ["Júnior da Picanha"]
        assert relatorio.total_templates == 1

    def test_a_janela_e_meio_aberta_nas_duas_bordas(self, db):
        """`[desde, ate)`. O instante `desde` entra e o instante `ate` não.

        É a borda que a rota constrói somando um dia a `end_date` — quem pede
        "até 31/08" quer o dia 31 inteiro. Errar de um lado come o último dia do
        mês; errar do outro cobra o primeiro dia do mês seguinte duas vezes.
        """
        restaurante, filial, canal = _cenario(db)
        _aviso(db, canal, fab.criar_pedido(db, restaurante, filial), created_at=INICIO)
        _aviso(
            db,
            canal,
            fab.criar_pedido(db, restaurante, filial),
            created_at=INICIO - timedelta(microseconds=1),
        )
        _aviso(db, canal, fab.criar_pedido(db, restaurante, filial), created_at=FIM)

        relatorio = WhatsAppUsageService(db).templates_por_restaurante(INICIO, FIM)

        assert relatorio.total_templates == 1

    def test_periodo_sem_aviso_nenhum_e_uma_lista_vazia(self, db):
        """Zero avisos não é erro nem `None`: é um mês em que ninguém mandou."""
        restaurante, filial, canal = _cenario(db)
        _aviso(db, canal, fab.criar_pedido(db, restaurante, filial), created_at=FIM)

        relatorio = WhatsAppUsageService(db).templates_por_restaurante(INICIO, FIM)

        assert relatorio.restaurants == []
        assert relatorio.total_templates == 0
        assert relatorio.total_sent == 0
