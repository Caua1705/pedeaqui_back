"""O que a revisão 0051 (canal de WhatsApp por filial) promete ao banco.

Nada disto aparece na suíte rápida: a instância transiente do model não tem
UNIQUE, não tem CHECK e não sabe o que é FK composta.

As quatro promessas, e o que cada uma impede:

1. **Um `phone_number_id` roteia para UM destino, sempre.** É o `UNIQUE`, e
   ele é a razão de o canal ser uma tabela em vez de colunas espalhadas por
   `branches` e `restaurant_settings` — não existe UNIQUE que atravesse duas
   tabelas, e o mesmo número cadastrado nos dois lugares roteia o webhook
   para dois lugares sem ninguém perceber.
2. **Uma filial tem no máximo um número**, e **um restaurante tem no máximo
   uma queda.** São duas travas e não uma: `NULL` é distinto de `NULL` no
   Postgres, então o `UNIQUE (branch_id)` deixa passar dois números de
   restaurante do MESMO restaurante — e aí a filial sem número herdaria um
   dos dois, o que o `ORDER BY` escolher. Quem fecha isso é o índice único
   PARCIAL.
3. **A filial é do restaurante que a linha diz.** FK composta contra
   `uq_branches_restaurant_id`, o mesmo cinto do cardápio por filial
   (armadilha 36). Com `branch_id` nulo ela não é conferida — `MATCH SIMPLE`
   —, que é exatamente o que a linha de restaurante precisa.
4. **`kind` e `status` são conjuntos fechados** e os CHECKs espelham
   `WHATSAPP_MESSAGE_KINDS` e `WHATSAPP_MESSAGE_STATUSES` (armadilha 15). O
   par é cobrado de verdade por `test_espelhos_de_enum_db.py`; aqui o que se
   prova é que o CHECK existe e recusa.

Tudo por SQL cru, e não pelo model: um INSERT do ORM não prova nada sobre o
DDL — o que está sendo exercitado aqui é a constraint do Postgres.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _canal(
    db: Session,
    restaurant_id,
    branch_id=None,
    phone_number_id: str | None = None,
) -> uuid.UUID:
    return db.execute(
        text(
            "INSERT INTO whatsapp_channels "
            "(restaurant_id, branch_id, waba_id, phone_number_id, "
            " display_phone_number, access_token_encrypted) "
            "VALUES (:restaurant_id, :branch_id, :waba_id, :phone_number_id, "
            "        :display, :token) RETURNING id"
        ),
        {
            "restaurant_id": restaurant_id,
            "branch_id": branch_id,
            "waba_id": "waba-de-teste",
            "phone_number_id": phone_number_id or f"pni-{uuid.uuid4().hex[:12]}",
            "display": "+55 85 99999-0000",
            "token": "gAAAAA-cifrado-de-teste",
        },
    ).scalar_one()


def _mensagem(
    db: Session,
    order_id,
    channel_id,
    kind: str = "order_accepted",
    status: str = "sent",
) -> uuid.UUID:
    return db.execute(
        text(
            "INSERT INTO whatsapp_messages "
            "(order_id, channel_id, kind, status, wamid) "
            "VALUES (:order_id, :channel_id, :kind, :status, :wamid) RETURNING id"
        ),
        {
            "order_id": order_id,
            "channel_id": channel_id,
            "kind": kind,
            "status": status,
            "wamid": f"wamid-{uuid.uuid4().hex[:12]}",
        },
    ).scalar_one()


class TestORoteamentoTemUmDestinoSo:
    def test_o_mesmo_phone_number_id_nao_serve_a_dois_canais(self, db: Session) -> None:
        primeiro = fab.criar_restaurante(db)
        segundo = fab.criar_restaurante(db)
        numero = "111222333444555"
        _canal(db, primeiro.id, phone_number_id=numero)
        db.flush()

        with pytest.raises(IntegrityError):
            _canal(db, segundo.id, phone_number_id=numero)
            db.flush()

    def test_duas_filiais_do_mesmo_restaurante_tem_numeros_proprios(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        centro = fab.criar_filial(db, restaurante, nome="Centro")
        aldeota = fab.criar_filial(db, restaurante, nome="Aldeota")

        _canal(db, restaurante.id, branch_id=centro.id)
        _canal(db, restaurante.id, branch_id=aldeota.id)
        db.flush()

    def test_a_mesma_filial_nao_tem_dois_numeros(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        _canal(db, restaurante.id, branch_id=filial.id)
        db.flush()

        with pytest.raises(IntegrityError):
            _canal(db, restaurante.id, branch_id=filial.id)
            db.flush()


class TestAQuedaDoRestaurante:
    def test_o_restaurante_tem_no_maximo_uma_queda(self, db: Session) -> None:
        """A trava que o `UNIQUE (branch_id)` NÃO dá: dois nulos não colidem."""
        restaurante = fab.criar_restaurante(db)
        _canal(db, restaurante.id, branch_id=None)
        db.flush()

        with pytest.raises(IntegrityError):
            _canal(db, restaurante.id, branch_id=None)
            db.flush()

    def test_dois_restaurantes_tem_cada_um_a_sua_queda(self, db: Session) -> None:
        primeiro = fab.criar_restaurante(db)
        segundo = fab.criar_restaurante(db)

        _canal(db, primeiro.id, branch_id=None)
        _canal(db, segundo.id, branch_id=None)
        db.flush()

    def test_a_filial_tem_que_ser_do_restaurante_da_linha(self, db: Session) -> None:
        dono = fab.criar_restaurante(db)
        outro = fab.criar_restaurante(db)
        filial_do_outro = fab.criar_filial(db, outro)

        with pytest.raises(IntegrityError):
            _canal(db, dono.id, branch_id=filial_do_outro.id)
            db.flush()


class TestOQueFoiEnviado:
    def test_o_mesmo_aviso_nao_sai_duas_vezes_para_o_mesmo_pedido(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial)
        canal = _canal(db, restaurante.id, branch_id=filial.id)
        _mensagem(db, pedido.id, canal, kind="order_accepted")
        db.flush()

        with pytest.raises(IntegrityError):
            _mensagem(db, pedido.id, canal, kind="order_accepted")
            db.flush()

    def test_avisos_diferentes_do_mesmo_pedido_convivem(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial)
        canal = _canal(db, restaurante.id, branch_id=filial.id)

        _mensagem(db, pedido.id, canal, kind="order_accepted")
        _mensagem(db, pedido.id, canal, kind="order_out_for_delivery")
        db.flush()

    def test_kind_fora_da_lista_e_recusado(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial)
        canal = _canal(db, restaurante.id, branch_id=filial.id)

        with pytest.raises(IntegrityError):
            _mensagem(db, pedido.id, canal, kind="pedido_atrasado")
            db.flush()

    def test_status_fora_da_lista_e_recusado(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial)
        canal = _canal(db, restaurante.id, branch_id=filial.id)

        with pytest.raises(IntegrityError):
            _mensagem(db, pedido.id, canal, status="entregue")
            db.flush()


class TestAJanelaDeVinteQuatroHoras:
    def test_um_telefone_tem_uma_janela_por_canal(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        canal = _canal(db, restaurante.id, branch_id=filial.id)

        def abrir() -> None:
            db.execute(
                text(
                    "INSERT INTO whatsapp_contact_windows "
                    "(channel_id, phone_e164, window_expires_at) "
                    "VALUES (:channel_id, :phone, now() + interval '24 hours')"
                ),
                {"channel_id": canal, "phone": "5585999990000"},
            )

        abrir()
        db.flush()

        with pytest.raises(IntegrityError):
            abrir()
            db.flush()
