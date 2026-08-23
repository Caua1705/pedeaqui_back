"""Cadastro de usuario do painel, pelo dono do restaurante.

Antes destas rotas todo cadastro era `docker exec` + `create_admin_user.py`,
com `--branch-id` colado de uma consulta SQL feita antes.

## A senha nasce temporaria, e o servidor a escolhe

`POST` devolve uma senha aleatoria UMA vez e marca `must_change_password`. A
alternativa — o dono digitar a senha da equipe — foi descartada por um motivo
que nao e de senha: com ela o dono passa a CONHECER a credencial de outra
pessoa, e o `admin:{email}` que `order_status_history` grava deixa de
identificar quem de fato agiu.

## Ninguem e apagado

Nao ha `DELETE` aqui, e a ausencia e deliberada. Tres motivos concretos:
`print_agents.created_by_admin_user_id` e FK para `admin_users.id`;
`order_status_history.changed_by` guarda `admin:{email}` como TEXTO, entao um
e-mail reaproveitado depois faz o historico apontar para a pessoa errada, sem
sintoma; e `uq_admin_users_email` e unico e GLOBAL, entao e-mail liberado e
e-mail que reaparece em outro restaurante.

Desativar tem efeito na requisicao SEGUINTE, sem codigo novo:
`AdminAuthService._load_admin_from_token` recarrega o usuario do banco a cada
chamada e recusa `is_active` falso — inclusive no ticket do stream.

## As tres guardas, que sao o mesmo buraco por tres caminhos

Um restaurante sem dono ativo so se conserta por `docker exec`, que e o que
esta frente existe para eliminar. Chega-se la desativando a si mesmo,
desativando o ultimo dono, ou REBAIXANDO o ultimo dono — o terceiro e o mais
facil de esquecer, porque nao parece uma remocao.
"""

import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.core.constants import PAPEIS_DE_PESSOA, PAPEL_DE_DONO
from src.models.admin_user_model import AdminUser
from src.repositories.admin_user_repository import AdminUserRepository
from src.repositories.branch_repository import BranchRepository
from src.schemas.admin_user_schema import (
    AdminUserCreate,
    AdminUserCreatedResponse,
    AdminUserDetailResponse,
    AdminUserUpdate,
)
from src.utils.security import (
    PasswordTooLongError,
    generate_temporary_password,
    hash_password,
    utcnow,
)


logger = logging.getLogger("uvicorn.error")

USUARIO_NAO_ENCONTRADO = "Usuario nao encontrado"
# Mensagem unica para e-mail repetido, sem dizer de qual restaurante ele e. O
# UNIQUE e global, entao qualquer resposta ja revela que o e-mail existe na
# plataforma; o que esta mensagem evita e revelar ONDE.
EMAIL_EM_USO = "Este e-mail ja esta em uso"


class AdminUserService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = AdminUserRepository(db)
        self.branch_repository = BranchRepository(db)

    def list_people(self, scope: AdminScope) -> list[AdminUserDetailResponse]:
        usuarios = self.repository.list_people_by_restaurant(scope.restaurant_id)
        return [AdminUserDetailResponse.model_validate(usuario) for usuario in usuarios]

    def create(self, scope: AdminScope, payload: AdminUserCreate) -> AdminUserCreatedResponse:
        self._ensure_branch_belongs_to_restaurant(scope.restaurant_id, payload.branch_id)
        if self.repository.get_by_email(payload.email) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=EMAIL_EM_USO)

        temporary_password = generate_temporary_password()
        admin_user = AdminUser(
            restaurant_id=scope.restaurant_id,
            branch_id=payload.branch_id,
            name=payload.name,
            email=payload.email,
            password_hash=self._hash(temporary_password),
            role=payload.role,
            is_active=True,
            must_change_password=True,
        )
        try:
            self.repository.create(admin_user)
            self.db.commit()
            self.db.refresh(admin_user)
        except IntegrityError as exc:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=EMAIL_EM_USO) from exc
        except Exception:
            self.db.rollback()
            raise

        # Sem e-mail e sem id de quem foi criado no texto da senha: a linha de
        # log e a unica coisa deste fluxo que sobrevive a requisicao, e ela nao
        # pode carregar a credencial que a resposta carrega uma vez so.
        logger.info(
            "[AdminUsers] usuario criado admin_user_id=%s restaurant_id=%s role=%s criado_por=%s",
            admin_user.id,
            admin_user.restaurant_id,
            admin_user.role,
            scope.admin_user.id,
        )
        return AdminUserCreatedResponse(
            admin_user=AdminUserDetailResponse.model_validate(admin_user),
            temporary_password=temporary_password,
        )

    def update(
        self,
        scope: AdminScope,
        admin_user_id: uuid.UUID,
        payload: AdminUserUpdate,
    ) -> AdminUserDetailResponse:
        admin_user = self._get_person(scope, admin_user_id)
        changes = payload.model_dump(exclude_unset=True)

        self._ensure_not_deactivating_self(scope, admin_user, changes)
        self._ensure_keeps_an_active_owner(scope, admin_user, changes)
        if "branch_id" in changes:
            self._ensure_branch_belongs_to_restaurant(scope.restaurant_id, changes["branch_id"])

        desativando = changes.get("is_active") is False and admin_user.is_active
        for campo, valor in changes.items():
            setattr(admin_user, campo, valor)
        if desativando:
            # Sem esta linha, reativar a pessoa depois ressuscita os tokens de
            # 12h que ainda estiverem dentro do prazo — inclusive o de quem foi
            # desativado por suspeita.
            admin_user.password_changed_at = utcnow()

        try:
            self.repository.save(admin_user)
            self.db.commit()
            self.db.refresh(admin_user)
        except Exception:
            self.db.rollback()
            raise

        logger.info(
            "[AdminUsers] usuario editado admin_user_id=%s restaurant_id=%s campos=%s editado_por=%s",
            admin_user.id,
            admin_user.restaurant_id,
            sorted(changes),
            scope.admin_user.id,
        )
        return AdminUserDetailResponse.model_validate(admin_user)

    def reset_password(
        self,
        scope: AdminScope,
        admin_user_id: uuid.UUID,
    ) -> AdminUserCreatedResponse:
        """Nova senha temporaria, e todo token da pessoa morre na hora.

        E a segunda via da senha do POST — nao existe rota que mostre a
        anterior de novo, porque ela seria uma rota que devolve a senha de
        outra pessoa.
        """
        admin_user = self._get_person(scope, admin_user_id)
        temporary_password = generate_temporary_password()
        admin_user.password_hash = self._hash(temporary_password)
        admin_user.must_change_password = True
        # A linha que revoga. Sem ela a senha muda e a sessao de quem estava
        # com a antiga continua aberta pelas horas que faltam do token.
        admin_user.password_changed_at = utcnow()

        try:
            self.repository.save(admin_user)
            self.db.commit()
            self.db.refresh(admin_user)
        except Exception:
            self.db.rollback()
            raise

        logger.info(
            "[AdminUsers] senha redefinida admin_user_id=%s restaurant_id=%s redefinida_por=%s",
            admin_user.id,
            admin_user.restaurant_id,
            scope.admin_user.id,
        )
        return AdminUserCreatedResponse(
            admin_user=AdminUserDetailResponse.model_validate(admin_user),
            temporary_password=temporary_password,
        )

    def _get_person(self, scope: AdminScope, admin_user_id: uuid.UUID) -> AdminUser:
        """O usuario, se for uma PESSOA deste restaurante. 404 no resto.

        404 e nao 403 para o usuario de outro restaurante, pela regra de sempre:
        403 confirmaria que aquele id existe.

        E 404 tambem para a conta de MAQUINA, ainda que ela seja deste
        restaurante — estas rotas nao a alcancam nem para ler. Ela nao aparece
        no `GET`, entao editar por id seria a porta lateral da mesma decisao.
        """
        admin_user = self.repository.get_by_id_and_restaurant(admin_user_id, scope.restaurant_id)
        if admin_user is None or admin_user.role not in PAPEIS_DE_PESSOA:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=USUARIO_NAO_ENCONTRADO)
        return admin_user

    def _ensure_branch_belongs_to_restaurant(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None,
    ) -> None:
        """Filial nula = todas as filiais do restaurante, e isso e valido.

        404 e nao 403 na filial de outro restaurante, igual a
        `AdminScope.ensure_branch_allowed`: para quem esta neste restaurante,
        as filiais dos outros nao existem.
        """
        if branch_id is None:
            return
        if self.branch_repository.get_by_id_and_restaurant(branch_id, restaurant_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filial nao encontrada"
            )

    @staticmethod
    def _ensure_not_deactivating_self(
        scope: AdminScope,
        admin_user: AdminUser,
        changes: dict,
    ) -> None:
        """Ninguem se desativa sozinho.

        E o jeito mais rapido de um restaurante ficar sem dono, e o unico
        conserto seria `docker exec` — que e o que estas rotas existem para
        eliminar. Vale para qualquer papel: o atendente que se desativa por
        engano tambem precisa de alguem para desfazer.
        """
        if changes.get("is_active") is not False:
            return
        if admin_user.id != scope.admin_user.id:
            return
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Voce nao pode desativar a propria conta",
        )

    def _ensure_keeps_an_active_owner(
        self,
        scope: AdminScope,
        admin_user: AdminUser,
        changes: dict,
    ) -> None:
        """O restaurante nao pode ficar sem dono ativo.

        Chega-se la por DOIS caminhos, e o segundo e o que se esquece:
        desativar o ultimo dono, e REBAIXAR o ultimo dono. O segundo nao parece
        remocao nenhuma — o usuario continua ativo, so deixou de ser dono — e o
        restaurante fica sem quem cadastre gente do mesmo jeito.
        """
        if admin_user.role != PAPEL_DE_DONO or not admin_user.is_active:
            return

        desativando = changes.get("is_active") is False
        rebaixando = changes.get("role", PAPEL_DE_DONO) != PAPEL_DE_DONO
        if not desativando and not rebaixando:
            return
        if self.repository.count_active_owners(scope.restaurant_id) > 1:
            return

        acao = "desativar" if desativando else "rebaixar"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Nao da para {acao} o unico dono ativo do restaurante",
        )

    @staticmethod
    def _hash(password: str) -> str:
        try:
            return hash_password(password)
        except PasswordTooLongError as exc:  # pragma: no cover - a gerada tem 20 caracteres
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Senha muito longa"
            ) from exc
