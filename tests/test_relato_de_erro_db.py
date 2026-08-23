"""O relato de erro contra o Postgres: o que é gravado e o que a retenção apaga.

Banco de verdade porque as três coisas que importam aqui **são** o banco: o
`CHECK` que recusa descrição em branco, o `SET NULL` que preserva o relato
quando o usuário é desligado, e o `DELETE` da varredura de 90 dias — que
nesta tabela não é faxina de disco, é o mecanismo de exclusão (armadilha 38).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.api.dependencies.admin_scope import build_admin_scope
from src.models.admin_error_report_model import AdminErrorReport
from src.models.admin_user_model import AdminUser
from src.repositories.admin_error_report_repository import AdminErrorReportRepository
from src.schemas.admin_error_report_schema import CreateErrorReportRequest
from src.services.admin_error_report_service import (
    AdminErrorReportService,
    error_report_retention_cutoff,
)
from tests.fabricas_db import criar_filial, criar_restaurante


pytestmark = pytest.mark.db


AGORA = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)


def criar_admin(db, restaurante, filial=None, role="attendant") -> AdminUser:
    usuario = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial else None,
        name=f"Pessoa {role}",
        email=f"{uuid.uuid4().hex[:12]}@exemplo.com",
        password_hash="$2b$12$" + "x" * 53,
        role=role,
        is_active=True,
    )
    db.add(usuario)
    db.flush()
    return usuario


def relatar(db, usuario, **campos):
    valores = {"description": "cliquei em salvar e a tela ficou branca"}
    valores.update(campos)
    return AdminErrorReportService(db).create(
        build_admin_scope(usuario),
        CreateErrorReportRequest(**valores),
    )


def gravado(db, resposta) -> AdminErrorReport:
    return db.get(AdminErrorReport, resposta.id)


# ---------------------------------------------------------------------------
# O que vem do token, e não do corpo
# ---------------------------------------------------------------------------


def test_restaurante_filial_e_usuario_saem_do_token(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    usuario = criar_admin(db, restaurante, filial)

    relato = gravado(db, relatar(db, usuario))

    assert relato.restaurant_id == restaurante.id
    assert relato.branch_id == filial.id
    assert relato.admin_user_id == usuario.id


def test_o_dono_relata_sem_filial(db):
    """`branch_id` nulo = "o relato não aponta uma loja", e não "loja desconhecida".

    O dono enxerga todas as filiais e não está em nenhuma — `build_admin_scope`
    devolve nulo para ele mesmo com `admin_users.branch_id` preenchido.
    """
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    dono = criar_admin(db, restaurante, filial, role="owner")

    relato = gravado(db, relatar(db, dono))

    assert relato.branch_id is None


# ---------------------------------------------------------------------------
# Credencial não entra na tabela
# ---------------------------------------------------------------------------


def test_o_token_colado_no_log_nao_chega_ao_banco(db):
    """O motivo inteiro da redação: quem lesse a tabela abriria o painel."""
    restaurante = criar_restaurante(db)
    usuario = criar_admin(db, restaurante)
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGd1ZW0ifQ.YXNzaW5hdHVyYV9kZV90ZXN0ZQ"

    relato = gravado(db, relatar(db, usuario, error_log=f"Authorization: Bearer {jwt}"))

    assert jwt not in relato.error_log
    assert "[redigido]" in relato.error_log


def test_a_descricao_tambem_atravessa_a_redacao(db):
    """Não é só o campo de log: o lojista cola token na história também."""
    restaurante = criar_restaurante(db)
    usuario = criar_admin(db, restaurante)

    relato = gravado(
        db,
        relatar(db, usuario, description='mandei {"tracking_token": "abcdefgh"} e deu 404'),
    )

    assert "abcdefgh" not in relato.description


# ---------------------------------------------------------------------------
# O que o banco recusa e o que ele preserva
# ---------------------------------------------------------------------------


def test_o_banco_recusa_descricao_em_branco(db):
    """A segunda porta: o schema já recusa, e o CHECK é a rede embaixo.

    Escrito pelo model de propósito — passar pelo service é impossível desde
    o validator, e o que está sob teste aqui é a constraint.
    """
    restaurante = criar_restaurante(db)
    usuario = criar_admin(db, restaurante)

    db.add(
        AdminErrorReport(
            restaurant_id=restaurante.id,
            admin_user_id=usuario.id,
            description="   ",
        )
    )
    with pytest.raises(Exception) as erro:
        db.flush()

    assert "ck_admin_error_reports_description_not_blank" in str(erro.value)
    db.rollback()


def test_desligar_o_usuario_nao_leva_o_relato_junto(db):
    """`SET NULL` e não `CASCADE`.

    O relato é o único registro de um bug que talvez ainda exista. Perder
    quem escreveu é aceitável; perder o relato porque a pessoa saiu da
    empresa não é.
    """
    restaurante = criar_restaurante(db)
    usuario = criar_admin(db, restaurante)
    resposta = relatar(db, usuario)

    db.execute(text("DELETE FROM admin_users WHERE id = :id"), {"id": usuario.id})
    db.flush()
    db.expire_all()

    relato = gravado(db, resposta)
    assert relato is not None
    assert relato.admin_user_id is None


def test_o_numero_do_pedido_e_gravado_sem_conferencia(db):
    """Sem FK e sem validação, de propósito.

    É um número que uma pessoa digitou olhando para a tela. Recusar o relato
    porque ele não casa com pedido nenhum seria recusá-lo exatamente na hora
    em que ele importa.
    """
    restaurante = criar_restaurante(db)
    usuario = criar_admin(db, restaurante)

    relato = gravado(db, relatar(db, usuario, order_number=999999))

    assert relato.order_number == 999999


# ---------------------------------------------------------------------------
# A retenção
# ---------------------------------------------------------------------------


def envelhecer(db, resposta, dias: int) -> None:
    db.execute(
        text("UPDATE admin_error_reports SET created_at = :quando WHERE id = :id"),
        {"quando": datetime.now(timezone.utc) - timedelta(days=dias), "id": resposta.id},
    )
    db.flush()


def test_a_varredura_apaga_o_relato_vencido_e_deixa_o_novo(db):
    restaurante = criar_restaurante(db)
    usuario = criar_admin(db, restaurante)
    velho = relatar(db, usuario, description="relato de quatro meses atras")
    novo = relatar(db, usuario, description="relato de ontem")
    envelhecer(db, velho, 120)
    envelhecer(db, novo, 1)

    apagados = AdminErrorReportRepository(db).delete_created_before(
        error_report_retention_cutoff(datetime.now(timezone.utc))
    )
    db.flush()

    assert apagados == 1
    assert gravado(db, velho) is None
    assert gravado(db, novo) is not None


def test_a_varredura_apaga_a_LINHA_e_nao_so_o_texto(db):
    """Diferente de `order_reviews`, aqui não há metade que valha guardar.

    Sem a descrição e sem o log sobra um carimbo de que alguém relatou
    alguma coisa um dia.
    """
    restaurante = criar_restaurante(db)
    usuario = criar_admin(db, restaurante)
    velho = relatar(db, usuario)
    envelhecer(db, velho, 91)

    AdminErrorReportRepository(db).delete_created_before(
        error_report_retention_cutoff(datetime.now(timezone.utc))
    )
    db.flush()

    assert gravado(db, velho) is None
