"""As quatro rotas de usuários do painel, contra o Postgres.

Banco de verdade porque quase tudo o que pode dar errado aqui é SQL ou
constraint: o UNIQUE global de e-mail, a contagem de donos ativos, o recorte
por restaurante e o `server_default` de `must_change_password`.

O que este arquivo protege, em ordem de gravidade:

1. **O restaurante não pode ficar sem dono ativo.** Como as quatro rotas são
   `SOMENTE_DONO`, quem edita é sempre um dono ativo — e com um dono só, ele é
   o próprio alvo. Sobram dois caminhos de verdade: **desativar a si mesmo** e
   **rebaixar a si mesmo**, e o segundo não parece remoção nenhuma (a pessoa
   continua ativa, só deixou de ser dono). O terceiro caminho — outro dono
   desativando o último — é coberto por defesa em profundidade, e o teste
   correspondente diz por que ele não é alcançável pela rota.
2. **A conta de máquina não é alcançada por nenhuma das quatro.** Nem para
   ler, nem para editar por id.
3. **Nada de outro restaurante entra**, nem pela lista nem pelo `{id}`.
4. **A senha temporária existe uma vez** e o banco só guarda o bcrypt dela.
"""

import uuid

import pytest
from fastapi import HTTPException

from src.api.dependencies.admin_scope import AdminScope, build_admin_scope
from src.models.admin_user_model import AdminUser
from src.schemas.admin_user_schema import AdminUserCreate, AdminUserUpdate
from src.services.admin_user_service import AdminUserService
from src.utils.security import verify_password
from tests.fabricas_db import criar_filial, criar_restaurante


pytestmark = pytest.mark.db


def criar_admin(db, restaurante, filial=None, role="owner", is_active=True) -> AdminUser:
    usuario = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial else None,
        name=f"Pessoa {role}",
        email=f"{uuid.uuid4().hex[:12]}@exemplo.com",
        password_hash="$2b$12$" + "x" * 53,
        role=role,
        is_active=is_active,
    )
    db.add(usuario)
    db.flush()
    return usuario


def escopo(usuario: AdminUser) -> AdminScope:
    return build_admin_scope(usuario)


def payload(**overrides) -> AdminUserCreate:
    valores = {
        "name": "Novo Atendente",
        "email": f"novo-{uuid.uuid4().hex[:10]}@exemplo.com",
        "role": "attendant",
    }
    valores.update(overrides)
    return AdminUserCreate(**valores)


# --- criar ---------------------------------------------------------------


def test_criar_devolve_a_senha_temporaria_e_o_banco_so_guarda_o_hash(db):
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)

    resposta = AdminUserService(db).create(escopo(dono), payload())

    assert len(resposta.temporary_password) >= 12
    criado = AdminUserService(db).repository.get_by_id(resposta.admin_user.id)
    assert criado.password_hash != resposta.temporary_password
    assert verify_password(resposta.temporary_password, criado.password_hash)


def test_criar_marca_a_troca_obrigatoria(db):
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)

    resposta = AdminUserService(db).create(escopo(dono), payload())

    assert resposta.admin_user.must_change_password is True


def test_duas_senhas_temporarias_nao_se_repetem(db):
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)
    servico = AdminUserService(db)

    primeira = servico.create(escopo(dono), payload()).temporary_password
    segunda = servico.create(escopo(dono), payload()).temporary_password

    assert primeira != segunda


def test_o_usuario_nasce_no_restaurante_de_quem_criou(db):
    """`restaurant_id` sai do token e nao do corpo — nao ha campo para mandar."""
    restaurante = criar_restaurante(db)
    vizinho = criar_restaurante(db, nome="Vizinho")
    dono = criar_admin(db, restaurante)

    resposta = AdminUserService(db).create(escopo(dono), payload())

    assert resposta.admin_user.restaurant_id == restaurante.id
    assert resposta.admin_user.restaurant_id != vizinho.id


def test_email_repetido_responde_409(db):
    """O UNIQUE de e-mail e GLOBAL, nao por restaurante."""
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)
    servico = AdminUserService(db)
    primeiro = servico.create(escopo(dono), payload())

    with pytest.raises(HTTPException) as erro:
        servico.create(escopo(dono), payload(email=primeiro.admin_user.email))

    assert erro.value.status_code == 409


def test_email_de_outro_restaurante_tambem_colide(db):
    """Global quer dizer global: e o motivo de ninguem ser apagado."""
    restaurante = criar_restaurante(db)
    vizinho = criar_restaurante(db, nome="Vizinho")
    dono = criar_admin(db, restaurante)
    dono_vizinho = criar_admin(db, vizinho)
    servico = AdminUserService(db)
    do_vizinho = servico.create(escopo(dono_vizinho), payload())

    with pytest.raises(HTTPException) as erro:
        servico.create(escopo(dono), payload(email=do_vizinho.admin_user.email))

    assert erro.value.status_code == 409


def test_filial_de_outro_restaurante_responde_404(db):
    """404 e nao 403: para este restaurante, a filial do outro nao existe."""
    restaurante = criar_restaurante(db)
    vizinho = criar_restaurante(db, nome="Vizinho")
    filial_alheia = criar_filial(db, vizinho)
    dono = criar_admin(db, restaurante)

    with pytest.raises(HTTPException) as erro:
        AdminUserService(db).create(escopo(dono), payload(branch_id=filial_alheia.id))

    assert erro.value.status_code == 404


def test_filial_nula_e_valida_e_significa_todas(db):
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)

    resposta = AdminUserService(db).create(escopo(dono), payload(branch_id=None))

    assert resposta.admin_user.branch_id is None


# --- listar --------------------------------------------------------------


def test_a_lista_nao_traz_a_conta_de_maquina(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    dono = criar_admin(db, restaurante)
    criar_admin(db, restaurante, filial, role="attendant")
    criar_admin(db, restaurante, filial, role="print_agent")

    papeis = [item.role for item in AdminUserService(db).list_people(escopo(dono))]

    assert sorted(papeis) == ["attendant", "owner"]


def test_a_lista_nao_traz_gente_de_outro_restaurante(db):
    restaurante = criar_restaurante(db)
    vizinho = criar_restaurante(db, nome="Vizinho")
    dono = criar_admin(db, restaurante)
    do_vizinho = criar_admin(db, vizinho, role="attendant")

    ids = [item.id for item in AdminUserService(db).list_people(escopo(dono))]

    assert ids == [dono.id]
    assert do_vizinho.id not in ids


def test_a_lista_nao_traz_password_hash(db):
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)

    item = AdminUserService(db).list_people(escopo(dono))[0]

    assert not hasattr(item, "password_hash")
    assert "password_hash" not in item.model_dump()


# --- editar --------------------------------------------------------------


def test_editar_nome_e_papel(db):
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)
    pessoa = criar_admin(db, restaurante, role="attendant")

    resposta = AdminUserService(db).update(
        escopo(dono), pessoa.id, AdminUserUpdate(name="  Nome Novo  ", role="manager")
    )

    assert resposta.name == "Nome Novo"
    assert resposta.role == "manager"


def test_desativar_grava_password_changed_at(db):
    """Sem isso, reativar depois ressuscita token de 12h ainda no prazo."""
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)
    pessoa = criar_admin(db, restaurante, role="attendant")
    assert pessoa.password_changed_at is None

    resposta = AdminUserService(db).update(
        escopo(dono), pessoa.id, AdminUserUpdate(is_active=False)
    )

    assert resposta.is_active is False
    assert resposta.password_changed_at is not None


def test_reativar_nao_mexe_no_password_changed_at(db):
    """So a DESATIVACAO revoga. Reativar nao pode revogar de novo o que ja foi."""
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)
    pessoa = criar_admin(db, restaurante, role="attendant")
    servico = AdminUserService(db)
    desativado = servico.update(escopo(dono), pessoa.id, AdminUserUpdate(is_active=False))

    reativado = servico.update(escopo(dono), pessoa.id, AdminUserUpdate(is_active=True))

    assert reativado.is_active is True
    assert reativado.password_changed_at == desativado.password_changed_at


def test_ninguem_desativa_a_propria_conta(db):
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)

    with pytest.raises(HTTPException) as erro:
        AdminUserService(db).update(escopo(dono), dono.id, AdminUserUpdate(is_active=False))

    assert erro.value.status_code == 400
    assert "própria conta" in erro.value.detail


def test_o_atendente_tambem_nao_se_desativa(db):
    """A guarda e de qualquer papel: desfazer exigiria outra pessoa."""
    restaurante = criar_restaurante(db)
    criar_admin(db, restaurante)
    atendente = criar_admin(db, restaurante, role="attendant")

    with pytest.raises(HTTPException) as erro:
        AdminUserService(db).update(escopo(atendente), atendente.id, AdminUserUpdate(is_active=False))

    assert erro.value.status_code == 400


def test_o_unico_dono_ativo_nao_se_rebaixa(db):
    """O caminho REAL de deixar o restaurante sem dono, e o que menos parece.

    As quatro rotas sao `SOMENTE_DONO`, entao quem edita e sempre um dono
    ativo. Se so existe UM dono ativo, ele e o proprio ator — e "desativar o
    ultimo dono" ja cai na guarda de nao se desativar. Sobra este: ele continua
    ativo, so deixou de ser dono, e o restaurante fica sem quem cadastre gente.
    """
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)
    criar_admin(db, restaurante, role="attendant")

    with pytest.raises(HTTPException) as erro:
        AdminUserService(db).update(escopo(dono), dono.id, AdminUserUpdate(role="manager"))

    assert erro.value.status_code == 400
    assert "rebaixar" in erro.value.detail


def test_o_dono_se_rebaixa_quando_ha_outro_dono_ativo(db):
    """A guarda conta donos, nao proibe auto-edicao: com dois, sair e legitimo."""
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)
    criar_admin(db, restaurante, role="owner")

    resposta = AdminUserService(db).update(escopo(dono), dono.id, AdminUserUpdate(role="manager"))

    assert resposta.role == "manager"


def test_a_guarda_tambem_vale_para_a_desativacao_de_outro_dono(db):
    """Defesa em profundidade: pela rota este ator nao existe.

    Desativar o ultimo dono ativo exigiria um ator que e dono, esta ativo e
    NAO e o alvo — e se o alvo e o ultimo dono ativo, esse ator nao existe.
    A guarda cobre o caminho mesmo assim, porque quem chamar o service de
    outro lugar (script, rota nova) nao repete a conta.
    """
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)
    ator = criar_admin(db, restaurante, role="owner", is_active=False)

    with pytest.raises(HTTPException) as erro:
        AdminUserService(db).update(escopo(ator), dono.id, AdminUserUpdate(is_active=False))

    assert erro.value.status_code == 400
    assert "único dono" in erro.value.detail


def test_um_dono_rebaixa_o_outro_quando_ha_dois(db):
    restaurante = criar_restaurante(db)
    primeiro = criar_admin(db, restaurante)
    segundo = criar_admin(db, restaurante, role="owner")

    resposta = AdminUserService(db).update(
        escopo(primeiro), segundo.id, AdminUserUpdate(role="manager")
    )

    assert resposta.role == "manager"


def test_dono_inativo_nao_conta_como_dono_ativo(db):
    """A contagem e de donos ATIVOS: um dono desativado nao libera o outro.

    Sem o `is_active` no WHERE, um dono antigo desativado faria a conta dar
    dois e o unico dono de verdade conseguiria se rebaixar.
    """
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)
    criar_admin(db, restaurante, role="owner", is_active=False)

    with pytest.raises(HTTPException) as erro:
        AdminUserService(db).update(escopo(dono), dono.id, AdminUserUpdate(role="manager"))

    assert erro.value.status_code == 400


def test_editar_usuario_de_outro_restaurante_responde_404(db):
    restaurante = criar_restaurante(db)
    vizinho = criar_restaurante(db, nome="Vizinho")
    dono = criar_admin(db, restaurante)
    alheio = criar_admin(db, vizinho, role="attendant")

    with pytest.raises(HTTPException) as erro:
        AdminUserService(db).update(escopo(dono), alheio.id, AdminUserUpdate(name="Invadido"))

    assert erro.value.status_code == 404


def test_editar_a_conta_de_maquina_responde_404(db):
    """Ela nao aparece no GET; editar por id seria a porta lateral disso."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    dono = criar_admin(db, restaurante)
    agente = criar_admin(db, restaurante, filial, role="print_agent")

    with pytest.raises(HTTPException) as erro:
        AdminUserService(db).update(escopo(dono), agente.id, AdminUserUpdate(role="owner"))

    assert erro.value.status_code == 404


def test_mover_para_filial_de_outro_restaurante_responde_404(db):
    restaurante = criar_restaurante(db)
    vizinho = criar_restaurante(db, nome="Vizinho")
    filial_alheia = criar_filial(db, vizinho)
    dono = criar_admin(db, restaurante)
    pessoa = criar_admin(db, restaurante, role="attendant")

    with pytest.raises(HTTPException) as erro:
        AdminUserService(db).update(
            escopo(dono), pessoa.id, AdminUserUpdate(branch_id=filial_alheia.id)
        )

    assert erro.value.status_code == 404


def test_soltar_a_filial_e_uma_edicao_valida(db):
    """`branch_id: null` no corpo significa "todas as filiais", nao "nao mexa"."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    dono = criar_admin(db, restaurante)
    pessoa = criar_admin(db, restaurante, filial, role="manager")
    assert pessoa.branch_id == filial.id

    resposta = AdminUserService(db).update(
        escopo(dono), pessoa.id, AdminUserUpdate(branch_id=None)
    )

    assert resposta.branch_id is None


# --- redefinir senha ------------------------------------------------------


def test_reset_gera_senha_nova_e_revoga_os_tokens(db):
    restaurante = criar_restaurante(db)
    dono = criar_admin(db, restaurante)
    servico = AdminUserService(db)
    criado = servico.create(escopo(dono), payload())

    redefinido = servico.reset_password(escopo(dono), criado.admin_user.id)

    assert redefinido.temporary_password != criado.temporary_password
    assert redefinido.admin_user.must_change_password is True
    assert redefinido.admin_user.password_changed_at is not None
    usuario = servico.repository.get_by_id(criado.admin_user.id)
    assert verify_password(redefinido.temporary_password, usuario.password_hash)
    assert not verify_password(criado.temporary_password, usuario.password_hash)


def test_reset_de_outro_restaurante_responde_404(db):
    restaurante = criar_restaurante(db)
    vizinho = criar_restaurante(db, nome="Vizinho")
    dono = criar_admin(db, restaurante)
    alheio = criar_admin(db, vizinho, role="attendant")

    with pytest.raises(HTTPException) as erro:
        AdminUserService(db).reset_password(escopo(dono), alheio.id)

    assert erro.value.status_code == 404


def test_reset_da_conta_de_maquina_responde_404(db):
    """A rotacao daquela senha continua sendo o script, seguido do config.ini."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    dono = criar_admin(db, restaurante)
    agente = criar_admin(db, restaurante, filial, role="print_agent")

    with pytest.raises(HTTPException) as erro:
        AdminUserService(db).reset_password(escopo(dono), agente.id)

    assert erro.value.status_code == 404
