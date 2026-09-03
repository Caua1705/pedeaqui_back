"""As duas perguntas que o canal de WhatsApp responde, e elas são inversas.

**"De qual número esta filial fala?"** é a do envio, e é o regime da armadilha
35: filial com número usa o dela, filial sem número herda o do restaurante, e
`NULL` — só `NULL` — significa "herda".

**"De quem é este número?"** é a do webhook, que chega com um
`phone_number_id` e precisa achar a filial. É o motivo de o canal ser tabela
com `UNIQUE`, e não coluna.

O canal inativo fica de fora das duas: desligar o número de uma filial não
pode fazer a filial herdar o do restaurante sem ninguém pedir — o que se
espera de um número desligado é que a loja pare de mandar, não que ela passe
a mandar por outro.
"""

import pytest
from sqlalchemy.orm import Session

from src.models.whatsapp_model import WhatsAppChannel
from src.repositories.whatsapp_repository import WhatsAppChannelRepository
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _canal(
    db: Session,
    restaurante,
    filial=None,
    phone_number_id: str = "pni-1",
    is_active: bool = True,
) -> WhatsAppChannel:
    canal = WhatsAppChannel(
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial is not None else None,
        waba_id="waba-de-teste",
        phone_number_id=phone_number_id,
        display_phone_number="+55 85 99999-0000",
        access_token_encrypted="gAAAAA-cifrado-de-teste",
        is_active=is_active,
    )
    db.add(canal)
    db.flush()
    return canal


class TestDeQualNumeroEstaFilialFala:
    def test_a_filial_com_numero_usa_o_dela(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        _canal(db, restaurante, phone_number_id="pni-do-restaurante")
        da_filial = _canal(db, restaurante, filial, phone_number_id="pni-da-filial")

        achado = WhatsAppChannelRepository(db).resolve_for_branch(restaurante.id, filial.id)

        assert achado is not None
        assert achado.id == da_filial.id

    def test_a_filial_sem_numero_herda_o_do_restaurante(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        do_restaurante = _canal(db, restaurante, phone_number_id="pni-do-restaurante")

        achado = WhatsAppChannelRepository(db).resolve_for_branch(restaurante.id, filial.id)

        assert achado is not None
        assert achado.id == do_restaurante.id

    def test_sem_canal_nenhum_nao_ha_o_que_herdar(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)

        assert WhatsAppChannelRepository(db).resolve_for_branch(restaurante.id, filial.id) is None

    def test_a_filial_nao_herda_o_numero_de_outro_restaurante(self, db: Session) -> None:
        dono = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, dono)
        vizinho = fab.criar_restaurante(db)
        _canal(db, vizinho, phone_number_id="pni-do-vizinho")

        assert WhatsAppChannelRepository(db).resolve_for_branch(dono.id, filial.id) is None

    def test_numero_da_filial_desligado_nao_cai_no_do_restaurante(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        _canal(db, restaurante, phone_number_id="pni-do-restaurante")
        _canal(db, restaurante, filial, phone_number_id="pni-da-filial", is_active=False)

        assert WhatsAppChannelRepository(db).resolve_for_branch(restaurante.id, filial.id) is None


class TestDeQuemEEsteNumero:
    def test_o_webhook_acha_a_filial_pelo_phone_number_id(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        canal = _canal(db, restaurante, filial, phone_number_id="111222333444555")

        achado = WhatsAppChannelRepository(db).get_by_phone_number_id("111222333444555")

        assert achado is not None
        assert achado.id == canal.id
        assert achado.branch_id == filial.id

    def test_phone_number_id_desconhecido_devolve_nada(self, db: Session) -> None:
        assert WhatsAppChannelRepository(db).get_by_phone_number_id("999888777") is None

    def test_canal_desligado_nao_e_roteado(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        _canal(db, restaurante, phone_number_id="111222333444555", is_active=False)

        assert WhatsAppChannelRepository(db).get_by_phone_number_id("111222333444555") is None
