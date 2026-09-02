"""A taxa do entregador: quanto a loja PAGA ao motoboy por uma corrida.

Funcao pura, sem banco, pelo mesmo motivo de `coupon_window.py`: a regra e
uma so e mora num lugar so. Quem a chama e a atribuicao
(`AdminCourierService.assign`), que congela o resultado em
`courier_assignments.courier_fee_snapshot` — mudar a taxa amanha nao muda a
corrida de ontem, como `unit_price_snapshot` faz com o preco.

Espelha a formula do frete do CLIENTE (`base + km x por_km`) sem piso nem
teto: com dois numeros o dono descreve os dois acordos que existem no piloto
(por corrida, ou por corrida mais distancia), e cada coluna a mais e uma que
o painel precisa desenhar e explicar.

**Nenhum numero de dinheiro do cliente passa por aqui.** Isto nao entra em
estimativa, em `orders.total` nem na comissao. E registro.
"""

from decimal import Decimal

from src.utils.money import quantize_money, to_decimal


def calculate_courier_fee(
    base: Decimal | None,
    per_km: Decimal | None,
    distance_km: Decimal | float | None,
) -> Decimal | None:
    """`base + distancia x por_km`, ou NULO quando nao ha o que somar.

    Nulo e nunca zero, e a diferenca e a armadilha 11 aplicada ao outro lado
    do balcao: zero e um numero que SOMA no historico que o dono usa para
    pagar, e faria a corrida de uma filial que nunca configurou taxa aparecer
    como "gratis" em vez de "sem taxa".

    Sem distancia (pedido precificado em contingencia, com o Google fora do
    ar) conta so a base: e a parte que nao depende dela. Se so o por-km
    estiver configurado, nao ha o que multiplicar e o resultado e nulo — o
    historico mostra a distancia nula ao lado para o dono acertar a mao.
    """
    if base is None and per_km is None:
        return None

    total = to_decimal(base)
    if per_km is not None and distance_km is not None:
        total += to_decimal(per_km) * to_decimal(distance_km)
    elif base is None:
        return None
    return quantize_money(total)
