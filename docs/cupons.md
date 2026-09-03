# Cupons

Quem enxerga, quem consegue usar, e quem decide. Desenho de 28/08/2026,
revisão `20260828_0043`.

## O resumo, em uma frase

Um cupom tem **visibilidade** (quem vê), **código** (se precisa ser digitado)
e **regras de uso** (janela, mínimo, tetos). As três são conferidas por uma
função só — `CouponService.evaluate` —, e a resposta que vale é a da criação
do pedido.

---

## 1. Visibilidade

`restaurant_coupons.visibility`, três valores, CHECK no banco:

| valor | aparece na lista | como o cliente chega nele |
|---|---|---|
| `public` | para todos | está lá |
| `segment` | só para quem está em `target_segment` | está lá, com etiqueta |
| `private` | para ninguém | digita o código no Clube |

Substituiu `is_public`, que tinha dois valores para uma pergunta de três. O
que faltava era o `segment` — a campanha de reativação ("sumiu há dois meses,
volte com R$ 15"), que publicada dá desconto a quem pede toda semana e
privada depende de um canal de distribuição que não existe.

**`target_segment` reusa os cinco rótulos RFV** (`novo`, `ocasional`, `fiel`,
`em_risco`, `perdido`), os mesmos que o painel pinta na lista de clientes. Não
há vocabulário próprio do cupom, de propósito: uma segunda definição de
"sumiu" divergiria da primeira, e o cabeçalho de
`src/services/customer_segment.py` registra o que custou a duplicata anterior.

O segmento é medido **por telefone**, com as mesmas expressões SQL do painel.
Quem pediu como convidado antes de criar a conta conta na mesma linha — sem
isso, um cupom de `novo` iria parar na mão de quem já pediu três vezes sem
estar logado.

### O gate, e o único lugar que lê `visibility`

`CouponService._can_see`. Duas propriedades que não são detalhe:

- **Convidado enxerga só o que é público**, e a recusa dos outros dois sai
  como `not_visible`, que a lista esconde. Devolver "entre na conta para usar"
  num cupom privado seria anunciar a existência dele, com título e código —
  exatamente o que `private` existe para não publicar.
- **A vitrine do cardápio (`GET /{slug}/menu`) filtra `= 'public'`, e nunca
  `!= 'private'`.** Ela é anônima e não passa por `evaluate`. Por exclusão,
  publicaria os cupons de segmento para qualquer pessoa que abrisse o
  cardápio, sem erro e sem log.

---

## 2. Código, e o cupom que aplica sozinho

`code` é **nullable** desde a revisão `20260828_0043`:

- **com código** — a pessoa digita, no checkout ou no Clube;
- **sem código** — aplica sozinho no checkout, quando a sacola permite.

Os três índices únicos de código continuam intactos: o `UNIQUE` do Postgres
trata `NULL` como distinto de `NULL`, então N campanhas automáticas convivem
no mesmo restaurante.

### A auto-aplicação

`CouponService.auto_apply_for_order`, chamada de `OrderService._resolve_coupon`
**somente quando o corpo do pedido não trouxe cupom nenhum**.

- **Seletor explícito vence.** É o que faz "trocar de cupom" funcionar sem
  rota de remoção: o pedido carrega um cupom só, e o último escolhido é o que
  vai. Sem essa ordem, quem escolhesse um cupom de R$ 10 ganharia também o
  automático de R$ 5 — dois cupons no mesmo pedido pela porta dos fundos.
- **Só para cliente logado.** Não é política: `coupon_redemptions.customer_id`
  é `NOT NULL`, e um desconto automático para convidado não teria onde
  registrar o uso — o teto por cliente da campanha deixaria de existir para
  quem não entrasse na conta.
- **Entre dois que cabem, vale o maior.** O cliente não tem tela para
  escolher, e nem sabe que há dois. O empate é resolvido por `sort_order` e
  depois por `id`, para a escolha ser determinística: sem o segundo critério,
  dois cupons de R$ 10 se revezariam e o nome do cupom mudaria entre o preview
  e o checkout.
- **A confirmação passa pelo mesmo lock do caminho explícito.** Entre escolher
  e gravar, o teto total da campanha pode ter sido atingido por outro pedido.

---

## 3. Resgate ≠ uso

Duas tabelas, e confundi-las quebra o teto da campanha:

| | `coupon_redemptions` | `coupon_claims` |
|---|---|---|
| o que é | **USO** | **RESGATE** |
| tem `order_id` | sim, `NOT NULL` | não existe |
| tem valor | `discount_amount` | não |
| conta no `total_usage_limit` | **sim** | não |
| o que concede | o desconto daquele pedido | **visibilidade** |

`POST /{slug}/coupons/claim` grava a segunda. Guardar resgate na primeira
exigiria relaxar o `order_id NOT NULL`, e o cupom de 100 usos se esgotaria com
gente que só digitou o código e nunca fechou pedido.

**O resgate não antecipa regra nenhuma.** Janela, mínimo, teto total, teto por
cliente, cooldown e primeira-compra continuam sendo conferidos na criação do
pedido, sobre a sacola daquele momento. Resgatar um cupom vencido é possível e
inútil — ele nunca aparece na lista.

### As duas metades da defesa contra varredura

Sem as duas, alguém varre `PROMO1`, `PROMO2`, `VOLTA10` até achar um código
privado, e o desconto que o lojista mandou para dez clientes por WhatsApp vira
desconto para a internet inteira.

1. **`COUPON_CLAIM_RATE_LIMIT = "5/minute;30/hour"`**, por IP. Encarece a
   varredura.
2. **Uma resposta só para todos os jeitos de o código não servir** — código
   inexistente, de outro restaurante, campanha desativada, cupom de segmento
   que não é daquela pessoa: 404 com a mesma frase. Faz o que a varredura
   devolve não valer nada (armadilha 18).

Resíduo conhecido: o limite é por IP, e quem gira IP escapa dele. O que segura
esse caso é a resposta uniforme. Um teto por cliente exigiria contar
**tentativas erradas**, que não geram linha nenhuma hoje — seria um contador
novo no Redis, e ele entra se a varredura aparecer no log.

---

## 4. O que a rota do cliente devolve

`GET /{slug}/coupons?subtotal=&delivery_fee=&order_type=` — substituiu
`GET /{slug}/coupons/available`.

Cada card vem com o estado **já decidido**. O front não calcula nada:

- **`label`** — `selected_for_you` no cupom de segmento, `null` no público e
  no privado. Se todo mundo vê, "para todos" é ruído e ocupa o espaço do card.
  E a etiqueta **nunca fala de disponibilidade**: quem diz se dá para usar é o
  `state`. Sem essa separação as duas se contradizem na mesma tela —
  "selecionado para você" ao lado de "faltam R$ 12".
- **`visibility`** — `public`, `segment` ou `private`. É o que o app usa para
  a etiqueta "para todos"; `label` continua sendo só a de segmento, porque
  uma diz o que o cupom **é** e a outra o que o card **destaca**.
- **`auto_apply`** — `true` em exatamente o card que o checkout aplicaria
  sozinho para esta sacola. Calculado por `CouponService._pick_automatic`, a
  **mesma** função que `auto_apply_for_order` usa: a escolha entre dois
  automáticos é decisão de dinheiro e tem um dono só. Sem `subtotal` (o
  Clube) e para convidado é sempre `false`.
- **`state`** — `applicable`, `missing_amount`, `login_required`,
  `payment_method_not_allowed` ou `outside_hours`.
- **`allowed_payment_methods`** e **`valid_hours_from`/`valid_hours_until`**
  — as restrições do cupom, **sempre** presentes quando existem (nulas
  quando não), e não só quando o estado as cita: a sacola antes da escolha
  da forma precisa mostrar "só no pix" antes de o cliente escolher o cartão
  e ver o desconto sumir. Ver §7.
- **`discount_amount`** — o que aquele cupom tiraria **desta** sacola. Zero
  quando não cabe: o card não pode anunciar um valor que o checkout não vai
  dar.
- **`missing_amount`** — quanto falta no subtotal.

**Cupom sem conserto nesta sacola não vem na lista.** Vencido, de outro
segmento, primeira-compra para quem já comprou, teto estourado, cooldown
correndo, privado sem resgate: nada disso o cliente resolve agora, e um card
cinza com uma negativa que ele não consegue desfazer só ocupa a tela. Os dois
que entram são exatamente os que ele muda — pôr mais coisa na sacola, ou
entrar na conta.

A tabela que decide isso é `REASON_TO_STATE`, em `coupon_service.py`. **O que
não está nela não vira card**, e é essa a regra inteira.

### O que NÃO sai nessa rota, e não é esquecimento

`discount_value`, `max_discount_amount`, `total_usage_limit`,
`usage_limit_per_customer` e `cooldown_days` são parâmetros da conta e limites
internos da campanha. Quem precisa deles é quem calcula, e quem calcula é o
backend. Publicados, só serviriam para alguém refazer a conta errado — ou para
mapear os limites da campanha de fora.

`min_order_value` e `valid_until` ficam porque não são parâmetros: são as
frases "pedido mínimo R$ 30" e "válido até", que o card precisa escrever.

---

## 5. Um cupom por pedido, e o cashback acumula

**Nunca dois cupons juntos, e a estrutura garante isso** — `orders.coupon_id` é
uma coluna só, `coupon_redemptions` tem `UNIQUE (order_id)`, e
`CreateOrderRequest` recusa `coupon_id` e `coupon_code` no mesmo corpo. Não há
rota de "remover cupom": o corpo carrega o último escolhido, e a troca é o
cliente mandando outro.

**Cupom e cashback acumulam**, e sempre acumularam. São coisas diferentes — o
cupom é campanha do lojista, o cashback é dinheiro que o cliente já tinha —, e
o teto do resgate de cashback já desconta o cupom (`subtotal -
coupon_discount`), então os dois juntos nunca passam do subtotal.

### Resíduo conhecido: o pedido com cupom automático não tem nome do cupom

`orders.coupon_code_snapshot` recebe `coupon.code`, que é `NULL` num cupom
automático. O pedido fica com `coupon_id` preenchido, `coupon_discount_amount`
maior que zero e **nenhum rótulo** — o histórico do cliente mostra "desconto
R$ 10" sem dizer de quê.

Não foi consertado aqui de propósito, e a alternativa tem preço: uma coluna
`coupon_title_snapshot` é campo novo em respostas gravadas em
`idempotency_keys.response_body`, o que custa 24h de 409 se ela for
obrigatória (armadilha 7). Se entrar, entra como opcional com default.

---

## 6. A função única

`CouponService.evaluate` responde "cabe ou não cabe", e chamam ela:

| chamador | o que é |
|---|---|
| `list_for_customer` | preview |
| `preview` | preview |
| `auto_apply_for_order` | preview (e depois confirma no de baixo) |
| `lock_and_validate_for_order` | **a validação que vale** |

A última roda com o cupom travado (`SELECT ... FOR UPDATE`), dentro da
transação do pedido. É o que impede dois pedidos simultâneos de furarem o
mesmo `total_usage_limit`.

**O parâmetro `require_public` saiu, e a saída é o ponto.** Ele fazia "de qual
superfície eu vim" ser decisão do chamador, e uma superfície nova que
esquecesse de passar `True` publicava cupom privado sem erro nenhum. Hoje quem
responde é a coluna `visibility`, sempre, dentro de `evaluate`.

---

## 7. As restrições: duas implementadas, uma registrada

O Cardápio Web tem as três. Duas entraram em 04/09/2026 (revisão
`20260904_0046`); a terceira continua registrada com o preço na mão.

### 7.1 Restrição por forma de pagamento — implementada

*"Este cupom só vale no pix."* O lojista paga ~0,89 no pix e ~3,58 no cartão
(armadilha 25); um cupom que empurra para o pix se paga.

`restaurant_coupons.allowed_payment_methods text[]`, nulo = qualquer forma.
Os valores são os de `PAYMENT_METHODS`, e o CHECK do banco cobra a mesma
lista — método novo entra nos três lugares juntos (armadilha 15). Lista
vazia é recusada nos dois lados: vazia é "em nenhuma forma", não "em
qualquer".

**`evaluate` ganhou `payment_method`, e os quatro chamadores a passam.** A
listagem recebe `?payment_method=` (opcional), o preview recebe no corpo, a
auto-aplicação e a validação do pedido recebem a forma do próprio pedido
(que `_resolve_payment_flow` já validou).

**A armadilha da ordem foi resolvida do lado do card, não da regra.** A forma
é a última coisa que o cliente escolhe, então:

- **sem forma** (o Clube, a sacola antes da escolha) o cupom restrito
  **cabe**, e o card traz `allowed_payment_methods` sempre — o app escreve
  "só no pix" antes de o cliente escolher o cartão;
- **com forma errada** o card vem com `state = payment_method_not_allowed`,
  que é conserto do cliente (trocar a forma), e por isso está em
  `REASON_TO_STATE`;
- **quem barra de verdade é o pedido**, que sempre tem a forma: 400
  `payment_method_not_allowed`.

### 7.2 Restrição por horário do dia — implementada

*"Vale das 15h às 18h."* Para encher o vazio da tarde sem dar desconto no
pico do almoço.

`valid_hours_from` e `valid_hours_until`, `time` **sem fuso, em hora local da
operação** (`PLATFORM_TIMEZONE`) — o regime de `branch_business_hours`. Quem
lê é `coupon_window.dentro_do_horario`, sobre `hora_da_operacao(agora)`;
comparar com a hora UTC do servidor poria a campanha das 15h valendo ao
meio-dia. Início inclusivo, fim exclusivo ("até as 18h" acaba às 18:00:00);
faixa que vira a noite (22h às 2h) vale todo dia, sem weekday. Os dois
juntos ou nenhum, e nunca iguais — CHECK no banco e validador no schema.

**Só existe a forma em Python**, ao contrário da janela de datas: a faixa
não recorta a vitrine. O card fora do horário **aparece**, com
`state = outside_hours` e a faixa escrita, porque às 15h ele passa a valer —
sem isso a campanha da tarde seria invisível de manhã e o lojista juraria
que ela sumiu.

**Sem tolerância, de propósito.** O cupom visto às 17h58 e o pedido criado às
18h01 é recusado com 400 `outside_hours`. A regra sem tolerância é a que o
lojista escreveu; o app mostra a faixa antes do clique, e é isso que evita a
surpresa. Uma tolerância seria uma segunda faixa que ninguém configurou.

### 7.3 Cupom valendo só em itens específicos do cardápio — RECUSADA por medida (04/09/2026)

**Recusada, não adiada.** O custo medido foi o dobro de 7.1 e 7.2 somadas:
tabela nova, reescrita de `calculate_discount` para receber os itens do
pedido, um terceiro caso na base da comissão, vínculo por `catalog_key`
porque o cupom é do restaurante e o produto é da filial, e um estado novo
"falta o item". E mexe em dinheiro já congelado nos pedidos. **Nenhum
restaurante pediu.** Se um dia pedir, o preço está abaixo, e a decisão é
outra — com o pedido na mão.

O texto original da medição:

*"R$ 5 off na picanha."*

Para que serve: girar um item parado, ou um lançamento.

O que custaria: uma tabela `coupon_products` (ou `coupon_categories`), e **a
reescrita de `calculate_discount`**. Hoje ela desconta sobre `subtotal` ou
sobre `delivery_fee`, dois números; passaria a precisar da lista de itens do
pedido para achar a base — e a listagem do Clube não tem itens, só um subtotal.

Três coisas que mudam junto, e não são detalhe:

- **a base da comissão.** `_coupon_discount_on_products` separa hoje o
  desconto que morde produto do que morde frete, e ela passaria a precisar de
  um terceiro caso: quanto do desconto caiu em quais linhas;
- **o cardápio é por filial** desde a revisão `20260820_0026`. Um cupom é do
  RESTAURANTE, e `products.id` é da filial — o vínculo teria que ser por
  `catalog_key` (que existe para somar o mesmo item entre lojas), ou o cupom
  só valeria numa loja;
- **a tela.** "R$ 5 off na picanha" com a picanha fora da sacola precisa de um
  estado novo — nem `applicable`, nem `missing_amount`, e sim "falta o item".
  `REASON_TO_STATE` ganharia uma linha, e o app um card novo.

**É o mais caro dos três, com folga**, e é o que o lojista pede primeiro.
Vale medir quanto ele é pedido antes de pagar isso.
