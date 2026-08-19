"""Como UMA filial opera: se esta aberta, o que aceita e quanto cobra.

Existe para haver **um** lugar que combine o que esta na filial com o que
esta no restaurante. Antes da revisao `20260818_0025` nao havia o que
combinar — tudo vinha de `restaurant_settings` e valia para a rede inteira.
Agora ha dois regimes, e misturar os dois em cada chamador seria a receita da
armadilha 10: duas implementacoes da mesma regra, discordando sem erro.

## Os dois regimes

**Estado do dia** — `is_open`, `accepts_delivery`, `accepts_pickup`. Moram so
na filial, `NOT NULL`, e **nao herdam nada**. Sao o que alguem no balcao
aperta durante o expediente.

**Termo comercial** — `min_order_value`, `service_fee_enabled`,
`service_fee_amount`, `estimated_delivery_time_min`/`_max`,
`default_delivery_fee`. A filial tem a coluna nullable e **NULL significa
"herda do restaurante"**. Sao preco negociado da marca: quem abre a quinta
loja nao redigita a taxa de servico cinco vezes, e quem precisa divergir
escreve o proprio valor.

## O que este modulo NAO faz

Nao le horario de funcionamento. `is_open` e a pausa manual; a agenda da
semana e de `BranchHoursService`, e as duas se combinam em quem responde
"aberta agora" — a filial precisa estar dentro de uma faixa **e** nao estar
pausada. Juntar as duas aqui exigiria banco, e este modulo e uma funcao pura
de proposito: ele e chamado dentro do laco da tela de escolha de filial.
"""

from dataclasses import dataclass
from decimal import Decimal

from src.models.branch_model import Branch
from src.models.restaurant_setting_model import RestaurantSetting
from src.utils.money import quantize_money, to_decimal


@dataclass(frozen=True)
class BranchOperation:
    """O valor EFETIVO de cada campo, ja resolvido. E o que o pedido usa.

    Frozen porque e resultado de leitura, nao estado: quem quiser mudar a
    operacao escreve na filial e resolve de novo.
    """

    is_open: bool
    accepts_delivery: bool
    accepts_pickup: bool
    min_order_value: Decimal
    service_fee_enabled: bool
    service_fee_amount: Decimal
    estimated_delivery_time_min: int | None
    estimated_delivery_time_max: int | None
    default_delivery_fee: Decimal | None


@dataclass(frozen=True)
class _PadraoDoRestaurante:
    """O que a filial herda quando a propria coluna esta nula."""

    min_order_value: Decimal | None = None
    service_fee_enabled: bool | None = None
    service_fee_amount: Decimal | None = None
    estimated_delivery_time_min: int | None = None
    estimated_delivery_time_max: int | None = None
    default_delivery_fee: Decimal | None = None


# Restaurante sem linha em `restaurant_settings` nao tem o que herdar. Nao e
# erro: a linha sempre foi opcional no schema, e o restaurante que nunca
# passou pelo painel continua vendendo com os defaults da plataforma.
SEM_PADRAO = _PadraoDoRestaurante()


def resolve_branch_operation(
    branch: Branch,
    restaurant_settings: RestaurantSetting | None,
) -> BranchOperation:
    """Junta filial e padrao do restaurante em um valor por campo.

    Chame isto em vez de ler `branch.min_order_value` direto: a coluna crua
    responde "o que esta sobrescrito", que quase nunca e a pergunta.
    """
    padrao = _padrao_do_restaurante(restaurant_settings)
    return BranchOperation(
        is_open=bool(branch.is_open),
        accepts_delivery=bool(branch.accepts_delivery),
        accepts_pickup=bool(branch.accepts_pickup),
        min_order_value=quantize_money(
            to_decimal(_ou_herdado(branch.min_order_value, padrao.min_order_value))
        ),
        # `is not False` e nao `bool(...)`: os dois nulos significam "ninguem
        # configurou", e a taxa de servico nasce ligada — do mesmo jeito que
        # o default da coluna sempre disse. Sem valor cadastrado ela sai zero
        # de qualquer forma, entao ligada-e-zerada nao cobra nada de ninguem.
        service_fee_enabled=_ou_herdado(
            branch.service_fee_enabled, padrao.service_fee_enabled
        ) is not False,
        service_fee_amount=quantize_money(
            to_decimal(_ou_herdado(branch.service_fee_amount, padrao.service_fee_amount))
        ),
        estimated_delivery_time_min=_ou_herdado(
            branch.estimated_delivery_time_min, padrao.estimated_delivery_time_min
        ),
        estimated_delivery_time_max=_ou_herdado(
            branch.estimated_delivery_time_max, padrao.estimated_delivery_time_max
        ),
        # Sem quantize: aqui o nulo tem significado proprio ("nao ha taxa de
        # contingencia configurada") e `to_decimal(None)` o transformaria em
        # zero, que DeliveryEstimateService trata como desligado — mesmo
        # resultado por acidente, e um acidente a menos de que depender.
        default_delivery_fee=_ou_herdado(
            branch.default_delivery_fee, padrao.default_delivery_fee
        ),
    )


def _padrao_do_restaurante(
    restaurant_settings: RestaurantSetting | None,
) -> _PadraoDoRestaurante:
    if restaurant_settings is None:
        return SEM_PADRAO
    return _PadraoDoRestaurante(
        min_order_value=restaurant_settings.min_order_value,
        service_fee_enabled=restaurant_settings.service_fee_enabled,
        service_fee_amount=restaurant_settings.service_fee_amount,
        estimated_delivery_time_min=restaurant_settings.estimated_delivery_time_min,
        estimated_delivery_time_max=restaurant_settings.estimated_delivery_time_max,
        default_delivery_fee=restaurant_settings.default_delivery_fee,
    )


def _ou_herdado(valor_da_filial, valor_do_restaurante):
    """O valor da filial; o do restaurante quando a filial nao sobrescreveu.

    A distincao e entre NULL e valor: `service_fee_enabled = False` na filial
    e uma escolha ("esta loja nao cobra taxa"), e nao pode cair no `true` do
    restaurante. Por isso `is not None`, e nunca um teste de veracidade.
    """
    if valor_da_filial is not None:
        return valor_da_filial
    return valor_do_restaurante
