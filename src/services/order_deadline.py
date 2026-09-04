"""O prazo prometido ao cliente, num lugar so.

## O que este arquivo responde, e o que ele NAO responde

Responde: **ate quando o pedido foi prometido ao cliente.** E o instante do
checkout mais o teto da janela que ele viu na tela antes de fechar.

Nao responde "o entregador esta atrasado". Sao perguntas diferentes e a
confusao entre elas e o motivo de esta funcao existir com nome — ver a secao
final.

## Por que a soma mora aqui e nao em quem serializa

`delivery_eta_max` e MINUTO e `delivery_estimated_at` e INSTANTE. Somar os dois
e uma linha, e uma linha repetida em cada tela e a armadilha 54: no dia em que
a regra mudar (uma tolerancia, um arredondamento), uma das copias fica para
tras e duas superficies passam a mostrar prazos diferentes para o mesmo pedido.

## `delivery_estimated_at` e a ancora, e NAO `created_at`

Os dois sao gravados no mesmo INSERT e hoje valem o mesmo instante. A
diferenca esta no que cada um significa quando NAO ha estimativa:

- `created_at` existe em TODO pedido — inclusive na retirada, que nunca teve
  promessa de entrega. Ancorar nele daria um prazo a quem nao tem prazo;
- `delivery_estimated_at` e nulo exatamente quando nao houve estimativa
  (`OrderService._estimate_delivery` devolve `None` fora da entrega). Ele e
  nulo junto com `delivery_eta_min` e `delivery_eta_max`, porque os tres saem
  do MESMO objeto, numa escrita so.

Ou seja: a ancora certa e a que desaparece junto com o resto da promessa.

## O prazo NAO e revisado depois da criacao, e isso tem consequencia

Nada no sistema reescreve esses tres campos — `OrderService.create_order` e o
unico escritor. O relogio comeca no CHECKOUT, e `delivery_eta_max` ja inclui o
`prep_time_max` da faixa de horario: o tempo de cozinha esta orcado dentro da
janela.

Entao um pedido aceito 20 minutos depois do checkout chega ao entregador com
boa parte do prazo ja gasta. **O numero continua verdadeiro sobre o CLIENTE** —
ele esta mesmo esperando alem do que lhe foi prometido —, e seria mentira so
se a tela o apresentasse como desempenho do motoboy.

Por isso a resposta do entregador leva `delivery_due_at` ao lado de
`assigned_at`: comparar os dois separa "o prazo venceu antes de ele receber a
corrida" de "venceu com ele na rua". Quem quiser medir a corrida mede a partir
de `assigned_at`, e isso e outra conta — nao esta.
"""

from datetime import datetime, timedelta


def prazo_prometido(order) -> datetime | None:
    """Ate quando o pedido foi prometido. `None` quando nao houve promessa.

    Os dois campos sao conferidos, e nao so um: uma linha com estimativa e sem
    teto de janela nao existe pelo caminho do ORM, mas existe no banco (as
    colunas sao nulaveis, e ha pedido anterior a estas colunas). Somar sobre um
    nulo ali seria `TypeError` na tela do motoboy, no meio da corrida.
    """
    if order.delivery_estimated_at is None:
        return None
    if order.delivery_eta_max is None:
        return None
    return order.delivery_estimated_at + timedelta(minutes=order.delivery_eta_max)
