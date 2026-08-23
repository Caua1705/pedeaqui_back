# Cashback

> **Estado: a frente está completa em código, em 22/08/2026.** A revisão
> `20260822_0032` põe o schema de pé; o crédito, o resgate e a devolução
> existem; `GET /customers/me/cashback` publica `by_restaurant[]` com
> `expires_at` (seção 4); e `scripts/expire_cashback.py` vence o saldo parado,
> no container `limpeza` (seção 3).
>
> **O que segura tudo é um dado, e não o código: `cashback_rules.enabled`
> nasce FALSO em todo restaurante.** Enquanto for falso, `resolve_cashback_terms`
> devolve `SEM_CASHBACK`, o razão continua sem escritor e nada vence.
>
> **Ligar a chave de um restaurante liga o crédito e o resgate no mesmo
> minuto — e o resgate entra como subtração na base da comissão.** Antes de
> ligar, a medição pendente da seção 1 ("o lojista fecha o pedido no
> painel?") precisa ser refeita com operação real.
>
> **Duas coisas ficaram para depois, e nenhuma bloqueia:**
>
> 1. **Termos por filial ao lado do cardápio**, para o app conseguir explicar
>    *por que não descontou* (saldo abaixo do mínimo devolve zero sem erro).
>    O porquê de não terem entrado em `by_restaurant[]` está na seção 4.
> 2. **O `balance` somado não é gastável**, e a tela do app tem que chamá-lo
>    de **"acumulado"**, nunca de "disponível". É contrato de front, não de
>    backend: a API já entrega a lista gastável ao lado.

Este documento é o desenho combinado, e existe para que as decisões não
precisem ser retomadas do zero quando o código chegar.

---

## 0. A decisão comercial, que estava travada

**A comissão da plataforma incide sobre o que o CLIENTE PAGOU, com o desconto
já aplicado.** Pedido de R$ 100 com R$ 10 de cashback resgatado gera comissão
sobre R$ 90.

**A mesma regra vale para cupom**, e já valia: `_calculate_commission` sempre
subtraiu `coupon_discount` da base. Duas mecânicas para dois descontos
parecidos seria impossível de explicar ao lojista.

A consequência aceita: a plataforma divide o custo do desconto com o
restaurante, na proporção da comissão. Quem paga os R$ 10 é o lojista; a
plataforma abre mão da comissão sobre eles.

### O resíduo que essa regra obriga a consertar

Cupom do tipo `free_delivery` tem `discount = delivery_fee`, e esse valor é
subtraído da base — que é composta **só de mercadoria**, porque taxa de
entrega está explicitamente fora dela. O cliente pagou os produtos
integralmente e a plataforma cobra como se ele tivesse pago menos.

E é incoerente com o código ao lado: frete grátis **por regra**
(`delivery_fee_waived`) não mexe na base. Duas mecânicas para o mesmo frete
grátis.

**Combinado: `free_delivery` sai da subtração da base**, junto com esta
frente. A frase que vamos dizer ao lojista — "comissão sobre o que o cliente
pagou pelos produtos" — só é verdadeira depois disso.

---

## 1. Quando o crédito acontece

**Na conclusão do pedido (`status = completed`).** Não no pago, não no aceite.

- `paid` acontece na criação do pedido online, e a loja ainda pode recusar.
  Creditar ali faz do estorno o caminho **comum**, não a exceção.
- `accepted` ainda permite `cancelled` — o grafo aceita cancelar de
  `accepted`, `preparing`, `ready` e `out_for_delivery`.
- `completed` é terminal. Depois dele o pedido não volta.

**Base do crédito = `commission_base_amount`**, que já está congelada no
pedido. Uma base, dois usos, impossível divergirem — e isso responde três
perguntas de graça: taxa de entrega e taxa de serviço não geram cashback;
pedido pago com cashback gera sobre o líquido; e o percentual do dia fica
congelado no `metadata` da transação, do mesmo jeito que
`commission_percent` já é congelado no pedido.

**Pedido de convidado não gera nada**: sem `customer_id` não há a quem
creditar. O app precisa dizer isso na tela, senão vira reclamação.

**Forma de pagamento sem linha na filial não gera**, e a falta vai para o log
(`[Cashback] forma de pagamento sem linha na filial`). Acontece quando o
lojista remove o método depois do pedido. A configuração é quem diz o que
gera; ausência de configuração não é permissão para gastar o dinheiro dele — e
o log é o que torna diagnosticável um cashback que parou de sair.

### Medição PENDENTE — reabrir quando houver operação real

Em 22/08/2026 a base tinha 35 pedidos desde 06/08, **todos de teste do
próprio time** (23 `pending` de Pix gerado e não pago, 2 `completed`). Não há
operação com cliente de verdade ainda.

Ou seja: **não existe evidência sobre se o lojista fecha o pedido no painel**,
e a decisão de creditar em `completed` depende disso. `completed` não é
load-bearing hoje — `billable_order_conditions` conta como venda tudo que não
é `cancelled`/`rejected`, então nenhum relatório denunciaria pedidos parados
em `ready`.

**Antes de anunciar cashback a cliente de verdade, medir de novo:**

```sql
SELECT order_type, status, count(*) FROM orders
WHERE created_at > now() - interval '30 days' GROUP BY 1, 2;
```

Se a fatia de `completed` for baixa em operação real, isso vira decisão de
produto (varredura de conclusão, ou crédito num status anterior), não detalhe
de implementação. **Não concluir nada a partir do número de teste.**

### Cancelamento e estorno

| Evento | O que acontece |
|---|---|
| `cancelled` / `rejected` antes de concluir | nunca chegou a creditar; **devolve o cashback resgatado** |
| `refunded` depois de concluir | estorna o crédito, **limitado ao saldo disponível** |

A devolução do resgate espelha `coupon_service.reverse_for_order`, no mesmo
ponto de `_apply_status_change`. Sem ela o cliente cancela e perde o saldo,
que é o pior chamado possível.

O estorno **nunca deixa o saldo negativo**: o que não deu para estornar sai
como `logger.warning` próprio — o mesmo padrão de "dinheiro parado que se acha
com grep" da armadilha 25. Saldo negativo seria uma dívida que o cliente
descobre no próximo pedido.

Toda linha inversa é linha NOVA no razão (`type="cancelled"`), com
`idempotency_key` própria — nunca um UPDATE do resgate original.

---

## 2. O resgate

**Parcial, mas quem escolhe o valor é o servidor.** O corpo do pedido manda
`use_cashback: true` e mais nada; o servidor aplica
`min(saldo, subtotal - desconto_de_cupom)`, respeitando o saldo mínimo. Um
`Decimal` vindo do app seria preço escolhido pelo cliente, e conferir exigiria
refazer o cálculo inteiro.

**Convidado com `use_cashback` recebe 401**, e não um resgate silencioso de
zero — a mesma resposta que o cupom dá em `lock_and_validate_for_order`. As
duas mecânicas de desconto não podem divergir em nada que o app precise
explicar.

**Saldo abaixo do mínimo, loja sem campanha e teto zerado devolvem zero sem
erro nenhum.** Nada disso é culpa de quem está pedindo, e o valor efetivo volta
na resposta em `cashback_redeemed_amount`.

**O lock.** `CustomerRepository.lock_customer` (`SELECT ... FOR UPDATE` na
linha do cliente) já existe e nunca foi chamado. O resgate o chama **antes de
ler o saldo**, dentro da transação do pedido. Dois pedidos simultâneos do
mesmo cliente: o segundo trava até o primeiro commitar e então lê o saldo já
reduzido. O saldo mostrado no checkout nunca é fonte de verdade.

**A ordem dos locks não é livre: cliente primeiro, cupom depois, em todo
caminho.** Duas transações pegando os mesmos dois recursos em ordens opostas
fecham ciclo, e o Postgres mata uma por deadlock — no checkout, no pico.

**Campo novo em `CreateOrderRequest` custa 24h de 422 (armadilha 37).**
`use_cashback` muda o que foi pedido, então fica **dentro** do
`_idempotency_fingerprint`, ao contrário do `source`. Deploy de madrugada, e o
app precisa tratar a recusa gerando chave nova.

**O código é 422, e não 409** — a armadilha 37 dizia 409 e estava errada nisso
(o 409 daquela rota é "requisição em andamento", que quer o oposto: tentar de
novo com a MESMA chave). O corpo é
`{"detail": "Idempotency-Key ja utilizada com um corpo diferente. Gere uma nova chave para uma nova requisicao."}`.

Do lado da resposta não há custo: `cashback_redeemed_amount` já existe em
`CreateOrderResponse` e em `OrderDetailResponse`, e a comanda já imprime a
linha (`print_layout.py`). Eles só param de sair zero.

---

## 3. Expiração: o relógio é o ÚLTIMO PEDIDO

**A validade conta a partir do último pedido, não da data do crédito.** É a
peça que simplifica todo o resto: não existe expiração por lote, não existe
FIFO, não existe partir crédito ao meio. Existe **um relógio por (cliente,
restaurante)**, que todo pedido reinicia, e quando ele vence **o saldo inteiro
vira zero de uma vez**.

- **Quem roda:** script próprio (`scripts/expire_cashback.py`), no container
  `limpeza` que já existe — mesma imagem, mesmo laço diário, mesmo retry. Os
  dois scripts são encadeados com `&&`; a limpeza é idempotente, então o
  retry de 5 min repeti-la é inofensivo.
- **Por que não dentro do `cleanup_idempotency_keys.py`:** aquele arquivo tem
  um contrato explícito no docstring — "apagar linha vencida não afeta
  correção". Aqui afeta: um bug apaga dinheiro de cliente. Arquivo próprio,
  `--dry-run` próprio, log próprio.
- **O que faz:** as linhas `available` daquele par viram `expired`, e entra
  UMA linha `type="expired"` com o total negativo, para o extrato ficar
  legível.

### O que a escrita da expiração obrigou a decidir

- **Trava o cliente (`lock_customer`) e RECONFERE o vencimento já travado.**
  A lista de candidatos é lida sem lock; entre lê-la e agir, a pessoa pode ter
  feito um pedido — e pedido empurra a validade. Sem a segunda leitura, quem
  pediu no segundo errado perde o saldo inteiro. É o mesmo lock do resgate, e
  ele também impede que a varredura marque as linhas como `expired` no
  instante em que o checkout já leu o saldo e vai gravar o débito.
- **A linha negativa nasce com `status="expired"`, não `available`.** Com
  `available` ela seria a única linha na soma, e o saldo do cliente ficaria
  **negativo** — uma dívida que ele descobriria no próximo pedido.
- **Ela NÃO leva `idempotency_key`, ao contrário das outras três escritas.**
  Lá a chave é o pedido, e o mesmo pedido pode voltar. Aqui o evento não tem
  chave natural que se repita: o que impede a segunda gravação é não haver
  mais saldo `available` para vencer. Uma chave derivada da data do
  vencimento faria pior — saldo lançado a mão depois (o `adjustment`, que só
  entra por SQL) ficaria preso em `available` para sempre, sem erro em lugar
  nenhum.
- **Commit por cliente**, e não um no fim: interrompido no meio, o que já
  venceu está gravado, e nenhum lock fica de pé pela duração da varredura —
  o que travaria o checkout dessas pessoas.
- **Campanha desligada CONGELA o saldo em vez de vencê-lo**, e a tela
  concorda: com a regra desligada ela responde `expires_at: null`. Vencer o
  que a loja já prometeu porque ela saiu da campanha seria a plataforma
  apagando dinheiro que não é dela.
- **A conta do vencimento é uma só** (`expires_at_from_last_order`), chamada
  pela tela e pela varredura. Duas contas discordariam no dia em que uma
  fosse ajustada, e a divergência seria saldo apagado antes da data que o app
  mostrou.
- **"Último pedido"** = o último pedido daquele cliente naquele restaurante
  que chegou a `KITCHEN_ORDER_STATUSES` — a constante que já existe em
  `order_state_machine.py`. Não se cria uma quarta definição de "quais
  pedidos".

**O que o cliente vê:** `GET /customers/me/cashback` ganha `expires_at`, e o
app mostra "seu saldo expira em 12/09". O ponto do mecanismo é que **essa data
anda para frente a cada pedido** — se o app não mostrar, o desenho perde
metade do valor.

O `expires_at` **por transação**, que já existe no schema de leitura, fica
sempre nulo: a validade é do saldo, não da linha.

---

## 4. Por filial ou por restaurante

**Configuração: padrão do restaurante, sobrescrita por filial.** O percentual
por dia existe para mover o dia fraco, e o dia fraco de uma filial não é o da
outra. O regime é o de termo comercial da revisão `20260818_0025`, com uma
diferença: **a herança é por LINHA, não por coluna** — a filial tem a regra
inteira ou herda a inteira. O motivo está no model e na revisão `0032`: o
percentual por dia mora numa tabela filha, e "coluna nula" não existe numa
tabela filha.

**Saldo: do restaurante inteiro.** Acumulou na Matriz, gasta na Varjota. Saldo
presos por loja é o oposto do cashback concentrado.

**Mas do restaurante, não da plataforma.** Cashback do Júnior gasto em outro
restaurante seria o Júnior pagando o marketing do concorrente, e não existe
mecanismo de compensação entre eles. O saldo é dinheiro de quem o concedeu.

### O que isso obriga na API — feito em 22/08/2026

**A v1 não sai com o total somado.** Publicar "R$ 40" com R$ 5 gastáveis
naquela loja é criar a reclamação com as próprias mãos. Como hoje só existe um
restaurante, a divergência ainda não existe — e foi por isso que saiu barato
acertar agora. A tela do app muda junto com o backend.

`GET /customers/me/cashback` ficou assim:

```json
{
  "balance": 42.50,
  "currency": "BRL",
  "by_restaurant": [
    {
      "restaurant_id": "…",
      "restaurant_name": "Júnior da Picanha",
      "restaurant_slug": "junior-da-picanha",
      "balance": 40.00,
      "expires_at": "2026-10-23T12:00:00Z"
    }
  ]
}
```

**`balance` continua sendo o acumulado, e continua não sendo gastável.** Ele
fica por dois motivos: tirá-lo quebraria o app que já o consome, e ele é a
resposta certa para a única pergunta que a soma responde — quanto a pessoa
perde ao excluir a conta (seção 5). A tela mostra a lista; o total, se
aparecer, é "acumulado", nunca "disponível para usar".

Três decisões que foram tomadas aqui e não estavam no desenho original:

- **`restaurant_slug` vai junto.** É por ele que o app chega no cardápio. Sem
  ele a tela mostra um saldo sem botão para gastá-lo.
- **`expires_at` sai da regra do RESTAURANTE (`branch_id IS NULL`), nunca da
  filial.** O saldo é do restaurante inteiro; duas filiais com `expiry_days`
  diferentes dariam duas respostas para "quando este saldo vence", e o saldo é
  um só. Restaurante sem regra própria (ou com ela desligada) responde
  `expires_at: null` — "não vence". Apagar dinheiro por ausência de
  configuração é o lado errado do erro, e é o mesmo critério pelo qual falta de
  regra também não gera cashback.
- **`min_redeem_balance` NÃO entra na lista.** Ele é termo do resgate, e o
  resgate acontece numa FILIAL, que pode ter regra própria — publicá-lo sob uma
  chave por restaurante mostraria um piso que não vale na loja onde a pessoa
  está pedindo. A linha é essa: `by_restaurant[]` carrega o que é verdade por
  restaurante (saldo, validade), e nada que seja verdade por filial.

**O que fica pendente por causa dessa última decisão:** o checkout não tem como
explicar "por que não descontou". Saldo abaixo do mínimo devolve zero sem erro
(seção 2), e o app só descobre isso lendo `cashback_redeemed_amount = 0` na
resposta do pedido. O lugar de resolver, quando doer, é uma resposta de termos
por filial ao lado do cardápio — que é quem conhece a filial —, e não este
campo aqui.

Saldo órfão (`restaurant_id` nulo, de restaurante apagado — a coluna é
`ON DELETE SET NULL`) conta no total e fica de fora da lista: não tem nome para
mostrar nem cardápio onde gastar. Continua aparecendo no extrato.

---

## 5. O saldo na exclusão de conta

**Quem exclui a conta perde o saldo, e isso continua de pé.** O código já está
escrito para isso: `CustomerAnonymizationService` lê o saldo antes de
anonimizar, não toca em `cashback_transactions`, e loga
`[LGPD] conta anonimizada com saldo de cashback perdido` só quando há saldo.

O motivo está inteiro no docstring de `_log_forfeited_cashback`: o recadastro
nasce com **id novo** — e o que libera o e-mail e o telefone é justamente a
saída deles da tabela —, então o razão continua ligado ao id velho e não há
caminho de volta.

O `DELETE` continua impossível (`coupon_redemptions.customer_id` é NOT NULL
sem `ON DELETE`), e o `CASCADE` do razão é argumento **contra** o DELETE.

**Com o resgate de pé, o aviso deixou de ser hipotético.** Chamar
`GET /customers/me/cashback` na tela de confirmação da exclusão é requisito de
produto, e o `logger.warning` é um grep que devolve dinheiro real.

O número a mostrar é o `balance` — o acumulado em todos os restaurantes é
exatamente o que se perde aqui, e é o mesmo `get_available_balance` que a
anonimização lê. O `by_restaurant[]` serve para nomear as lojas no aviso ("R$
40 no Júnior da Picanha"), o que torna a perda concreta em vez de abstrata.

---

## 6. O que NÃO entra na primeira versão

| Fora | Por quê |
|---|---|
| Exceção por produto | quebra a identidade "base do cashback = base da comissão" e vira apuração por item, com opções e adicionais dentro |
| "Produto em promoção não gera" | **promoção não existe neste schema.** `products` tem `price` e mais nada — não é um botão, é uma frente de cardápio |
| Valor de resgate escolhido pelo cliente | `use_cashback: true` basta, e não põe preço na mão do cliente |
| Crédito `pending` ("a liberar") | dobra os caminhos de escrita; volta se a medição do `completed` mostrar demora |
| Ajuste manual pelo painel | `type="adjustment"` fica só por SQL: um botão de creditar dinheiro sem trilha de aprovação |
| Saldo entre restaurantes | quem concede é quem paga |
| Push de "seu saldo vai expirar" | o `expires_at` na resposta já permite o aviso na tela |
| Teto por pedido em percentual | o subtotal já é o teto natural |

---

## 7. O estado que já existia em produção, e o que a revisão 0032 faz com ele

Conferido no banco vivo em 22/08/2026. A tabela nunca teve escritor, e o que
estava errado era **schema**, não saldo:

| O que | Estado encontrado | O que a `0032` faz |
|---|---|---|
| Trigger de imutabilidade do rascunho de 03/07 | **não existe** (foi removido pelo `.sql` de 04/07) | nada — confirmado que não derruba o checkout |
| `ck_cashback_transactions_type` / `_status` | **não existem** — dava para gravar `type='banana'`, e a leitura do extrato morreria com `KeyError` | cria os dois |
| Índices | `idx_cashback_transactions_*`, e o model declara `ix_*` | adota: DROP do nome antigo, CREATE do canônico (armadilha 4) |
| Linhas no razão | **zero** | nada a limpar; é o que torna a criação dos CHECK segura |
| `orders.cashback_redeemed_amount` | zerado e coerente em todo lugar | nada |

**O "Em breve" do painel e o saldo na tela do app não estavam mentindo sobre
um número errado — estavam mostrando um zero honesto.** O que existia de
inconsistente era schema, e só viraria dano no minuto da primeira escrita.
