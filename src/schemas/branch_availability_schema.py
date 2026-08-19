"""Corpo e resposta de `POST /restaurants/{slug}/branches/availability`.

A tela de escolha de filial. Duas perguntas por filial, e a segunda so existe
quando o cliente ja informou onde mora:

    esta ABERTA agora?          sempre respondida
    ENTREGA no meu endereco?    so com endereco no corpo

O que a resposta NAO faz e inventar: sem endereco, os campos de entrega vem
todos nulos em vez de virem com um numero aproximado. Distancia em linha reta
serve para DESCARTAR filial fora do raio (ver `src/utils/geo.py`), nunca para
mostrar preco — taxa aproximada na lista vira reclamacao no checkout, quando
o valor exato aparecer diferente.
"""

from datetime import time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.schemas.delivery_schema import DeliveryAddressInput
from src.schemas.restaurant_schema import BranchAddressResponse


class BranchAvailabilityRequest(BaseModel):
    """Endereco OPCIONAL, e no maximo um dos dois jeitos de informa-lo.

    Diferente de `DeliveryEstimateRequest`, que exige exatamente um: ali o
    endereco e o proprio objeto da pergunta, aqui ele e um refinamento. Sem
    ele a rota ainda responde a lista de filiais com aberta/fechada, que e o
    que a tela precisa antes de o cliente ter digitado qualquer coisa.
    """

    model_config = ConfigDict(extra="forbid")

    address_id: UUID | None = None
    address: DeliveryAddressInput | None = None

    @field_validator("address_id", mode="before")
    @classmethod
    def normalize_optional_uuid(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_address_source(self):
        if self.address_id is not None and self.address is not None:
            raise ValueError("Informe no maximo um entre address_id e address")
        return self


# Por que a filial nao esta atendendo. Nulo quando esta.
#
# Os dois casos sao diferentes para quem le a tela: `outside_business_hours`
# passa sozinho quando o relogio virar, `branch_paused` so passa quando
# alguem no balcao apertar o botao de volta. Sem esta distincao a tela nao
# tem como escolher entre "abre as 18:00" e "fechada no momento".
BranchClosedReason = Literal["outside_business_hours", "branch_paused"]


class BranchOpenPeriodResponse(BaseModel):
    """A faixa de horario da AGENDA que contem o momento atual.

    Vai junto para a tela poder escrever "aberta ate 23:00" sem pedir os
    horarios de novo em outra rota. Faixa que vira a noite (18:00-02:00) sai
    com `closes_at` menor que `opens_at`, e isso e o dado correto: ela
    pertence ao dia em que COMECA.

    Desde que o "fechar agora" passou a ser por filial, este campo pode vir
    PREENCHIDO com `is_open_now = false`: a agenda diz aberta e o balcao
    pausou. Nao e contradicao, sao duas coisas — a agenda e cadastro, a pausa
    e o dia de hoje. Quem decide se a filial atende e `is_open_now`, sempre.
    """

    weekday: int
    opens_at: time
    closes_at: time


class BranchDeliveryResponse(BaseModel):
    """A resposta de entrega para UMA filial. So existe com endereco no corpo.

    `delivers_to_address` e a unica pergunta que a tela precisa responder para
    habilitar ou desabilitar a filial. `reason` diz por que nao, e vem do
    mesmo vocabulario de `POST /delivery/estimate` — os codigos sao os
    mesmos, de proposito.

    `distance_km` e `delivery_fee` podem vir preenchidos MESMO com
    `delivers_to_address = false`: e o caso de "fora da area", em que a rota
    foi calculada e reprovada pelo raio. Vem nulos quando nem chegou a
    calcular (filial fechada, ou descartada pela linha reta).
    """

    delivers_to_address: bool
    reason: str | None = None
    message: str | None = None
    distance_km: float | None = None
    travel_time_min: int | None = None
    delivery_fee: float | None = None
    eta_min: int | None = None
    eta_max: int | None = None


class BranchAvailabilityItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    display_name: str | None = None
    slug: str
    address: BranchAddressResponse
    phone: str | None = None
    whatsapp: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_main: bool = False

    # A UNICA resposta para "esta filial esta atendendo agora?". Combina a
    # agenda da semana com a pausa manual da filial: a loja precisa estar
    # dentro de uma faixa E nao estar pausada.
    is_open_now: bool
    closed_reason: BranchClosedReason | None = None
    current_period: BranchOpenPeriodResponse | None = None

    # Nulo quando o corpo nao trouxe endereco. NAO e "nao entrega": e "nao
    # perguntei". A tela precisa distinguir os dois para nao desabilitar a
    # filial antes de o cliente informar onde mora.
    delivery: BranchDeliveryResponse | None = None


class BranchAvailabilityResponse(BaseModel):
    restaurant_slug: str

    # Repete o que a requisicao pediu, para a tela nao precisar guardar
    # estado: `true` quando o corpo trouxe endereco e os campos `delivery`
    # foram preenchidos.
    address_provided: bool

    # A filial que a plataforma usa quando o cliente nao escolhe nenhuma — a
    # MESMA que `POST /delivery/estimate` sem `branch_id` e que
    # `GET /restaurants/{slug}/info` sem `branch_id` usam. Vem nula quando o
    # restaurante nao tem filial ativa.
    default_branch_id: UUID | None = None

    branches: list[BranchAvailabilityItem]
