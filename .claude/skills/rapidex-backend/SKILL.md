---
name: rapidex-backend
description: Convenções e armadilhas do backend Rapidex (FastAPI + SQLAlchemy + Postgres). Use ao escrever, revisar ou depurar qualquer código deste repositório — especialmente pedido, pagamento, comissão, horário de funcionamento, migração Alembic ou impressão de comanda. Contém os bugs que já aconteceram em produção e que voltariam se ninguém soubesse.
---

# Backend Rapidex — convenções e armadilhas

Este arquivo guarda o que **o teste não pega**. Cada armadilha registra o caso
concreto: o que aconteceu, como, e o que custou.

Mapa do repositório: `docs/arquitetura.md`.

---

## Convenções que valem sempre

**Camadas.** `endpoint → service → repository → banco`. O repositório **só**
consulta: não decide, não levanta erro de regra, não commita. Quem commita é o
service, sempre — para várias escritas caírem na mesma transação. `endpoint →
repository` não existe; não crie.

**Dinheiro é `Decimal`.** `float` só na serialização da resposta
(`money_to_float`). Arredondamento com `quantize_money` (2 casas, `ROUND_HALF_UP`).
Nunca `float` no cálculo.

**Nenhum preço vem do cliente.** O corpo do pedido manda `product_id`,
`quantity`, `selected_options`. Todo valor é recalculado no servidor.

**Escopo de lojista vem do token, nunca da URL.** Divergência responde **404, não
403** — 403 confirmaria que aquele recurso existe.

**Nada é apagado no cardápio**, só desativado (`is_active`). `order_items`
referencia `products` por FK; um DELETE quebraria o histórico que o cliente ainda
consulta. Vale para categoria, produto, grupo, opção e setor de impressão.

**Logs não levam dado pessoal.** Mensagem de chat vira digest sha256, chave de
idempotência também, e-mail do pagador sai como `[email]`. Não passe a logar cru
para depurar.

**Schema muda por revisão do Alembic, nunca à mão no Supabase.** Coluna, índice,
constraint, sequence, default — todos. Ver a armadilha 33: foi o schema criado
por fora que fez a baseline nascer no-op, e é o que faria
`alembic/schema_baseline.sql` envelhecer sem ninguém perceber.

**Comentário explica o *porquê*, nunca o *quê*.** O código deste repositório é
comentado com o motivo da decisão estranha. Mantenha esse padrão; comentário que
narra a linha abaixo é ruído.

---

# Como escrever código aqui

**Vale para todo código novo deste repositório**, não só para refatoração.

**Quem lê este código é uma pessoa só, sozinha, que entende Python e SQL sem ser
especialista.** Isso é um requisito, não um detalhe biográfico. Código esperto que
o autor não entende quando volta nele em três meses é, para este projeto, pior que
código simples e repetitivo — porque o repetitivo se lê de cima a baixo às duas da
manhã com a operação parada, e o esperto precisa ser destrinchado antes.

Nenhuma das regras abaixo é sobre estética. Todas são sobre **quanto custa
entender a função na segunda leitura.**

## As regras

1. **Uma função faz uma coisa.** Se o nome precisa de "e" para descrever o que
   ela faz, são duas funções.
2. **Retorno cedo.** Trate o caso de saída primeiro e retorne.
3. **Máximo de 3 níveis de indentação dentro de uma função.**
4. **Nome que dispensa comentário ao lado.**
5. **Comentário só para o PORQUÊ de uma decisão não óbvia**, nunca para o QUE uma
   linha faz.
6. **Decorador escrito à mão: não crie.** Os de rota do FastAPI e os de fixture do
   pytest ficam — são da ferramenta, não nossos.
7. **Repetição clara vence abstração esperta.** Duas funções parecidas que se leem
   inteiras valem mais que uma genérica que precisa ser destrinchada.
8. **Nada de list comprehension aninhada, ternário dentro de ternário, nem
   walrus.**
9. **Type hint em toda assinatura pública.**

Os três exemplos abaixo são **código real deste repositório**, não ilustração
genérica. O "antes" é o que está lá hoje; o "depois" mostra a forma, e é candidato
de refatoração — não algo já feito.

---

### Regra 1 — o nome precisa de "e", então são duas funções

`DeliveryEstimateService.estimate_and_store` (`services/delivery_estimate_service.py`).
O nome é honesto sobre o problema: ela calcula **e** grava.

```python
# ANTES
def estimate_and_store(self, restaurant_slug, payload, current_customer):
    result = self.estimate(restaurant_slug, payload, current_customer)
    if not result.serviceable:
        return result, None

    restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
    branch = self._resolve_branch(restaurant.id, payload.branch_id)
    stored = DeliveryEstimate(...)          # 18 linhas de montagem
    try:
        self.delivery_estimate_repository.create(stored)
        self.db.commit()
    except Exception:
        self.db.rollback()
        raise
    return result, stored
```

O `estimate` puro já existe ao lado — e o próprio docstring diz por que ele é
separado ("continua sem efeito colateral, que é o que permite chamá-lo de dentro
da transação do pedido"). Falta terminar a separação: quem grava vira função
própria, e quem chama compõe as duas.

```python
# DEPOIS
def store_estimate(
    self,
    restaurant_slug: str,
    payload: DeliveryEstimateRequest,
    current_customer: Customer | None,
    result: DeliveryEstimateResult,
) -> DeliveryEstimate | None:
    # Fora de area e loja fechada nao geram token: os dois precisam ser
    # reavaliados no momento do pedido, nao reaproveitados.
    if not result.serviceable:
        return None
    ...
    return stored


# no endpoint, as duas na ordem obvia:
result = service.estimate(slug, payload, customer)
stored = service.store_estimate(slug, payload, customer, result)
```

O ganho não é o tamanho: é que **"calcular" e "gravar" passam a poder ser lidos,
testados e mudados separados** — e o retorno deixa de ser uma tupla cujo segundo
item às vezes é `None` por um motivo que só o corpo explica.

---

### Regra 2 — retorno cedo

`AuthService.resend_email_code` (`services/auth_service.py`). Todo o corpo vive
dentro de um `if`, e a mesma resposta é construída em dois lugares:

```python
# ANTES
def resend_email_code(self, payload: ResendEmailCodeRequest) -> MessageResponse:
    email = normalize_email(payload.email)
    customer = self.customer_repository.get_by_email(email)
    if customer and not customer.email_verified_at:
        latest = self.customer_repository.latest_unused_email_code(email)
        if latest and latest.created_at and (utcnow() - latest.created_at).total_seconds() < RESEND_COOLDOWN_SECONDS:
            return MessageResponse(message="Se este e-mail estiver cadastrado, enviaremos um codigo de verificacao.")
        recent_count = self.customer_repository.count_email_codes_since(
            email=email,
            since=utcnow() - timedelta(minutes=RESEND_WINDOW_MINUTES),
        )
        if recent_count < MAX_RESENDS:
            resend_count = (latest.resend_count + 1) if latest else 0
            self._create_email_verification_code(customer, resend_count=resend_count)
            self.db.commit()

    return MessageResponse(message="Se este e-mail estiver cadastrado, enviaremos um codigo de verificacao.")
```

Cada saída antecipada vira uma linha, e a condição de cooldown — que hoje é uma
linha de 120 caracteres com três `and` — vira uma função com nome:

```python
# DEPOIS
def resend_email_code(self, payload: ResendEmailCodeRequest) -> MessageResponse:
    # Resposta unica em TODO caminho, inclusive e-mail inexistente: variar a
    # mensagem deixaria enumerar a base de clientes (armadilha 18).
    answer = MessageResponse(message="Se este e-mail estiver cadastrado, enviaremos um codigo de verificacao.")

    email = normalize_email(payload.email)
    customer = self.customer_repository.get_by_email(email)
    if not customer:
        return answer
    if customer.email_verified_at:
        return answer

    latest = self.customer_repository.latest_unused_email_code(email)
    if self._is_in_resend_cooldown(latest):
        return answer
    if self._resends_in_window(email) >= MAX_RESENDS:
        return answer

    resend_count = (latest.resend_count + 1) if latest else 0
    self._create_email_verification_code(customer, resend_count=resend_count)
    self.db.commit()
    return answer
```

Quatro níveis de indentação viram dois, e as quatro razões para não reenviar ficam
alinhadas na mesma coluna — dá para conferir a lista batendo o olho.

---

### Regra 3 — no máximo 3 níveis de indentação

`MenuService.product_response` (`services/menu_service.py`). É o pior caso do
repositório: uma list comprehension dentro de outra, cada uma com `sorted` de um
generator com `lambda` dentro.

```python
# ANTES
@staticmethod
def product_response(product) -> ProductResponse:      # <- sem type hint em `product`
    return ProductResponse(
        ...
        option_groups=[
            ProductOptionGroupResponse(
                id=group.id,
                ...
                options=[
                    ProductOptionResponse(
                        id=option.id,
                        ...
                    )
                    for option in sorted(
                        (option for option in group.options if option.is_active),
                        key=lambda option: (option.sort_order or 0, option.name),
                    )
                ],
            )
            for group in sorted(
                (group for group in product.option_groups if group.is_active),
                key=lambda group: (group.sort_order or 0, group.name),
            )
        ],
    )
```

O filtro-e-ordenação é **o mesmo nos dois níveis** — e é justamente a repetição que
paga uma função, porque ela tem nome e a regra passa a estar escrita uma vez:

```python
# DEPOIS
@staticmethod
def _active_in_menu_order(items: list) -> list:
    """Ativos, na ordem do cardapio. O nome desempata sort_order repetido —
    sem isso a mesma tela sai com os adicionais em ordens diferentes."""
    active = [item for item in items if item.is_active]
    return sorted(active, key=lambda item: (item.sort_order or 0, item.name))


@staticmethod
def _option_response(option: ProductOption) -> ProductOptionResponse:
    return ProductOptionResponse(...)


@staticmethod
def _option_group_response(group: ProductOptionGroup) -> ProductOptionGroupResponse:
    return ProductOptionGroupResponse(
        ...
        options=[
            MenuService._option_response(option)
            for option in MenuService._active_in_menu_order(group.options)
        ],
    )


@staticmethod
def product_response(product: Product) -> ProductResponse:
    return ProductResponse(
        ...
        option_groups=[
            MenuService._option_group_response(group)
            for group in MenuService._active_in_menu_order(product.option_groups)
        ],
    )
```

Nenhuma comprehension aninhada sobrou, o `product` ganhou o type hint que faltava
(regra 9), e cada uma das três funções cabe na tela.

---

## 1. `weekday` 0 = segunda no backend, 0 = domingo no JavaScript

O backend usa `datetime.weekday()` do Python: **0 = segunda, 6 = domingo**. Está
no CHECK do banco (`ck_branch_business_hours_weekday`), no schema
(`BusinessHourInput.weekday`) e em `BranchHoursService`.

O `getDay()` do JavaScript devolve **0 = domingo**. Os dois não conversam
sozinhos.

**Sintoma:** o lojista configura segunda no painel e a loja abre no domingo — ou
fica fechada na segunda e ninguém entende por quê, porque a tela mostra o que ele
digitou.

Nenhum teste do backend pega isso: do lado de cá o número está consistente.

---

## 2. `unit_price_snapshot` já vem com os adicionais somados

`OrderService._build_order_item` grava:

```python
unit_price = quantize_money(to_decimal(product.price) + options_total)
total      = quantize_money(unit_price * quantity)
```

Ou seja, `unit_price_snapshot` **não é** `products.price`. É o preço do produto
**mais** a soma dos `additional_price` das opções escolhidas.

**O que quebra se você esquecer:** somar os adicionais de novo ao montar
relatório, comanda ou tela de detalhe. O adicional entra em dobro no valor
exibido, e o total do pedido não bate com a soma das linhas — o cliente reclama
antes de alguém notar.

Os adicionais também estão gravados individualmente em `order_item_options`
(`additional_price_snapshot`), o que torna a soma dupla fácil de fazer sem
perceber.

---

## 3. `PUT` de horários substitui a semana inteira

`PUT /admin/branches/{branch_id}/business-hours` apaga **todas** as faixas da
filial e grava as do corpo.

**Dia ausente do corpo = dia fechado.** Não é merge, não é patch.

**Sintoma:** o painel manda só o dia que o lojista editou, e a loja fica fechada
nos outros seis dias. Nenhum erro, nenhum log — só nenhum pedido entrando.

Correlato: sem faixa cadastrada para o weekday de hoje, `ensure_branch_is_open`
recusa **todo** pedido, inclusive retirada.

---

## 4. `if_not_exists` em índice: depende de quem criou a tabela

- Índice sobre tabela **do baseline** (existe em produção, possivelmente criada à
  mão): `create_index(..., if_not_exists=True)` e `drop_index(..., if_exists=True)`.
- Índice sobre tabela **criada na própria revisão**: fica **estrito**. A tabela
  nasce vazia, não há colisão possível, e a permissividade só esconderia erro.

**E o mais importante: `IF NOT EXISTS` casa por NOME, não por definição.**

Aconteceu com `order_items.order_id`: o índice já existia em produção, criado à
mão como `idx_order_items_order_id`. Criar `ix_order_items_order_id` com
`IF NOT EXISTS` **não** teria detectado nada — para o Postgres seriam dois
índices sobre a mesma coluna, os dois mantidos em toda escrita, dobrando o custo
de INSERT de item de pedido sem servir a nenhuma consulta a mais.

A revisão `20260810_0012` resolve **adotando** o índice existente: DROP do nome
antigo e CREATE do canônico, na mesma transação, DROP primeiro para não manter as
duas cópias em disco.

`scripts/audit_indexes.py` procura as duas coisas — colisão de nome e duplicata
de definição.

**`admin_users` teve a mesma duplicata, e a revisão `20260823_0036` a
fechou.** Achada em 22/08/2026 rodando o audit: `idx_admin_users_restaurant_id`
e `ix_admin_users_restaurant_id` existiam os dois, sobre a mesma coluna, os
dois no `schema_baseline.sql` — quer dizer, no banco de verdade.

Foi a revisão `20260726_0003` que criou o segundo: `CREATE INDEX IF NOT EXISTS
ix_admin_users_restaurant_id` sobre uma tabela que já tinha o `idx_`. É esta
armadilha inteira, materializada, e ninguém percebeu porque nada falha — o
Postgres só mantém os dois em toda escrita.

O conserto foi o mesmo da `20260810_0012` (DROP do antigo, CREATE do canônico,
mesma transação, DROP primeiro), em revisão própria. Não foi feito junto da
`20260822_0032`, que fez essa adoção em `cashback_transactions`: são tabelas
diferentes e motivos diferentes de mexer, e misturar as duas faria um rollback
de uma arrastar a outra.

**Uma diferença entre as duas que só aparece no `downgrade`.** A `0012`
derruba o nome canônico ao voltar, porque foi ela quem o criou. A `0036` não
derruba: quem criou o `ix_` de `admin_users` foi a `0003`, e derrubá-lo ali
deixaria sem índice nenhum quem parasse entre as duas revisões. A regra é
"desfaça o que ESTA revisão fez", não "termine sem o índice".

E o audit tem um falso positivo conhecido: ele lista o mesmo índice repetido
(`restaurants_pkey (16 kB), restaurants_pkey (16 kB), ...`) uma vez por FK que
o referencia. Par com **dois nomes diferentes** é o achado de verdade; nome
repetido é ruído.

---

## 5. O container roda `alembic upgrade head` antes do Uvicorn

`docker-entrypoint.sh`, com `set -e` e sem `|| true`.

**Migração ruim vira loop de restart.** O container morre, o `restart: always`
tenta de novo. É melhor que uma API de pé contra o schema errado — mas quando
"não sobe", olhe a saída do alembic **antes** de qualquer outra coisa.

**E `CREATE INDEX` sem `CONCURRENTLY` trava escrita na tabela com a API fora do
ar.** Numa tabela grande (`order_items` é a maior), aplicar em horário de pico é
derrubar a operação: nenhum pedido novo é gravado enquanto o índice constrói.
Rode fora do movimento, ou faça à mão com `CREATE INDEX CONCURRENTLY` (que não
roda dentro de transação) e depois `alembic stamp <revisão>`.

Correlato: `.gitattributes` força LF em `*.sh`. O repositório é editado no Windows
com `core.autocrlf=true`, e um shebang terminado em `\r` vira
`exec /app/docker-entrypoint.sh: no such file or directory`.

---

## 6. Chave de idempotência do gateway é por TENTATIVA, não por pedido

A chave era `str(order_id)`, constante para sempre. O Mercado Pago devolve a
**mesma** cobrança quando a mesma chave chega de novo — certo num retry, errado
em dois casos:

- **retry depois de uma cobrança recusada:** a chave antiga devolvia a própria
  cobrança recusada. **O pedido nunca mais teria como ser pago.**
- **tentativa com corpo diferente** (total mudou, ou o cliente logou e o
  `payer.email` deixou de ser o sintético de convidado): mesma chave com corpo
  diferente é **conflito** de idempotência, não retry, e conflito eles respondem
  com erro.

Hoje: `{order_id}:{sha256(corpo + cobrança substituída)}`. O `previous_payment_id`
só vem preenchido quando `payment_status == "failed"` — com `pending` fica `None`
**de propósito**, para a chave se repetir e um segundo clique em "pagar" devolver
o mesmo pix em vez de abrir um segundo QR code.

Se você mexer em `_mercadopago_idempotency_key` ou no corpo da cobrança, essas
duas propriedades precisam continuar valendo.

---

## 7. Resposta de idempotência gravada por versão anterior da API

A Fase 2 acrescentou campos **obrigatórios** em `CreateOrderResponse`
(`tracking_token`, `payment_flow`, `payment_status`). Uma `Idempotency-Key`
criada **antes** do deploy fica até 24h no banco com a resposta antiga, e o replay
dela quebrava na validação do Pydantic → 500.

Hoje `IdempotencyService.parse_stored_response` responde **409 pedindo chave
nova**. As duas alternativas eram piores: 500 não diz ao cliente o que fazer, e
ignorar a chave criaria um segundo pedido — exatamente o que ela existe para
impedir.

**A regra que fica:** acrescentar campo obrigatório em qualquer response que seja
gravada em `idempotency_keys.response_body` tem custo de 24h de 409s. Campo novo
com default é de graça; campo obrigatório não.

---

## 8. Não commite entre `begin()` da idempotência e o `try` de `create_order`

A reserva da chave fica na **mesma transação** do INSERT do pedido. É isso que faz
o mecanismo funcionar: se algo falhar antes do commit, o rollback leva a reserva
junto e a chave volta a ser utilizável.

Entre `begin()` e o `try` há validações que podem levantar `HTTPException`, e
**não há `rollback()` explícito** nesse trecho. Está correto — a sessão é fechada
sem commit no `finally` do `get_db` e o Postgres descarta tudo — mas é implícito,
e olhando o código isolado parece vazamento.

**Se alguém colocar um `db.commit()` nesse trecho**, a reserva passa a ser
persistida sem o pedido, e a chave fica queimada por 24h devolvendo 409 para um
pedido que nunca existiu.

---

## 9. Estado terminal é terminal — o caso do cupom estornado em dobro

`update_order_status` validava apenas que o status **existia** em
`ORDER_STATUSES`. Dava para ir de `cancelled` para `completed`: o pedido
cancelado, cujo cupom já tinha sido estornado, era faturado e **o cliente levava o
desconto duas vezes**.

Hoje o grafo é explícito em `order_state_machine.py`, e `completed`, `cancelled` e
`rejected` são terminais. `rejected` entrou na lista pelo mesmo motivo:
"desrecusar" traz de volta o mesmo problema.

Duas ordens que importam:

- A validação da transição roda **depois** do replay de idempotência. Validando
  antes, um retry legítimo da mesma chave morreria com "o pedido já está em
  accepted" em vez de devolver a resposta gravada.
- `ensure_payment_allows_order_status` **nunca** bloqueia `cancelled`/`rejected` —
  são justamente a saída para o pagamento que não chegou.

`PATCH /admin/orders/{id}/cancel` **não** é uma segunda escrita de status: delega
para o mesmo `_apply_status_change`. Duas escritas independentes seriam a chance
de a máquina valer numa e não na outra.

---

## 10. A faixa de horário "mais parecida" não existe

Quando a hora atual não caía em nenhuma faixa do dia, o código pegava a
**primeira faixa cadastrada**. Às 3h da manhã, um pedido passava com o tempo de
preparo do almoço — **e passava com a loja fechada**.

Segundo bug do mesmo lugar: `is_closed` era ignorado. Uma linha marcada como
fechada mas com horários preenchidos (o jeito comum de registrar "domingo
fechado") **abria** a filial.

Hoje: ou existe faixa que contém o agora, ou está fechado. Ponto.

Consequência de desenho: faixa que vira a noite (18:00–02:00) pertence ao weekday
em que **começa**, então `find_current_period` consulta **hoje e ontem**. À 1h da
manhã de terça, quem está aberto é a faixa da segunda.

E por isso `PATCH /admin/branches/{id}/prep-time` responde **409** fora do horário
de funcionamento, em vez de escolher uma faixa por conta própria: escolher "a mais
parecida" foi exatamente o bug acima.

---

## 11. `default_delivery_fee = 0` não é fallback

Quando o Google cai, a taxa cai para o `default_delivery_fee` **resolvido** (o
da filial quando ela sobrescreveu, o do restaurante quando não — armadilha 35) —
**mas só se for maior que zero.**

A coluna é nullable com default 0, e a maioria das linhas em produção está em 0
sem ninguém ter escolhido isso. Tratar esse 0 como "entrega grátis na
contingência" faria uma queda do Google virar **frete grátis para a plataforma
inteira**, sem nenhum lojista ter pedido.

Consequência: filial sem `default_delivery_fee` — nem nela, nem no padrão do
restaurante — continua recusando todo pedido de entrega quando o Google cai. A saída operacional é a
retirada.

O preço: não dá para configurar "entrega grátis quando a rota falha". Quem quer
entrega grátis põe `delivery_base_fee = 0` na filial.

---

## 12. A estimativa reaproveitada não pode trazer valor do cliente

`delivery_estimate_token` é otimização de custo, **nunca** fonte de valor. O
cliente devolve só o token; taxa, distância e prazo saem de `delivery_estimates`.

A checagem que importa é o `address_fingerprint`: sem ela, o cliente estimaria o
endereço a 500m e fecharia o pedido para o de 15km **pagando a taxa do primeiro**.

Também é descartada quando o cliente é outro (a estimativa pode ter usado endereço
salvo da conta), quando a filial é outra e quando venceu (15 min).

Qualquer divergência simplesmente chama o Google de novo — no pior caso pagamos a
chamada que pagaríamos antes. Se você acrescentar campo à estimativa, ele entra no
fingerprint ou não entra na decisão.

---

## 13. Item nunca some da comanda

Item cujo setor não serve vai para a via **`"SEM SETOR"`** e o caso é logado,
em vez de desaparecer da produção.

**Esta armadilha perdeu metade em 20/08/2026, e é a única do arquivo que
encolheu — vale saber por quê.** O texto era: "o setor pende de filial e o
produto pende de restaurante, logo um produto só consegue apontar para o setor
de UMA filial; o pedido da filial B pode conter produto apontando para setor da
filial A". Aquilo nunca foi bug de código — era o **único desenho possível**
enquanto o cardápio era do restaurante, e a via de resgate era o conserto certo
para ele.

Com o cardápio por filial (revisão `20260820_0026`), produto e setor passaram a
viver na mesma loja e a FK composta `(branch_id, printing_sector_id)` tornou
esse par **impossível de gravar**. A checagem correspondente saiu de
`_split_items_by_sector` junto com o motivo: era um ramo que nenhum dado
alcança, e um dia alguém gastaria uma tarde tentando reproduzi-lo.

**A lição que fica não é sobre impressão.** Quando uma armadilha existe porque
o schema não consegue expressar a regra, o conserto definitivo é o schema — e
aí o tratamento em código sai junto, senão vira ruína que parece proteção.

Os dois motivos que sobraram, e que nenhuma FK fecha:

- **setor desativado** depois de vinculado — `is_active` é estado, não
  integridade referencial;
- **produto que não está mais naquele restaurante** — o item guarda
  `product_id` para sempre.

**O único jeito de um item NÃO ser impresso** é `printing_sector_id` nulo, que é
decisão explícita do lojista (a lata que sai da geladeira do balcão).

E: **pedido com pagamento online não confirmado não gera via de produção.**
Comanda impressa é ordem de produção — se saísse, a praça prepararia o pedido do
mesmo jeito e a regra do painel valeria só para quem olha a tela. A via do
cliente continua saindo.

---

## 14. Os adicionais precisam de `sorted` explícito

`order_item_options` **não tem coluna de ordem nem `created_at`**, e o id é
`gen_random_uuid()`. Sem `sorted`, a mesma comanda sai com os adicionais
embaralhados a cada requisição, e quem confere o pedido pela posição na tela erra.

Alfabética é a única ordem estável disponível — a do cardápio (`sort_order` do
grupo) não foi congelada no pedido.

Não "otimize" removendo esse `sorted`.

---

## 15. Duas listas de forma de pagamento têm que mudar juntas

`PAYMENT_METHODS` (`core/constants.py`) espelha o CHECK de
`branch_payment_methods.method_type`.

**Se um método entrar no banco e não na constante**, a filial consegue oferecê-lo
no cardápio e o pedido é **recusado na criação** com 400. O lojista vê a opção na
tela do cliente e o cliente não consegue fechar.

Contexto: antes, `payment_method` era texto livre de 50 caracteres gravado direto
no pedido. Dava para fechar pedido com `payment_method="banana"`, e a filial
recebia um pedido numa forma que ela não aceita.

Efeito colateral de `_resolve_payment_flow`: quando a filial oferece o **mesmo
método nos dois fluxos** (pix pelo gateway e pix na entrega), vale `online` — o
caminho restritivo. Não há campo para o cliente escolher, e errar para o lado
restritivo não entrega comida de graça.

---

## 16. `HTTPException` embrulha tudo em `detail` — o OpenAPI precisa dizer isso

O `model` das respostas 502/503 do pagamento anunciava `PaymentErrorDetail` na
**raiz**, mas o FastAPI entrega `{"detail": {...}}`. O documento descrevia um
formato que a rota **nunca** devolve, e o frontend escreveu o parser contra ele.

Hoje o model é `PaymentErrorResponse`, o envelope.
`tests/test_new_route_contracts.py::PaymentErrorContractTests` trava isso no
`/openapi.json` **gerado**, não no código — formato que existe no service mas não
no documento é formato que ninguém consegue consumir sem ler o backend.

Regra geral: **o painel consome o `/openapi.json`.** Renomear campo, schema ou
rota que apareça lá quebra o painel junto. Trate como mudança de contrato, não
como refatoração.

Correlato: códigos de erro que o frontend precisa distinguir vão como `str, Enum`
(`PaymentErrorCode`), não como constantes soltas — só assim a **lista** sai no
documento.

---

## 17. `platform_commission_percent` não aparece em nenhum schema do painel

Nem para leitura. É o percentual que a plataforma cobra do restaurante, negociado
por contrato. Publicá-lo no OpenAPI do painel do lojista o transformaria em campo
de tela, e editá-lo seria **o lojista escolhendo quanto nos paga**.

Quem precisa dele é o relatório de comissão, que só mostra o valor **já
congelado** em cada pedido.

Se você for "completar" o `AdminRestaurantSettingsResponse` com os campos que
faltam da tabela, esse fica de fora.

---

## 18. Segredo se compara com `hmac.compare_digest`

`!=` aborta no primeiro byte divergente e permite recuperar a chave byte a byte
por timing. Aconteceu com `INTERNAL_API_KEY`.

Vale para assinatura de webhook, token de acompanhamento, código de verificação —
qualquer comparação de segredo.

Do mesmo grupo: **`/auth/forgot-password` respondia 404 para e-mail não
cadastrado**, permitindo enumerar a base de clientes. Hoje responde sempre a mesma
mensagem, mantém o mesmo perfil de consultas ao banco nos dois caminhos e aplica
um **piso de latência** — porque o tempo de resposta também vaza. Falhas internas
são logadas, não propagadas.

E no login de lojista, `verify_password` roda **mesmo sem usuário encontrado**,
senão o tempo do bcrypt denunciaria quais e-mails existem.

---

## 19. `order_number` é sequence GLOBAL e previsível

A rota `GET /orders/{order_number}?phone=...` casava um número de sequence global
com o telefone. **Com o telefone de alguém, dava para varrer os números vizinhos e
ler endereço residencial, itens e histórico de outras pessoas.**

Foi removida. Em lugar dela, `tracking_token` (`secrets.token_urlsafe(32)`),
que não é enumerável.

**O token é gravado em HASH** desde as revisões 0016/0017:
`orders.tracking_token_hash`, sha-256 sem chave, UNIQUE. A coluna em claro não
existe mais e o model não a mapeia. A busca hasheia o que chega pela URL antes
do `WHERE`.

sha-256 e não `_hmac_hex` (que é o dos outros segredos) porque chave compra
resistência a **força bruta sobre a entrada**, e a entrada aqui tem 256 bits —
enquanto o código de verificação tem seis dígitos e por isso precisa da chave.
Em troca não existe variável de ambiente cuja perda apague o acesso de todo
cliente ao próprio pedido de uma vez. **Trocar isso por HMAC mata todo link em
circulação**, e não há como devolvê-los.

Três consequências:

- **O token só sai da API uma vez**, em `CreateOrderResponse`. Não há rota para
  reemitir — o segredo **é** a chave de acesso, e uma rota de "me manda de novo"
  precisaria autenticar por telefone e número de pedido, que é exatamente o buraco
  que se fechou. O front tem que guardar (localStorage) ao criar pedido de
  convidado.
- Agora nem o servidor consegue reemitir: o banco só tem o hash.
- **O token em claro continua gravado em `idempotency_keys.response_body`**, por
  até 24h, porque a resposta inteira é armazenada lá e o replay precisa devolver
  o mesmo token. É o resíduo que o hash **não** fecha: um dump vazado ainda
  entrega os pedidos das últimas 24h. Conta na hora de decidir quem lê aquela
  tabela e por quanto tempo ela é retida.

E: `order_number` continua sendo sequence global e UNIQUE na tabela inteira, não
numeração por restaurante. O primeiro pedido de um restaurante novo pode ser o
5471.

---

## 20. Nada em memória sobrevive a mais de um worker

Três estruturas são dicionários no processo:

| O quê | Efeito com N workers |
|---|---|
| Histórico de sessão do chat | o Rapi "esquece" a conversa quando cai em outro worker |
| Cache de estimativa de entrega | chamadas repetidas (e pagas) ao Google |
| Contadores de rate limit | limite efetivo vira N × o configurado |

`REDIS_URL` resolve os dois últimos sem tocar em código. **O histórico do chat não
tem caminho de Redis** — continuaria em memória.

Foi por isso que o stream SSE do painel **não** usa fila em memória: com mais de
um worker, o pedido criado no worker A nunca chegaria ao painel conectado no
worker B, e um deploy no meio do almoço perderia a fila. O cursor mora no banco.

---

## 21. O tipo da imagem sai dos bytes, não do `content-type`

O `content_type` do multipart é escolhido pelo cliente e não vale nada como
validação: quem quiser envia um HTML dizendo `image/png`.

O bucket serve os arquivos **publicamente**, então um HTML ou SVG aceito ali
viraria script executando no domínio do Storage.

`detect_image_extension` lê a assinatura binária (JPEG, PNG, WEBP/RIFF). **SVG
fica de fora de propósito**: é XML, aceita `<script>` dentro e não tem assinatura
binária para conferir.

---

## 22. O regex de CORS precisa do escopo e do `$`

Previews do painel na Vercel não dão para listar (o subdomínio muda a cada
deploy), então há `allow_origin_regex`.

Duas partes que **não** são decoração:

- o escopo `cauas-projects-3c9f6aea`. Sem ele, `.*\.vercel\.app` aceitaria
  qualquer app hospedado na Vercel — **de qualquer pessoa** — chamando a API com
  credenciais.
- o `$` no fim. O Starlette de hoje compara com `fullmatch`, mas versões antigas
  usavam `match`, e aí um `...vercel.app.atacante.com` passaria.

O `CORSMiddleware` é adicionado **por último** para ficar na camada mais externa:
assim até 413 e 429 saem com os cabeçalhos de CORS.

---

## 23. `is_available` gera 400 mudo no checkout

`product_repository.list_active_by_ids` exige `is_active` **e** `is_available`.

Se o lojista marcar um produto como indisponível enquanto o cliente está com ele
no carrinho, o pedido falha com `"Produto inválido ou indisponível"` **sem dizer
qual produto**. É proposital — não virar oráculo de quais UUIDs existem — mas na
hora de depurar significa comparar os `product_id` do corpo com o banco.

---

## 24. `alembic --autogenerate` propõe apagar metade do banco

O schema nasceu antes do Alembic e tem objetos que o ORM não mapeia: sequences
(incluindo a de `order_number`), defaults e índices criados à mão nos 12 `.sql` de
`migrations/`. O autogenerate compara ORM × banco e propõe `DROP` em tudo que não
achar no ORM.

**Leia sempre o arquivo gerado antes de aplicar, e apague as linhas de drop.**

E em banco que já tem o schema, a baseline é `alembic stamp 20260726_0001`, nunca
`upgrade` — aplicar a baseline como migração falha sobre tabelas que já existem.

---

## 25. Dinheiro de cliente parado tem DOIS logs, e a ordem dos eventos decide qual

O estorno só acontece quando o gateway avisa (webhook `refunded`) ou quando alguém
o faz no painel do próprio gateway. Nada aqui estorna sozinho.

**A ordem em que as duas coisas acontecem produz logs diferentes, em arquivos
diferentes, e cada um cobre só a metade dele.**

**Pagou primeiro, o lojista cancelou depois** — `AdminOrderService._log_cancellation_of_paid_order`:

```
[Pagamento] pedido pago foi cancelled sem estorno automatico order_id=...
```

**O lojista recusou primeiro, o pagamento entrou depois** — `PaymentService._log_payment_on_terminal_order`:

```
[Pagamento] pagamento confirmado em pedido ja rejected sem estorno automatico order_id=...
```

A segunda metade ficou **sem rastro nenhum até 23/08/2026**, e é a corrida mais
provável das duas: `handle_webhook` valida só a transição de **pagamento**, e
`pending → paid` é válida qualquer que seja o `orders.status`. O pedido termina
`rejected` + `paid` sem que nada recuse a escrita — corretamente, porque recusar
esconderia o dinheiro que de fato entrou.

**E o faturamento não denuncia:** `billable_order_conditions` exclui `cancelled` e
`rejected` da comissão, o que está certo e é justamente o que faz o pedido sumir
do extrato levando junto a única pista de que havia dinheiro nele.

O grep que vale no radar cobre os dois: `sem estorno automatico`.

Isso fica mais frequente com **cartão**: o antifraude do Mercado Pago pode segurar
um pagamento em análise por até 48h úteis, e nenhum lojista espera 48h para
recusar um pedido de almoço.

---

## 26. O cashback está ligado no código, e desligado nos dados

**Isto aqui era o oposto até 22/08/2026** ("cashback está pela metade, não
confie no saldo"). O crédito na conclusão, o resgate no `create_order` com lock
e a devolução no cancelamento existem, e `cashback_redeemed_amount` deixou de
gravar zero sempre. Desenho inteiro em `docs/cashback.md`.

**O que segura tudo é um dado, não o código: `cashback_rules.enabled` nasce
FALSO em todo restaurante.** Enquanto for falso, `resolve_cashback_terms`
devolve `SEM_CASHBACK` e o razão continua sem escritor. Ligar a chave de um
restaurante liga o crédito e o resgate ao mesmo tempo — e o resgate entra como
subtração na base da comissão, então **ligar cashback mexe em faturamento no
mesmo minuto.** Não é botão de teste.

**E desde 23/08/2026 esse botão existe de verdade**, em
`PUT /admin/cashback-rules` — antes a chave só virava por SQL, o que na
prática significava que ninguém a virava sem querer. Por isso as rotas de
escrita são **SOMENTE_DONO**, e por isso `earns_cashback` (que escolhe em
quais formas de pagamento o dinheiro sai) também é, por
`ensure_pode_definir_cashback` — a terceira exceção a `exigir_papel`. Sem
ela o dono definiria o percentual e o gerente escolheria onde ele sai, que é
meia decisão de cada lado da mesma campanha.

**Duas coisas que continuam valendo:**

- **O saldo é por (cliente, restaurante), nunca da plataforma.**
  `get_available_balance` soma tudo e serve para duas coisas só: o campo
  `balance` (o *acumulado*) e o que se perde ao excluir a conta. Para decidir
  quanto entra num pedido é `get_available_balance_for_restaurant` — usar a
  soma ali seria gastar no Varjota o dinheiro que o Júnior concedeu.
- **A validade é do SALDO, e o relógio é o ÚLTIMO PEDIDO.** Não há FIFO, não
  há lote, não há crédito partido ao meio: um relógio por (cliente,
  restaurante), que todo pedido reinicia, e quando vence o saldo daquele par
  vira zero de uma vez. `cashback_transactions.expires_at` fica **sempre
  nulo** — preenchê-lo criaria a segunda resposta para "quando isto vence".
  Quem responde é `expires_at_from_last_order`, e ela é chamada pela tela
  (`GET /customers/me/cashback`) **e** pela varredura, de propósito.
- **Quem vence é `scripts/expire_cashback.py`, no container `limpeza`, e ele
  não é o `cleanup_idempotency_keys.py`.** Aquele arquivo tem o contrato
  "apagar linha vencida não afeta correção" no docstring; aqui um bug apaga
  **dinheiro de cliente**. Três coisas dele que parecem detalhe e não são: ele
  trava o cliente e **reconfere o vencimento já travado** (a lista é lida sem
  lock, e um pedido feito no meio da varredura salva o saldo); a linha
  negativa nasce com `status="expired"`, senão o saldo fica **negativo**; e
  ela **não leva `idempotency_key`** — uma chave derivada da data prenderia
  em `available`, para sempre e sem erro, o saldo lançado a mão depois.
- **Campanha desligada CONGELA o saldo, não o vence.** A regra desligada cai
  em `SEM_CASHBACK`, a tela passa a responder `expires_at: null` e a varredura
  concorda. Fazer a varredura vencer assim faria o app mostrar "não vence" no
  dia anterior ao saldo sumir.

---

## 27. A normalização de telefone é redundante de propósito

`normalize_digits` aparece no schema do pedido **e** na escrita do snapshot. Não é
duplicação a remover: o snapshot também pode vir de `current_customer.phone`, que
o schema do pedido não toca — e contas antigas podem não estar normalizadas.

O bug original: `customer_phone_snapshot` era gravado cru, mas a consulta comparava
por igualdade exata. Quem digitava `(85) 99999-9999` no checkout não achava o
próprio pedido depois procurando por `85999999999`. E o escopo da idempotência já
normalizava o mesmo campo, então os dois lados discordavam.

**Resíduo conhecido:** a normalização é dígitos-apenas, então `+55 85 99999-9999`
vira `5585999999999` e **não** casa com `85999999999`. O DDI não é removido. Só
vira problema se o frontend passar a mandar DDI; o lugar de resolver é
`normalize_digits`, nunca um dos pontos isolados.

---

## 28. `"Filé"` tem duas formas Unicode, e a CP850 só conhece uma

A térmica **não entende UTF-8**. Ela lê byte a byte na tabela que o `ESC t n`
seleciona, e `escpos.encode_text` converte o texto para essa tabela antes de
enviar. Isso sempre esteve certo.

O que estava errado era a queda de qualidade, e o caso concreto é o cardápio do
Júnior da Picanha — **82 dos 161 produtos têm acento**:

- **`"Filé"` decomposto** (`e` + acento combinante, o que teclado de iPhone/macOS
  produz) é idêntico na tela ao composto e **não existe na CP850**. Sem
  `unicodedata.normalize("NFC", ...)` antes de codificar, o cardápio inteiro
  imprimia **sem um acento** — com a codepage certa, a impressora certa, e nada
  no log.
- **Um emoji rebaixava a comanda toda.** O `_strip_accents` rodava no texto
  inteiro quando qualquer caractere falhava, então `"Sortidão 🍖 Filé"` saía
  `"Sortidao ? File"`. Hoje a degradação é **por caractere**
  (`_encode_degrading`): só o impossível vira `?`.

E `[printing] codepage` + `encoding` são **duas** configurações que têm que
concordar: a primeira diz à impressora qual tabela usar, a segunda diz ao Python
em quais bytes escrever. Trocar só uma imprime `"Picanha Ó Moda"` sem erro em
lugar nenhum — é o defeito mais caro de diagnosticar por telefone. O agente avisa
no boot quando o par não bate (`_warn_on_codepage_mismatch`), só avisa: fabricante
exótico numera as tabelas do jeito dele.

---

## 29. O agente roda no Windows do balcão, que tem duas armadilhas de encoding

**O Bloco de Notas salva em ANSI.** É o padrão nas máquinas antigas — que são
justamente as de balcão. Em ANSI (CP1252) o `ç` de `"Impressora Cozinha Ação"` é
um byte `0xE7` solto, que não é UTF-8 válido. `parser.read(encoding="utf-8-sig")`
levantava `UnicodeDecodeError`, que **não é `ConfigError`**: escapava inteiro pelo
`__main__` e o lojista via um traceback de Python — exatamente o que o tratamento
de config ausente existe para evitar. Hoje `_read_ini` tenta UTF-8 e cai para
CP1252.

**A ordem importa e não dá para inverter.** CP1252 aceita *qualquer* byte, então
tentada primeiro ela leria um arquivo UTF-8 legítimo como mojibake (`"Ação"` →
`"AÃ§Ã£o"`) em silêncio, e o nome nunca casaria com o da impressora instalada.

**O console do Windows abre na codepage do sistema** (850/1252 no Brasil). Um
caractere de fora dela — o travessão que o lojista digitou em `"Praça Quente —
Térreo"` — fazia o `emit` do handler levantar `UnicodeEncodeError`. O `logging`
engole a exceção, cospe `--- Logging error ---` com traceback na tela **e perde a
linha**. `harden_console()` roda no começo do `__main__`, antes de qualquer
`print`, porque as mensagens que mais precisam sair são as que acontecem **antes**
de existir log: o caminho do config e o erro de config inválido.

E `pywin32` **não** é problema: ele fala Unicode direto com a API `W` do Windows,
nome de impressora acentuado chega intacto (conferido).

---

## 30. `text/*` sem `charset` é ISO-8859-1 — e o Starlette já resolve

A regra default do HTTP para `text/*` sem `charset` é **ISO-8859-1**, e o
`requests` a aplica até hoje (`get_encoding_from_headers`). Um SSE UTF-8 lido
assim entrega mojibake (`"Praça"` → `"PraÃ§a"`) sem erro, sem log, sem nada.

**Mas isso não acontece nesta API**, e o motivo importa: o Starlette acrescenta
`; charset=utf-8` sozinho em todo `media_type` que comece com `text/`
(`Response.init_headers`). O cabeçalho que sai na rede já está correto —
conferido.

**A conclusão prática é o contrário da intuição: não "conserte" o
`EventStreamResponse.media_type` acrescentando o charset.** O cabeçalho na rede
fica idêntico, e o que muda é o **OpenAPI**, que usa esse valor como chave do
`content`: a rota passaria a ser publicada sob `text/event-stream; charset=utf-8`
em vez de `text/event-stream`. Pela armadilha 16, o painel consome o
`/openapi.json` — custo real, benefício zero.

O agente de impressão força `response.encoding = "utf-8"` no cliente como cinto de
segurança (proxy que reescreve content-type, troca de framework). SSE é UTF-8 por
especificação: não existe caso em que esse `utf-8` esteja errado.

---

## 31. Para o Postgres, `NFC ≠ NFD` — e `ILIKE` compara bytes

Mesma dualidade da armadilha 28, do outro lado do sistema. Conferido no banco de
produção (PG 17.6):

```
'Filé'(NFC) = 'Filé'(NFD)        -> false
'Filé'(NFD) ILIKE '%Filé%'(NFC)  -> false
```

O estrago: o lojista digita `"Filé"` na busca do cardápio, o produto gravado
decomposto **não aparece**, ele conclui que não está cadastrado e **cadastra de
novo**. Ninguém vê erro nenhum.

A defesa tem **dois lados, e um sozinho não resolve**:

1. **Escrita em NFC** (`normalize_text` nos validators de nome) — mantém a base
   inteira numa forma só.
2. **Termo de busca em NFC** (as três `_build_*_search_condition`) — faz a
   consulta casar com o que foi gravado.

Normalizar só a busca não acha o que já está decomposto no banco; normalizar só a
escrita não ajuda quem digita decomposto.

**`slugify` fica de fora de propósito** — ele já é imune, porque `NFKD` +
`encode("ascii", "ignore")` colapsa as duas formas no mesmo resultado. Se alguém
trocar esse `NFKD` por `NFC`, o slug passa a depender da forma da entrada e URLs
do cardápio público mudam sem ninguém pedir.

**Estado do banco em 11/08/2026:** varredura completa deu **zero** registros fora
de NFC — produtos, categorias, restaurantes, filiais, setores de impressão e
`customer_name_snapshot`. A correção é preventiva; a base está limpa e o que a
mantém limpa é o item 1.

**Resíduo conhecido:** `admin_settings_schema` normaliza `label` só com `strip()`.
Ficou de fora porque não é campo de busca — se um dia virar, entra aqui.

---

## 32. Dois públicos não compartilham chave de assinatura — e `purpose` não salva

`ADMIN_AUTH_SECRET` era opcional e caía em `CUSTOMER_AUTH_SECRET` quando vazia.
Em produção ela **estava vazia**: o token do lojista e o token do cliente eram
assinados com a mesma chave, e o startup só emitia warning.

A defesa que existia era o campo `purpose` (`admin_access` × `customer_access`),
conferido em `decode_signed_token`. **Ele não defende contra quem tem a chave:**
`purpose` viaja dentro do token, então quem conhece o segredo de cliente assina
um `purpose="admin_access"` com `type="admin"` e um `sub` de `admin_users`, e o
painel inteiro abre — pedidos, faturamento, cardápio, dados de cliente. Vazar o
segredo do app do cliente passava a valer o painel de todo restaurante.

Hoje:

- `ADMIN_AUTH_SECRET` é obrigatória (sem default em `config.py`) — falta dela
  derruba o boot com `ValidationError`, como os outros segredos;
- valor **igual** ao de `CUSTOMER_AUTH_SECRET` derruba o boot também
  (`startup_checks`): preencher o campo copiando o valor do lado é o outro jeito
  de chegar no mesmo lugar;
- `admin_auth_secret()` não tem mais fallback.

O `purpose` continua e continua necessário — mas para o que ele de fato faz:
separar usos que **compartilham** a mesma chave. O token de lojista de 12h e o
ticket de stream de 30s são os dois assinados com `ADMIN_AUTH_SECRET`, e um
ticket vazado no log do proxy não pode virar `PATCH /admin/orders/{id}/status`.

**Custo de trocar o segredo:** todo token de lojista em circulação morre na
hora. O painel volta para a tela de login (aceitável), e o agente de impressão
instalado com `token =` fixo no `config.ini` **para de imprimir em silêncio** até
alguém colar um token novo na máquina do balcão. O que roda com
`email`/`password` refaz o login sozinho — é por isso que a instalação com senha
é a recomendada em `print-agent/config.ini.example`.

Regra que fica: **segredo novo por público novo.** Reaproveitar chave "porque o
`purpose` separa" é confundir quem-assinou com para-que-serve.

---

## 33. Schema só muda por revisão do Alembic — nunca à mão no Supabase

**Regra:** toda alteração de schema entra por `alembic revision`, é aplicada por
`alembic upgrade head` e chega em produção pelo `docker-entrypoint.sh`. Abrir o
editor de tabelas do Supabase e acrescentar uma coluna "só para destravar" está
proibido, mesmo quando a migração equivalente parece burocracia para um `ALTER
TABLE` de uma linha.

**O caso concreto que fecha essa porta.** O schema deste projeto nasceu à mão no
painel do Supabase e foi remendado pelos 13 `.sql` de `migrations/`, aplicados
um a um por fora. Quando o Alembic entrou, não havia como versionar o que já
existia: a revisão de baseline `20260726_0001` teve que nascer **no-op**, um
`pass` com docstring, servindo só de ponto de partida para as seguintes.

O preço apareceu inteiro no dia em que alguém quis um banco de teste:

```
alembic upgrade head          # num banco VAZIO
→ relation "orders" does not exist   (morre na revisão 0002)
```

**Não existia, em lugar nenhum do repositório, o DDL que constrói este banco.**
Nem no ORM (que não mapeia sequences, defaults nem os índices feitos à mão), nem
nos `.sql` (que são `ALTER TABLE` sobre tabelas que ninguém versionou). O
repositório inteiro descrevia mudanças sobre um schema que só existia dentro do
Supabase.

A saída foi `alembic/schema_baseline.sql`, um `pg_dump --schema-only` de
produção congelado na revisão `20260810_0012`, que a fixture aplica antes de
`alembic stamp` + `upgrade head`.

**E é justamente esse arquivo que a regra protege.** Ele é uma foto. Uma coluna
acrescentada à mão em produção não aparece nele, não aparece em revisão nenhuma,
e o resultado não é um erro: é a suíte `db` passando contra um schema que **não
é** o de produção. O teste continua verde, a query continua certa no CI, e quebra
só no servidor — que é a pior ordem possível para descobrir.

O mesmo vale para índice criado à mão, que já custou caro uma vez: o
`idx_order_items_order_id` da armadilha 4 existia só em produção, e o
`IF NOT EXISTS` não o detectou porque casa por NOME.

**Na prática:**

- coluna, índice, constraint, sequence, default: `alembic revision`, sempre;
- leia o arquivo gerado antes de aplicar e apague as linhas de `DROP` — o
  autogenerate ainda propõe remover o que o ORM não mapeia (armadilha 24);
- se um dia for inevitável mexer por fora (janela de emergência, `CREATE INDEX
  CONCURRENTLY`, que não roda em transação), o trabalho **não terminou** no
  `ALTER`: escreva a revisão correspondente e feche com `alembic stamp`, para o
  banco e o histórico voltarem a concordar no mesmo minuto;
- `scripts/audit_indexes.py` existe para achar o que escapou. Rodá-lo é barato.

**Quando regerar o `schema_baseline.sql`: idealmente nunca.** Toda revisão nova
se aplica por cima dele — o `stamp` fixa a foto em `0012` e o `upgrade` faz o
resto. Precisar regerá-lo é o sintoma de que esta regra foi quebrada.

---

## 34. `money_to_float` na resposta — e as duas respostas que estão fora dessa regra

**A regra continua valendo e não mudou:** `Decimal` no cálculo, `float` só na
serialização da resposta, via `money_to_float`. É o que está no topo deste
arquivo e é o que a maioria dos schemas faz.

**Mas hoje ela não vale em toda a API,** e saber onde ela não vale evita duas
horas de confusão. `CreateOrderResponse` e `OrderDetailResponse`
(`order_schema.py`) declaram assim:

| Campo | Tipo | O que sai no JSON |
|---|---|---|
| `subtotal`, `delivery_fee`, `service_fee`, `total` | `float` | número: `52.9` |
| `coupon_discount_amount`, `cashback_redeemed_amount`, `discount_total` | `Decimal` | **string**: `"2.50"` |

Duas consequências que já apareceram:

- **`total + discount_total` levanta `TypeError`.** Ninguém soma esses dois
  hoje; quem escrever o primeiro relatório sobre esses schemas vai escrever
  exatamente essa linha e descobrir na hora.
- **A mesma resposta entrega dinheiro em dois formatos**, e o front precisa
  saber de cor quais campos converter.

### Por que não foi corrigido

Não é esquecimento. **As duas direções mudam o formato de fio**, e o app do
cliente consome essas respostas:

- **tudo `float`** → os três descontos deixam de ser string e perdem as casas
  fixas (`"2.50"` vira `2.5`, `"10.00"` vira `10.0`);
- **tudo `Decimal`** → os quatro campos que hoje são NÚMERO viram string.

E não existe terceira via: **JSON não tem número com casa decimal fixa.**
`json.dumps({"total": 52.90})` produz `{"total": 52.9}`. Duas casas só existem
como string. Serializar `Decimal` como número é escolher perder as casas.

Corrigir **uma resposta só é pior que não corrigir**: o mesmo campo passa a ter
tipo diferente em rotas diferentes. Foi o que aconteceu uma vez — o
`CustomerOrderHistoryItem` foi convertido para tudo `float` e revertido em
seguida (`bffca0e`), porque deixava `discount_total` como número em
`/customers/me/orders` e como string em `/orders/{token}`.

### O que fica pendente

**Uma decisão única, sobre a API inteira, tomada junto com o app do cliente:**
ou todo dinheiro sai como número, ou todo dinheiro sai como string com duas
casas. Meia API de cada jeito é pior que qualquer uma das duas.

Enquanto essa decisão não é tomada, **não converta um schema isolado** — nem
para "arrumar de passagem" enquanto mexe em outra coisa. É a regra que este
item existe para registrar.

---

## 35. Na operação da filial, `NULL` significa "herda" — e só `NULL`

Desde a revisão `20260818_0025` há **dois regimes** de configuração em
`branches`, e confundi-los custa caro de jeitos diferentes.

**Estado do dia** — `is_open`, `accepts_delivery`, `accepts_pickup`. `NOT NULL`,
sem herança. São o que alguém no balcão aperta. Antes moravam em
`restaurant_settings` e fechavam a rede inteira: pausar a loja do Centro pausava
a da Aldeota, e não havia como pausar só uma.

**Termo comercial** — `min_order_value`, `service_fee_enabled/amount`,
`estimated_delivery_time_min/max`, `default_delivery_fee`. Nullable na filial, e
**`NULL` significa "herda o padrão de `restaurant_settings`"**.

O único lugar que combina os dois é `resolve_branch_operation`
(`src/services/branch_operation.py`). **Não leia `branch.min_order_value`
direto**: a coluna crua responde "o que está sobrescrito", que quase nunca é a
pergunta.

**O `or` que quebraria isso.** A distinção é entre `NULL` e valor, nunca entre
verdadeiro e falso:

```python
valor_da_filial or valor_do_restaurante      # ERRADO
```

`service_fee_enabled = False` na filial é uma escolha ("esta loja não cobra
taxa") e `min_order_value = 0` também. Com `or`, as duas caem no valor do
restaurante e a filial passa a cobrar o que o lojista desligou — sem nada no
log. Por isso `_ou_herdado` testa `is not None`.

**E há DUAS checagens de "fechado", que não se substituem.** `is_open` é a pausa
manual; `branch_business_hours` é a agenda da semana. `OrderService` checa as
duas, e a tela de escolha de filial as combina em `is_open_now`, com
`closed_reason` dizendo qual fechou. Remover uma "porque a outra já cobre" abre
o buraco de novo: estar dentro do horário não significa que o balcão não apertou
"fechar agora" às 21h.

**Consequência de contrato que já vai gerar chamado:** `current_period` pode vir
PREENCHIDO com `is_open_now: false` — é a loja pausada dentro do horário. O
documento é `docs/operacao-por-filial.md`.

---

## 36. O cardápio é da filial, e nada nele herda

Desde a revisão `20260820_0026`, `categories` e `products` têm `branch_id`
`NOT NULL`. **Não é o regime da armadilha 35:** ali `NULL` significa "herda o
padrão do restaurante"; aqui não existe nulo, não existe padrão e não existe
sobrescrita. A picanha do Centro e a da Aldeota são duas linhas independentes.

`restaurant_id` continua nas duas tabelas e **não é o filtro**. Consultar
cardápio por restaurante devolve as lojas todas misturadas — que é exatamente
o que o código antigo faria contra o schema novo, com 200 e sem log. Quatro FKs
compostas impedem que os dois divirjam; ver `docs/cardapio-por-filial.md`.

**`branch_id` atravessa CINCO caminhos, e cada um errava sozinho:**

| Onde | O que acontece se faltar |
|---|---|
| `MenuRepository.get_active_products` | o cliente vê o cardápio das duas lojas |
| `ProductRepository.list_active_by_ids` | pedido fecha na filial B com produto de A |
| `AIRepository.similarity_search` | o Rapi oferece **com preço** o que a loja não vende |
| `ChatCache.retrieval_key` | a segunda loja é servida do cache da primeira |
| `ProductRepository.sellable_prices_by_id` | preço de uma loja carimbado em produto de outra |

Os três últimos são o mesmo defeito em camadas diferentes: consertar só a busca
deixa o cache servindo o resultado errado por 20 minutos, e o sintoma vira
"intermitente".

**`/chat` e `/voice/*` exigem `branch_id`, sem queda para a filial padrão.** Um
default daria a mesma resposta errada escondida atrás de um caminho que parece
configurado. Na voz é pior: o modelo **fala** nome e preço em áudio, e não há
tela onde o cliente notasse.

**`catalog_key` não tem semântica de herança.** Nada no cardápio, no pedido ou
no Rapi a lê para decidir preço ou disponibilidade — ela existe só para
`/admin/reports/products` somar o mesmo item das várias lojas numa linha. Única
por `(branch_id, catalog_key)`: repetir entre lojas é o uso, repetir dentro de
uma faz o relatório contar a mesma venda duas vezes. Não é o `products.code`,
que é o código que o lojista imprime e busca no painel.

**O slug passou a ser único por `(branch_id, slug)`.** Ele não identifica
produto globalmente, e as duas lojas têm `picanha`. Quem usar slug como chave
de cache no app serve o preço de uma loja na tela da outra.

**A ordem dentro da revisão importa e só quebra com dado dentro.** A troca dos
índices de slug tem que rodar ANTES da cópia — a cópia grava o mesmo slug numa
outra filial do mesmo restaurante, e o índice antigo recusaria. Num banco vazio
a cópia é no-op e a ordem errada passa verde na suíte inteira; no Júnior, com
duas lojas, derruba o deploy. `tests/test_migracao_cardapio_por_filial_db.py`
trava as duas metades.

**O modo de falha mais caro deste passo não está no código.** É voltar a imagem
sem `alembic downgrade`: o código antigo consulta por `restaurant_id`, que
continua preenchido, e `/menu` responde 200 com os produtos das duas lojas
misturados. Rollback de imagem exige downgrade junto.

---

## 37. Campo novo em `CreateOrderRequest` custa 24h de 422 — mesmo sendo opcional

A armadilha 7 registrou o custo de acrescentar campo **obrigatório** numa
resposta gravada em `idempotency_keys.response_body`. Este é o **mesmo problema
pelo lado do pedido**, e ele não perdoa nem campo opcional.

`OrderService` assina o corpo com `payload.model_dump()`. Um campo novo aparece
no dump de **todos** os pedidos — inclusive dos que não o mandaram, com o
default — então o corpo canonicalizado muda, e com ele o `request_fingerprint`.

O estrago:

```
antes do deploy   chave X reservada, fingerprint gravado = A
depois do deploy  MESMO corpo, MESMA chave  ->  fingerprint = B
                  B != A  ->  422 "Idempotency-Key já utilizada com um corpo diferente"
```

**Por 24h, um retry legítimo de um pedido idêntico é recusado.** Nada no
código parece errado, o campo é opcional, e o sintoma aparece só em produção e
só para quem estava no meio de uma tentativa no minuto do deploy.

**E o código é 422, não 409** — este item dizia 409 até 22/08/2026, e a
diferença não é acadêmica: quem escrever o tratamento do app contra 409 não
pega este caso. Os dois convivem na mesma rota e querem coisas diferentes:

| Código | Quando | O que o app faz |
|---|---|---|
| **422** `Idempotency-Key já utilizada com um corpo diferente` | fingerprint diferente do gravado — inclusive o campo novo do deploy | **gera chave nova** e refaz |
| **409** `Requisição em andamento` | reserva ainda aberta | espera e tenta a MESMA chave |
| **409** `A resposta original ... versão anterior da API` | resposta gravada não valida mais (armadilha 7) | **gera chave nova** |

`tests/test_idempotency.py::test_same_key_with_different_body_is_rejected_with_422`
e `::test_in_progress_is_rejected_with_409` travam os dois.

**A regra que fica:** campo novo em `CreateOrderRequest` que **não muda o que
foi pedido** entra na lista de exclusão de `_idempotency_fingerprint`. Campo que
muda o pedido fica de fora dela — aí a recusa é a resposta certa.

**Hoje a lista de exclusão está VAZIA, e isso é resultado, não escolha.** Ela
teve um único membro: o `source` do funil (revisão `20260822_0031`), que saía do
fingerprint porque origem não muda item, preço, endereço nem forma de pagamento
— mesma chave com outra origem tinha que devolver o pedido original. A frente de
funil e origem foi removida na revisão `20260822_0033` e o campo foi junto. A
regra continua valendo para o próximo campo dessa natureza; o que não existe
mais é um campo dela.

**`use_cashback` é o exemplo vivo do outro lado.** Ele muda o total do pedido,
então fica **dentro** do fingerprint, e a recusa é a resposta certa — ao preço
das 24h de 422 descritas acima, que foi o custo real do deploy do cashback.
`tests/test_cashback_no_pedido.py::ImpressaoDigitalTests` trava essa metade.

E vale saber que a remoção do `source` **não mexeu no fingerprint**: o corpo
canonicalizado é byte a byte o mesmo de antes dela, porque o campo já estava
fora da assinatura. Tirar da lista de exclusão um campo que também sai do
schema é a única forma de mexer nas duas coisas sem mudar o hash de ninguém.

---

## 38. Tabela sem `customer_id` só é apagada pela retenção

`customer_anonymization_service` alcança o que pende de `customers`. Duas
tabelas não pendem, **de propósito**, e nelas a retenção não é faxina de disco:
**é o mecanismo de exclusão.**

- **`ai_feedback`** — `user_message` guarda em texto puro o que a pessoa digitou
  para o Rapi. Prazo em `chat_service.feedback_retention_cutoff`.
- **o `comment` de `order_reviews`** — o de pedido de convidado (`customer_id`
  nulo) é inalcançável a partir de conta nenhuma. Prazo em
  `order_review_service.review_retention_cutoff`, e é a única das duas que apaga
  só a coluna: apagar a linha levaria a NOTA junto e reescreveria a média
  histórica do lojista todo mês.

Quem executa é `scripts/cleanup_idempotency_keys.py`, no container `limpeza`.

**Esticar o prazo "porque disco é barato" troca o mecanismo de exclusão por
espaço em disco.** É a decisão que o número esconde, e ela precisa ser
deliberada nas duas.

**Houve uma terceira, e ela mostra o custo de não escrever isso.** `menu_events`
(o funil do cardápio, revisão `20260822_0031`) chegou no mesmo desenho — sem
`customer_id`, com 90 dias de retenção como única forma de exclusão — e a frente
inteira foi removida na revisão `20260822_0033` sem nunca ter sido usada. A
regra é a que fica: **tabela nova que guarde rastro de gente e não penda de
`customers` nasce com prazo, e o prazo é a defesa** — não um `TODO` de limpeza
para depois.

---

## 39. Cartão é SÍNCRONO, e todo o desenho do pagamento assumia o contrário

O pix nasce `pending` e o veredito chega por webhook. **O `POST /v1/payments`
de cartão já responde o desfecho**, e três coisas do código antigo assumiam que
isso não acontecia. As três estão consertadas; ficam aqui porque cada uma volta
sozinha se alguém "simplificar" a função errada.

**1. A resposta da criação era descartada.** `create_payment` lia só o `id` e o
`point_of_interaction`, e `start_online_payment` devolvia `payment_status`
**literal** `"pending"`. Inofensivo no pix (a cobrança nasce mesmo `pending`), e
fatal no cartão: `rejected` dentro de um HTTP 201 **não é exceção**, e o pedido
ficaria pendente para sempre.

O segundo andar, que é o mais fácil de não enxergar: `previous_payment_id` só é
preenchido quando `payment_status == "failed"`. Sem gravar o veredito, ele fica
`None`, **a chave de idempotência se repete e o Mercado Pago devolve a própria
cobrança recusada** — armadilha 6 reaberta pela porta do cartão. Gravar o
veredito não é melhoria: é o que faz a nova tentativa existir.

**2. `in_review` não pode entrar em `PAYMENT_STATUSES_THAT_RELEASE_ORDER`.**
Ele é o `in_process` deles, o antifraude segurando a cobrança — e pode durar
**até 48h úteis**. É tentador liberar ("já quase pagou"), e um pedido preparado
durante a análise vira prejuízo do lojista quando ela recusa. Ele existe
separado de `pending` só para a TELA e a comanda dizerem a verdade: uma espera
pede ligar para o cliente, a outra pede não ligar.

**3. Estorno parcial não muda `payment_status` nenhum.** No Mercado Pago
devolver parte do valor mantém o pagamento em `approved` → `paid`, que é onde o
pedido já está. O `if order.payment_status == event.payment_status: return
already_applied` engolia isso **sem nem logar**.

O sinal é `transaction_amount_refunded`, e ele mora em `orders.refunded_amount`.
**A comissão não é reduzida por ele, de propósito** — a plataforma cobra sobre a
venda que aconteceu. E mapear estorno parcial para `refunded` seria pior nas
duas direções: tiraria o pedido inteiro do extrato, e a plataforma deixaria de
cobrar sobre a parte que o cliente pagou.

**E o sandbox recusa cartão.** Ele ignorava `payment_method` inteiro —
`_create_sandbox_payment` só recebia o `order_id` — então devolvia intent válido
para `credit_card`, o webhook marcava como pago e **a comanda imprimia**. O
fluxo inteiro parecendo funcionar sem dinheiro nenhum ter existido é a pior
demonstração possível de um meio de pagamento. Cartão só se testa contra a
credencial de teste do Mercado Pago.

---

## 40. O boot da API cabe porque ela NÃO tem healthcheck — e isso virou dependência

`src/core/warmup.py` roda no lifespan e espera até `AI_WARMUP_TIMEOUT_SECONDS`
por etapa, em três etapas (banco, embedding, LLM). O teto do boot é a soma:
**3 × 8 s = 24 s**, somados ao `alembic upgrade head` que o
`docker-entrypoint.sh` já roda antes do Uvicorn (armadilha 5).

Esse número só é aceitável por um motivo que não está escrito em lugar nenhum
do `docker-compose.yml`: **o serviço `pedeaqui-api` não tem `healthcheck`.** Só
o `redis` tem. E `restart: always` dispara quando o processo **morre**, não
quando ele demora. Boot lento hoje vira 502 no Traefik por alguns segundos, e
nada mais.

**A armadilha é acrescentar um healthcheck à API sem olhar este número.** É uma
mudança que parece puro ganho — "o compose passa a saber quando a API está de
pé" — e o padrão do Docker é `start_period: 0s`. Com isso, o healthcheck começa
a sondar imediatamente, falha durante o aquecimento, o container é marcado
`unhealthy`, é reiniciado, e reinicia **antes de terminar de subir**: loop de
restart permanente, com a API nunca atendendo. O sintoma não aponta para o
warmup em lugar nenhum — parece healthcheck mal configurado, ou API quebrada.

**A regra, se um healthcheck for acrescentado:**

```yaml
healthcheck:
  # start_period > 3 × AI_WARMUP_TIMEOUT_SECONDS + tempo do alembic,
  # com folga. Com o teto em 8 s, 24 s é o piso teórico — não o valor a usar.
  start_period: 60s
  interval: 10s
  timeout: 3s
  retries: 3
```

E os dois números passam a estar **acoplados**: baixar o `start_period` ou subir
`AI_WARMUP_TIMEOUT_SECONDS` sem olhar o outro reabre o loop. Se um dia isso
incomodar, o conserto certo não é encurtar o warmup — é tirá-lo do lifespan e
deixar a API subir enquanto ele aquece em segundo plano, aceitando a corrida com
a primeira requisição que o desenho síncrono existe justamente para evitar.

Correlato: `AI_WARMUP_ENABLED=false` zera o teto inteiro, e é a saída de
emergência se um boot travar em produção por causa disso.

---

## 41. Banco separado do Redis (`/0` vs `/1`) NÃO isola despejo

O Redis sobe com `--maxmemory 256mb --maxmemory-policy volatile-lru`, e ele é
**compartilhado**: contador de rate limit, cache de estimativa de entrega e
cache de embedding do Rapi moram todos ali.

**`volatile-lru` escolhe a vítima entre as chaves COM TTL.** Todas as nossas
têm — no rate limit, a janela *é* o TTL. E o consumo é assimétrico: um embedding
ocupa ~6 KB, um contador de limite algumas dezenas de bytes. Sob pressão de
memória, um contador parado há trinta segundos é "menos recentemente usado" que
um embedding lido há dez, e sai.

**Contador despejado não dá erro.** O `slowapi` lê a chave ausente como zero, o
cliente ganha orçamento novo, e o limite deixa de valer em silêncio. O
`in_memory_fallback_enabled=True` não cobre este caso: ele existe para o Redis
**fora do ar**, não para o Redis respondendo normalmente sobre uma chave que foi
apagada.

**A armadilha é achar que separar o banco lógico resolve.** É o primeiro reflexo
— `REDIS_URL=.../0` para o rate limit, `.../1` para o cache — e não muda nada:

> `maxmemory` e `maxmemory-policy` são configuração **da instância**, não do
> banco. O despejo varre o keyspace inteiro, todos os `SELECT` juntos.
> `SWAPDB`, `FLUSHDB` e afins operam por banco; a pressão de memória, não.

O que isola de verdade é **outra instância**, com `maxmemory` próprio:

```yaml
  redis-ai:
    image: redis:7-alpine
    command: [redis-server, --requirepass, ${REDIS_PASSWORD:?}, --maxmemory, 64mb,
              --maxmemory-policy, volatile-lru, --save, "", --appendonly, "no"]
```

com `AI_CACHE_REDIS_URL` apontando para ela. A costura já existe no código:
`cliente_redis()`, em `src/ai/services/chat_cache.py`, é o único lugar que
constrói o cliente do cache.

**Hoje isso não está montado, de propósito**: `256 MB / 6 KB ≈ 43 mil` perguntas
distintas dentro da janela de 60 min do TTL, para um restaurante — não acontece
organicamente, e um flood artificial dispara antes o alarme de custo (cada
pergunta distinta é uma chamada paga de embedding). O que está montado é a
**detecção**: `conferir_despejo_do_redis`, em
`scripts/cleanup_idempotency_keys.py`, roda diariamente no container `limpeza` e
grita quando `evicted_keys` deixa de ser zero. **O número esperado é zero, para
sempre.**

E corolário para qualquer chave nova no Redis: **grave sempre com TTL**
(`SETEX`, nunca `SET` pelado). Chave sem expiração é invisível para
`volatile-lru` — ela nunca é despejada, e portanto o despejo inteiro recai sobre
as chaves que se comportaram.

---

## 42. Medição de comportamento de biblioteca só vale no venv do lock

**O primeiro passo de qualquer spike é dizer em que ambiente ele está rodando.**
Não é cerimônia: em 24/08/2026 um spike sobre streaming do LangChain foi montado
no interpretador local e quase virou decisão de arquitetura.

O `requirements.lock.txt` — que é o que o `Dockerfile` instala e o que roda em
produção — fixa `langchain-openai==1.6.0` e `openai==3.3.1`. O interpretador
local tinha `1.4.1` e `2.52.0`. Medido no dia: **41 pacotes batendo, 24
divergentes e 3 ausentes**, entre eles `SQLAlchemy`, `psycopg`, `alembic` e a
pilha HTTP inteira.

O pior era o ausente: `httpx2` e `httpcore2`. A versão do lock do `openai` fala
com a OpenAI por eles; a versão antiga usa o `httpx` clássico. **Toda conclusão
sobre pool de conexão, timeout ou keepalive tirada dali valeria para uma
biblioteca que produção não tem.**

**A causa não foi um venv que envelheceu — não havia venv.** O `README` manda
criar um e o `.gitignore` o ignora, mas o diretório não existia; `python -m
pytest` caía no Python global da máquina, que tem o que sobrou de outros
trabalhos, e não reclamava de nada.

**O CI sempre esteve certo** (`pip install -r requirements.lock.txt` num runner
limpo). O verde que não valia era o local — que é justamente o que se olha antes
de abrir o PR.

**A trava:** `tests/test_ambiente_bate_com_o_lock.py` compara
`importlib.metadata.version` com o lock, respeitando marcador de plataforma.
Nasce verde no CI, então qualquer vermelho dela é local. Se ela falhar, **pare**
— nenhuma medição feita naquele ambiente vale, inclusive as que já pareciam
conclusivas.

Correlato, para spike de comportamento de biblioteca: dublar o **transporte**
(o cliente HTTP, o socket) e deixar a biblioteca real por cima é o que separa
"medi o LangChain" de "medi a minha lembrança do LangChain". Um dublê alto
demais testa o dublê.

---

## 43. A transcrição da fala do cliente NÃO é o que o modelo de voz ouviu

Contraintuitivo, e engana rápido: no `/voice` o log mostra "eu falei: *quero o
bairro mesmo*" quando a pessoa disse "quero o **baião** mesmo", e a conclusão
óbvia — "o áudio virou texto errado antes de o modelo ver" — **está errada**.

O `gpt-realtime-mini` é *speech-to-speech*: **ele consome o áudio direto**, em
tokens de áudio. A transcrição da entrada é um serviço SEPARADO, que roda em
paralelo e não está no caminho da resposta. A documentação do próprio SDK diz
isso, em `openai/types/realtime/realtime_audio_config_input.py` (conferido no
`openai==3.3.1`, o do lock):

> *Input audio transcription is not native to the model, since the model
> consumes audio directly. Transcription runs asynchronously through the
> /audio/transcriptions endpoint and should be treated as guidance of input
> audio content rather than precisely what the model heard.*

**O que isso muda na prática:**

- **Melhorar a transcrição melhora a TELA, e nada mais.** Vocabulário do
  cardápio no `prompt`/`keywords`, `language: "pt"`, trocar `whisper-1` por
  `gpt-transcribe` — tudo isso deixa o log mais fiel e **não muda uma palavra
  da resposta falada**. É gasto que não compra qualidade de atendimento.
- **O sinal real do que o modelo entendeu é o argumento da ferramenta.** A
  linha `[tool] buscar_no_cardapio {"consulta":"..."}` é o modelo dizendo, com
  as palavras dele, o que ele acha que foi pedido. É ela que separa "ele
  ouviu errado" de "ele ouviu certo e a busca trouxe lixo" — e são consertos
  diferentes. Olhe essa linha ANTES de mexer em transcrição.
- **O lever que muda o que ele entende é o prefixo de texto** (as
  `instructions` da sessão): nome que ele nunca viu escrito ele tem mais
  dificuldade de reconhecer falado. Mas isso é reenviado em TODA resposta —
  o cardápio do Júnior tem 136 produtos, ~2.000 tokens por resposta. Se um dia
  entrar, é uma lista curta dos mais pedidos, e com medição antes.
- **A transcrição é cobrada à parte, por minuto.** O backend não a liga (não
  precisa dela); quem liga é a bancada, por `session.update`, só para mostrar
  "o que eu falei". Medição de custo limpa se faz com ela desligada.

O mesmo vale ao contrário: `response.output_audio_transcript.done` é a
transcrição do que o assistente falou, e serve para conferir de olho — não é
o que o cliente ouviu.

---

## 44. Exemplo em prompt: ancorado na FONTE ensina, solto na SAÍDA vira molde

Em 24/08/2026 o prompt de voz ganhou, como ilustração da forma curta de falar
preço, a frase:

```
- Copie o valor EXATO ..., na forma curta do balcao: "trinta e cinco e trinta"
```

Na sessão seguinte o assistente disse **"trinta e quatro e noventa"** e **"vinte
e quatro e noventa"** — mesma forma, mesmo ritmo, e **sem ter chamado a
ferramenta em nenhum dos dois turnos**. Números que não vieram de lugar nenhum.
Na sessão anterior, sem essa linha, os dois preços falados eram reais.

**A diferença que explica os dois casos** — porque o `system_prompt.py` do texto
tem `Exemplo: "R$ 23,90"` há muito tempo e não produz isso:

| | onde o exemplo vive | o que o modelo aprende |
|---|---|---|
| texto | *"o preço vem em `retrieved_products`, no campo `price`, já escrito como deve aparecer. Exemplo: `R$ 23,90`"* | como **reconhecer** o campo de origem |
| voz (errado) | *"na forma curta do balcão: `trinta e cinco e trinta`"* | como **produzir** a frase — inclusive sem fonte |

Exemplo ancorado na FONTE é referência: ele descreve um dado que existe fora do
prompt, e o modelo precisa ir buscar o dado. Exemplo solto da SAÍDA é molde: é
uma frase pronta, no formato final, e preencher um molde é mais barato para o
modelo do que chamar uma ferramenta.

**A regra:** todo exemplo de valor num prompt aponta para de onde o valor vem, e
não para como a resposta soa. Quando a forma de falar precisa mesmo de exemplo,
escreva o par **fonte → saída**, nunca a saída sozinha:

```
"R$ 43,50" vira "quarenta e tres e cinquenta"
```

Vale para os dois agentes, e vale além de preço: quantidade, prazo, distância,
número de pedido — qualquer campo que o modelo deveria copiar e pode inventar.

**Correlato, e foi o que denunciou este caso:** o argumento da tool call no log
(`consulta=` em `[Voz] busca`) é o que separa "o modelo inventou" de "o dado
veio errado". Sem ele, os dois parecem iguais de fora. Ver [[43]] para o outro
lado dessa mesma linha.

---

## 45. O log da busca mostra o que ela ACHOU, não o que o modelo RECEBEU

Duas linhas diferentes, e confundi-las manda consertar o lugar errado.

Em 25/08/2026 o atendente de voz falou três produtos numa frase, com o teto do
prompt em dois. O log da sessão trazia:

```
[Voz] busca | ... | encontrados=5 | hidratados=5
```

Lido de fora, isso parece dizer "o modelo recebeu cinco e escolheu falar três".
**Não diz.** `encontrados` e `hidratados` são o que a busca achou e o que o
banco devolveu — o que chega ao modelo é o `resumo`, que é montado depois, tem
teto próprio, e não aparece nessa linha.

Naquele caso a busca daquele turno havia devolvido **dois** produtos, e o
terceiro nome veio da conversa, de um turno anterior. O diagnóstico correto era
"o teto é da frase e o modelo somou produto de outro turno", e não "o corte
falhou". A proposta que quase foi implementada — cortar o `resumo` no código —
atacava um alvo que já estava certo.

**A regra:** ao ler log de agente, separe sempre três coisas que parecem uma só:

| o que a linha diz | onde ela é escrita | o que NÃO prova |
|---|---|---|
| o que a busca **achou** | `[Voz] busca`, `[AI /chat perf] context_products` | o que foi enviado |
| o que o modelo **recebeu** | o `resumo` / o prompt montado | o que ele usou |
| o que o modelo **falou** | `output_audio_transcript`, a resposta do `/chat` | de onde cada nome veio |

Um produto citado na fala pode ter vindo do **histórico da conversa**, e não da
busca daquele turno. Em voz isso é regra, não exceção: a sessão inteira está no
contexto, e o modelo não distingue "o que a ferramenta me deu agora" de "o que
eu disse há três turnos" sem uma regra explícita mandando distinguir.

**Antes de mexer no código de corte, confira o corte com os olhos** — imprima o
`resumo` daquele turno, não a contagem da busca. E quando a diferença importar
para uma decisão, meça o terceiro nível: o contador de produtos citados na fala
(`[teto]` na bancada) existe exatamente porque nenhuma das duas primeiras linhas
responde "quantos ele falou?".

Ver [[43]] para o outro lado da mesma confusão — a transcrição da fala do
cliente não é o que o modelo ouviu — e [[44]] para o caso em que só o argumento
da tool call separou "inventou" de "veio errado".
