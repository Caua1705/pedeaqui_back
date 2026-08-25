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
