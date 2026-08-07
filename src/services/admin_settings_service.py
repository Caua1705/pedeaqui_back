"""Regras da configuracao no painel (BLOCO C da Fase 3).

Duas coisas mudam de dono aqui, e a diferenca importa:

- **Restaurante**: `is_open`, valor minimo, taxa de servico, aceita
  entrega/retirada. Vale para todas as filiais.
- **Filial**: endereco, contato, regras de entrega, horario e formas de
  pagamento. E aqui que o escopo por filial morde — um manager preso a uma
  filial nao enxerga nem escreve a configuracao das outras.

`platform_commission_percent` nao e lido nem escrito por nada deste
arquivo: e dado da plataforma, negociado por contrato (ver
admin_settings_schema).

Validacao de campo que so faz sentido em par (minimo x maximo) acontece
sobre a MESCLA com o que esta no banco e responde 422 — o mesmo codigo que
o Pydantic daria se todos os campos tivessem vindo no corpo. Sem isso, um
`delivery_max_fee` enviado sozinho passaria valido e deixaria a filial com
teto menor que o piso.
"""

import uuid
from datetime import time

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_model import Branch
from src.models.branch_payment_method_model import BranchPaymentMethod
from src.models.restaurant_setting_model import RestaurantSetting
from src.repositories.admin_settings_repository import AdminSettingsRepository
from src.repositories.branch_repository import BranchRepository
from src.schemas.admin_settings_schema import (
    MAX_PERIODS_PER_WEEKDAY,
    AdminBranchDeliveryRules,
    AdminBranchResponse,
    AdminBranchUpdate,
    AdminPaymentMethodCreate,
    AdminPaymentMethodResponse,
    AdminPaymentMethodUpdate,
    AdminRestaurantSettingsResponse,
    AdminRestaurantSettingsUpdate,
    BusinessHourInput,
    BusinessHourResponse,
    BusinessHoursReplaceRequest,
    StoreStatusRequest,
)
from src.utils.money import money_to_float, quantize_money


MINUTES_IN_A_DAY = 24 * 60


class AdminSettingsService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = AdminSettingsRepository(db)
        self.branch_repository = BranchRepository(db)

    def get_restaurant_settings(self, scope: AdminScope) -> AdminRestaurantSettingsResponse:
        return self._settings_response(self._get_or_create_settings(scope.restaurant_id))

    def update_restaurant_settings(
        self,
        scope: AdminScope,
        payload: AdminRestaurantSettingsUpdate,
    ) -> AdminRestaurantSettingsResponse:
        settings_row = self._get_or_create_settings(scope.restaurant_id)
        changes = payload.model_dump(exclude_unset=True)

        self._ensure_delivery_time_range(
            changes.get("estimated_delivery_time_min", settings_row.estimated_delivery_time_min),
            changes.get("estimated_delivery_time_max", settings_row.estimated_delivery_time_max),
        )
        for field in ("min_order_value", "default_delivery_fee", "service_fee_amount"):
            if changes.get(field) is not None:
                changes[field] = quantize_money(changes[field])

        for field, value in changes.items():
            setattr(settings_row, field, value)
        self._commit()
        return self._settings_response(settings_row)

    def set_store_status(
        self,
        scope: AdminScope,
        payload: StoreStatusRequest,
    ) -> AdminRestaurantSettingsResponse:
        """Abre ou fecha a loja (BLOCO C1).

        Fechar aqui bloqueia pedido de verdade: `OrderService` le
        `is_open` antes de aceitar qualquer pedido. Nao mexe em horario —
        e a pausa manual do dia (faltou gas, fila grande), nao o cadastro
        de funcionamento.
        """
        settings_row = self._get_or_create_settings(scope.restaurant_id)
        settings_row.is_open = payload.is_open
        self._commit()
        return self._settings_response(settings_row)

    def list_branches(self, scope: AdminScope) -> list[AdminBranchResponse]:
        """Filiais que este lojista enxerga.

        Quem esta preso a uma filial recebe uma lista de um item — e o que
        faz o seletor de filial do painel ja vir resolvido, sem a tela
        precisar conhecer a regra de escopo.
        """
        branches = self.branch_repository.list_active_by_restaurant(scope.restaurant_id)
        if scope.sees_all_branches:
            return [self._branch_response(branch) for branch in branches]
        return [
            self._branch_response(branch)
            for branch in branches
            if branch.id == scope.branch_id
        ]

    def get_branch(self, scope: AdminScope, branch_id: uuid.UUID) -> AdminBranchResponse:
        return self._branch_response(self._get_branch(scope, branch_id))

    def update_branch(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: AdminBranchUpdate,
    ) -> AdminBranchResponse:
        branch = self._get_branch(scope, branch_id)
        changes = payload.model_dump(exclude_unset=True)
        self._validate_merged_delivery_rules(branch, changes)

        for field, value in changes.items():
            setattr(branch, field, value)
        self._commit()
        return self._branch_response(branch)

    def list_business_hours(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
    ) -> list[BusinessHourResponse]:
        self._get_branch(scope, branch_id)
        periods = self.branch_repository.list_business_hours(branch_id)
        return [BusinessHourResponse.model_validate(period) for period in periods]

    def replace_business_hours(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: BusinessHoursReplaceRequest,
    ) -> list[BusinessHourResponse]:
        """Substitui a semana inteira da filial (BLOCO C2).

        Apagar e regravar, e nao editar faixa por faixa, porque a tela
        salva a grade dos sete dias de uma vez. Como as duas operacoes
        ficam na mesma transacao, ou a semana nova entra inteira ou a
        antiga permanece — nunca meia semana.

        `branch_business_hours` nao e referenciada por pedido nenhum, entao
        aqui o DELETE e seguro (diferente do cardapio, onde nada e apagado).
        """
        self._get_branch(scope, branch_id)
        self._validate_weekly_periods(payload.periods)

        # sort_order acompanha a ordem em que as faixas do dia chegaram: e
        # o desempate que BranchHoursService usa para ler "almoco" antes de
        # "jantar".
        position_by_weekday: dict[int, int] = {}
        rows: list[BranchBusinessHour] = []
        for period in payload.periods:
            position = position_by_weekday.get(period.weekday, 0)
            position_by_weekday[period.weekday] = position + 1
            rows.append(
                BranchBusinessHour(
                    branch_id=branch_id,
                    weekday=period.weekday,
                    opens_at=period.opens_at,
                    closes_at=period.closes_at,
                    prep_time_min=period.prep_time_min,
                    prep_time_max=period.prep_time_max,
                    is_closed=period.is_closed,
                    sort_order=position,
                )
            )

        def write() -> None:
            self.repository.delete_business_hours(branch_id)
            if rows:
                self.repository.add_business_hours(rows)

        self._commit(write)
        return [
            BusinessHourResponse.model_validate(period)
            for period in self.branch_repository.list_business_hours(branch_id)
        ]

    def list_payment_methods(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
    ) -> list[AdminPaymentMethodResponse]:
        self._get_branch(scope, branch_id)
        methods = self.repository.list_payment_methods(branch_id)
        return [AdminPaymentMethodResponse.model_validate(method) for method in methods]

    def create_payment_method(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: AdminPaymentMethodCreate,
    ) -> AdminPaymentMethodResponse:
        self._get_branch(scope, branch_id)
        duplicated = self.repository.get_payment_method_by_type(
            branch_id,
            payload.payment_flow,
            payload.method_type,
            payload.brand,
        )
        if duplicated is not None:
            # Duas linhas iguais apareceriam duas vezes no checkout do
            # cliente, e desabilitar uma nao tiraria a outra da tela.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Esta forma de pagamento ja esta cadastrada nesta filial",
            )

        method = BranchPaymentMethod(branch_id=branch_id, **payload.model_dump())
        self._commit(lambda: self.repository.add_payment_method(method))
        return AdminPaymentMethodResponse.model_validate(method)

    def update_payment_method(
        self,
        scope: AdminScope,
        method_id: uuid.UUID,
        payload: AdminPaymentMethodUpdate,
    ) -> AdminPaymentMethodResponse:
        method = self._get_payment_method(scope, method_id)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(method, field, value)
        self._commit()
        return AdminPaymentMethodResponse.model_validate(method)

    def delete_payment_method(self, scope: AdminScope, method_id: uuid.UUID) -> None:
        """Remove a forma de pagamento da filial.

        Aqui o DELETE e de verdade, ao contrario do cardapio: `orders`
        guarda a forma de pagamento como texto (`payment_method`), nao por
        FK, entao apagar esta linha nao mexe em pedido nenhum ja fechado.
        """
        method = self._get_payment_method(scope, method_id)
        self._commit(lambda: self.repository.delete_payment_method(method))

    def _get_or_create_settings(self, restaurant_id: uuid.UUID) -> RestaurantSetting:
        """Linha de restaurant_settings, criada na primeira edicao.

        A linha e opcional no schema (restaurante sem configuracao aceita
        pedido com os defaults, ver OrderService._validate_store_accepts_order).
        Criar sob demanda evita que a primeira visita ao painel responda 404
        para um restaurante que funciona normalmente.
        """
        settings_row = self.repository.get_settings(restaurant_id)
        if settings_row is not None:
            return settings_row

        settings_row = RestaurantSetting(restaurant_id=restaurant_id)
        self._commit(lambda: self.repository.add_settings(settings_row))
        return settings_row

    def _get_branch(self, scope: AdminScope, branch_id: uuid.UUID) -> Branch:
        """Filial dentro do escopo do token.

        As duas conferencias sao necessarias e diferentes: `ensure_branch_allowed`
        barra a filial que existe mas nao e a deste lojista; o repositorio
        barra a filial de outro restaurante. Mesmo 404 para as duas, para
        nao virar oraculo de quais filiais existem na plataforma.
        """
        scope.ensure_branch_allowed(branch_id)
        branch = self.branch_repository.get_active_by_id_and_restaurant(
            branch_id, scope.restaurant_id
        )
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filial nao encontrada"
            )
        return branch

    def _get_payment_method(
        self,
        scope: AdminScope,
        method_id: uuid.UUID,
    ) -> BranchPaymentMethod:
        method = self.repository.get_payment_method(method_id, scope.restaurant_id)
        if method is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Forma de pagamento nao encontrada",
            )
        # A juncao do repositorio ja conferiu o restaurante; falta a filial,
        # que so da para conferir depois de ler `method.branch_id`.
        scope.ensure_branch_allowed(method.branch_id)
        return method

    def _validate_merged_delivery_rules(self, branch: Branch, changes: dict) -> None:
        merged = {
            field: changes.get(field, getattr(branch, field))
            for field in AdminBranchDeliveryRules.model_fields
        }
        try:
            AdminBranchDeliveryRules.model_validate(merged)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="; ".join(error["msg"] for error in exc.errors()),
            ) from exc

    def _validate_weekly_periods(self, periods: list[BusinessHourInput]) -> None:
        by_weekday: dict[int, list[BusinessHourInput]] = {}
        for period in periods:
            by_weekday.setdefault(period.weekday, []).append(period)

        for weekday, day_periods in by_weekday.items():
            if len(day_periods) > MAX_PERIODS_PER_WEEKDAY:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Dia {weekday}: no maximo {MAX_PERIODS_PER_WEEKDAY} "
                        "faixas de horario"
                    ),
                )
            open_periods = [period for period in day_periods if not period.is_closed]
            if open_periods and len(open_periods) != len(day_periods):
                # Uma faixa "fechado" convivendo com faixa aberta e
                # contraditorio, e BranchHoursService resolveria a favor da
                # aberta — o lojista veria a loja abrir no dia que marcou
                # como fechado.
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Dia {weekday}: nao misture faixa fechada com faixa aberta",
                )
            self._ensure_no_overlap(weekday, open_periods)

    @staticmethod
    def _ensure_no_overlap(weekday: int, periods: list[BusinessHourInput]) -> None:
        """Recusa faixas do mesmo dia que se sobrepoem.

        Duas faixas sobrepostas nao "abrem mais": elas tornam o tempo de
        preparo imprevisivel, porque `BranchHoursService.find_current_period`
        devolve a PRIMEIRA que contem o agora e o pedido sai com o prazo
        dessa.

        Faixa que vira a noite e esticada para depois das 24h (18:00–02:00
        vira 1080–1560) para caber na mesma comparacao. A sobreposicao entre
        a virada de um dia e a madrugada do dia SEGUINTE nao e conferida
        aqui: nesse caso a loja esta aberta pelos dois lados, que e o que o
        lojista quis dizer de qualquer jeito.
        """
        intervals = sorted(
            (_minutes(period.opens_at), _closing_minutes(period))
            for period in periods
        )
        for (_, previous_end), (current_start, _) in zip(intervals, intervals[1:]):
            if current_start < previous_end:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Dia {weekday}: as faixas de horario se sobrepoem",
                )

    @staticmethod
    def _ensure_delivery_time_range(minimum: int | None, maximum: int | None) -> None:
        if minimum is not None and maximum is not None and maximum < minimum:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="estimated_delivery_time_max nao pode ser menor que o minimo",
            )

    def _commit(self, before_commit=None) -> None:
        """Grava, com rollback em qualquer falha.

        Mesmo arranjo de AdminMenuService._commit: o que precede o commit
        (delete + insert do horario, por exemplo) entra no mesmo try, para
        que uma falha no meio nao deixe a sessao suja.
        """
        try:
            if before_commit is not None:
                before_commit()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _settings_response(settings_row: RestaurantSetting) -> AdminRestaurantSettingsResponse:
        return AdminRestaurantSettingsResponse(
            min_order_value=money_to_float(settings_row.min_order_value),
            estimated_delivery_time_min=settings_row.estimated_delivery_time_min,
            estimated_delivery_time_max=settings_row.estimated_delivery_time_max,
            default_delivery_fee=money_to_float(settings_row.default_delivery_fee),
            service_fee_enabled=settings_row.service_fee_enabled,
            service_fee_amount=money_to_float(settings_row.service_fee_amount),
            accepts_delivery=settings_row.accepts_delivery,
            accepts_pickup=settings_row.accepts_pickup,
            is_open=settings_row.is_open,
        )

    @staticmethod
    def _branch_response(branch: Branch) -> AdminBranchResponse:
        return AdminBranchResponse(
            id=branch.id,
            name=branch.name,
            slug=branch.slug,
            display_name=branch.display_name,
            email=branch.email,
            phone=branch.phone,
            whatsapp=branch.whatsapp,
            address=branch.address,
            neighborhood=branch.neighborhood,
            city=branch.city,
            state=branch.state,
            zipcode=branch.zipcode,
            latitude=float(branch.latitude) if branch.latitude is not None else None,
            longitude=float(branch.longitude) if branch.longitude is not None else None,
            delivery_base_fee=_optional_money(branch.delivery_base_fee),
            delivery_fee_per_km=_optional_money(branch.delivery_fee_per_km),
            delivery_min_fee=_optional_money(branch.delivery_min_fee),
            delivery_max_fee=_optional_money(branch.delivery_max_fee),
            delivery_max_distance_km=(
                float(branch.delivery_max_distance_km)
                if branch.delivery_max_distance_km is not None
                else None
            ),
            is_main=branch.is_main,
            is_active=branch.is_active,
        )


def _optional_money(value) -> float | None:
    """Nulo continua nulo.

    `money_to_float(None)` devolveria 0.00, e "sem taxa por km cadastrada"
    viraria "taxa por km igual a zero" na tela — duas coisas diferentes para
    quem esta configurando entrega.
    """
    return money_to_float(value) if value is not None else None


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _closing_minutes(period: BusinessHourInput) -> int:
    """Fim da faixa em minutos, esticado quando ela vira a noite."""
    opens = _minutes(period.opens_at)
    closes = _minutes(period.closes_at)
    return closes if closes > opens else closes + MINUTES_IN_A_DAY
