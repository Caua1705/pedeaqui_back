# Rodada 5 do backend — fonte da verdade

Branch: `rodada/backend-5`, saindo de `rodada/backend-4`. Nunca commitar na
`main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Rodadas anteriores: `rodada-back.md` a `rodada-back-4.md`.

**Nada de produção.** O dono roda hoje os três comandos do Redis (§3 da rodada
3) e o das filiais. O `6fcaccc` continua esperando.

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 3 | A rota de cancelamento pelo cliente, para o front | **feito** — texto colável na §3 |
| 1 | A revisão de alinhamento das colunas, pronta e testada | pendente |
| 2 | O que eu faria a seguir, escolhido e justificado | pendente |

---

## Antes de tudo: são 15 colunas, não 13

O pedido diz "as 13 colunas". **O número certo é 15**, e o 13 saiu de uma conta
minha errada na rodada 3:

- depois de `valid_until` sair, sobraram **15** (16 − 1). Eu escrevi "sobraram
  14";
- daí "1 de código + 13 para a revisão", que fechava com o 14 errado.

Conferido agora contra o Postgres de teste:
`divergencias_orm_schema.py` diz **41 divergências, 15 na primeira classe**, e
`alembic/preparadas/` já lista as 15 certas. A revisão preparada **nunca esteve
errada** — o erro foi só na prosa do scratchpad da rodada 3.

**E `ai_feedback.created_at` continua na lista, apesar do conserto de código.**
O `OR created_at IS NULL` do DELETE de retenção fechou o buraco de LGPD; ele não
tocou no model nem no schema, então a divergência segue lá e a coluna segue
precisando de `SET NOT NULL`. As duas coisas são independentes.

---

## 3. `POST /orders/track/{token}/cancel` — pronto para colar no front

A rota existe desde 25/08/2026 e **o front nunca a chamou**. Enquanto isso,
quem desiste liga para o restaurante e pede que o lojista cancele pelo painel —
e num pix já pago isso atrasa a devolução do dinheiro até alguém atender o
telefone.

O texto abaixo é para copiar inteiro.

---

### PARA COLAR NO REPOSITÓRIO DO APP

> **Assunto: o cliente pode cancelar o próprio pedido, e a rota já existe**
>
> ```
> POST /restaurants/{restaurant_slug}/orders/track/{tracking_token}/cancel
> ```
>
> Responde `OrderDetailResponse` — **o mesmo schema da tela de
> acompanhamento**. Depois de cancelar, dá para renderizar a resposta direto,
> sem um GET a mais.
>
> #### Autenticação: nenhuma, e é de propósito
>
> **Não mande token de cliente.** Quem autoriza é o `tracking_token` da URL —
> o mesmo que já abre o acompanhamento e a avaliação, 256 bits, sem rota de
> reemissão.
>
> Pedido de convidado é caso normal, e exigir conta aqui deixaria justamente o
> convidado sem saída.
>
> #### Quando o botão pode aparecer
>
> **Só com `status` em `pending` ou `accepted`.**
>
> | `status` | Botão |
> |---|---|
> | `pending` | **aparece** — inclui o pix ainda não pago |
> | `accepted` | **aparece** — o lojista aceitou, a comida não começou |
> | `preparing`, `ready`, `out_for_delivery` | **não** — a comida já está sendo feita |
> | `completed`, `cancelled`, `rejected` | **não** — estado final |
>
> A partir de `preparing` o insumo já saiu do estoque, e quem decide quem come
> o prejuízo passa a ser o lojista. **A tela deve trocar o botão por "Falar com
> o restaurante"**, não escondê-lo sem explicação.
>
> Esconder o botão é a defesa da tela; a do servidor é o 409, e ele existe
> porque o status pode mudar entre a tela carregar e o dedo chegar no botão.
>
> #### O corpo é opcional — e ele todo
>
> ```jsonc
> // válido:
> {}
> // também válido — pode não mandar corpo nenhum
> // também válido:
> { "reason": "mudei de ideia" }
> ```
>
> `reason` é `string | null`, **máximo 150 caracteres**, opcional.
>
> **Não faça dele um campo obrigatório na tela.** Exigir justificativa de quem
> desiste de um pedido que nem começou produz um campo preenchido com "a". O
> histórico já registra que foi o cliente.
>
> Se vier, o backend grava `"Cancelado pelo cliente: mudei de ideia"` no
> histórico; sem ele, `"Cancelado pelo cliente"`.
>
> #### O que acontece junto — o app NÃO precisa pedir nada disso
>
> É a **mesma escrita** do cancelamento pelo painel, então tudo isto acontece
> numa chamada só:
>
> | | |
> |---|---|
> | **Cupom** | volta a ficar disponível para o cliente usar de novo |
> | **Cashback resgatado** | volta para o saldo. Sem isto o cliente cancelaria e **perderia** o dinheiro |
> | **Cashback a ganhar** | não é creditado — ele só entra em `completed`, e não houve venda |
> | **Pagamento online** | é **estornado automaticamente** |
>
> **Não chame nenhuma rota de estorno, de cupom ou de cashback depois.** Uma
> segunda chamada não tem o que fazer e só cria caminho para erro.
>
> #### O estorno, e o que a resposta diz sobre ele
>
> O estorno acontece **depois** de o cancelamento estar gravado, e de propósito:
> se o Mercado Pago estiver fora do ar, **o cancelamento vale mesmo assim** e
> uma varredura devolve o dinheiro depois.
>
> Consequência para a tela: no `OrderDetailResponse` que volta, o
> `payment_status` pode estar
>
> - **`refunded`** — o gateway confirmou que o dinheiro saiu; ou
> - **ainda `paid`** — o estorno foi pedido e o gateway ainda não liquidou, ou
>   estava fora do ar.
>
> **Nos dois casos o pedido está cancelado.** Não trate `paid` como falha.
>
> A frase certa é *"cancelado — a devolução aparece na sua fatura em até X
> dias"*, e não *"cancelado, mas o estorno falhou"*: o app não tem como saber a
> diferença entre "ainda não liquidou" e "falhou", e nos dois a varredura
> resolve.
>
> #### Os erros, um a um
>
> | Status | Quando | O que a tela faz |
> |---|---|---|
> | **200** | cancelou | renderiza o `OrderDetailResponse` que voltou |
> | **404** | token errado, pedido de outro restaurante, ou `restaurant_slug` inválido/inativo | *"Não encontramos este pedido."* **Não diga "token inválido"** — as três causas respondem igual de propósito, para a rota não virar um oráculo de tokens válidos |
> | **409** | o pedido saiu da janela | **use o `detail`**: ele cita o status atual (`"Pedido em 'preparing' nao pode mais ser cancelado por voce. Fale com o restaurante."`). Recarregue o pedido e mostre o estado novo |
> | **422** | `reason` acima de 150 caracteres | erro de validação do FastAPI, com `loc` apontando o campo |
> | **429** | limite: **10/min e 60/hora por IP** | *"Muitas tentativas, aguarde um instante."* |
>
> **O 409 é o caso que mais vai acontecer na prática**, e não é erro do app: é
> a corrida normal entre a tela e a cozinha. Trate-o como "o pedido andou",
> recarregue, e mostre o botão certo para o estado novo.
>
> #### Clicar duas vezes
>
> **Não mande `Idempotency-Key`** — a rota não a aceita, e não faz falta: o
> segundo clique chega com o pedido já em `cancelled` e leva **409**. Basta
> desabilitar o botão enquanto a requisição está em voo e tratar o 409 como
> acima.
>
> #### Como testar
>
> 1. crie um pedido e pegue o `tracking_token` da resposta de criação;
> 2. `POST .../track/{token}/cancel` com corpo vazio → **200**, e o
>    `OrderDetailResponse` volta com `status: "cancelled"`;
> 3. repita a mesma chamada → **409**;
> 4. num pedido `preparing` → **409**, com o status citado no `detail`;
> 5. com um token inventado → **404**.
>
> O caso que mais vale testar é o **pix pago e cancelado em seguida**: é o
> motivo de a rota existir, e é onde o cupom, o cashback e o estorno acontecem
> todos na mesma chamada.

---

### O que fica do lado do backend

**Nada.** A rota está no `openapi.json` versionado, com os quatro status
documentados (`200`, `404`, `409`, `422`), e o `429` vem do rate limit global.
Não há deploy pendente para o app começar.

O que existe hoje é uma rota funcionando e ninguém chamando: **todo
cancelamento que passa pelo telefone do restaurante já podia ter sido um
toque.**
