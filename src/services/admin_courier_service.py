"""O lado do PAINEL da frente de entregadores.

Tres assuntos, e os tres ficam no mesmo service porque partilham o mesmo
escopo (a filial do token) e o mesmo `_get_branch`: a taxa que a filial
paga, o cadastro dos motoboys, e a atribuicao de pedido a motoboy.

O que este arquivo NAO faz: escrever status de pedido. Quem escreve e
`OrderStatusChangeService`, e a porta do entregador e
`CourierDeliveryService`.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.models.branch_model import Branch
from src.models.courier_model import Courier
from src.repositories.branch_repository import BranchRepository
from src.repositories.courier_repository import CourierRepository
from src.schemas.courier_schema import (
    AdminBranchCourierFeeResponse,
    AdminBranchCourierFeeUpdate,
    AdminCourierAccessResponse,
    AdminCourierCreate,
    AdminCourierResponse,
    AdminCourierUpdate,
)
from src.utils.money import money_to_float, quantize_money
from src.utils.security import (
    generate_courier_access_code,
    generate_courier_link_token,
    hash_courier_access_code,
    hash_courier_link_token,
    utcnow,
)


TELEFONE_EM_USO = "Já existe um entregador com este telefone nesta filial"
ENTREGADOR_NAO_ENCONTRADO = "Entregador não encontrado"


class AdminCourierService:
    def __init__(self, db: Session):
        self.db = db
        self.branch_repository = BranchRepository(db)
        self.courier_repository = CourierRepository(db)

    # --- A taxa da filial ---------------------------------------------------

    def get_courier_fee(
        self, scope: AdminScope, branch_id: uuid.UUID
    ) -> AdminBranchCourierFeeResponse:
        branch = self._get_branch(scope, branch_id)
        return self._courier_fee_response(branch)

    def update_courier_fee(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: AdminBranchCourierFeeUpdate,
    ) -> AdminBranchCourierFeeResponse:
        """Grava a taxa desta filial. Campo ausente nao e tocado; `null`
        explicito apaga — e `exclude_unset` que separa os dois."""
        branch = self._get_branch(scope, branch_id)
        changes = payload.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(branch, field, None if value is None else quantize_money(value))
        self._commit()
        return self._courier_fee_response(branch)

    # --- O cadastro ---------------------------------------------------------

    def list_couriers(
        self, scope: AdminScope, branch_id: uuid.UUID | None = None
    ) -> list[AdminCourierResponse]:
        effective_branch_id = scope.resolve_branch_filter(branch_id)
        couriers = self.courier_repository.list_by_restaurant(
            scope.restaurant_id, branch_id=effective_branch_id
        )
        return [self._courier_response(courier) for courier in couriers]

    def create_courier(self, scope: AdminScope, payload: AdminCourierCreate) -> AdminCourierResponse:
        branch = self._get_branch(scope, payload.branch_id)
        self._ensure_phone_is_free(branch.id, payload.phone)
        courier = Courier(
            restaurant_id=scope.restaurant_id,
            branch_id=branch.id,
            name=payload.name,
            phone=payload.phone,
            is_active=True,
        )
        self.courier_repository.create(courier)
        self._commit()
        return self._courier_response(courier)

    def get_courier(self, scope: AdminScope, courier_id: uuid.UUID) -> AdminCourierResponse:
        return self._courier_response(self._get_courier(scope, courier_id))

    def update_courier(
        self,
        scope: AdminScope,
        courier_id: uuid.UUID,
        payload: AdminCourierUpdate,
    ) -> AdminCourierResponse:
        courier = self._get_courier(scope, courier_id)
        changes = payload.model_dump(exclude_unset=True)

        if changes.get("phone") is not None:
            self._ensure_phone_is_free(courier.branch_id, changes["phone"], exclude_courier_id=courier.id)
        for field, value in changes.items():
            if value is not None:
                setattr(courier, field, value)

        # Desativar devolve os pedidos abertos para a fila: inativo nao
        # consegue mais marcar nada (a dependencia recusa o link), e um
        # pedido que ficasse com ele so apareceria ao painel como
        # "atribuido". Reativar NAO reabre: o painel reatribui.
        if changes.get("is_active") is False:
            self.courier_repository.mark_open_assignments_unassigned(
                courier.id, scope.admin_user.id
            )
        self._commit()
        return self._courier_response(courier)

    def delete_courier(self, scope: AdminScope, courier_id: uuid.UUID) -> None:
        """Exclusao logica: `deleted_at`, acesso revogado, corridas fechadas.

        DELETE de verdade nao existe porque `courier_assignments` referencia
        este cadastro e o historico e o que o dono usa para pagar — ele tem
        que sobreviver ao motoboy que saiu. Excluido some de toda lista e de
        toda leitura; o telefone fica livre para um cadastro novo.
        """
        courier = self._get_courier(scope, courier_id)
        courier.deleted_at = utcnow()
        courier.is_active = False
        courier.access_link_hash = None
        courier.access_code_hash = None
        self.courier_repository.mark_open_assignments_unassigned(courier.id, scope.admin_user.id)
        self._commit()

    def generate_access(self, scope: AdminScope, courier_id: uuid.UUID) -> AdminCourierAccessResponse:
        """Sorteia link e codigo, grava os hashes, devolve os dois EM CLARO.

        E a unica vez que o par existe fora do hash. Chamar de novo gera
        outro par e o anterior morre na mesma escrita: e assim que "o
        motoboy saiu, gera outro e o link velho morre na hora" e verdade —
        nao ha sessao nem token derivado que sobreviva a esta linha.

        Inativo nao ganha acesso (409): seria um par que a dependencia
        recusaria de qualquer jeito, e o dono veria "funcionou" numa tela e
        "nao entra" na outra.
        """
        courier = self._get_courier(scope, courier_id)
        if not courier.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Entregador inativo não recebe acesso. Reative-o antes.",
            )
        link_token = generate_courier_link_token()
        access_code = generate_courier_access_code()
        now = utcnow()
        courier.access_link_hash = hash_courier_link_token(link_token)
        courier.access_code_hash = hash_courier_access_code(access_code, link_token)
        courier.access_generated_at = now
        self._commit()
        return AdminCourierAccessResponse(
            courier_id=courier.id,
            link_token=link_token,
            access_code=access_code,
            access_generated_at=now,
        )

    # --- Escopo -------------------------------------------------------------

    def _get_branch(self, scope: AdminScope, branch_id: uuid.UUID) -> Branch:
        """Filial dentro do escopo do token.

        Duas conferencias, e as duas sao necessarias (o padrao de
        `AdminSettingsService._get_branch`): `ensure_branch_allowed` barra a
        filial que existe mas nao e a deste lojista; o repositorio barra a
        filial de outro restaurante. Mesmo 404 para as duas.
        """
        scope.ensure_branch_allowed(branch_id)
        branch = self.branch_repository.get_active_by_id_and_restaurant(
            branch_id, scope.restaurant_id
        )
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filial não encontrada"
            )
        return branch

    def _get_courier(self, scope: AdminScope, courier_id: uuid.UUID) -> Courier:
        """Entregador dentro do escopo: o repositorio confere o restaurante,
        e a filial so da para conferir depois de ler `courier.branch_id`.
        Mesmo 404 para "nao existe", "e de outro restaurante" e "e da
        filial vizinha"."""
        courier = self.courier_repository.get_by_id_and_restaurant(courier_id, scope.restaurant_id)
        if courier is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ENTREGADOR_NAO_ENCONTRADO)
        if not scope.sees_all_branches and courier.branch_id != scope.branch_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ENTREGADOR_NAO_ENCONTRADO)
        return courier

    def _ensure_phone_is_free(
        self,
        branch_id: uuid.UUID,
        phone: str,
        exclude_courier_id: uuid.UUID | None = None,
    ) -> None:
        if self.courier_repository.exists_phone_in_branch(branch_id, phone, exclude_courier_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=TELEFONE_EM_USO)

    def _commit(self) -> None:
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _courier_response(courier: Courier) -> AdminCourierResponse:
        return AdminCourierResponse(
            id=courier.id,
            branch_id=courier.branch_id,
            name=courier.name,
            phone=courier.phone,
            is_active=bool(courier.is_active),
            has_access=courier.access_link_hash is not None and courier.access_code_hash is not None,
            access_generated_at=courier.access_generated_at,
            created_at=courier.created_at,
        )

    @staticmethod
    def _courier_fee_response(branch: Branch) -> AdminBranchCourierFeeResponse:
        return AdminBranchCourierFeeResponse(
            branch_id=branch.id,
            courier_fee_base=(
                None if branch.courier_fee_base is None else money_to_float(branch.courier_fee_base)
            ),
            courier_fee_per_km=(
                None
                if branch.courier_fee_per_km is None
                else money_to_float(branch.courier_fee_per_km)
            ),
        )
