"""A revisão 20260823_0037 contra o Postgres.

O que se prova aqui não é que a coluna existe — é que ela nasce **falsa** e
que o `server_default` ficou no banco.

As duas coisas têm o mesmo dono e motivos diferentes. A primeira: marcar
usuário antigo como pendente inventaria um fato sobre a senha dele, e poria
todo lojista da plataforma numa tela de troca no dia do deploy. A segunda: o
`scripts/create_admin_user.py` continua criando usuário sem mencionar este
campo, e sem o default no banco esse INSERT passaria a violar o NOT NULL.
"""

import uuid

import pytest
from sqlalchemy import text

from src.models.admin_user_model import AdminUser
from tests.fabricas_db import criar_filial, criar_restaurante


pytestmark = pytest.mark.db


def test_a_coluna_e_not_null_com_default_falso(db):
    linha = db.execute(
        text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'admin_users' AND column_name = 'must_change_password'"
        )
    ).mappings().one()

    assert linha["is_nullable"] == "NO"
    assert "false" in linha["column_default"]


def test_usuario_criado_sem_mencionar_a_coluna_nasce_falso(db):
    """O caminho do `create_admin_user.py`, que nao conhece este campo."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)

    db.execute(
        text(
            "INSERT INTO admin_users (restaurant_id, branch_id, name, email, "
            "password_hash, role, is_active) "
            "VALUES (:restaurante, :filial, 'Antigo', :email, 'hash', 'owner', true)"
        ),
        {
            "restaurante": restaurante.id,
            "filial": filial.id,
            "email": f"antigo-{uuid.uuid4().hex[:10]}@exemplo.com",
        },
    )

    gravado = db.execute(
        text("SELECT must_change_password FROM admin_users WHERE name = 'Antigo'")
    ).scalar()
    assert gravado is False


def test_o_model_le_a_coluna(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    usuario = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        name="Pelo ORM",
        email=f"orm-{uuid.uuid4().hex[:10]}@exemplo.com",
        password_hash="hash",
        role="attendant",
        is_active=True,
    )
    db.add(usuario)
    db.flush()
    db.refresh(usuario)

    assert usuario.must_change_password is False


def test_password_changed_at_continua_independente(db):
    """Os dois campos respondem perguntas diferentes.

    `password_changed_at` nulo e o estado de TODO lojista antigo, e nao pode
    significar "precisa trocar" — foi por isso que esta coluna nasceu.
    """
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    usuario = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        name="Antigo de verdade",
        email=f"velho-{uuid.uuid4().hex[:10]}@exemplo.com",
        password_hash="hash",
        role="owner",
        is_active=True,
    )
    db.add(usuario)
    db.flush()
    db.refresh(usuario)

    assert usuario.password_changed_at is None
    assert usuario.must_change_password is False
