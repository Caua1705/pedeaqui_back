# Rodada 8 do backend — fonte da verdade

Branch: `rodada/backend-8`, saindo de `rodada/backend-7`. Nunca commitar na
`main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Rodadas anteriores: `rodada-back.md` a `rodada-back-7.md`.

Os três itens vieram dos outros dois repositórios (painel e app).

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 1 | `sort_order` dos grupos de opção e das opções não chega na vitrine | **feito** |
| 2 | Cliente logado não cancela o próprio pedido de outro aparelho | **feito** — rota autenticada, token não publicado |
| 3 | Vitrine ordena cupom por `sort_order` e o painel não define esse valor | pendente |

---

## 1. A ordem do cardápio

### O relato estava certo, e faltava metade dele

`Product.option_groups` e `ProductOptionGroup.options` eram
`relationship(...)` sem `order_by`, e **`selectinload` não ordena**: ele emite
um segundo `SELECT ... WHERE fk IN (...)` sem `ORDER BY` nenhum. O Postgres
devolve na ordem que for barata — na prática, a de inserção.

São **seis** `selectinload(Product.option_groups).selectinload(...options)`
espalhados por três repositórios (`menu_repository`, `product_repository` ×4,
`admin_menu_repository`). Nenhum ordenava nada.

**A metade que o relato não mencionava:** a única consulta que ordenava era
`AdminMenuRepository.list_option_groups`, e ela ordenava **só os grupos** —
`.order_by(sort_order, name)`. As opções de dentro de cada grupo saíam soltas
**também no painel**. Então não era "painel ordenado × vitrine desordenada": as
opções não tinham ordem em lugar nenhum.

### O conserto: uma definição, não duas parecidas

O pedido foi "a ordem tem que ser a mesma nas duas". Fazer isso com dois
`.order_by` iguais seria repetir, uma camada acima, exatamente o defeito que se
está consertando — duas expressões parecidas divergem no dia em que alguém
ajusta uma.

`relationship(order_by=...)` **aceita callable** (conferido antes de escrever).
Então a ordem virou duas funções em `src/models/product_option_model.py`:

```python
def ordem_dos_grupos() -> list:
    return [ProductOptionGroup.sort_order, ProductOptionGroup.name, ProductOptionGroup.id]

def ordem_das_opcoes() -> list:
    return [ProductOption.sort_order, ProductOption.name, ProductOption.id]
```

e as três pontas chamam **a função**: os dois `relationship` e o
`.order_by(*ordem_dos_grupos())` de `list_option_groups`. Consertar no
`relationship` é o que alcança os seis `selectinload` de uma vez — um
`.order_by` por consulta deixaria o sétimo sem ordem de novo, e calado.

Funções e não listas prontas também por um motivo mecânico: chamadas na
definição da classe, elas rodariam antes de `ProductOption` existir. O callable
só é avaliado quando o mapper é configurado.

### O `id` no fim não é decoração

`sort_order` é `DEFAULT 0` nas duas tabelas, então **enquanto o lojista não
arrastar nada, todo mundo tem 0** e o desempate decide a ordem inteira. Sem uma
chave única no fim, duas linhas com o mesmo `sort_order` e o mesmo `name` podem
sair trocadas entre duas requisições — o cardápio piscando sem nada ter mudado.
Há teste exigindo que a última chave seja `primary_key`.

`created_at` seria mais expressivo, mas não serve: duas linhas criadas na mesma
transação compartilham `now()`, então a ordem continuaria parcial.

**Nulo não é problema aqui:** as duas colunas são `NOT NULL DEFAULT 0` no
schema. (O ORM diz `Mapped[int | None]` — é uma das divergências já catalogadas
em `docs/alinhamento-orm-schema.md`, e continua parada esperando a etapa 0.)

### Os testes, e o vermelho

`tests/test_ordem_do_cardapio.py`, 7 testes. Os de banco são os que valem: sem
`ORDER BY`, a ordem de um `SELECT` é decisão do planejador, e planejador não se
dubla.

**Visto vermelho antes**, com o `order_by` desligado e as funções mantidas (a
primeira tentativa deu `ImportError`, que não é vermelho de verdade — é o teste
não carregando). Os seis falharam, e a mensagem é o defeito relatado:

```
assert ['Bebida', 'Tamanho', 'Borda'] == ['Tamanho', 'Borda', 'Bebida']
```

A fábrica cadastra **fora de ordem de propósito**. Inserindo já na ordem certa o
teste passaria com ou sem `ORDER BY` — verde pelo motivo errado.

O teste de "uma definição só" também foi conferido por mutação: trocando o
callable por uma lista escrita à mão no `relationship`, ele fica vermelho. É a
regressão que ele existe para pegar.

### Arquivos

`src/models/product_option_model.py` · `src/models/product_model.py` ·
`src/repositories/admin_menu_repository.py` · `tests/fabricas_db.py` (as duas
fábricas ganharam `sort_order`) · `tests/test_ordem_do_cardapio.py` (novo).

**Portão: 2920 verdes** (2297 rápidos + 623 `db`), ruff limpo, `openapi.json` em
dia, varredura de escrita/commit em 0.

### Para o front: nada muda no contrato

Nenhum campo novo, nenhum campo removido — só a ordem dos arrays
`option_groups` e `option_groups[].options` passa a ser a do painel. Quem já
ordenava do lado do cliente pode parar; quem não ordenava passa a ver a ordem
certa sozinho.

---

## 2. O cancelamento pelo cliente logado

### A escolha: **`POST /customers/me/orders/{order_id}/cancel` pelo Bearer**

Não publicar o `tracking_token`. Cinco motivos, em ordem de peso:

**1. O token é credencial portadora, não um id.** Quem o tem abre o detalhe
inteiro do pedido (nome, telefone, endereço), avalia, consulta o pagamento e
cancela. São 256 bits **sem rota de reemissão e sem prazo**: não dá para
revogar nem para expirar. Publicá-lo transforma uma identidade autenticada e
revogável num segredo compartilhado que não é nem uma coisa nem outra.

**2. Este projeto já o trata como segredo, em três lugares escritos.**
`CreateOrderResponse` diz "devolvido somente aqui, para quem acabou de criar o
pedido"; `AdminErrorReportService` o **mascara** antes de o registro existir; e
o docstring de `GET /customers/me/orders/{id}` já dizia, sobre o caminho
autenticado, "aqui o vínculo sai de `orders.customer_id`, então **não há token
nenhum para guardar nem para vazar**". Publicá-lo contradiria uma decisão que
já estava tomada e documentada.

**3. `OrderDetailResponse` não é só do app.** É o `response_model` de **três
rotas do painel** também (`admin_orders.py`), então o campo cairia na resposta
do lojista junto — um vazamento de credencial para um público que não pediu
nada.

**4. Não resolvia bem nem o caso pedido.** O app teria que buscar o detalhe e
só então chamar a rota do token: duas requisições, e o token passando a existir
no segundo aparelho — memória, log, histórico de URL.

**5. Não tem volta.** Campo publicado não se despublica sem quebrar contrato no
app e no painel.

### O que a rota nova faz

O vínculo é `orders.customer_id`, o mesmo de `get_customer_order` — um cliente
só alcança o próprio pedido mesmo tendo o UUID de outro. Fora isso, **nada
muda**: mesma janela (`pending` e `accepted`, 409 depois), mesmo corpo
opcional, mesma assinatura `cliente` no histórico, e a mesma escrita
(`OrderStatusChangeService`), então cupom, cashback e estorno saem de graça.

**A rota do token continua existindo, e não é redundância**: ela é a única
saída do convidado, que não tem conta para autenticar. As duas portas provam
"fui eu" de jeitos diferentes, e cada uma é a certa para o seu caso.

Três detalhes que não são cópia da outra porta:

- **`restaurant_id` vem do pedido**, não de uma busca por slug. `order.restaurant_id`
  é autoritativo (não veio da URL). E não exigir o restaurante ativo é
  proposital: loja desativada com pedido pago em aberto não pode ser motivo
  para o cliente ficar sem cancelar — e sem o estorno que vem junto;
- **escopo de idempotência próprio** (`CUSTOMER_ACCOUNT_CANCEL_ROUTE`). Nenhuma
  das duas aceita `Idempotency-Key` hoje; o dia em que aceitar, a mesma chave
  chegando pelas duas portas são dois pedidos, e tratá-los como repetição de um
  só devolveria a resposta errada;
- **`requester` leva o cliente** (`cliente:{customer.id}`), e não o pedido. Na
  outra porta não há cliente autenticado, e é por isso que ela usa o pedido.

### Os testes, e o que os torna não-vacuosos

`tests/test_cancelamento_pelo_cliente_logado.py`, 16 testes. O primeiro grupo é
o que torna a **decisão** durável — sem ele o campo volta num PR de dez linhas
e ninguém lembra por que não estava lá:

- `test_o_detalhe_do_pedido_NAO_leva_o_token` — conferido por mutação:
  acrescentando `tracking_token: str` ao `OrderDetailResponse`, ele e só ele
  fica vermelho;
- `test_e_o_teste_acima_nao_e_vacuo` — o campo **existe** e é devolvido, uma vez
  só, no `CreateOrderResponse`. Sem esta metade, renomear o campo deixaria o
  primeiro verde para sempre.

`test_os_estados_recusados_sao_os_mesmos_da_porta_do_token` compara o
**comportamento** das duas portas em cinco estados, e não o código: uma cópia da
janela numa delas passaria por qualquer leitura de diff.

O 404 (e não 403 — 403 confirmaria que o pedido existe) foi conferido por
mutação, tirando o `if order is None`.

**A posse no SQL já tinha teste, e eu não dupliquei:**
`tests/test_order_repository_db.py:381` prova que
`get_order_detail_for_customer` filtra por dono contra o Postgres. As duas
metades ficam cobertas — a consulta filtra, e o service usa essa consulta.

### Arquivos

`src/services/customer_order_cancel_service.py` ·
`src/api/endpoints/customers.py` · `openapi.json` ·
`tests/test_cancelamento_pelo_cliente_logado.py` (novo).

**Portão: 2937 verdes** (2314 rápidos + 623 `db`), ruff limpo, `openapi.json`
regenerado, escopo de tenant e escrita/commit em 0.

---

### Pronto para colar no app

> **Cancelar o próprio pedido estando logado**
>
> Rota nova: `POST /customers/me/orders/{order_id}/cancel`
>
> **Autenticação:** o `Authorization: Bearer <token do cliente>` que vocês já
> mandam em `/customers/me/orders`. **Não** precisa do `tracking_token` — é
> justamente o que ela resolve: o token só vive no `localStorage` do aparelho
> que fez o pedido, e agora o mesmo cliente cancela de qualquer aparelho.
>
> **O `tracking_token` não vai ser publicado no `OrderDetailResponse`.** Ele é
> credencial portadora sem prazo e sem revogação: quem o tem abre o pedido
> inteiro, avalia, vê o pagamento e cancela. O mesmo schema é usado por rotas do
> painel, então ele cairia na resposta do lojista também.
>
> **Corpo (opcional):**
> ```json
> { "reason": "mudei de ideia" }
> ```
> Pode mandar `null` ou omitir. Sem `reason`, o histórico registra "Cancelado
> pelo cliente".
>
> **Sucesso (200):** o `OrderDetailResponse` completo, já com
> `status: "cancelled"` — o mesmo corpo de `GET /customers/me/orders/{id}`.
> Não precisa refazer o GET.
>
> **Erros:**
>
> | Código | Quando | O que mostrar |
> |---|---|---|
> | 401 | sem Bearer, ou token expirado | fluxo de login de sempre |
> | 404 | o pedido não existe **ou não é deste cliente** | "Pedido não encontrado" — os dois casos são o mesmo 404 de propósito, para não confirmar a existência de pedido alheio |
> | 409 | o pedido já saiu da janela (`preparing` em diante) ou já é final | "Este pedido já está em preparo. Fale com o restaurante." — a `detail` traz o texto |
> | 429 | 10/minuto, 60/hora | tentar de novo mais tarde |
>
> **Quando pode chamar:** só em `pending` e `accepted`. A partir de `preparing`
> o insumo já saiu do estoque e quem decide o prejuízo é o lojista. Se vocês já
> escondem o botão fora dessa janela, o 409 é só a rede de segurança.
>
> **O que ela faz junto, e vocês não precisam pedir:** o cupom volta a ficar
> disponível, o cashback resgatado volta para o saldo e **o pagamento online é
> estornado**. Um pix pago e cancelado em seguida volta sem ninguém ligar para o
> restaurante. O estorno acontece depois do commit — se o gateway estiver fora,
> o cancelamento vale e uma varredura devolve o dinheiro depois. **Não** mostrem
> "estorno concluído" na resposta desta rota; mostrem "cancelado, o estorno
> aparece em até X dias".
>
> **Sem `Idempotency-Key`**, e ela não faz falta: o segundo clique chega com o
> pedido já em `cancelled` e leva 409.
>
> **A rota antiga continua valendo.**
> `POST /restaurants/{slug}/orders/track/{token}/cancel` é a saída de quem pediu
> **sem conta** — não removam. A regra simples: tem Bearer, usa a nova; é
> convidado, usa a do token.
