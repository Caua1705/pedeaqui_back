"""A classificacao RFV de um cliente, a partir do que a listagem ja consulta.

Funcao pura, sem banco e sem `Session`: recebe os quatro numeros que
`AdminCustomerRepository.list_customers` ja devolve por linha
(`orders_count`, `first_order_at`, `last_order_at`) mais o agora, e responde
um `CustomerSegment`. Nao ha consulta nova, coluna nova nem migracao.

## A cadencia e do CLIENTE, nao do restaurante

    cadencia_bruta = (last_order_at - first_order_at) / (orders_count - 1)

Faixa fixa nao serve, e faixa fixa por restaurante tambem nao: o mesmo
cardapio atende o cliente de almoco de terca e o de aniversario, e uma media
da casa erra os dois. Erra para o lado caro, ainda: "em risco" existe para
disparar reativacao, e chamar de volta quem esta no proprio ritmo queima o
canal com quem nunca foi embora.

A media crua tem dois modos de falha, e os dois sao reais — por isso ela e
grampeada entre `RFV_MIN_CADENCE_DAYS` e `RFV_MAX_CADENCE_DAYS`:

| Caso | Cadencia crua | O que aconteceria sem o grampo |
|---|---|---|
| dois pedidos no mesmo almoco (esqueceu a bebida) | ~0 dia | "perdido" no dia seguinte |
| dois pedidos separados por oito meses | 240 dias | nunca sai de "fiel" |

Quem tem UM pedido nao tem intervalo: nao ha o que dividir, e vale
`RFV_FALLBACK_CADENCE_DAYS`.

## O que "novo" significa, e por que existe "ocasional"

`NOVO` conta do PRIMEIRO pedido (`RFV_NEW_WINDOW_DAYS`), e nao do ultimo: o
que envelhece e o relacionamento. Sem essa janela, o cliente de dois pedidos
espacados em dez meses que pediu semana passada sairia como "novo" — a tela
mentindo na primeira leitura de quem a abre. Ele e `OCASIONAL`: poucos
pedidos, relacionamento antigo, e em dia com o proprio ritmo.

## A CLASSIFICACAO NAO E ARMAZENADA — e o que isso custa depois

Ela e derivada na leitura. Ninguem precisa de cron para ela ficar em dia, e
nao ha coluna que envelheca. Em troca, duas coisas que a frente de reativacao
vai encontrar, e que ficam anotadas aqui de proposito:

1. **Nao existe evento "virou em risco hoje".** Um gatilho do tipo "me avise
   quando alguem sumir" precisa de varredura periodica comparando com o
   estado anterior, ou de gravar a classe. Nada disso esta construido.
2. **FILTRAR por classe nao funciona daqui.** Esta funcao roda sobre a pagina
   ja paginada, entao filtrar depois devolveria pagina com tres linhas.
   Quando a tela pedir "mostre so os em risco", a formula tem que ir para o
   SQL — e **o buraco e escreve-la duas vezes**: uma versao em Python e uma
   em `CASE`, discordando em silencio no dia em que alguem mexer so numa. Ou
   a versao SQL passa a ser a unica e esta some, ou as duas nascem do mesmo
   lugar. Nao ha terceira saida que envelheca bem.
"""

from datetime import datetime

from src.core.config import settings
from src.schemas.admin_customer_schema import CustomerSegment


def days_since_last_order(last_order_at: datetime | None, now: datetime) -> int | None:
    """Dias inteiros desde o ultimo pedido. `None` quando nao ha pedido.

    Piso em zero: `orders.created_at` e preenchido pelo banco, mas relogio
    adiantado em qualquer ponta produziria dias negativos, e "-1 dia sem
    pedir" nao e uma frase que o painel consiga mostrar.
    """
    if last_order_at is None:
        return None
    return max((now - last_order_at).days, 0)


def cadence_days(
    orders_count: int,
    first_order_at: datetime | None,
    last_order_at: datetime | None,
) -> float:
    """De quantos em quantos dias ESTE cliente costuma pedir.

    Com menos de dois pedidos nao ha intervalo para medir — nem com datas
    faltando, que o model permite (`created_at` e nullable).
    """
    if orders_count < 2:
        return float(settings.RFV_FALLBACK_CADENCE_DAYS)
    if first_order_at is None or last_order_at is None:
        return float(settings.RFV_FALLBACK_CADENCE_DAYS)

    intervalo = (last_order_at - first_order_at).total_seconds() / 86400
    media = intervalo / (orders_count - 1)
    return min(max(media, settings.RFV_MIN_CADENCE_DAYS), settings.RFV_MAX_CADENCE_DAYS)


def classify_customer(
    orders_count: int,
    first_order_at: datetime | None,
    last_order_at: datetime | None,
    now: datetime,
) -> CustomerSegment:
    """O rotulo RFV desta linha da listagem.

    A escada e lida de cima para baixo e a ORDEM e a regra: recencia primeiro,
    contagem depois. Invertida, o cliente de doze pedidos semanais sumido ha
    seis meses sairia como "fiel" — e e exatamente ele que a reativacao
    precisa achar.
    """
    dias = days_since_last_order(last_order_at, now)
    if dias is None:
        return CustomerSegment.NOVO

    cadencia = cadence_days(orders_count, first_order_at, last_order_at)
    if dias > cadencia * settings.RFV_LOST_FACTOR:
        return CustomerSegment.PERDIDO
    if dias > cadencia * settings.RFV_AT_RISK_FACTOR:
        return CustomerSegment.EM_RISCO
    if orders_count >= settings.RFV_ORDERS_FOR_LOYAL:
        return CustomerSegment.FIEL
    if _relacionamento_e_novo(first_order_at, now):
        return CustomerSegment.NOVO
    return CustomerSegment.OCASIONAL


def _relacionamento_e_novo(first_order_at: datetime | None, now: datetime) -> bool:
    """O PRIMEIRO pedido ainda esta dentro da janela de novidade?

    Sem `first_order_at` nao da para dizer que o relacionamento e antigo, e
    quem chegou ate aqui esta em dia com o proprio ritmo: "novo" e a leitura
    que nao inventa historico.
    """
    if first_order_at is None:
        return True
    return (now - first_order_at).days <= settings.RFV_NEW_WINDOW_DAYS
