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
| 3 | Vitrine ordena cupom por `sort_order` e o painel não define esse valor | **feito** |
| 4 | Varredura de teste que não roda (veio do erro do item 3) | **feito** |

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

---

## 3. O `sort_order` do cupom

### A lacuna era maior do que "o painel não define"

As **duas** superfícies públicas já ordenavam por `sort_order`, e ordenavam
**diferente**:

| Consulta | Ordem | Alimenta |
|---|---|---|
| `CouponRepository.list_in_window` | `sort_order`, `created_at desc` | lista do cliente, auto-aplicação |
| `MenuRepository.get_active_coupons` | `sort_order` — **e mais nada** | card da vitrine do cardápio |

Como `CouponCreate`/`CouponUpdate` não tinham o campo, **todo cupom estava no
`DEFAULT 0` da coluna** — então o desempate decidia a lista inteira, e as duas
telas mostravam as mesmas campanhas em ordens diferentes. A do cardápio, sem
desempate nenhum, podia trocar de ordem entre duas requisições.

### O conserto, em três partes

**1. O painel escreve.** `sort_order: int = Field(default=0, ge=0)` em
`CouponCampaignFields` — de onde ele flui sozinho para `create_admin` (via
`_campaign_columns`) e para o merge do PATCH — e
`sort_order: int | None = Field(default=None, ge=0)` em `CouponUpdate`.

`ge=0` e não "positivo": zero é a posição normal de quem nunca foi arrastado, e
recusá-lo tornaria inválido o valor que **todo cupom de hoje** já tem gravado.
Como `update_admin` valida o cupom inteiro depois do merge, um
`{"is_active": false}` passaria a dar 422 por causa de um campo que o lojista
nem tocou.

**2. O painel lê de volta.** `sort_order: int` no `CouponAdminResponse` — sem
isso o painel não tem como desenhar a lista na ordem que acabou de gravar.

**3. A ordem vira uma só, e total.** `ordem_dos_cupons()` em
`src/models/coupon_model.py`, mesma medicina do item 1: `sort_order`,
`created_at desc`, `id`. As duas consultas chamam **a função**.

O `id` no fim torna a ordem total — sem ele, dois cupons com o mesmo
`sort_order` criados na mesma transação (`created_at` é um `now()` só) podem
sair trocados. As três colunas são `NOT NULL` no schema, então não há
`NULLS FIRST` escondido: `created_at DESC` com nulo poria a linha sem data no
**topo** da vitrine.

### O que a suíte me ensinou no caminho

**Uma restrição de domínio que eu não conhecia:**
`restaurant_coupons_restaurant_template_unique` — **uma campanha por arte, por
loja**. O primeiro teste caiu com violação de unicidade por reusar o mesmo
template; a fábrica passou a criar uma arte por cupom.

**Classe de teste sem prefixo `Test` não é coletada.** Escrevi
`class OPainelEscreveTests:` e o pytest coletou **zero** casos dela — sem erro
e sem aviso. Os outros arquivos da suíte escapam disso porque herdam de
`unittest.TestCase`, que é coletada por herança e não por nome. Só notei porque
a saída dizia "4 failed" e eu esperava 13 testes. É a mesma família do que
`tests/rotas_do_app.py` documenta: **descoberta vazia não falha, some.**

### Os vermelhos

Duas mutações, uma por metade:

- **tirando os três campos dos schemas** → 5 vermelhos, os do painel;
- **devolvendo as duas consultas às ordens antigas e diferentes** → 3
  vermelhos, os de banco.

E a dupla de anti-vacuidade se pagou aqui: com o campo removido,
`test_negativo_e_recusado` **continua verde** (o `ValidationError` passa a vir
do `extra="forbid"`, não do `ge=0`). Quem denuncia é o par
`test_e_a_recusa_acima_e_do_campo_certo`, que exige que a mesma chamada com
dado correto **não** levante. É o procedimento adotado na rodada 2, e foi a
segunda vez que ele pegou algo.

### Arquivos

`src/schemas/coupon_schema.py` · `src/models/coupon_model.py` ·
`src/repositories/coupon_repository.py` · `src/repositories/menu_repository.py` ·
`openapi.json` · `tests/test_ordem_dos_cupons.py` (novo).

**Portão: 2951 verdes** (2324 rápidos + 627 `db`), ruff limpo, `openapi.json`
regenerado, lockfile em dia, escrita/commit em 0, divergências ORM×schema sem
aviso novo.

---

### Pronto para colar no painel

> **Ordenar os cupons da vitrine**
>
> `sort_order` passou a existir nos três lugares:
>
> - `POST /admin/coupons` — campo **opcional**, inteiro `>= 0`, default `0`.
>   O corpo que vocês mandam hoje continua válido sem mudar nada.
> - `PATCH /admin/coupons/{id}` — campo opcional. Mandem **só** ele para
>   reordenar; o PATCH é parcial e não toca no resto.
> - `GET /admin/coupons` e as respostas de `POST`/`PATCH` — `sort_order` agora
>   vem no corpo, para vocês desenharem a lista na ordem gravada.
>
> **Menor primeiro.** `sort_order: 1` aparece acima de `sort_order: 2`. Para
> arrastar-e-soltar, mandem um PATCH por linha que mudou de posição, ou
> renumerem a lista inteira (0, 1, 2, …) — não há rota de reordenação em lote.
>
> **Cuidado com um detalhe do PATCH:** ele valida o cupom **inteiro** depois de
> fundir o que veio. Isso já era assim, e é por isso que `sort_order` aceita
> `0` — se ele exigisse `>= 1`, todo cupom antigo (que está em `0`) passaria a
> recusar qualquer PATCH, inclusive um `{"is_active": false}`.
>
> **Hoje todos estão em `0`.** Enquanto ninguém arrastar, a ordem é
> `sort_order`, depois **campanha mais nova primeiro**, depois um desempate
> estável. Antes desta mudança essa ordem podia mudar sozinha entre duas
> requisições, e as telas do cliente e do cardápio discordavam — as duas agora
> usam exatamente a mesma.

---

## 4. A varredura de teste mudo

### O item saiu do erro do item 3

`class OPainelEscreveTests:` — sem prefixo `Test` e sem herdar
`unittest.TestCase` — foi coletada **zero** vezes. Nove testes escritos,
revisados e commitados que nunca rodaram. Não falhou, não avisou, não apareceu.

É a família do dublê de dado e da descoberta vazia de `tests/rotas_do_app.py`:
**o portão fica verde porque nada rodou**, e verde por ausência é
indistinguível de verde por acerto.

### A primeira versão estava errada, e o erro define a ferramenta

Comecei por AST: "classe com método `test_*` que não começa com `Test` e não
herda `TestCase`". Ela acusou **sete** classes — e as sete estavam sendo
coletadas o tempo todo:

```
tests/test_delivery_estimate.py :: DefaultDeliveryFeeFallbackTests (9 métodos)
tests/test_delivery_estimate.py :: FaixaDePrazoTests               (7)
tests/test_delivery_estimate.py :: PausaDaEntregaTests             (3)
tests/test_escrita_e_transacao.py :: quatro classes                (18)
```

`DefaultDeliveryFeeFallbackTests(DeliveryEstimateTests)` herda `TestCase` de
**avô**. Nenhum AST razoável resolve isso sem virar um interpretador de
herança — e as regras reais (`python_files`, `python_classes`,
`python_functions`, `norecursedirs`, `__init__` numa classe `Test*`) são do
pytest, mudam com a versão e com o `.ini`. Reimplementá-las seria manter uma
segunda cópia de regra de terceiro, errada exatamente nos casos raros — que são
os que passam despercebidos.

**Então a divisão é:**

| Pergunta | Quem responde |
|---|---|
| o que **é** coletado | o próprio pytest, via `--collect-only` |
| o que **parece** teste | o AST, por um critério grosso: alguém escreveu `def test*` |

O achado é a diferença. **Nenhuma regra de coleta foi reimplementada.**

### O segundo erro: a ferramenta chamada errada

Rodando no `print-agent`, ela acusou **9 arquivos mudos** — os 9 arquivos de
teste do agente, 126 testes. Não era achado: o agente tem `pytest.ini`
próprio, e o `norecursedirs = print-agent` da API faz a coleta devolver zero
quando ela roda da raiz errada. Rodando de dentro, 126 coletados.

Daí a `--raiz`: **cada suíte é auditada a partir da raiz dela**, porque é de lá
que o pytest resolve o `.ini` que define as regras.

### Achado que nomeia, e não que avisa

A primeira execução do teste plantado mostrou os dois arquivos-isca caindo em
"arquivo mudo" em vez de "teste não coletado" — tecnicamente certo (o arquivo
coleta zero) e menos útil. Passou a ser: **se há teste escrito no arquivo, o
achado nomeia quais não rodam**, com arquivo e linha. "Arquivo mudo" ficou
reservado para o caso genuinamente diferente — nome de teste e **nada** que
pareça teste dentro.

### O resultado

**Zero nas duas suítes.** API: 2340 casos, tudo que parece teste é coletado.
`print-agent`: 126 casos, idem.

### A anti-vacuidade

`tests/test_testes_mudos.py`, 15 testes sobre **uma** árvore plantada com iscas
e legítimos convivendo — um `auditar()` por teste custaria um subprocesso de
pytest por caso, e juntos eles provam que os legítimos não apagam as iscas nem
o contrário.

Aqui a anti-vacuidade vale em dobro: o varredor procura **exatamente a falha
que ele próprio poderia ter**. Se as classes deste arquivo não fossem
coletadas, o varredor de teste mudo seria ele mesmo um teste mudo.

**As iscas:** classe sem prefixo; classe `Test*` **com `__init__`** (prefixo
certo e coleta zero mesmo assim — a prova de que ele não está olhando o nome);
arquivo com nome de teste e nada dentro; arquivo com teste dentro e nome fora
do padrão.

**Os legítimos** são os padrões da suíte real, e o terceiro é o que matou a
primeira versão: `class FilhoTests(_Base)` com `_Base(unittest.TestCase)` —
herança **indireta**. Mais: classe de apoio (`FakeRepositorio`, sem método
`test_*`) não pode ser acusada, senão a saída enche de todo dublê da suíte.

Duas mutações fecham: **AST cego** → 5 vermelhos; **pytest cego** → 7. As duas
metades carregam peso.

### No portão: falha, e não aviso

Nunca houve dívida herdada — o único caso nasceu e morreu no mesmo dia. Quem
cobra é a suíte rápida, e ela audita **as duas** suítes do repositório. O passo
do CI é `if: failure()` e só imprime, no job `api` (que é onde a suíte rápida
roda).

### Arquivos

`scripts/testes_mudos.py` (novo) · `tests/test_testes_mudos.py` (novo) ·
`.github/workflows/ci.yml`.

**Portão: 2967 verdes** (2340 rápidos + 627 `db`), ruff limpo. Nada de
produção: o script lê código e roda a COLETA do pytest, que não executa teste
nenhum e não abre banco.
