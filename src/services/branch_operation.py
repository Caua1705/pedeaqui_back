"""Como UMA filial opera: se esta aberta, o que aceita e quanto cobra.

Existe para haver **um** lugar que combine o que esta na filial com o que
esta no restaurante. Antes da revisao `20260818_0025` nao havia o que
combinar — tudo vinha de `restaurant_settings` e valia para a rede inteira.
Agora ha dois regimes, e misturar os dois em cada chamador seria a receita da
armadilha 10: duas implementacoes da mesma regra, discordando sem erro.

## Os dois regimes

**Estado do dia** — `is_open`, `accepts_delivery`, `accepts_pickup` e a pausa
temporaria da entrega (`delivery_paused_until`). Moram so na filial e **nao
herdam nada**. Sao o que alguem no balcao aperta durante o expediente.

**Termo comercial** — `min_order_value`, `service_fee_enabled`,
`service_fee_amount`, `estimated_delivery_time_min`/`_max`,
`default_delivery_fee`, `free_delivery_enabled`/`free_delivery_min_order_value`.
A filial tem a coluna nullable e **NULL significa "herda do restaurante"**.
Sao preco negociado da marca: quem abre a quinta loja nao redigita a taxa de
servico cinco vezes, e quem precisa divergir escreve o proprio valor.

## Duas chaves de entrega, e elas nao sao a mesma

`accepts_delivery` e a chave ESTRUTURAL: "este quiosque nao entrega, ponto".
`delivery_paused_until` e o dia de chuva. A segunda tem prazo e se desfaz
sozinha; a primeira espera alguem. Quem decide pedido e
`accepts_delivery_now`, que e as duas juntas — ler `accepts_delivery`
sozinho aceita pedido de uma filial pausada.

## A mensagem do rodape da comanda segue esse regime, e mesmo assim fica FORA
## do `BranchOperation`

`branches.receipt_footer_message` (revisao `20260821_0029`) e herdada pela
mesma regra e por isso e resolvida aqui, no `_ou_herdado` que ja existe — a
alternativa era uma segunda implementacao da heranca dentro do servico de
impressao, que e como duas respostas diferentes para a mesma pergunta
nascem.

Mas ela tem funcao propria (`resolve_receipt_footer`) em vez de ser mais um
campo do `BranchOperation`, e a razao esta na primeira linha daquela classe:
ela e **o que o PEDIDO usa**. O rodape nao entra em nenhum calculo de
pedido — ele so existe na hora de desenhar a bobina. Como campo, obrigaria
todo chamador de `resolve_branch_operation` (o checkout, a tela de escolha
de filial, o painel de operacao) a carregar uma configuracao de impressora
que nenhum deles le.

## O que este modulo NAO faz

Nao LE horario de funcionamento — mas combina o que outro leu. `is_open` e a
pausa manual; a agenda da semana e de `BranchHoursService`, que precisa de
banco. A juncao das duas ("aberta agora" e a filial dentro de uma faixa **e**
nao pausada) mora em `resolver_atendimento`, que recebe a faixa ja lida em vez
de ir busca-la.

Essa e a linha que o modulo nao cruza: ele nao abre consulta. E o que permite
chama-lo dentro do laco da tela de escolha de filial sem uma consulta por
volta, e o motivo de `resolver_atendimento` pedir um `period` pronto em vez de
um `branch_id`.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_model import Branch
from src.models.restaurant_setting_model import RestaurantSetting
from src.schemas.branch_availability_schema import BranchClosedReason
from src.utils.money import quantize_money, to_decimal
from src.utils.security import utcnow


@dataclass(frozen=True)
class BranchOperation:
    """O valor EFETIVO de cada campo, ja resolvido. E o que o pedido usa.

    Frozen porque e resultado de leitura, nao estado: quem quiser mudar a
    operacao escreve na filial e resolve de novo.
    """

    is_open: bool
    accepts_delivery: bool
    accepts_pickup: bool
    # `accepts_delivery` diz se esta filial ENTREGA; este diz se ela esta
    # entregando AGORA. A pausa temporaria (chuva, entregador que sumiu) e a
    # unica diferenca entre os dois, e quem decide pedido e este.
    accepts_delivery_now: bool
    delivery_paused_until: datetime | None
    delivery_pause_reason: str | None
    min_order_value: Decimal
    service_fee_enabled: bool
    service_fee_amount: Decimal
    estimated_delivery_time_min: int | None
    estimated_delivery_time_max: int | None
    default_delivery_fee: Decimal | None
    free_delivery_enabled: bool
    # Nulo mesmo com `free_delivery_enabled` ligado significa "ligado sem
    # valor cadastrado", que nao da entrega de graca a ninguem: sem numero
    # nao ha comparacao a fazer. Quem aplica a regra e `OrderService`.
    free_delivery_min_order_value: Decimal | None


@dataclass(frozen=True)
class _PadraoDoRestaurante:
    """O que a filial herda quando a propria coluna esta nula."""

    min_order_value: Decimal | None = None
    service_fee_enabled: bool | None = None
    service_fee_amount: Decimal | None = None
    estimated_delivery_time_min: int | None = None
    estimated_delivery_time_max: int | None = None
    default_delivery_fee: Decimal | None = None
    free_delivery_enabled: bool | None = None
    free_delivery_min_order_value: Decimal | None = None


# Restaurante sem linha em `restaurant_settings` nao tem o que herdar. Nao e
# erro: a linha sempre foi opcional no schema, e o restaurante que nunca
# passou pelo painel continua vendendo com os defaults da plataforma.
SEM_PADRAO = _PadraoDoRestaurante()


def resolve_branch_operation(
    branch: Branch,
    restaurant_settings: RestaurantSetting | None,
    agora: datetime | None = None,
) -> BranchOperation:
    """Junta filial e padrao do restaurante em um valor por campo.

    Chame isto em vez de ler `branch.min_order_value` direto: a coluna crua
    responde "o que esta sobrescrito", que quase nunca e a pergunta.

    `agora` existe para o teste conseguir olhar a pausa da entrega de um
    instante escolhido. Em producao ninguem passa: o default e o relogio, e
    passar o horario errado aqui reabriria a entrega de uma filial pausada.
    """
    padrao = _padrao_do_restaurante(restaurant_settings)
    agora = agora or utcnow()
    return BranchOperation(
        is_open=bool(branch.is_open),
        accepts_delivery=bool(branch.accepts_delivery),
        accepts_pickup=bool(branch.accepts_pickup),
        accepts_delivery_now=(
            bool(branch.accepts_delivery) and not _delivery_esta_pausada(branch, agora)
        ),
        # Devolvidos crus para a resposta poder dizer ATE QUANDO e POR QUE —
        # "sem entrega agora" sem prazo faz o cliente fechar o app, e com
        # prazo ele volta. Ficam preenchidos mesmo depois de a pausa vencer;
        # quem responde "esta pausada?" e `accepts_delivery_now`.
        delivery_paused_until=branch.delivery_paused_until,
        delivery_pause_reason=branch.delivery_pause_reason,
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
        # `is True`, e nao `is not False` como a taxa de servico. A assimetria
        # e deliberada: taxa de servico ligada sem valor cobra zero, que nao
        # machuca ninguem; frete gratis ligado por omissao DA A ENTREGA DE
        # GRACA em nome de um lojista que nao pediu. E a armadilha 11 de novo
        # — na duvida, o lado que nao gasta o dinheiro dos outros.
        free_delivery_enabled=_ou_herdado(
            branch.free_delivery_enabled, padrao.free_delivery_enabled
        ) is True,
        free_delivery_min_order_value=_ou_herdado(
            branch.free_delivery_min_order_value, padrao.free_delivery_min_order_value
        ),
    )


def _delivery_esta_pausada(branch: Branch, agora: datetime) -> bool:
    """A pausa temporaria da entrega esta valendo neste instante?

    Comparacao com o RELOGIO, e nao um booleano no banco, e e isso que faz a
    pausa se desfazer sozinha: ninguem precisa lembrar de reabrir a entrega
    as 21h de um dia de chuva. Um `delivery_paused = true` no banco pediria
    esse alguem, e o dia em que a pausa e usada e o dia em que ninguem lembra.

    Coluna ingenua (linha gravada por script, ou correcao a mao) e tratada
    como UTC em vez de levantar: comparar ingenuo com consciente levanta
    TypeError, que aqui viraria 500 no meio do checkout de todo cliente
    daquela filial. Mesmo arranjo de `print_agent_service._seconds_since`.
    """
    pausada_ate = branch.delivery_paused_until
    if pausada_ate is None:
        return False
    if pausada_ate.tzinfo is None:
        pausada_ate = pausada_ate.replace(tzinfo=agora.tzinfo)
    return pausada_ate > agora


def resolver_atendimento(
    period: BranchBusinessHour | None,
    operation: BranchOperation,
) -> tuple[bool, BranchClosedReason | None]:
    """"Esta filial esta atendendo agora?", em UM lugar so.

    Vive aqui, e nao no servico que a usa, porque agora ha DOIS chamadores
    com consequencias diferentes: a tela de escolha de filial
    (`BranchAvailabilityService._branch_item`), que habilita ou desabilita o
    botao, e o prompt do Rapi (`ChatService._build_branch_state`), que decide
    se o assistente recomenda picanha com preco a uma pessoa que nao tem como
    pedir.

    Duas copias da linha `period is not None and operation.is_open` seriam a
    armadilha 10 na forma mais cara dela: elas nao divergem hoje, divergem no
    dia em que alguem acrescentar uma terceira condicao a uma so. O sintoma
    seria a tela dizer "fechada" enquanto o Rapi separa a picanha — sem erro,
    sem log, e descoberto pelo cliente.

    Continua PURA, e por isso recebe o `period` pronto em vez de o `branch_id`:
    quem le a agenda e `BranchHoursService`, que precisa de banco. O modulo
    inteiro e funcao pura de proposito (ver o cabecalho), e e o que permite
    chamar isto dentro do laco da tela de filiais sem uma consulta por volta.

    A ORDEM DO `closed_reason` E DELIBERADA e veio do `_closed_reason` que
    esta funcao substitui: a agenda vem primeiro quando as duas coisas valem
    ao mesmo tempo (fora do horario E pausada), porque `current_period` ja sai
    nulo nesse caso — responder "pausada" faria a tela dizer que a agenda esta
    em ordem enquanto o campo da agenda vem vazio. Os dois campos contam a
    mesma historia ou nao contam nenhuma.
    """
    if period is None:
        return False, "outside_business_hours"
    if not operation.is_open:
        return False, "branch_paused"
    return True, None


def resolve_receipt_footer(
    branch: Branch,
    restaurant_settings: RestaurantSetting | None,
) -> str | None:
    """A mensagem do rodape que vale nesta filial, ja resolvida.

    Mesmo regime dos termos comerciais — `NULL` na filial significa "herda o
    padrao do restaurante" — com um terceiro estado que os outros campos nao
    tem: **a string vazia e uma escolha**, "esta loja nao imprime rodape". E
    o unico jeito de uma filial recusar a campanha da rede, e e o mesmo caso
    do `service_fee_enabled = False` que `_ou_herdado` existe para proteger.

    Devolve `None` ou `""` para "nao imprime nada" — quem desenha a via
    (`print_layout._footer_block`) trata os dois igual, porque para o papel
    eles sao a mesma coisa. A diferenca so importa para quem GRAVA.
    """
    padrao = None
    if restaurant_settings is not None:
        padrao = restaurant_settings.receipt_footer_message
    return _ou_herdado(branch.receipt_footer_message, padrao)


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
        free_delivery_enabled=restaurant_settings.free_delivery_enabled,
        free_delivery_min_order_value=restaurant_settings.free_delivery_min_order_value,
    )


def _ou_herdado(valor_da_filial, valor_do_restaurante):
    """O valor da filial; o do restaurante quando a filial nao sobrescreveu.

    A distincao e entre NULL e valor: `service_fee_enabled = False` na filial
    e uma escolha ("esta loja nao cobra taxa"), e nao pode cair no `true` do
    restaurante. Por isso `is not None`, e nunca um teste de veracidade.

    `receipt_footer_message = ""` e o mesmo caso escrito com texto: e a
    filial dizendo "nao imprima o rodape da marca aqui". Um `or` faria a
    mensagem da rede voltar a sair justamente na loja que a recusou.
    """
    if valor_da_filial is not None:
        return valor_da_filial
    return valor_do_restaurante
