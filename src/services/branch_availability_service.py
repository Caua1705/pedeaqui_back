"""A tela de escolha de filial: quais existem, quais estao abertas, quais
entregam no endereco do cliente.

## Por que esta rota existe

Antes dela o app nao tinha como montar essa tela. `/restaurants/{slug}/menu`
devolve as filiais sem `is_open` e sem horario, e `/restaurants/{slug}/info`
responde por UMA filial de cada vez, entregando as faixas cruas para o
aplicativo refazer a conta de intervalo — inclusive a faixa que vira a noite,
que `BranchHoursService._open_periods` ja resolve deste lado. Duplicar essa
regra no cliente e exatamente o defeito que a armadilha 10 registra: quando as
duas implementacoes discordam, a loja aparece aberta numa tela e fechada na
outra, e nenhuma das duas erra alto.

Aqui **o backend responde `is_open_now` como booleano.** O app nao calcula
horario.

E `is_open_now` combina DUAS coisas, desde que o "fechar agora" virou da
filial: a agenda da semana e a pausa manual daquela loja. Elas sao
independentes — a agenda diz que hoje abre as 18h, a pausa diz que hoje o
gas acabou — e nenhuma das duas sozinha responde "posso pedir?". `closed_reason`
diz qual das duas fechou, porque a tela escreve coisas diferentes para cada
uma: uma passa sozinha quando o relogio virar, a outra so quando alguem no
balcao apertar o botao.

## O custo do Google, que e o que desenha este arquivo

Cada filial exigiria uma rota paga (`computeRoutes`), e uma tela que abre
sozinha nao pode multiplicar a fatura pelo numero de lojas. Tres decisoes
cortam isso, nesta ordem:

1. **O endereco e geocodificado UMA vez.** `DeliveryEstimateService.estimate`
   geocodifica quando o endereco chega sem coordenada — chamado N vezes, ele
   geocodificaria N vezes o MESMO endereco. Este service resolve a coordenada
   antes do laco e injeta `latitude`/`longitude` no corpo que entrega a cada
   filial, o que faz aquele caminho nunca ser tomado.

2. **Linha reta elimina antes de o Google ser chamado.** Distancia geodesica
   e sempre menor ou igual a dirigida, entao filial cujo haversine ja passa do
   `delivery_max_distance_km` esta provadamente fora e nao precisa de rota.
   Nao ha falso negativo possivel — ver `src/utils/geo.py`. Ela so ELIMINA:
   nenhum numero aproximado chega ao cliente.

3. **O cache que ja existe.** As filiais que sobram passam por
   `DeliveryEstimateService.estimate`, que consulta `DeliveryEstimateCache`
   antes de ligar para o Google. Com `REDIS_URL` configurada o cache e
   compartilhado entre workers; sem ela cada processo tem o seu (armadilha 20)
   e a economia cai, mas o comportamento nao muda.

O que NAO foi feito, e por que: `computeRouteMatrix` resolveria N filiais numa
requisicao so, mas o Google cobra **por elemento** — ele corta latencia e
cota, nao a fatura. Com o filtro geometrico na frente, o numero de elementos
ja e pequeno. Ele e o proximo passo quando uma rede tiver muitas filiais
dentro do mesmo raio.

## O que este service NAO faz

Nao grava estimativa e nao emite `estimate_token`. `estimate_and_store` cria
uma linha em `delivery_estimates` por chamada, e fazer isso para N filiais a
cada abertura de tela encheria a tabela com N-1 estimativas que ninguem vai
usar. O app continua chamando `POST /delivery/estimate` para a filial
escolhida — e essa chamada cai no cache que ESTA rota acabou de preencher,
entao ela nao custa uma rota nova ao Google.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from src.integrations.google_maps_routes_client import (
    GoogleMapsLocationNotFoundError,
    GoogleMapsUnavailableError,
)
from src.models.branch_model import Branch
from src.models.customer_model import Customer
from src.repositories.branch_repository import BranchRepository
from src.repositories.menu_repository import MenuRepository
from src.schemas.branch_availability_schema import (
    BranchAvailabilityItem,
    BranchAvailabilityRequest,
    BranchAvailabilityResponse,
    BranchDeliveryResponse,
    BranchOpenPeriodResponse,
)
from src.schemas.delivery_schema import DeliveryAddressInput, DeliveryEstimateRequest
from src.services.branch_hours_service import BRANCH_TIMEZONE, BranchHoursService
from src.services.branch_operation import (
    resolve_branch_operation,
    resolver_atendimento,
)
from src.services.delivery_estimate_service import DeliveryEstimateService
from src.services.restaurant_service import RestaurantService
from src.utils.geo import haversine_km


logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class DestinoDoCliente:
    """Onde o cliente esta, para ESTA requisicao — ou por que nao deu para saber.

    Tres estados, e a tela trata os tres diferente:

    - `address` preenchido: deu para localizar; cada filial ganha distancia,
      taxa e veredito;
    - `falha` preenchido: o cliente informou endereco e o Google nao
      respondeu. A lista de filiais SAI MESMO ASSIM, com aberta/fechada, que
      nao depende de Google nenhum;
    - os dois nulos: nao veio endereco na pergunta.

    Existe como objeto, e nao como tupla de dois itens em que o segundo as
    vezes e nulo, porque a diferenca entre o segundo e o terceiro estado e
    invisivel numa tupla — e e justamente ela que decide se a tela pede o
    endereco de novo ou nao.
    """

    address: DeliveryAddressInput | None = None
    falha: BranchDeliveryResponse | None = None

    @property
    def foi_informado(self) -> bool:
        return self.address is not None or self.falha is not None


class BranchAvailabilityService:
    def __init__(
        self,
        db: Session,
        delivery_service: DeliveryEstimateService | None = None,
    ) -> None:
        self.db = db
        self.restaurant_service = RestaurantService(db)
        self.branch_repository = BranchRepository(db)
        self.menu_repository = MenuRepository(db)
        self.branch_hours_service = BranchHoursService(db)
        # Injetavel para o teste substituir o Google sem tocar em rede.
        self.delivery_service = delivery_service or DeliveryEstimateService(db)
        # O relogio, injetavel — ver o comentario em `DeliveryEstimateService`.
        # Aqui ele vale duas vezes: a lista compara filiais entre si, e um
        # teste que dependesse do minuto compararia duas coisas medidas em
        # momentos diferentes.
        self.clock = lambda: datetime.now(BRANCH_TIMEZONE)

    def list_availability(
        self,
        restaurant_slug: str,
        payload: BranchAvailabilityRequest,
        current_customer: Customer | None,
    ) -> BranchAvailabilityResponse:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        branches = self.branch_repository.list_active_by_restaurant(restaurant.id)
        default_branch = self.branch_repository.get_default_branch(restaurant.id)
        # Uma consulta para a lista inteira. Do que esta rota usa, so
        # `is_open` importa — e ele nao herda nada. Mesmo assim a leitura
        # passa por `resolve_branch_operation`, e nao por `branch.is_open`
        # direto: um segundo jeito de ler a mesma regra e como a armadilha 10
        # comeca, e o custo aqui e um SELECT ao lado de N chamadas ao Google.
        restaurant_settings = self.menu_repository.get_settings(restaurant.id)

        destino = self._resolve_destination_once(payload, current_customer)
        # UM instante para todas as filiais. Lendo o relogio dentro do laco,
        # duas filiais da mesma resposta poderiam cair em lados diferentes da
        # virada de horario — a lista sairia contando uma historia que nunca
        # foi verdade num unico momento.
        now = self.clock()

        itens = [
            self._branch_item(
                branch, restaurant.slug, restaurant_settings, destino, current_customer, now
            )
            for branch in branches
        ]
        logger.info(
            "[Filiais] restaurant_slug=%s filiais=%s com_endereco=%s",
            restaurant_slug,
            len(itens),
            str(destino.foi_informado).lower(),
        )
        return BranchAvailabilityResponse(
            restaurant_slug=restaurant.slug,
            address_provided=destino.foi_informado,
            default_branch_id=default_branch.id if default_branch else None,
            branches=itens,
        )

    # -----------------------------------------------------------------
    # O endereco, uma vez so
    # -----------------------------------------------------------------

    def _resolve_destination_once(
        self,
        payload: BranchAvailabilityRequest,
        current_customer: Customer | None,
    ) -> DestinoDoCliente:
        """O endereco do cliente JA com coordenada, uma vez para todas as filiais.

        E aqui que a economia de geocode acontece: o resultado e reaproveitado
        por todas as filiais. Sem isso, cada filial refaria o mesmo geocode do
        mesmo endereco.

        E e aqui que a falha do Google e absorvida. Endereco sem coordenada
        precisa de geocode, e geocode e chamada de rede que cai — deixar a
        excecao subir derrubaria a tela INTEIRA com 500, inclusive a parte que
        nao depende de Google nenhum (quais filiais existem e quais estao
        abertas). O erro vira um bloco de entrega com motivo, igual para todas
        as filiais, e a lista sai.
        """
        if payload.address_id is None and payload.address is None:
            return DestinoDoCliente()

        # Reaproveita a resolucao do proprio servico de entrega — inclusive a
        # busca do endereco salvo por `address_id` e os erros dela (401 sem
        # login, 404 de endereco inexistente). Esses continuam subindo: sao
        # erro do pedido, nao indisponibilidade de terceiro.
        pedido = DeliveryEstimateRequest(
            address_id=payload.address_id,
            address=payload.address,
        )
        try:
            endereco = self.delivery_service.resolve_destination_address(
                pedido, current_customer
            )
        except GoogleMapsLocationNotFoundError:
            logger.info("[Filiais] geocode_falhou motivo=address_not_found")
            return DestinoDoCliente(falha=BranchDeliveryResponse(
                delivers_to_address=False,
                reason="address_not_found",
                message="Nao foi possivel localizar este endereco.",
            ))
        except GoogleMapsUnavailableError:
            logger.warning("[Filiais] geocode_falhou motivo=route_unavailable")
            return DestinoDoCliente(falha=BranchDeliveryResponse(
                delivers_to_address=False,
                reason="route_unavailable",
                message="Nao foi possivel calcular a entrega agora.",
            ))

        return DestinoDoCliente(address=endereco)

    # -----------------------------------------------------------------
    # Uma filial
    # -----------------------------------------------------------------

    def _branch_item(
        self,
        branch: Branch,
        restaurant_slug: str,
        restaurant_settings,
        destino: DestinoDoCliente,
        current_customer: Customer | None,
        now: datetime,
    ) -> BranchAvailabilityItem:
        period = self.branch_hours_service.find_current_period(branch.id, now)
        operation = resolve_branch_operation(branch, restaurant_settings)
        is_open_now, closed_reason = resolver_atendimento(period, operation)

        return BranchAvailabilityItem(
            id=branch.id,
            name=branch.name,
            display_name=branch.display_name,
            slug=branch.slug,
            address=RestaurantService._build_address(branch),
            phone=branch.phone,
            whatsapp=branch.whatsapp,
            latitude=float(branch.latitude) if branch.latitude is not None else None,
            longitude=float(branch.longitude) if branch.longitude is not None else None,
            is_main=bool(branch.is_main),
            is_open_now=is_open_now,
            closed_reason=closed_reason,
            current_period=self._period_response(period),
            delivery=self._delivery(branch, restaurant_slug, destino, current_customer),
        )

    @staticmethod
    def _period_response(period) -> BranchOpenPeriodResponse | None:
        if period is None:
            return None
        if period.opens_at is None or period.closes_at is None:
            return None
        return BranchOpenPeriodResponse(
            weekday=period.weekday,
            opens_at=period.opens_at,
            closes_at=period.closes_at,
        )

    def _delivery(
        self,
        branch: Branch,
        restaurant_slug: str,
        destino: DestinoDoCliente,
        current_customer: Customer | None,
    ) -> BranchDeliveryResponse | None:
        """A entrega desta filial. `None` quando nao houve endereco na pergunta."""
        if not destino.foi_informado:
            return None
        if destino.falha is not None:
            return destino.falha

        destination = destino.address
        if self._provably_out_of_range(branch, destination):
            # Nenhuma chamada ao Google: a linha reta ja passou do raio, e o
            # caminho dirigido so pode ser maior. Mesmo `reason` da rota de
            # estimativa, para o front ter um vocabulario so.
            return BranchDeliveryResponse(
                delivers_to_address=False,
                reason="outside_delivery_area",
                message="Endereco fora da area de entrega.",
            )

        # `estimate` e a MESMA funcao que o `POST /delivery/estimate` chama:
        # mesmo cache, mesmas regras de taxa, mesmos `reason`. Nao ha um
        # segundo calculo de taxa neste arquivo, de proposito — duas
        # implementacoes da regra de preco divergiriam, e a que mentiria e a
        # da tela que o cliente ve primeiro.
        #
        # `address` ja carrega latitude e longitude, entao esta chamada nao
        # geocodifica: o unico geocode da requisicao ficou em
        # `_resolve_destination_once`.
        resultado = self.delivery_service.estimate(
            restaurant_slug,
            DeliveryEstimateRequest(branch_id=branch.id, address=destination),
            current_customer,
        )
        return BranchDeliveryResponse(
            delivers_to_address=resultado.serviceable,
            reason=resultado.reason,
            message=resultado.message,
            distance_km=resultado.distance_km,
            travel_time_min=resultado.travel_time_min,
            delivery_fee=resultado.delivery_fee,
            eta_min=resultado.eta_min,
            eta_max=resultado.eta_max,
        )

    @staticmethod
    def _provably_out_of_range(branch: Branch, destination: DeliveryAddressInput) -> bool:
        """A filial esta fora do raio SEM sombra de duvida?

        Tres coisas precisam existir para a pergunta ter resposta: o raio
        configurado e as duas coordenadas. Faltando qualquer uma, devolve
        False — "nao sei" tem que virar chamada ao Google, nunca recusa.
        """
        raio = branch.delivery_max_distance_km
        if raio is None:
            return False
        if branch.latitude is None or branch.longitude is None:
            return False
        if destination.latitude is None or destination.longitude is None:
            return False

        linha_reta = haversine_km(
            float(branch.latitude),
            float(branch.longitude),
            float(destination.latitude),
            float(destination.longitude),
        )
        return linha_reta > float(raio)
