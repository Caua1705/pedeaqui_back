# Pagamentos e comissão

Tudo que envolve dinheiro entrando. Leia antes de encostar em
`services/payment_service.py`, `integrations/payment_gateway.py` ou no cálculo de
comissão em `services/order_service.py`.

---

## 1. Os dois caminhos do dinheiro

Qual caminho o pedido segue é decidido **na criação**, por
`OrderService._resolve_payment_flow`, a partir do `payment_flow` da forma de
pagamento habilitada **naquela filial** (`branch_payment_methods`).

```
pagamento na entrega   (payment_flow = "delivery")
    pedido nasce  status=pending, payment_status=on_delivery
    o lojista já pode aceitar

pagamento online       (payment_flow = "online")
    pedido nasce  status=pending, payment_status=pending
    POST /restaurants/{slug}/orders/{tracking_token}/payment  → cria a cobrança
    gateway confirma → POST /payments/webhooks/{provider}     → payment_status=paid
    só então o lojista consegue aceitar
```

Quando a filial oferece o **mesmo método nos dois fluxos** (pix pelo gateway e
pix na entrega), vale `online` — o caminho mais restritivo. Não há campo no
pedido para o cliente escolher, e errar para o lado restritivo não entrega comida
de graça.

A regra central é `ensure_payment_allows_order_status`
(`services/order_state_machine.py`): nenhum pedido entra em `accepted`,
`preparing`, `ready`, `out_for_delivery` ou `completed` com pagamento online não
confirmado.

**Pagar não aceita o pedido automaticamente.** O webhook grava
`payment_status="paid"` e registra no histórico; aceitar continua sendo decisão
do lojista. Pagar não pode obrigar o restaurante a produzir.

---

## 2. Os dois providers

`integrations/payment_gateway.py` implementa dois providers atrás das mesmas três
funções (`create_payment`, `verify_webhook_signature`, `parse_webhook_event`).
Nada fora desse arquivo sabe qual está ativo.

- **`sandbox`** — implementação de verdade, sem chamada externa. Cria a cobrança
  localmente e valida o webhook por HMAC-SHA256 do corpo cru com
  `PAYMENT_WEBHOOK_SECRET`. Permite exercitar o fluxo inteiro sem depender do
  Mercado Pago responder. É o padrão.
- **`mercadopago`** — chama a API de Pagamentos v1 de verdade, Checkout
  Transparente, com **pix e cartão de crédito**.

**O sandbox recusa cartão de propósito.** Ele ignorava `payment_method`
inteiro — `_create_sandbox_payment` só recebia o `order_id` —, e com cartão
isso significaria intent válido, webhook marcando como pago e **comanda
imprimindo**: o fluxo inteiro parecendo funcionar sem dinheiro nenhum ter
existido. Cartão só se testa contra a credencial de teste do Mercado Pago.

---

## 2.1 Pix e cartão não são o mesmo fluxo com outro nome

As três diferenças que decidem o desenho inteiro:

| | pix | cartão |
|---|---|---|
| veredito | **assíncrono** — nasce `pending`, o webhook confirma | **síncrono** — o próprio `POST /v1/payments` responde |
| antifraude | não passa | pode segurar em análise, **até 48h úteis** |
| dado do cliente | só `payer.email` | token gerado no navegador; o PAN nunca chega aqui |

**A primeira é a que mais custou.** `create_payment` descartava o `status` da
resposta, o que era inofensivo no pix (a cobrança nasce sempre `pending`) e
fatal no cartão: `rejected` dentro de um HTTP 201 não é exceção, e o pedido
ficaria `pending` para sempre — sem nunca alcançar o caminho de nova
tentativa, que só existe a partir de `failed`. Hoje `PaymentIntent` carrega
`payment_status`, e `StartPaymentResponse` o devolve ao front.

**A segunda criou um estado.** `in_review` (o `in_process` deles) **não libera
o pedido**, exatamente como `pending`. Existe separado porque as duas esperas
pedem conversas opostas com o cliente:

```
pending     "o cliente ainda não pagou"    -> ligar para o cliente
in_review   "o gateway está analisando"    -> não há o que fazer, esperar
```

Com um estado só, o lojista lia "AGUARDANDO PAGAMENTO" nos dois casos e não
tinha como saber qual ligação fazer. A comanda também os distingue
(`PAYMENT_STATUS_LABELS`).

**A terceira exige login.** Ver seção 3.

---

## 2.2 O contrato do FRONT para cartão

1. **`GET /restaurants/{slug}/payment-config`** — devolve `provider`,
   `public_key` e `card_enabled`. A chave pública é o único dado do gateway
   que o próprio Mercado Pago manda expor no frontend, e é por isso que é a
   única coluna de `restaurant_payment_credentials` em texto puro.

   **`card_enabled` existe para o front não descobrir no 503.** Ele é falso
   quando o restaurante não tem credencial para o ambiente ativo, ou quando o
   provider ativo não processa cartão (o sandbox não processa). Sem ele, o
   cliente digitaria o cartão inteiro para só então ouvir que não dá.
2. Carregar o `MercadoPago.js v2` e inicializar com ela.
3. **O número do cartão nunca toca em input nosso**: os campos são os Secure
   Fields / CardForm deles, que são iframes do Mercado Pago. É isso que mantém
   o PAN fora do nosso perímetro de PCI.
4. Tokenizar no navegador. O token é de **uso único** e vida curta.
5. `POST /restaurants/{slug}/orders/{tracking_token}/payment` **com corpo**:
   `card.token`, `card.payment_method_id` (a bandeira que o SDK resolveu —
   `"master"`, não `"credit_card"`), `card.issuer_id`,
   `card.payer_document_type`/`payer_document_number` (CPF). O corpo é
   **opcional**: pix continua sendo um POST sem corpo, e quem já integrou não
   muda nada.
6. **Mandar o `Authorization` do cliente logado.** Cartão sem ele responde
   `401 login_required`.
7. **Tratar três desfechos, não dois**: `paid`, `failed`, `in_review`. Recusa
   precisa de texto por motivo — `status_detail` separa
   `cc_rejected_insufficient_amount` (tentar outro cartão) de
   `cc_rejected_bad_filled_security_code` (redigitar o CVV).
8. Nada de guardar token, PAN ou CVV — nem em localStorage, nem em log, nem em
   telemetria.

Trocar de um para o outro é só `PAYMENT_PROVIDER` no `.env`.

---

## 3. Criar a cobrança (Mercado Pago)

`POST /v1/payments` com:

```
Authorization: Bearer {access_token}   credencial do RESTAURANTE do pedido, nunca global
X-Idempotency-Key: {order_id}:{hash}   uma chave por TENTATIVA, ver abaixo
transaction_amount, description
payment_method_id: "pix"
payer.email                            ver abaixo
application_fee                        só no corpo se != None (split; hoje sempre None)
```

### A chave de idempotência é por tentativa, não por pedido

O Mercado Pago devolve a **mesma** cobrança quando a mesma chave chega de novo —
é o que protege um retry (timeout na ida, cliente clicando duas vezes) de virar
duas cobranças pix abertas para o mesmo pedido.

A chave era `str(order_id)`, constante para sempre, e isso tratava como "a mesma
cobrança" duas coisas que não são:

- **retry depois de uma cobrança recusada** — a chave antiga devolve a própria
  cobrança recusada, e o pedido nunca mais teria como ser pago;
- **tentativa com o corpo diferente** — o total mudou, ou o cliente fez login e o
  `payer.email` deixou de ser o sintético de convidado. Mesma chave com corpo
  diferente é **conflito** de idempotência, não retry, e conflito eles respondem
  com erro.

Hoje a chave é `{order_id}:{sha256(corpo + cobrança substituída)}`
(`_mercadopago_idempotency_key`). O `order_id` fica no começo para a tentativa
continuar achável no painel de Atividade deles a partir do pedido.

Quem informa a cobrança substituída é `PaymentService.start_online_payment`, e
**só** quando o pagamento está `failed`:

```
payment_status = "pending"   previous_payment_id = None      → chave se REPETE
                                                                (2º clique devolve o mesmo pix)
payment_status = "failed"    previous_payment_id = o recusado → chave MUDA
                                                                (cobrança nova; a recusada não volta)
```

O que a mudança **não** faz: cancelar a cobrança antiga do lado deles quando o
corpo muda. Nasce uma cobrança nova e o QR antigo fica em aberto até expirar.

### `payer.email` — e por que **cartão exige login**

Exigido pela API deles, e o pedido **não guarda e-mail nenhum** (só nome e
telefone). `PaymentService._resolve_payer_email` resolve nesta ordem: quem está
pagando **agora** (o cliente autenticado da requisição), depois o dono do
pedido em `customers`, e por último um sintético
(`pedido-{order_number}@pederapidex.com`).

A primeira posição não é detalhe: um pedido pode ter nascido de convidado e a
pessoa ter entrado na conta antes de pagar — e aí o e-mail de verdade existe
mesmo com `orders.customer_id` nulo.

**No pix o sintético é inócuo; no cartão ele é caro.** O Mercado Pago valida a
identidade do pagador no cartão — em teste, um endereço que não corresponde a
ninguém rendeu recusa direta (`2034 Invalid users involved`), com um código que
não menciona e-mail e manda quem depura olhar para o cartão. E mesmo quando
passa, ele entra na análise antifraude como sinal fraco e derruba aprovação.

Por isso `_resolve_card_input` recusa cartão sem cliente autenticado, com
`401 login_required`, **antes de falar com o gateway**. Convidado continua
pagando por pix ou na entrega.

### O corpo do cartão

Além do que o pix manda: `token` (do navegador), `payment_method_id` com a
**bandeira**, `installments`, `issuer_id` e `payer.identification`.

```
capture: true          captura automática
binary_mode: false     NÃO mudar para true
installments: 1        parcelamento fora da v1 — ver seção 8
```

`binary_mode=true` forçaria approved/rejected e nada no meio, o que é tentador
para matar o "em análise" — e **mataria o desafio 3DS junto**, fechando a porta
da melhoria descrita na seção 8. Trocar uma análise de 30 minutos por uma
recusa imediata também recusa pedidos que seriam aprovados.

Da resposta, `qr_code` vem de `point_of_interaction.transaction_data.qr_code` (o
"copia e cola") e `checkout_url` de `.ticket_url` (página hospedada com o QR em
imagem). `qr_code_base64` não foi implementado — o front redireciona para
`ticket_url`.

---

## 4. Receber o webhook

Duas coisas não óbvias, as duas resolvidas pela ordem de operações em
`PaymentService.handle_webhook`:

- **o corpo do webhook não traz o status**, só avisa "o pagamento X mudou"
  (`data.id`). O status confiável exige `GET /v1/payments/{data.id}`, autenticado
  com a credencial do restaurante **dono** do pagamento — e o webhook não diz de
  qual restaurante é.
- **a assinatura também é por restaurante.** Não existe uma
  `MERCADOPAGO_WEBHOOK_SECRET` global. Só dá para saber qual segredo usar depois
  de saber de qual restaurante é o pagamento — o mesmo dado que falta para o GET.

Daí a ordem:

```
1. extract_provider_payment_id            só lê data.id. Nenhuma chamada externa nem ao banco.
2. get_order_by_provider_payment          SELECT indexado e local; acha o pedido (e o restaurante).
                                          Sem pedido → ignored/unknown_payment, PARA AQUI.
3. get_active_credential(restaurant_id)   uma consulta só: dá o segredo e o access_token.
4. verify_webhook_signature               SÓ AGORA confere a assinatura (401 se inválida).
5. parse_webhook_event                    SÓ com assinatura válida faz o GET com o token certo.
```

**Por que essa ordem não abre brecha.** A ordem antiga (assinatura antes de tudo)
existia para não gastar uma consulta forjada na API do Mercado Pago — o passo 5,
a única chamada de rede paga. Essa garantia continua: o passo 5 só roda depois da
assinatura verificada. Os passos 1 e 2 são leitura local e barata; o pior que um
corpo forjado com um `data.id` chutado provoca é um SELECT que não acha nada.

### A assinatura

Header `x-signature: ts=...,v1=...` mais `x-request-id`. Manifest:

```
f"id:{data_id};request-id:{x_request_id};ts:{ts};"     (id em minúsculas)
```

`hmac_sha256` com o `webhook_secret` do **restaurante**, comparado com
`hmac.compare_digest`. `ts` mais velho que **300 segundos** é recusado — sem isso
uma notificação capturada hoje serviria para replay amanhã. Corpo malformado
durante a verificação vira `False` (401), nunca exceção.

### Tradução de status

```
approved                → paid
in_process              → in_review
rejected, cancelled     → failed
refunded, charged_back  → refunded
```

`pending`, `authorized` e **qualquer status novo que eles venham a inventar**
não aplicam mudança nenhuma. O pagamento ainda não tem veredito, e é o próprio
gateway quem manda novo webhook quando isso mudar.

**São duas tabelas de tradução, não uma**, e a diferença está no `pending`: no
webhook ele não produz mudança (o pedido já está `pending`, e traduzi-lo seria
uma transição de X para X); na **criação** ele é o desfecho normal do pix e
precisa virar o estado inicial. Status desconhecido na criação cai em `pending`
— a queda segura: o pedido continua cobrável, o lojista continua bloqueado, e o
webhook corrige. Qualquer outra escolha erra para o lado de liberar comida.

### Estorno parcial não muda status nenhum

No Mercado Pago, **devolver parte do valor mantém o pagamento em `approved`**.
Não há status novo, não há notificação diferente: o único sinal é
`transaction_amount_refunded` na consulta.

Antes disto o webhook chegava, traduzia para `paid`, via que o pedido já estava
`paid` e retornava `already_applied` — **sem sequer logar**. Dinheiro voltava
ao cliente e não existia em lugar nenhum do nosso lado. Com pix quase não
acontece; com cartão é o caso comum.

Hoje `PaymentWebhookEvent` carrega `refunded_amount`, e `_apply_partial_refund`
grava em `orders.refunded_amount`, escreve `payment:partially_refunded` no
histórico e loga. Ele compara com o que já está gravado, o que o torna
idempotente de graça: o valor é o **total** devolvido, não um incremento, então
reaplicar a mesma notificação não é um segundo estorno.

### Política de resposta

Quase tudo que não é "assinatura inválida" responde **2xx**. Erro 5xx faz o
gateway reenviar em backoff por horas, e reenviar não conserta corpo malformado
nem pagamento que não existe aqui. O que precisa de atenção humana vai para o log
como warning.

Idempotência pelo **id do evento** (`idempotency_keys`), porque gateways
reenviam a mesma notificação até receber 2xx.

---

## 5. Erros do gateway

| Situação | Exceção | Como o `PaymentService` reage |
|---|---|---|
| timeout / rede / 5xx | `PaymentGatewayUnavailableError` | 503 `gateway_unavailable` (criar) / 503 e o gateway reenvia (webhook) |
| 401/403 token inválido | `PaymentGatewayCredentialError` | 503 `payment_unavailable` |
| 404 payment_id não existe lá | `PaymentNotFoundError` | 502 (criar) / `ignored, payment_not_found` (webhook) |
| 400/422 recusa da requisição | `PaymentGatewayError` | 502 `payment_rejected` |
| resposta não é JSON | `PaymentGatewayUnavailableError` | 503 `gateway_unavailable` |

### O que o cliente recebe

O `detail` da rota de criar cobrança é um **objeto**, não a string dos outros
erros da API:

```json
{"detail": {"code": "gateway_unavailable",
            "message": "Não foi possível gerar o pagamento agora. Tente de novo em alguns instantes.",
            "retryable": true,
            "provider_error_code": null}}
```

| `code` | HTTP | `retryable` | Quando |
|---|---|---|---|
| `gateway_unavailable` | 503 | `true` | instabilidade passageira deles — vale tentar de novo |
| `payment_unavailable` | 503 | `false` | indisponível para **este restaurante**: sem credencial, credencial recusada, método não suportado. Quem resolve é o lojista |
| `payment_rejected` | 502 | `false` | eles entenderam e **recusaram** (dado inválido, conflito de idempotência) |

502 no recusado é proposital: 503 diz "volte depois", e voltar depois com a mesma
cobrança dá no mesmo.

Os três códigos vivem em `PaymentErrorCode` (`schemas/payment_schema.py`), um
`str, Enum` e não três constantes soltas, porque a **lista** precisa sair no
`/openapi.json` — com só o `retryable`, o frontend cai no mesmo texto genérico
para `payment_unavailable` e `payment_rejected`, que pedem coisas diferentes do
cliente.

O `model` declarado nas respostas 502/503 é `PaymentErrorResponse`, o envelope
`{"detail": {...}}`, e **não** `PaymentErrorDetail`: o `HTTPException` embrulha
tudo em `detail`, e anunciar o detail na raiz faria o frontend escrever o parser
contra um formato que a rota nunca devolve.
`tests/test_new_route_contracts.py::PaymentErrorContractTests` trava isso no
documento gerado, não no código.

`provider_error_code` é o código do catálogo deles (`"bad_request"`, `"2062"`)
para citar num chamado de suporte — nunca a mensagem crua, que pode ecoar o
e-mail de quem pagou.

---

## 6. Credencial por restaurante

Não existe credencial global do Mercado Pago. Cada restaurante tem a própria
conta lá, e a cobrança do pedido dele sai em nome dessa conta.

`restaurant_payment_credentials`, uma linha por `(restaurant_id, environment)`:

```
public_key                 texto puro     — o próprio Mercado Pago manda expor no frontend
access_token_encrypted     cifrado        — credencial da CONTA DO RESTAURANTE, nunca em log
webhook_secret_encrypted   cifrado, NULLABLE — "Assinatura secreta" do painel deles
environment                'test' | 'production'
```

Cifrados com Fernet (`utils/crypto.py`), chave em
`PAYMENT_CREDENTIALS_ENCRYPTION_KEY`, só no `.env` — cifrar a coluna não protege
nada se a chave mora ao lado dela. **Perder essa chave torna toda credencial
cadastrada ilegível**; trocá-la exige plano de recadastro em mãos.

`startup_checks.py` derruba o boot se `PAYMENT_PROVIDER=mercadopago` e a chave
estiver vazia.

`webhook_secret_encrypted` é NULL até o restaurante cadastrar a Notification URL
no painel do Mercado Pago e receber a Assinatura secreta — um passo que pode
acontecer depois do `access_token`. Enquanto estiver NULL, o webhook **daquele
restaurante** responde 503.

Teste e produção **coexistem** no banco. Qual vale para todo mundo é a variável
global `MERCADOPAGO_ENVIRONMENT` (`test` por padrão) — todo mundo migra junto no
dia do go-live.

### Cadastrar

Não há tela. O script pede os segredos em prompt oculto, nunca argumento de linha
de comando:

```bash
python scripts/register_restaurant_payment_credential.py \
    --restaurant-slug junior-da-picanha \
    --environment test \
    --public-key TEST-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

Pede `access_token` (obrigatório) e depois `webhook_secret` (opcional — Enter em
branco pula e **não mexe** no que já estava cadastrado). Rodar de novo para o
mesmo `(restaurant, environment)` substitui a credencial, que é como se troca um
token vazado.

Para atualizar **só** o segredo do webhook, sem re-informar `access_token`:

```bash
python scripts/register_restaurant_payment_credential.py \
    --restaurant-slug junior-da-picanha \
    --environment test \
    --webhook-secret-only
```

---

## 7. Comissão da plataforma

Gravada **no pedido, na criação** (`OrderService._calculate_commission`), não
apurada no fechamento do mês.

```
base    = subtotal − desconto de cupom SOBRE PRODUTO − cashback usado
percent = restaurant_settings.platform_commission_percent   (por restaurante)
valor   = base × percent / 100                              (2 casas, HALF_UP)
```

Ficam **fora** da base:

- **taxa de entrega** — é do entregador/da filial, não é receita da venda;
- **taxa de serviço** — repasse;
- **taxa do gateway** — custo de quem recebe.

O desconto entra como subtração porque o restaurante recebeu menos: cobrar
comissão sobre um valor que ninguém pagou seria cobrar sobre o próprio desconto.

### Cupom de frete grátis NÃO reduz a base

É o que `_coupon_discount_on_products` separa, e a distinção não é sutileza
contábil: o desconto de um cupom `free_delivery` vale exatamente a **taxa de
entrega**, que já está fora da base. Subtraí-lo tirava comissão de mercadoria
que o cliente pagou integralmente.

E ficava incoerente com o frete grátis **por regra** (`free_delivery_enabled`,
acima de um valor), que zera a mesma taxa e nunca mexeu na base: eram dois
caminhos para o mesmo frete grátis cobrando comissões diferentes pelo mesmo
pedido.

Cupom `fixed` e `percent` continuam reduzindo a base inteira — esses derrubam o
preço do produto.

A base é limitada a zero de qualquer forma. Hoje nenhum caminho chega lá (o
cupom de valor já é limitado ao subtotal, e o frete grátis saiu da conta), mas
base negativa seria a plataforma pagando o restaurante, e o piso fica.

Restaurante sem linha em `restaurant_settings` (ou com o campo nulo) cai no
padrão `DEFAULT_PLATFORM_COMMISSION_PERCENT` = `"10.00"`. Devolver zero aqui
seria abrir mão da receita em silêncio.

**São três colunas** (`commission_percent`, `commission_base_amount`,
`commission_amount`) porque o valor sozinho não é conferível: com base e
percentual gravados, o lojista refaz a conta de cada linha do extrato.

**Congelar é o ponto.** Se fosse calculado no fim do mês, mudar o percentual do
restaurante hoje reescreveria a comissão de pedidos de três semanas atrás.

`GET /admin/reports/commission?start_date&end_date` só **soma** o que está
gravado. Cancelados, recusados e estornados não entram, e quantos ficaram de fora
vem em `excluded_orders_count` — sem esse número, a diferença para o painel
parece bug. As datas são lidas em `America/Fortaleza` e o fim é exclusivo no dia
seguinte, para não perder pedido das 23:59.

### Estorno parcial NÃO reduz a comissão — e isso é decisão, não pendência

`billable_order_conditions` continua olhando só `payment_status != 'refunded'`,
que é tudo-ou-nada. Um pedido com estorno parcial permanece **100% faturável**,
com as três colunas `commission_*` congeladas no valor cheio.

A regra: **a plataforma cobra sobre a venda que aconteceu.** Se o lojista
devolveu R$ 20 de um pedido de R$ 100 por um erro dele, o custo é dele.

O binário `refunded` erraria nas duas direções, e vale saber por quê antes de
"consertar": mapear estorno parcial para `refunded` tiraria o pedido **inteiro**
do extrato, e a plataforma deixaria de cobrar sobre a parte que o cliente de
fato pagou.

**Então por que `orders.refunded_amount` existe?** Porque a decisão contrária
(comissão proporcional ao que sobrou) é plausível, e ela é **irrecuperável para
trás**: o valor estornado só existe do lado do Mercado Pago, e não há como
reconstituí-lo depois para pedidos antigos. A coluna custou uma migração e
compra a possibilidade de mudar de ideia. Não gravá-la fecharia a porta em
silêncio.

Se um dia a decisão virar, o que muda é o **filtro do extrato**, nunca as
colunas congeladas — elas são o registro do que foi acordado no dia do pedido.

---

## 8. O que ficou de fora, de propósito

1. **Só pix e crédito passam pelo Mercado Pago.** Método não suportado responde
   503 (`payment_unavailable`) — falhar alto é proposital, um retorno plausível
   deixaria pedidos pendentes para sempre.

   **`debit_card` fica de fora**, e é o que mais parece vir de graça junto com
   o crédito: no Mercado Pago ele tem fluxo próprio (débito virtual) e 3DS
   obrigatório de fato.

   **Parcelamento fica de fora** (`CARD_INSTALLMENTS = 1`). A taxa que o
   restaurante contratou — 3,98%, prazo de 30 dias — é a de **cartão à vista**;
   parcelado sem juros sai do bolso de quem recebe, e habilitar parcelas sem
   essa decisão comercial mudaria o que o lojista recebe sem ele ter pedido.
   Quando entrar, `installments` vem do front no POST do pagamento — **nunca**
   em `CreateOrderRequest`, que custaria 24h de 422 (armadilha 37).

   **3DS 2.0 fica de fora**, e a premissa importa: o desafio **não** é imposto
   pelo emissor, é opt-in nosso via `three_d_secure_mode: "optional"` no corpo.
   Sem o campo, `pending_challenge` não existe e não há pedido pendurado. Os
   dois ganhos de implementá-lo depois: **aprovação maior** e **a
   responsabilidade por chargeback passa para o emissor** na transação
   autenticada (hoje ela é do restaurante, porque a conta é dele). Quando for
   feito: o front monta um iframe com POST de `creq` para
   `external_resource_url`, e os cartões de teste deles são
   `5483 9281 6457 4623` (sucesso) e `5361 9568 0611 7557` (falha), CVV 123,
   validade 11/30.
2. **Nada aqui estorna sozinho.** O estorno só acontece quando o gateway avisa
   (webhook `refunded`) ou quando alguém o faz no painel do próprio gateway.
   Dinheiro de cliente parado tem **dois** logs, e qual sai depende da ordem dos
   eventos:

   | Ordem | Onde | Linha |
   |---|---|---|
   | pagou → lojista cancelou | `AdminOrderService._log_cancellation_of_paid_order` | `pedido pago foi cancelled sem estorno automatico` |
   | lojista recusou → pagamento entrou | `PaymentService._log_payment_on_terminal_order` | `pagamento confirmado em pedido ja rejected sem estorno automatico` |

   A segunda é a corrida mais provável e ficou sem rastro nenhum até 23/08/2026:
   `handle_webhook` valida só a transição de **pagamento**, e `pending → paid` é
   válida qualquer que seja o `orders.status`. A escrita acontece de propósito —
   recusá-la esconderia o dinheiro que de fato entrou. E o extrato não denuncia,
   porque `cancelled`/`rejected` já estão fora da comissão.

   O grep cobre as duas: `sem estorno automatico`.
3. **Pagamento recusado não cancela o pedido.** Ele fica `status=pending` com
   `payment_status=failed`, visível para o lojista cancelar, e o cliente pode
   gerar nova cobrança para o mesmo pedido (`failed → pending`).
4. **Não há rota para trocar a forma de pagamento de um pedido já criado.** Ver
   abaixo.

Um pedido **estornado** não consegue avançar de status, porque `refunded` não
está em `PAYMENT_STATUSES_THAT_RELEASE_ORDER`. É intencional: pedido cujo
dinheiro voltou não deve continuar na cozinha.

### Trocar a forma de pagamento — pendência com desenho decidido

`payment_flow` é derivado na criação e nunca muda; `on_delivery` não tem nenhuma
aresta de **entrada**. Um pedido online é online para sempre.

**Por que não é urgente:** retentar o pix já funciona (`failed → pending`);
trocar pix por outro método online é inócuo enquanto só pix estiver implementado;
sobra online → pagar na entrega, que é raro e tem contorno pelo lojista.

**O que decide o desenho, quando for feito.** `on_delivery` está em
`PAYMENT_STATUSES_THAT_RELEASE_ORDER`. Uma rota que deixe o **cliente** fazer
`pending → on_delivery` por conta própria permite a quem tem o `tracking_token`
transformar "paga antes" em "paga depois" e empurrar o pedido para a cozinha sem
pagar — exatamente o que `ensure_payment_allows_order_status` existe para
impedir. O que torna a troca segura não é regra nova: é **aceitar só método que a
filial habilitou com `payment_flow='delivery'`**. Restaurante que quer dinheiro
adiantado não tem essa linha, e a troca fica impossível para ele — a configuração
do lojista **é** a autorização.

**A corrida, que é o motivo de isto não ser trivial.** O cliente paga o pix e
troca para dinheiro quase ao mesmo tempo. A troca limpa `provider_payment_id`, o
webhook atrasado chega num pedido já `on_delivery` (terminal), a transição é
recusada e o handler devolve `ignored/invalid_transition` — ou seja, **dinheiro
real entrou e ninguém registrou**, com um warning no log como único rastro.
Mitigação: antes de sair do fluxo online com um `provider_payment_id` vivo,
`GET /v1/payments/{id}` e recusar a troca se estiver `approved`.

Descartados: corpo no `POST .../payment` (mistura "criar cobrança" com "mudar os
termos do pedido"); cancelar e refazer o pedido (perde cupom já resgatado,
cashback, comissão congelada e estimativa, e o cupom pode ter batido o limite no
meio do caminho).

---

## 9. Testes

`tests/test_mercadopago_gateway.py` cobre as três funções com o gateway mocado
(`httpx.Client` substituído): formato da requisição, tradução da resposta, todos
os erros da tabela, o que a linha de erro leva para o log, quando a chave de
idempotência se repete e quando muda, e o formato da assinatura (HMAC calculado a
mão no teste, corpo adulterado, timestamp velho).

`tests/test_payments.py` cobre a resolução de `payer_email`, qual cobrança o
service informa como substituída em cada `payment_status`, o `retryable` de cada
erro, e a ordem extração → pedido → credencial → assinatura — inclusive que o
segredo usado é sempre o do restaurante **do pedido**, e que um pagamento sem
pedido correspondente nunca chega a chamar `verify_webhook_signature`.

**Nenhum teste chama o Mercado Pago de verdade.** Isso só se confirma testando de
ponta a ponta com a credencial de teste.
