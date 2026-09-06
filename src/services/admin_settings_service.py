"""Regras da configuracao no painel (BLOCO C da Fase 3).

Tres niveis, e a diferenca entre eles e o que este arquivo organiza:

- **Restaurante, e so padrao**: valor minimo, taxa de servico, prazo
  estimado, taxa de contingencia. Nenhum pedido le estes valores direto — a
  filial os herda nos campos que deixou nulos.
- **Filial, estado do dia**: `is_open` (o "fechar agora"), `accepts_delivery`
  e `accepts_pickup`. Nao herdam nada e nao tem padrao: sao o que alguem no
  balcao aperta durante o expediente, e ate a revisao 20260818_0025 eles
  fechavam a rede inteira de uma vez.
- **Filial, o resto**: endereco, contato, regras de entrega, horario, formas
  de pagamento e as sobrescritas comerciais. E aqui que o escopo por filial
  morde — um manager preso a uma filial nao enxerga nem escreve a
  configuracao das outras.

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
from datetime import time, timedelta

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope, ensure_pode_definir_cashback
from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_delivery_time_band_model import BranchDeliveryTimeBand
from src.models.branch_model import Branch
from src.models.branch_payment_method_model import BranchPaymentMethod
from src.models.restaurant_model import Restaurant
from src.models.restaurant_setting_model import RestaurantSetting
from src.repositories.admin_settings_repository import AdminSettingsRepository
from src.repositories.branch_repository import BranchRepository
from src.repositories.restaurant_repository import RestaurantRepository
from src.schemas.admin_settings_schema import (
    MAX_PERIODS_PER_WEEKDAY,
    MAX_PREP_TIME_MINUTES,
    AdminBranchDeliveryPauseRequest,
    AdminBranchDeliveryRules,
    AdminBranchOperationEffective,
    AdminBranchOperationOverrides,
    AdminBranchOperationResponse,
    AdminBranchOrderTypesRequest,
    AdminBranchSettingsUpdate,
    AdminBranchResponse,
    AdminBranchUpdate,
    AdminPaymentMethodCreate,
    AdminPaymentMethodResponse,
    AdminPaymentMethodUpdate,
    AdminRestaurantProfileResponse,
    AdminRestaurantProfileUpdate,
    AdminRestaurantSettingsResponse,
    AdminRestaurantSettingsUpdate,
    BranchPrepTimeAdjustRequest,
    BusinessHourInput,
    BusinessHourResponse,
    BusinessHoursReplaceRequest,
    DeliveryTimeBandResponse,
    DeliveryTimeBandsReplaceRequest,
    StoreStatusRequest,
)
from src.services.branch_hours_service import BranchHoursService
from src.services.branch_operation import BranchOperation, resolve_branch_operation
from src.services.branch_scope import BranchScope
from src.utils.money import money_to_float, quantize_money
from src.utils.security import utcnow


MINUTES_IN_A_DAY = 24 * 60

# Campos de dinheiro que passam por `quantize_money` antes de gravar, nos
# DOIS lados da heranca. Lista unica de proposito: eram duas copias identicas
# (padrao do restaurante e sobrescrita da filial), e um campo novo entrava
# numa e nao na outra — gravando 12.345 numa ponta e 12.35 na outra.
MONEY_FIELDS = (
    "min_order_value",
    "default_delivery_fee",
    "service_fee_amount",
    "free_delivery_min_order_value",
)


class AdminSettingsService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = AdminSettingsRepository(db)
        self.branch_repository = BranchRepository(db)
        self.branch_scope = BranchScope(db)
        # O PERFIL mora em `restaurants`, e nao em `restaurant_settings`: sao
        # tabelas diferentes, e as rotas de perfil sao as unicas deste
        # service que escrevem na primeira.
        self.restaurant_repository = RestaurantRepository(db)
        # Mesma classe que a estimativa de entrega usa para achar a faixa
        # vigente. O atalho de tempo de preparo PRECISA concordar com ela:
        # ajustar uma faixa diferente da que o pedido le seria mudar um
        # numero que ninguem vai olhar.
        self.branch_hours_service = BranchHoursService(db)

    def get_restaurant_profile(self, scope: AdminScope) -> AdminRestaurantProfileResponse:
        return AdminRestaurantProfileResponse.model_validate(self._get_restaurant(scope))

    def update_restaurant_profile(
        self,
        scope: AdminScope,
        payload: AdminRestaurantProfileUpdate,
    ) -> AdminRestaurantProfileResponse:
        """Grava os dois textos do lojista sobre a casa.

        Parcial como todo PATCH daqui: `exclude_unset` distingue "nao mandei
        este campo" de "mandei nulo". Nulo APAGA — e apagar `assistant_notes`
        devolve o assistente ao estado de antes desta frente, com o prompt
        sem a linha `Sobre a casa`. Nao ha terceiro estado nem heranca de
        quem: acima do restaurante nao ha ninguem.
        """
        restaurant = self._get_restaurant(scope)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(restaurant, field, value)
        self._commit()
        return AdminRestaurantProfileResponse.model_validate(restaurant)

    def _get_restaurant(self, scope: AdminScope) -> Restaurant:
        """A linha de `restaurants` do token. 404 se ela sumiu.

        `get_by_id` e nao `get_active_by_id`: um restaurante desativado ainda
        tem painel, e e por ele que o dono arruma o cadastro antes de voltar.
        Recusar aqui trancaria a porta pelo lado de dentro.
        """
        restaurant = self.restaurant_repository.get_by_id(scope.restaurant_id)
        if restaurant is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Restaurante não encontrado",
            )
        return restaurant

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
        for field in MONEY_FIELDS:
            if changes.get(field) is not None:
                changes[field] = quantize_money(changes[field])

        for field, value in changes.items():
            setattr(settings_row, field, value)
        self._commit()
        return self._settings_response(settings_row)

    def set_store_status(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: StoreStatusRequest,
    ) -> AdminBranchOperationResponse:
        """Abre ou fecha ESTA filial (BLOCO C1).

        Fechar aqui bloqueia pedido de verdade: `OrderService` le `is_open`
        da filial antes de aceitar qualquer pedido, e a tela de escolha de
        filial devolve `is_open_now = false` com `closed_reason =
        branch_paused`. Nao mexe em horario — e a pausa manual do dia
        (faltou gas, fila grande), nao o cadastro de funcionamento.

        Ate a revisao 20260818_0025 esta rota era do restaurante e fechava
        todas as filiais juntas. Nao havia como fechar so a do Centro.
        """
        branch = self.branch_scope.get(scope=scope, branch_id=branch_id)
        branch.is_open = payload.is_open
        self._commit()
        return self._branch_operation_response(branch)

    def set_branch_order_types(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: AdminBranchOrderTypesRequest,
    ) -> AdminBranchOperationResponse:
        """Liga e desliga entrega e retirada NESTA filial.

        Nao recusa desligar as duas. Uma filial que nao aceita nem entrega
        nem retirada esta, na pratica, fechada — e fechada e um estado que o
        lojista ja pode pedir por `store-status`. Recusar aqui obrigaria a
        adivinhar qual das duas ele quis manter.
        """
        branch = self.branch_scope.get(scope=scope, branch_id=branch_id)
        changes = payload.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(branch, field, value)
        self._commit()
        return self._branch_operation_response(branch)

    def pause_branch_delivery(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: AdminBranchDeliveryPauseRequest,
    ) -> AdminBranchOperationResponse:
        """Pausa (ou retoma) a entrega desta filial.

        `minutes = 0` retoma na hora, e por isso nao ha rota de DELETE: "pare
        por 40 minutos" e "volte agora" sao o mesmo gesto na mesma tela, e
        dois verbos para um botao so seriam dois lugares para o painel errar.

        **Nao mexe em `accepts_delivery`, de proposito.** A pausa e o dia; a
        chave e o negocio. Desligar a chave aqui faria a entrega NAO voltar
        quando o prazo vencesse, que e justamente a propriedade pela qual
        esta rota existe.

        O motivo e apagado junto ao retomar: motivo de uma pausa que acabou
        so tem como confundir quem abrir a tela depois.
        """
        branch = self.branch_scope.get(scope=scope, branch_id=branch_id)
        if payload.minutes == 0:
            branch.delivery_paused_until = None
            branch.delivery_pause_reason = None
        else:
            branch.delivery_paused_until = utcnow() + timedelta(minutes=payload.minutes)
            branch.delivery_pause_reason = payload.reason
        self._commit()
        return self._branch_operation_response(branch)

    def list_delivery_time_bands(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
    ) -> list[DeliveryTimeBandResponse]:
        self.branch_scope.get(scope=scope, branch_id=branch_id)
        return [
            DeliveryTimeBandResponse(
                id=band.id,
                branch_id=band.branch_id,
                max_distance_km=float(band.max_distance_km),
                delivery_time_min=band.delivery_time_min,
                delivery_time_max=band.delivery_time_max,
            )
            for band in self.branch_repository.list_delivery_time_bands(branch_id)
        ]

    def replace_delivery_time_bands(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: DeliveryTimeBandsReplaceRequest,
    ) -> list[DeliveryTimeBandResponse]:
        """Substitui TODAS as faixas da filial pelas do corpo.

        Substitui, e nao edita faixa a faixa, pelo mesmo motivo do
        `PUT /business-hours`: a tela e uma tabelinha salva junta. E com a
        mesma armadilha do outro lado — **lista vazia apaga tudo**, e o
        resultado nao e "sem entrega": e o prazo voltando a sair do tempo do
        Google, que e o comportamento de quem nunca configurou faixa.

        O flush entre o DELETE e o INSERT e obrigatorio pela mesma razao da
        lista de impressoras: sem ele, o UNIQUE `(branch_id, max_distance_km)`
        bate contra as linhas que estao sendo apagadas na mesma transacao.
        """
        self.branch_scope.get(scope=scope, branch_id=branch_id)
        novas = [
            BranchDeliveryTimeBand(
                branch_id=branch_id,
                max_distance_km=band.max_distance_km,
                delivery_time_min=band.delivery_time_min,
                delivery_time_max=band.delivery_time_max,
            )
            for band in payload.bands
        ]

        def replace() -> None:
            self.branch_repository.delete_delivery_time_bands(branch_id)
            self.db.flush()
            self.branch_repository.add_delivery_time_bands(novas)

        self._commit(replace)
        return self.list_delivery_time_bands(scope, branch_id)

    def list_branch_operation(
        self,
        scope: AdminScope,
        requested_branch_id: uuid.UUID | None,
    ) -> list[AdminBranchOperationResponse]:
        """Como cada filial no escopo esta operando agora.

        Uma chamada monta a tela inteira: o dono com cinco lojas ve as cinco
        chaves de abrir/fechar lado a lado, o que e justamente a conferencia
        que nao existia enquanto o `is_open` era um so.

        O filtro da querystring passa por `resolve_branch_filter`: ele so
        RESTRINGE. Um gerente preso a uma filial que pedir outra recebe 404,
        e um que nao pedir nada recebe a dele — a tela nao precisa conhecer
        a regra de escopo.
        """
        branch_id = scope.resolve_branch_filter(requested_branch_id)
        branches = self.branch_repository.list_active_by_restaurant(scope.restaurant_id)
        if branch_id is not None:
            branches = [branch for branch in branches if branch.id == branch_id]
        return [self._branch_operation_response(branch) for branch in branches]

    def update_branch_settings(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: AdminBranchSettingsUpdate,
    ) -> AdminBranchOperationResponse:
        """As sobrescritas comerciais desta filial.

        `null` explicito no corpo apaga a sobrescrita e devolve o campo a
        herdar o padrao do restaurante. Campo ausente do corpo nao e tocado —
        e `exclude_unset` que separa os dois, e por isso ele nao pode virar
        `exclude_none` numa limpeza futura.
        """
        branch = self.branch_scope.get(scope=scope, branch_id=branch_id)
        changes = payload.model_dump(exclude_unset=True)

        self._ensure_delivery_time_range(
            changes.get("estimated_delivery_time_min", branch.estimated_delivery_time_min),
            changes.get("estimated_delivery_time_max", branch.estimated_delivery_time_max),
        )
        for field in MONEY_FIELDS:
            if changes.get(field) is not None:
                changes[field] = quantize_money(changes[field])

        for field, value in changes.items():
            setattr(branch, field, value)
        self._commit()
        return self._branch_operation_response(branch)

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
        return self._branch_response(self.branch_scope.get(scope=scope, branch_id=branch_id))

    def update_branch(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: AdminBranchUpdate,
    ) -> AdminBranchResponse:
        branch = self.branch_scope.get(scope=scope, branch_id=branch_id)
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
        self.branch_scope.get(scope=scope, branch_id=branch_id)
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
        self.branch_scope.get(scope=scope, branch_id=branch_id)
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

    def adjust_prep_time(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: BranchPrepTimeAdjustRequest,
    ) -> BusinessHourResponse:
        """Muda o tempo de preparo da faixa que esta valendo agora.

        Por que a faixa ATUAL e nao "a filial": `prep_time` mora em
        `branch_business_hours`, uma linha por faixa de horario, e o pedido
        le exatamente a faixa que contem o agora
        (`DeliveryEstimateService._resolve_prep_time` ->
        `BranchHoursService.find_current_period`). Escrever em qualquer
        outra linha mudaria um numero que o proximo pedido nao consulta;
        escrever em todas faria o aperto do almoco de hoje virar o prazo
        padrao do jantar de quinta.

        Fora do horario de funcionamento nao ha faixa vigente, e a resposta
        e 409 em vez de escolher uma por conta propria. Escolher "a mais
        parecida" foi exatamente o bug que `BranchHoursService` nasceu para
        corrigir: as 3h da manha, a estimativa cobrava o prazo do almoco.
        """
        self.branch_scope.get(scope=scope, branch_id=branch_id)
        period = self.branch_hours_service.find_current_period(branch_id)
        if period is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A filial está fora do horário de funcionamento agora; "
                    "não há faixa de horário para ajustar"
                ),
            )

        period.prep_time_min, period.prep_time_max = self._resolve_prep_time(period, payload)
        self._commit()
        return BusinessHourResponse.model_validate(period)

    def list_payment_methods(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
    ) -> list[AdminPaymentMethodResponse]:
        self.branch_scope.get(scope=scope, branch_id=branch_id)
        methods = self.repository.list_payment_methods(branch_id)
        return [AdminPaymentMethodResponse.model_validate(method) for method in methods]

    def create_payment_method(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: AdminPaymentMethodCreate,
    ) -> AdminPaymentMethodResponse:
        self.branch_scope.get(scope=scope, branch_id=branch_id)
        if "earns_cashback" in payload.model_fields_set:
            ensure_pode_definir_cashback(scope.admin_user)
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
                detail="Esta forma de pagamento já está cadastrada nesta filial",
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
        campos = payload.model_dump(exclude_unset=True)
        if "earns_cashback" in campos:
            ensure_pode_definir_cashback(scope.admin_user)
        for field, value in campos.items():
            setattr(method, field, value)
        self._commit()
        return AdminPaymentMethodResponse.model_validate(method)

    def delete_payment_method(self, scope: AdminScope, method_id: uuid.UUID) -> None:
        """Remove a forma de pagamento da filial.

        Aqui o DELETE e de verdade, ao contrario do cardapio: `orders`
        guarda a forma de pagamento como texto (`payment_method`), nao por
        FK, entao apagar esta linha nao mexe em pedido nenhum ja fechado.

        O que ele NAO preserva e `earns_cashback`. A rota e GERENCIA, o campo
        e do dono (`ensure_pode_definir_cashback`), e a linha recriada nasce
        no default `True`: apagar e recriar reverte um `False` que o dono
        escolheu, sem passar pela checagem que existe para isso. Residuo
        conhecido e ACEITO — fechar exigiria tirar o DELETE do gerente, caro
        por um caminho que tambem tira a forma da tela do cliente. Ver
        `docs/cashback.md`.
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

    def _get_payment_method(
        self,
        scope: AdminScope,
        method_id: uuid.UUID,
    ) -> BranchPaymentMethod:
        method = self.repository.get_payment_method(method_id, scope.restaurant_id)
        if method is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Forma de pagamento não encontrada",
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
                        f"Dia {weekday}: no máximo {MAX_PERIODS_PER_WEEKDAY} "
                        "faixas de horário"
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
                    detail=f"Dia {weekday}: não misture faixa fechada com faixa aberta",
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
                    detail=f"Dia {weekday}: as faixas de horário se sobrepõem",
                )

    @staticmethod
    def _resolve_prep_time(
        period: BranchBusinessHour,
        payload: BranchPrepTimeAdjustRequest,
    ) -> tuple[int, int]:
        """Os dois valores a gravar, ja no modo que o corpo pediu."""
        if payload.delta_minutes is None:
            return payload.prep_time_min, payload.prep_time_max

        if period.prep_time_min is None or period.prep_time_max is None:
            # Delta sem base nao tem resultado. Tratar o nulo como zero
            # transformaria um "+5" em "o preparo desta filial agora e de 5
            # minutos", que e uma promessa que a cozinha nao fez.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Esta faixa de horário não tem tempo de preparo cadastrado; "
                    "envie prep_time_min e prep_time_max antes de usar o ajuste rápido"
                ),
            )
        return (
            _clamp_prep_time(period.prep_time_min + payload.delta_minutes),
            _clamp_prep_time(period.prep_time_max + payload.delta_minutes),
        )

    @staticmethod
    def _ensure_delivery_time_range(minimum: int | None, maximum: int | None) -> None:
        if minimum is not None and maximum is not None and maximum < minimum:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="estimated_delivery_time_max não pode ser menor que o mínimo",
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
            free_delivery_enabled=settings_row.free_delivery_enabled,
            free_delivery_min_order_value=_optional_money(
                settings_row.free_delivery_min_order_value
            ),
            # O PADRAO da marca. A filial que gravou a propria mensagem nao
            # le esta — quem resolve as duas e `resolve_branch_operation`, e
            # quem a mostra ja resolvida e
            # `GET /admin/branches/{id}/print-settings`.
            receipt_footer_message=settings_row.receipt_footer_message,
        )

    def _branch_operation_response(self, branch: Branch) -> AdminBranchOperationResponse:
        """Uma filial: a chave que o lojista controla, e o que vai valer.

        `is_open_now` custa uma consulta de horario por filial. Vale o preco:
        sem ele o painel mostra "aberta" para uma loja que a agenda deixou
        fechada, e o lojista so descobre pela ausencia de pedido.
        """
        settings_row = self.repository.get_settings(branch.restaurant_id)
        operation = resolve_branch_operation(branch, settings_row)
        period = self.branch_hours_service.find_current_period(branch.id)
        return AdminBranchOperationResponse(
            branch_id=branch.id,
            branch_name=branch.name,
            is_open=operation.is_open,
            is_open_now=period is not None and operation.is_open,
            accepts_delivery=operation.accepts_delivery,
            accepts_pickup=operation.accepts_pickup,
            accepts_delivery_now=operation.accepts_delivery_now,
            delivery_paused_until=operation.delivery_paused_until,
            delivery_pause_reason=operation.delivery_pause_reason,
            overrides=AdminBranchOperationOverrides(
                min_order_value=_optional_money(branch.min_order_value),
                estimated_delivery_time_min=branch.estimated_delivery_time_min,
                estimated_delivery_time_max=branch.estimated_delivery_time_max,
                default_delivery_fee=_optional_money(branch.default_delivery_fee),
                service_fee_enabled=branch.service_fee_enabled,
                service_fee_amount=_optional_money(branch.service_fee_amount),
                free_delivery_enabled=branch.free_delivery_enabled,
                free_delivery_min_order_value=_optional_money(
                    branch.free_delivery_min_order_value
                ),
            ),
            effective=self._effective_response(operation),
        )

    @staticmethod
    def _effective_response(operation: BranchOperation) -> AdminBranchOperationEffective:
        return AdminBranchOperationEffective(
            min_order_value=money_to_float(operation.min_order_value),
            estimated_delivery_time_min=operation.estimated_delivery_time_min,
            estimated_delivery_time_max=operation.estimated_delivery_time_max,
            # Nulo aqui e informacao: significa "esta filial nao tem taxa de
            # contingencia". `money_to_float` transformaria isso em 0.00, que
            # o painel exibiria como um valor configurado.
            default_delivery_fee=_optional_money(operation.default_delivery_fee),
            service_fee_enabled=operation.service_fee_enabled,
            service_fee_amount=money_to_float(operation.service_fee_amount),
            free_delivery_enabled=operation.free_delivery_enabled,
            free_delivery_min_order_value=_optional_money(
                operation.free_delivery_min_order_value
            ),
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


def _clamp_prep_time(value: int) -> int:
    """Mantem o prazo dentro do que o cadastro aceita (0 a 600 minutos).

    Aparar e nao recusar: quem apertou "-10" tres vezes seguidas em uma
    faixa de 20 minutos quer o menor prazo possivel, e nao um 422 no
    terceiro clique. Como minimo e maximo se deslocam pelo mesmo delta e o
    corte e monotonico, `min <= max` continua valendo depois de aparar.
    """
    return max(0, min(value, MAX_PREP_TIME_MINUTES))


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _closing_minutes(period: BusinessHourInput) -> int:
    """Fim da faixa em minutos, esticado quando ela vira a noite."""
    opens = _minutes(period.opens_at)
    closes = _minutes(period.closes_at)
    return closes if closes > opens else closes + MINUTES_IN_A_DAY
