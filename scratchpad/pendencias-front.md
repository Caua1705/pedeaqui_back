# Pendências do app e do painel — fonte da verdade

Branch: `rodada/pendencias-front`, saindo de `rodada/entregadores`. Nunca
commitar na `main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Nada de frente nova: são as pendências que os outros dois repositórios
listaram como bloqueadas pelo contrato.

---

## Tamanho, dito antes de fazer

| # | Item | Tamanho | Estado |
|---|---|---|---|
| a | `expires_at` do Pix em `StartPaymentResponse` | pequeno | **feito** |
| b1 | `visibility` no card do cliente | trivial | **feito** |
| b2 | `auto_apply` calculado pela mesma escolha do checkout | pequeno | **feito** |
| b3 | restrição por forma de pagamento (campo + estado) | médio | **feito** — revisão `20260904_0046` |
| b4 | restrição por horário do dia (campo + estado) | médio | **feito** — mesma revisão |
| b5 | restrição por itens do cardápio | **grande** | **recusado por medida** (04/09/2026) |

b5 é o mais caro dos três com folga (`docs/cupons.md` §7.3): tabela nova,
reescrita de `calculate_discount`, a base da comissão muda, o cardápio é por
filial e o cupom é do restaurante (vínculo por `catalog_key`), e um estado
novo "falta o item". Não entra sem decisão.

---

## a) `expires_at` do Pix

**O que existe.** `POST /v1/payments` do Mercado Pago aceita
`date_of_expiration` e devolve o mesmo campo na resposta. Hoje não mandamos
nada (vale o padrão deles) e não lemos nada de volta; `StartPaymentResponse`
não tem prazo, e o app conta pelo relógio do cliente.

**Desenho.**

- `PIX_EXPIRATION_MINUTES` na configuração, padrão 30. O service calcula o
  instante (`utcnow() + minutos`) só para pix e passa ao gateway;
- o corpo ganha `date_of_expiration` no formato deles
  (`2026-09-03T19:30:00.000-03:00`, no fuso da operação);
- **o prazo NÃO entra na chave de idempotência.** Ele muda a cada segundo;
  se entrasse no hash, o segundo clique em "pagar" abriria um segundo pix em
  vez de devolver o mesmo (armadilha 6, a propriedade que tem que continuar
  valendo). `_mercadopago_idempotency_key` descarta o campo antes do hash;
- `_mercadopago_intent` lê `date_of_expiration` da resposta e o publica como
  `expires_at` (aware). Como o segundo clique devolve a MESMA cobrança, o
  `expires_at` que volta é o original — o contador do app não reinicia;
- sandbox devolve o instante que recebeu;
- `StartPaymentResponse.expires_at: datetime | None` — nulo no cartão.

**O que NÃO muda:** nenhuma coluna nova. O pedido não guarda o prazo; ele
existe na cobrança do gateway, e é de lá que volta.

**Feito.** Vermelho visto (`create_payment() got an unexpected keyword
argument 'pix_expires_at'`), 11 verdes em `tests/test_pix_expira_em.py`;
os 200 de gateway e pagamento continuam verdes. Lição da rodada: um guarda
`if nome not in arquivo` num patch falha quando o nome já foi inserido em
outro ponto pelo mesmo patch — a constante ficou de fora e o `NameError`
apareceu no teste, não no ruff.

### Pronto para colar no app

> `POST /restaurants/{slug}/orders/{tracking_token}/payment` passou a devolver
> `expires_at` (ISO 8601 com fuso, ou `null` no cartão). **Contem o tempo por
> ele**, não pelo relógio local: é o `date_of_expiration` da cobrança no
> Mercado Pago. Um segundo clique em "pagar" devolve a mesma cobrança com o
> mesmo `expires_at` — o contador não reinicia. Quando o prazo vence, o
> webhook marca o pagamento como `failed` e um novo POST gera outro pix, com
> outro prazo. O padrão é 30 minutos (`PIX_EXPIRATION_MINUTES`).

---

## b1 + b2) `visibility` e `auto_apply` no card

`CustomerCouponResponse` ganhou os dois. `auto_apply` sai de
`CouponService._pick_automatic`, extraída de `auto_apply_for_order` e
chamada também por `list_for_customer`: entre os automáticos que cabem,
maior desconto, desempate por `sort_order` e `id`. É `true` em exatamente
um card, e o teste prova que é o mesmo cupom que o checkout escolhe. Sem
`subtotal` (Clube) e para convidado, sempre `false` — marcar seria prometer
o que o checkout não faz.

Vermelho visto (7), 7 verdes; 2499 rápidos + 654 `db`. `openapi.json`
regenerado.

### Pronto para colar no app

> Cada card de `GET /{slug}/coupons` traz agora `visibility`
> (`public` | `segment` | `private`) e `auto_apply` (bool). "Para todos" é
> `visibility == "public"`; `label` continua sendo só `selected_for_you`.
> **Não escolham o automático do lado de lá**: `auto_apply: true` marca o
> único cupom que o checkout vai aplicar sozinho para essa sacola. Sem
> `subtotal` na chamada, e sem login, ele vem sempre `false`.

---

## b3 + b4) Forma de pagamento e horário do dia

Um commit para os dois: são a mesma revisão (`20260904_0046`), os mesmos
schemas e a mesma função. `docs/cupons.md` §7 tem o desenho inteiro; o que
fica aqui é o que decidiu a forma:

- **`payment_method=None` em `evaluate` significa "ainda não escolheu", e o
  cupom cabe.** A forma é a última coisa que o cliente escolhe (§7.1 do doc
  já avisava); o card traz `allowed_payment_methods` sempre, e quem barra é
  o pedido, que sempre tem a forma. Passar a forma pela listagem
  (`?payment_method=`) é opcional e dá o estado `payment_method_not_allowed`;
- **os dois estados novos entram em `REASON_TO_STATE`** pelo critério do
  doc: são consertos que o cliente faz (trocar a forma; esperar as 15h). O
  card fora do horário aparece com a faixa escrita — invisível de manhã, o
  lojista juraria que a campanha sumiu;
- **hora da operação, não UTC**, `hora_da_operacao` em `coupon_window.py`,
  ao lado das outras duas perguntas do arquivo. Só forma Python (não recorta
  vitrine), com o motivo escrito;
- **sem tolerância de minutos** no fim da faixa: seria uma segunda faixa que
  ninguém configurou. O app mostra a faixa antes do clique;
- **b5 (itens) continua fora**, esperando decisão.

Vermelho visto (coleta interrompida em `dentro_do_horario`, depois os 30
casos), 171 verdes nas suítes de cupom; portão inteiro verde; 41
divergências ORM×schema (as colunas nasceram alinhadas). `openapi.json`
regenerado.

### Pronto para colar no painel

> **Cupom: duas restrições novas em `POST`/`PATCH /admin/coupons` e nas
> respostas**
>
> | Campo | Tipo | Significado |
> |---|---|---|
> | `allowed_payment_methods` | `string[] \| null` | as formas em que o cupom vale (`pix`, `credit_card`, `debit_card`, `cash`, `voucher`, `meal_voucher`, `other`). `null` = qualquer. Lista vazia é 422 |
> | `valid_hours_from` / `valid_hours_until` | `"HH:MM:SS" \| null` | faixa do dia, em hora local do restaurante. Os dois ou nenhum; iguais é 422; `22:00:00`/`02:00:00` vira a noite e vale |
>
> No PATCH: `null` explícito tira a restrição; campo ausente preserva. O
> par de horas é validado sobre a mescla com o que está gravado — mandar só
> `valid_hours_from` num cupom sem faixa dá 422. Fim exclusivo: "até 18h"
> acaba às 18:00:00.

### Pronto para colar no app

> **Cupom: dois estados e três campos novos no card de `GET /{slug}/coupons`**
>
> - `state` ganhou `payment_method_not_allowed` e `outside_hours`. Os dois são
>   coisas que o cliente resolve (trocar a forma; voltar às 15h), então o
>   card vem, com `discount_amount: 0`.
> - `allowed_payment_methods` (`string[] | null`) e `valid_hours_from` /
>   `valid_hours_until` (`"HH:MM:SS" | null`, hora de Fortaleza) vêm
>   **sempre** que existem — inclusive com `state: applicable`. Escrevam "só
>   no pix" e "das 15h às 18h" no card antes de o cliente escolher qualquer
>   coisa; é isso que evita o desconto aparecer e sumir.
> - A listagem aceita `?payment_method=` (os mesmos valores do pedido).
>   Mandem quando o cliente já escolheu a forma; sem ele, cupom restrito vem
>   `applicable` e o card avisa em qual forma vale. O preview
>   (`POST .../coupons/preview`) aceita `payment_method` no corpo.
> - O pedido recusa com 400 e `detail` igual ao motivo
>   (`payment_method_not_allowed`, `outside_hours`) — sem tolerância no fim
>   da faixa: às 18:00:00 o cupom das 15h às 18h já não vale.

---

## b5) Recusado por medida

Decisão do dono em 04/09/2026, pelo custo medido: o dobro de b3+b4, tabela
nova, e mexe em dinheiro já congelado nos pedidos. Nenhum restaurante pediu
essa restrição. Registrado em `docs/cupons.md` §7.3 com o preço, para a
decisão ser refeita com um pedido na mão e não de memória.

---

## c) O relatório do dono: quanto devo a cada motoboy

Escolha minha, pelo valor: o entregador vê o próprio histórico pelo link,
mas quem paga é o dono, e até aqui ele teria que perguntar a cada um — ou
abrir o banco.

`GET /admin/reports/couriers?start_date&end_date&branch_id`, GERENCIA com
`ensure_pode_ler_dinheiro` (dono lê tudo; gerente lê a própria filial),
mesmo recorte e mesmo teto (92 dias) dos relatórios de Desempenho.

- **uma consulta agrupada** (`totals_by_courier`) com a MESMA definição de
  entrega de `list_deliveries_by_courier`: atribuição aberta, pedido
  `completed`, instante da linha `completed`. Duas definições divergiriam
  no dia em que o motoboy conferisse o histórico dele contra o pagamento;
- **excluído entra, marcado** (`is_deleted`): saiu, mas fez corridas;
- **`deliveries_without_fee` separado**: corrida sem taxa conta como entrega
  e não como zero — somar zero a esconderia exatamente no relatório que
  existe para pagar;
- `Decimal` na resposta, como os outros relatórios de dinheiro do painel.

Vermelho visto (8), 8 verdes (7 rápidos + 1 de banco com os quatro
recortes: período, filial, restaurante e atribuição fechada). Papéis e
escopo cobrados pelas varreduras. `openapi.json` regenerado.

### Pronto para colar no painel

> `GET /admin/reports/couriers?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&branch_id=`
> (GERENCIA; gerente precisa de `branch_id`, senão 403; até 92 dias, senão 400).
>
> ```
> { restaurant_id, branch_id|null,
>   period: {start_date, end_date, days},
>   deliveries_count, deliveries_without_fee, fee_total: "117.00",
>   couriers: [{ courier_id, name, phone, branch_id, is_deleted,
>                deliveries_count, deliveries_without_fee, fee_total: "96.00" }] }
> ```
>
> `fee_total` vem como string com duas casas, igual aos relatórios de
> Desempenho. `deliveries_without_fee` é o número a acertar à mão (a filial
> não tinha taxa quando atribuiu); mostrem ao lado da soma, não dentro dela.
> `is_deleted` marca o motoboy que já saiu do cadastro e ainda tem corrida
> a receber no período.
