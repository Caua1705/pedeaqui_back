# Cartão salvo, por restaurante

O desenho do cartão salvo do cliente: o que fica no nosso banco, o que fica
no Mercado Pago, e o que acontece em cada desfecho de cobrança.

O risco de contestação — que é do lojista, e a frase para dizer a ele — está
em `cartao-o-risco-que-e-do-lojista.md`. Este documento é sobre o mecanismo.

---

## O que nunca chega ao backend

O número do cartão e o CVV são digitados no formulário do SDK do Mercado
Pago, **no navegador**, e vão direto para eles. O que atravessa para cá é o
`token`: de uso único, vida curta, inútil para qualquer outra coisa.

Não existe campo de PAN em nenhum schema, e não existe coluna de PAN em
nenhuma tabela. `tests/test_cartao_salvo.py::NumeroDoCartaoNaoAtravessaTests`
trava isso nos dois contratos e nas duas tabelas — se um deles falhar, a
integração saiu do padrão de tokenização e o perímetro de PCI do projeto
mudou.

O `CHECK (last_four_digits ~ '^[0-9]{4}$')` é a última barreira: um `INSERT`
que passasse o número inteiro por engano falha no banco, alto, em vez de
gravar 16 dígitos numa coluna chamada "últimos quatro".

---

## "Cartão salvo" não quer dizer "um clique"

**A pessoa ainda digita o CVV.** Para cobrar um cartão salvo, o SDK gera um
token novo a partir do `card_id` mais o código de segurança, de novo no
navegador. Não existe cobrança de cartão salvo sem token.

O que o cartão salvo poupa é redigitar **o número**, a validade e o nome. É
menos do que "um clique", e é o que o Mercado Pago oferece sem contrato de
cartão em arquivo. Se a tela do checkout prometer "pagar sem digitar nada", a
promessa quebra no CVV.

---

## Onde cada coisa mora

| O quê | Onde | Por quê |
|---|---|---|
| número, CVV, validade | **só no Mercado Pago** | nunca passaram por aqui |
| `card_id` (id opaco) | `customer_saved_cards.provider_card_id` | é o que a cobrança referencia |
| bandeira, últimos 4 | `customer_saved_cards` | para a pessoa reconhecer o cartão na tela |
| id do "customer" deles | `customer_payment_profiles.provider_customer_id` | o dono dos cartões dentro da conta do lojista |

O `id` que sai na API é **o nosso UUID**. O `card_id` do Mercado Pago não
aparece em resposta nenhuma: ele só tem sentido dentro da conta do
restaurante, e publicá-lo não ajudaria o front em nada.

---

## Por que o cartão pende do RESTAURANTE

A credencial do Mercado Pago é do lojista: a cobrança nasce na conta dele e o
dinheiro cai na conta dele. O "customer" do Mercado Pago, e os cartões
pendurados nele, vivem **dentro daquela conta**. O `card_id` salvo no Júnior
da Picanha não é cobrável pela conta de outro restaurante, e nem sequer é
legível por ela.

Consequência: **quem pede em duas lojas cadastra o cartão duas vezes.** Não
há como evitar isso enquanto a credencial for do lojista.

E, no dia em que o split entrar e as cobranças passarem a nascer numa conta
de marketplace, **estes ids param de valer e os clientes recadastram.** Não
há migração possível — os cartões não são nossos para mover. Isso é sabido e
aceito.

### `environment` está na chave pelo mesmo motivo

A conta de teste e a de produção são contas diferentes. Virar
`MERCADOPAGO_ENVIRONMENT` faz os perfis do ambiente antigo simplesmente
deixarem de ser encontrados: a lista do cliente vem vazia e ele recadastra.
Sem essa coluna, o backend tentaria cobrar cartões que a conta ativa nunca
viu — e o erro apareceria como **recusa de pagamento**, não como configuração
errada, que é o pior lugar possível para esconder um problema de ambiente.

---

## A chave pública chega ao front sem risco para a privada

**Ela já chegava, e o mecanismo já existia** —
`GET /restaurants/{restaurant_slug}/payment-config` (o
`PaymentConfigResponse`), que devolve `provider`, `public_key` e
`card_enabled`. Pública como o cardápio, e `card_enabled` responde antes de
o cliente digitar o cartão, em vez de depois, com um 503.

Não houve "separação" a fazer, e é isso que torna a resposta segura em vez de
cuidadosa. As duas chaves **nunca estiveram juntas**: em
`restaurant_payment_credentials` a pública é uma coluna em texto puro
(`public_key`) e o access token é outra coluna, cifrada com Fernet
(`access_token_encrypted`). São campos diferentes da mesma linha, não dois
usos do mesmo valor.

O que garante que a privada não vaza por esse caminho não é disciplina de
quem escreve o endpoint, é a forma do código:

- **decifrar é um ato explícito.** `decrypt_secret` só é chamado dentro de
  `PaymentCredentialService.get_active_credential`. Uma rota que não o chama
  não tem como produzir o token — não existe caminho acidental;
- **a rota de config lê `credential.public_key` e mais nada.** Nenhum schema
  de resposta em todo o projeto tem campo para o access token;
- o token decifrado nunca é guardado em objeto de vida longa nem logado —
  vai direto para o header `Authorization` da chamada ao gateway.

A regra que fica: **a chave pública é pública, e o jeito de mantê-la assim é
nunca dar à privada um caminho de saída.** Não há nada a esconder na pública;
há tudo a não construir para a privada.

### E ela NÃO entra no `RestaurantInfoResponse` — decidido em 25/08/2026

A pergunta veio do front, e vai voltar: *"por que a `public_key` não vem junto
do `GET /restaurants/{slug}/info`, que a tela já chama de qualquer jeito?"*

**Decisão: o front usa o `/payment-config`. A `public_key` não é duplicada no
`/info`.** Não foi esquecimento de escopo — a seção acima já tratava a chave
pública como problema resolvido, e ela é a rota corrente para isso, não uma
sobra de versão anterior.

O que se ganharia é uma requisição a menos na abertura da tela de pagamento.
É leitura barata, sem efeito e sem rate limit próprio: esse é o ganho inteiro.
Os três custos do outro lado:

- **não elimina a chamada.** O front não precisa só da chave — precisa de
  `card_enabled` para saber se deve **oferecer** cartão nesta tela (e de
  `provider`, porque o sandbox não processa cartão). Com só a `public_key` no
  `/info`, o `/payment-config` continua sendo chamado; o que muda é passar a
  haver duas fontes do mesmo valor em vez de uma;
- **o mesmo campo em dois contratos** é a forma de problema que a armadilha 34
  registra — duas fontes que um dia divergem, e cada tela acertando por conta
  própria qual delas ler;
- **espalharia um decrypt para a rota pública mais quente.**
  `PaymentCredentialService.get_active_credential` faz **dois**
  `decrypt_secret` (o access token e o webhook secret) para devolver a
  `public_key`. Hoje isso acontece uma vez, quando a tela de pagamento abre.
  No `/info` passaria a acontecer a cada abertura de loja — inclusive para
  quem vai pagar pix ou na entrega e nunca verá um formulário de cartão.

O terceiro custo tem conserto óbvio (ler `public_key` sem passar pelo serviço
que decifra), e é justamente por isso que ele merece a linha: **o conserto é
construir um segundo caminho de leitura da credencial**, para economizar uma
requisição. É exatamente o que a regra logo acima pede para não fazer — não há
nada a esconder na pública, há tudo a não construir para a privada.

**Se o incômodo real for latência**, o conserto é do lado do front: disparar
`/info` e `/payment-config` em paralelo, não em sequência. Duplicar o campo
não é mais rápido, só mais difícil de manter certo.

**E se um dia isto for reaberto com motivo novo**, a forma menos ruim é levar
o `PaymentConfigResponse` inteiro como bloco aninhado
(`payment: {provider, public_key, card_enabled}`), reusando o mesmo schema —
nunca um campo solto. Um bloco tem uma fonte só e some junto quando a rota
mudar; um `payment_public_key` avulso é a divergência já plantada.

---

## As rotas

| Rota | O que faz |
|---|---|
| `GET /customers/me/cards?restaurant_slug=…` | lista os cartões da pessoa **naquele restaurante** |
| `POST /customers/me/cards` | salva o cartão tokenizado (`{restaurant_slug, token}`) |
| `DELETE /customers/me/cards/{card_id}` | remove **nos dois lados** |

Todas exigem login. `restaurant_slug` é obrigatório no GET e no POST porque
não existe "o cartão da pessoa" sem dizer em que loja — um default silencioso
salvaria na loja errada e o cartão não apareceria no checkout.

**Lista vazia não quer dizer "nunca salvou".** É também a resposta para quem
salvou em outra loja e para quem salvou antes de o ambiente virar. Nos três
casos a tela é a mesma: oferecer o cadastro de um cartão novo.

---

## Ponta a ponta: o pedido pago com cartão salvo

Esta seção existe porque a pergunta chegou do front assim: *"falta a
`saved_card_id` chegar ao pedido"*. **Ela não vai no corpo do pedido, e isso é
desenho, não pendência.** São duas chamadas, e a segunda é a do cartão.

### Por que o cartão não entra em `POST /orders`

Dois motivos, e o segundo custa dinheiro:

- **a chamada ao gateway não pode acontecer com a transação do pedido aberta.**
  É por isso que `start_payment` é rota separada desde antes de o cartão
  existir: um gateway lento seguraria uma conexão do pool com o `INSERT` do
  pedido em aberto, e com o pool cheio a API inteira trava (§ `start_payment`,
  em `payments.py`);
- **campo novo em `CreateOrderRequest` custa 24h de 422**, mesmo opcional e
  mesmo que ninguém o leia. `OrderService` assina o corpo com
  `payload.model_dump()`, então um campo a mais muda o `request_fingerprint` de
  **todos** os pedidos — e toda `Idempotency-Key` em voo no minuto do deploy
  passa a receber `422 Idempotency-Key já utilizada com um corpo diferente`.
  É a armadilha 37, e ela não perdoa campo opcional.

Um `saved_card_id` no corpo do pedido seria, então, um campo que ninguém lê,
que ninguém pode ler (a cobrança acontece depois, noutra transação), e que
cobraria um dia de recusas para entrar. Se ele for mandado assim hoje, o
`extra="ignore"` do schema o descarta em silêncio — que é exatamente o sintoma
de "o pagamento não fecha".

### Passo 1 — criar o pedido

```http
POST /restaurants/junior-da-picanha/orders
Authorization: Bearer <token do cliente>        # obrigatório para cartão
Idempotency-Key: 5f1c2a7e-3b6d-4c9a-8e21-0d7b4f8a1c33
Content-Type: application/json
```

```json
{
  "branch_id": "6d2b8f10-4a3c-4f27-9c5e-1b8a7d3e2f45",
  "order_type": "delivery",
  "payment_method": "credit_card",
  "customer_address_id": "b71e4c02-9d55-4f18-a3c7-6e2f0a9b8d14",
  "delivery_estimate_token": "TQ4rZ0m6...",
  "items": [
    { "product_id": "0a9f3c11-2d47-4b8e-9f60-5c3a1e7d2b98", "quantity": 1 }
  ],
  "use_cashback": false
}
```

`payment_method: "credit_card"` é o que faz o pedido nascer com
`payment_flow: "online"`. A filial precisa ter `credit_card` habilitado em
`branch_payment_methods`, senão a criação responde 400 (armadilha 15).

Resposta — **`tracking_token` só sai aqui, uma vez, e não há rota para
reemiti-lo**:

```json
{
  "id": "9c1d7b64-5e08-4a3f-bb92-7d4e1f0c6a35",
  "order_number": 5471,
  "tracking_token": "kJ8vQ2mZ...43 caracteres",
  "status": "pending",
  "payment_flow": "online",
  "payment_status": "pending",
  "subtotal": 89.9,
  "delivery_fee": 8.0,
  "service_fee": 0.0,
  "coupon_code": null,
  "coupon_discount_amount": "0.00",
  "cashback_redeemed_amount": "0.00",
  "discount_total": "0.00",
  "total": 97.9,
  "message": "Pedido criado com sucesso"
}
```

> Os quatro primeiros valores são **número** e os três descontos são **string**
> com duas casas. Não é engano deste exemplo — é a armadilha 34, e o front
> precisa saber quais campos converter.

### Passo 2 — cobrar, com o cartão salvo

```http
POST /restaurants/junior-da-picanha/orders/kJ8vQ2mZ.../payment
Authorization: Bearer <token do cliente>        # cartão exige login
Content-Type: application/json
```

```json
{
  "card": {
    "token": "9f8e7d6c5b4a39281706f5e4d3c2b1a0",
    "saved_card_id": "3e7a1c98-6b25-4d0f-8a13-9c4e5f2b7d60",
    "payer_document_type": "CPF",
    "payer_document_number": "12345678909"
  }
}
```

**O front manda os dois: o `card_id` e um token novo, gerado na hora.** É a
pergunta que mais volta, e a resposta não é uma escolha nossa — é como o
Mercado Pago funciona:

| Campo | De onde vem | Por que |
|---|---|---|
| `saved_card_id` | o `id` de `GET /customers/me/cards` — **o nosso UUID** | diz *qual* cartão, e traz o `customer` dono dele |
| `token` | `createCardToken({ cardId, securityCode })` no navegador, **agora** | é de uso único e vida curta; sem ele não há cobrança |

`payment_method_id` (a bandeira) **não vai** quando há `saved_card_id`: ela sai
do que está gravado em `customer_saved_cards.brand`, que é mais confiável que o
que o cliente mandaria. Enviá-la não é erro — é ignorada.

O que o backend faz com o par, antes de falar com o gateway: confere que o
cartão é **desta pessoa** e **desta loja** (404 nos dois casos, nunca 403 — ver
`_resolve_saved_card`), e acrescenta `payer.type: "customer"` e `payer.id` ao
corpo da cobrança. Sem esses dois o Mercado Pago recusa um token que nasceu de
um `card_id`, porque para ele o cartão pertence ao customer.

Resposta, com o veredito já dentro — **cartão é síncrono**:

```json
{
  "provider": "mercadopago",
  "provider_payment_id": "1234567890",
  "payment_status": "paid",
  "checkout_url": null,
  "qr_code": null,
  "status_detail": "accredited"
}
```

`checkout_url` e `qr_code` são do pix e vêm nulos no cartão: não há para onde
mandar o cliente, o desfecho já está em `payment_status` — `paid`, `failed` ou
`in_review`. Os três estão descritos em *"Os três desfechos da cobrança"*.

Recusado, a resposta é a mesma forma com `payment_status: "failed"` e o
`status_detail` cru (`cc_rejected_bad_filled_security_code` pede do cliente
coisa diferente de `cc_rejected_insufficient_amount`). **O pedido continua
existindo e continua pagável**: a próxima tentativa é uma cobrança nova, com
chave de idempotência nova, e o front só precisa chamar o passo 2 de novo.

### Passo 3 — conferir o pedido pago

`GET /restaurants/{slug}/orders/track/{tracking_token}` devolve o
`OrderDetailResponse`, e o que muda depois de uma cobrança aprovada são três
campos:

```json
{
  "payment_method": "credit_card",
  "payment_flow": "online",
  "payment_status": "paid",
  "paid_at": "2026-08-25T18:42:07.913Z",
  "status": "pending",
  "status_history": [
    { "status": "pending", "changed_by": "system", "note": "Pedido criado" },
    { "status": "payment:paid", "changed_by": "gateway:mercadopago" }
  ]
}
```

`status` continua `pending`: pagamento confirmado libera o pedido para o
lojista **aceitar**, não o aceita sozinho. É a partir daqui que a comanda
imprime.

---

## Os sete campos de endereço do formulário: nenhum é exigido

Pergunta de 25/08/2026, e a resposta é curta: **o formulário do app pede CEP,
endereço, número, complemento, bairro, cidade e estado, e o Mercado Pago não
exige nenhum deles para tokenizar um cartão.** Foi precaução.

O que `createCardToken` usa é o que o próprio cartão tem: número, nome
impresso, validade, CVV, e o documento do portador. Endereço não entra na
tokenização — no `POST /v1/payments` ele existe como campo **opcional** de
`additional_info`, que alimenta o escore do antifraude e nada mais.

**O backend nunca recebeu nenhum desses sete campos**, e é o que torna a
resposta verificável em vez de opinativa: não há campo de endereço em
`SaveCardRequest`, em `CardPaymentPayload`, em `CustomerSavedCard`, nem no
corpo que `create_payment` monta e envia. Se algum fosse obrigatório, a
integração inteira já teria parado na primeira chamada — a de salvar o cartão,
que é a que roda hoje.

O que **fica em aberto até a primeira cobrança real contra a credencial de
teste** é o desfecho, e não a exigência: pode voltar `in_process` (análise) em
vez de `approved`. Isso não é o gateway pedindo endereço; é a mesma análise
antifraude que a seção acima descreve.

**A recomendação é tirar os sete.** São sete campos de atrito num checkout de
delivery de bairro, num formulário em que cada campo a mais é um cliente a
menos — e o endereço de entrega o app já tem, noutra tela, para o pedido.

O que se perde ao tirar: alguns pontos no escore do antifraude do Mercado Pago,
que podem virar um `in_review` a mais em vez de um `paid` direto. Não vira
recusa; vira análise. Se um dia o volume de `in_review` incomodar, o campo que
tem efeito de verdade é **`payer_document_number` (o CPF)**, que já está no
contrato e já é enviado — e o passo seguinte é 3DS, não endereço.

---

## Salvar não valida o cartão

**Não há cobrança de R$ 1,00 seguida de estorno.** Salvar pendura o cartão no
customer e mais nada. O cartão é validado na primeira cobrança de verdade.

O preço é conhecido e aceito: um cartão sem limite ou bloqueado entra na
lista e só falha no checkout. A alternativa é pior nos dois lados — a cobrança
de teste aparece na fatura do cliente, gera pergunta ao lojista, e **ainda
assim não prova que haverá limite no dia do pedido**.

---

## Os três desfechos da cobrança

Cartão é **síncrono**: o próprio `POST /v1/payments` responde o veredito. Isso
já vale para o cartão avulso e continua valendo para o salvo — o cartão salvo
não muda nenhum desfecho, só a origem do token.

### `approved` → `payment_status = "paid"`

**Para o cliente:** pagou. A resposta do checkout já volta com
`payment_status: "paid"`, sem espera e sem tela de "aguardando".

**Para o pedido:** `paid_at` é gravado, entra uma linha em
`order_status_history` com `changed_by="gateway:mercadopago"`, e o pedido é
liberado para o lojista aceitar. **A comanda imprime.**

### `rejected` → `payment_status = "failed"`

**Para o cliente:** recusado, na hora. O `status_detail` cru vem na resposta
justamente para o front escrever um texto próprio por motivo —
`cc_rejected_insufficient_amount` pede do cliente coisa diferente de
`cc_rejected_bad_filled_security_code`.

**Para o pedido:** ele **continua existindo** e continua pagável. Uma nova
tentativa não é retry: é uma cobrança **nova**, e é o
`previous_payment_id` — preenchido só quando o status é `failed` — que faz a
chave de idempotência mudar. Sem ele o Mercado Pago devolveria a própria
recusa de volta, para sempre, e o pedido nunca mais teria como ser pago
(armadilha 6, reaberta pela porta do cartão).

**A comanda não imprime.**

### `in_process` → `payment_status = "in_review"`

O antifraude segurou a cobrança. **Pode durar até 48h úteis.**

**Para o cliente:** não pagou, e não foi recusado. A tela precisa dizer isso
com essas palavras — `in_review` existe separado de `pending` exatamente para
a tela e a comanda poderem distinguir "o cliente ainda não pagou" (que pede
ligar e cobrar) de "o gateway está analisando" (que pede **não** ligar).

**Para o pedido:** `in_review` **não está** em
`PAYMENT_STATUSES_THAT_RELEASE_ORDER` — que é só `("paid", "on_delivery")`.
O pedido não é liberado e **a comanda não imprime.** É tentador liberar ("já
quase pagou"), e um pedido preparado durante a análise vira prejuízo do
lojista quando ela recusa.

O desfecho chega depois por **webhook**: `in_review → paid` ou
`in_review → failed`.

> **O risco operacional que o `in_review` cria** está na armadilha 25:
> nenhum lojista espera 48h para recusar um pedido de almoço. O pedido pode
> terminar `rejected` + `paid`, e o grep que acha esse dinheiro parado é
> `sem estorno automatico`.

---

## A remoção: dos dois lados, nessa ordem

`DELETE /customers/me/cards/{card_id}` apaga **no Mercado Pago e no nosso
banco**. Não é um nem outro: um cartão que some da tela e continua pendurado
na conta do lojista não foi removido, foi escondido.

**O gateway responde primeiro, e a ordem é a decisão inteira.** Apagar a
linha daqui antes deixaria o cartão lá **sem nenhuma referência nossa a ele**
— ninguém mais conseguiria removê-lo, nem o cliente nem o suporte, porque o
`provider_card_id` teria ido embora junto. A pessoa é informada de que o
cartão saiu, e ele fica.

Por isso:

| Situação | O que acontece |
|---|---|
| gateway apaga | apaga aqui, `200` |
| gateway responde **404** | conta como **sucesso** — o cartão já não está lá, que é o estado que se queria. Erro travaria para sempre uma linha que o cliente quer ver sumir |
| gateway **fora do ar** | `502`, **nada é apagado aqui**, o cartão continua na lista e o cliente tenta de novo |

O estado permanece coerente nos dois lados em todos os casos.

### Pedido em análise que usava aquele cartão: **nada acontece com ele**

E isso é proposital, não uma folga do desenho.

A cobrança já existe no Mercado Pago e vive por conta própria: ela foi criada
com um **token**, não com o `card_id`. Apagar o cartão salvo **não a cancela,
não a estorna e não muda o resultado da análise antifraude.** O `in_review`
segue seu curso e o webhook decide o pedido — aprovado ou recusado — como
decidiria se o cartão continuasse salvo.

O que o cliente perde ao remover é a comodidade de não redigitar **no próximo
pedido**. Se ele removeu o cartão para "cancelar a cobrança", removeu a coisa
errada: quem cancela pedido é o pedido, e quem devolve dinheiro é o estorno no
painel do lojista.

---

## Exclusão de conta (LGPD)

`CustomerAnonymizationService` apaga os cartões e os perfis, e tenta apagá-los
também na conta de cada lojista.

**A tentativa remota é melhor esforço, fora da transação e antes dela.** O
direito de apagar a própria conta não pode ficar refém de o Mercado Pago estar
de pé: uma instabilidade deles transformaria a exclusão num 502 que a pessoa
não tem como resolver. Falha ali é contada e logada, nunca propagada.

O que sobra quando falha é um cartão pendurado na conta do lojista sem
referência nossa. Não há PAN nem CVV lá — o Mercado Pago guarda o cartão, não
nós — mas é um resto que alguém precisa saber que existe. O grep é:

```
[LGPD] conta anonimizada com cartao remanescente no gateway
```

Linha própria e só quando sobrou cartão, pelo mesmo motivo do saldo de
cashback perdido: o que se quer de um grep é o caso que **precisa de ação**,
não todo evento do tipo.

O perfil vai junto com os cartões: ele guarda o id do "customer" que o Mercado
Pago criou a partir do e-mail da pessoa, e deixar a linha seria manter um
ponteiro para um cadastro dela num sistema de terceiro — exatamente o que a
anonimização existe para não fazer.

---

## O que ficou de fora

- **3DS.** O risco é do lojista e está comunicado
  (`cartao-o-risco-que-e-do-lojista.md`). `binary_mode` fica falso justamente
  para não matar o desafio 3DS se um dia ele entrar.
- **Parcelamento.** `CARD_INSTALLMENTS = 1`. Parcelado sem juros sai do bolso
  de quem recebe, e habilitar sem decisão comercial mudaria o que o lojista
  recebe sem ele ter pedido.
- **Cartão padrão.** Nenhuma coluna de "principal": a lista vem com o mais
  recente primeiro e o cliente escolhe.
- **Cartão salvo no sandbox.** O sandbox não simula cartão, e deixar salvar um
  cartão que nunca poderá ser cobrado é a mesma demonstração falsa que a
  armadilha 39 registra. `503`, alto.

E uma que **não** ficou de fora, apesar de parecer: a `public_key` no
`RestaurantInfoResponse`. Ela nunca esteve no escopo porque o mecanismo já
existia — o front usa o `GET /restaurants/{slug}/payment-config`. A decisão e
os três custos de duplicar o campo estão em *"E ela NÃO entra no
`RestaurantInfoResponse`"*, na seção da chave pública.
