# LGPD Fase 2 — exclusão de conta por anonimização

> **Status: DESENHO. Nada aqui foi implementado.**
>
> A decisão já está tomada ([`lgpd-proposta.md`](lgpd-proposta.md), Fase 2):
> anonimizar **liberando** e-mail e telefone para recadastro, com o pedido
> preservado — obrigação fiscal e histórico do restaurante. Este documento é
> o *como*, para ser lido antes de virar código.

Levantado em 14/08/2026, contra a árvore em `3be0d44`.

---

## 1. Três coisas que o levantamento original errou

A proposta da Fase 2 foi escrita sem abrir os modelos vizinhos. Três das suas
premissas não se sustentam, e as três mudam o desenho.

### 1.1 Existe FK apontando para `customer_addresses`

A proposta diz: *"`customer_addresses`: apagados de verdade (não há FK
apontando para eles)"*. Há:

```python
# order_model.py
customer_address_id = mapped_column(
    ForeignKey("customer_addresses.id", ondelete="SET NULL")
)
```

O `ON DELETE SET NULL` faz o DELETE **funcionar** — e é por isso que o erro
passaria despercebido. Ele não falha; ele apaga o vínculo do pedido com o
endereço, em silêncio.

Na prática isso não impede a exclusão, porque o pedido guarda o **próprio
snapshot** do endereço (`address_street`, `address_number`, …,
`delivery_latitude`, `delivery_longitude`). Mas muda a conclusão: apagar
`customer_addresses` **não** é suficiente para tirar o endereço da pessoa do
banco. O endereço está gravado duas vezes, e a cópia que importa é a do
pedido.

### 1.2 `DELETE` do cliente é impossível, e não por escolha

```python
# coupon_redemption_model.py
customer_id = mapped_column(ForeignKey("customers.id"), nullable=False)
```

`NOT NULL`, sem `ON DELETE`. Um `DELETE FROM customers` **falha** com violação
de FK para qualquer pessoa que já usou um cupom. As duas saídas seriam apagar
a redenção junto — e aí **o cupom de uso único volta a estar disponível**, o
que transforma "apagar a conta" numa forma de reciclar desconto — ou
adicionar `ON DELETE SET NULL`, que exigiria relaxar o `NOT NULL` e deixaria
o controle de "uma vez por cliente" sem cliente.

Ou seja: a anonimização não é a alternativa *preferida* ao DELETE. É a única
que existe.

### 1.3 O cashback seria apagado em silêncio

```python
# cashback_transaction_model.py
customer_id = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
```

`CASCADE`. Um DELETE levaria junto o extrato de cashback inteiro, incluindo
crédito não usado. Hoje isso quase não custa (o resgate não está ligado —
armadilha 26), mas o dia em que estiver, essa linha some sem aviso.

---

## 2. O desenho

Uma regra decide tudo: **fica o que é da VENDA, sai o que é da PESSOA.**

O restaurante vendeu, emitiu, faturou e pagou comissão sobre aquilo. Esses
números têm que continuar batendo — em relatório, em fatura e em fiscalização.
Nome, telefone, endereço e recado não entram em nenhum deles.

### 2.1 Tabela por tabela

| Tabela | O que acontece | Por quê |
|---|---|---|
| `customers` | **anonimiza**: e-mail e telefone viram sentinela derivada do id; `name` vira `"Cliente removido"`; `birth_date` vira `1900-01-01`; `password_hash` invalidado; `is_active=false`; `anonymized_at=now()`; `password_changed_at=now()` | A linha **precisa** sobreviver: `coupon_redemptions.customer_id` é `NOT NULL` |
| `customer_addresses` | **DELETE** | Não é histórico de venda. O pedido já tem o próprio snapshot |
| `orders` | **anonimiza os campos de pessoa**, mantém tudo de dinheiro e de item | É a venda. Ver 2.2 |
| `order_items`, `order_item_options` | **intactos** | São o que foi vendido |
| `order_status_history` | **intacto** | `changed_by` guarda `admin:<email>` do lojista, não do cliente |
| `coupon_redemptions` | **intactos** | Apagar devolveria o cupom de uso único |
| `cashback_transactions` | **intactos** | Extrato financeiro; `CASCADE` os apagaria |
| `delivery_estimates` | **DELETE** das linhas do cliente | Cache de rota com coordenada de casa. Expira em 15 min e não é histórico de nada |
| `email_verification_codes`, `password_reset_codes` | **DELETE** | Guardam o e-mail em texto puro numa segunda tabela |

### 2.2 O que sai e o que fica em `orders`

```
SAI                              FICA
customer_name_snapshot           order_number, created_at, status
customer_phone_snapshot          subtotal, delivery_fee, service_fee, total
address_street                   commission_percent/base/amount
address_number                   coupon_id, coupon_code_snapshot
address_complement               coupon_discount_amount, discount_total
address_reference                payment_method, payment_flow, payment_status
address_zipcode                  paid_at, payment_provider
delivery_latitude                delivery_distance_km
delivery_longitude               delivery_travel_time_min, delivery_eta_*
notes                            address_neighborhood, address_city, address_state
customer_address_id (→ NULL)     customer_id (→ o fantasma)
```

Três escolhas dentro dessa tabela que merecem ser conferidas antes de virar
código:

**`address_neighborhood` e `address_city` ficam.** Bairro não identifica
ninguém sozinho, e é o recorte que sustenta "de onde vêm meus pedidos" —
análise legítima do lojista. Rua, número e coordenada saem. Se você preferir
o corte mais duro, é uma linha a mais no `UPDATE`; o custo é o lojista perder
essa leitura para os pedidos anonimizados.

**`delivery_distance_km` fica, a coordenada sai.** A distância é o que
justifica a taxa de entrega cobrada naquele pedido — sem ela, uma linha do
relatório fica inexplicável. Ela não localiza a casa: é um raio, não um
ponto.

**`notes` sai inteiro.** É campo livre, e na prática mistura o que é pedido
("sem cebola") com o que é pessoa ("apartamento 302, falar com a Maria"). Não
há como separar os dois automaticamente, e o segundo é justamente o que a
exclusão existe para tirar.

### 2.3 A sentinela: como o e-mail e o telefone são liberados

`customers.email` e `customers.phone` são `NOT NULL` e `UNIQUE`. Liberar os
valores para recadastro significa que eles precisam **sair da coluna** — não
basta desativar a linha.

Proposta: **valor derivado do id**, que é único por construção.

```sql
email = 'removido+' || id::text || '@rapidex.invalid'
phone = 'removido-' || replace(id::text, '-', '')
```

`.invalid` é reservado pela RFC 2606 e nunca poderá ser um domínio de
verdade — nenhuma anonimização vai colidir com um e-mail real, hoje ou
depois. O telefone não é numérico de propósito: qualquer coisa que o trate
como telefone falha alto em vez de mandar SMS para um número inventado.

> **A alternativa é tornar as duas colunas `NULL`**, o que é mais limpo
> semanticamente (a pessoa *não tem* e-mail) e funciona igual sob o `UNIQUE`
> — o Postgres aceita vários NULL. Não é o que eu proponho: relaxar dois
> `NOT NULL` obriga a conferir todo caminho que lê `customer.email` e
> `customer.phone` sem checar nulo, e há pelo menos um em fluxo de dinheiro
> (`PaymentService._resolve_payer_email`). A sentinela custa um formato
> documentado; o NULL custa uma varredura de código que a gente não tem como
> provar completa.

### 2.4 O que faz o recadastro funcionar

Nada além disso. `_registration_conflict` procura por e-mail e telefone; os
valores antigos deixaram de existir na tabela, então o cadastro novo passa e
nasce com **id novo**.

A pessoa volta como desconhecida: sem histórico, sem cashback, sem endereços.
Os pedidos antigos continuam com o restaurante, ligados ao fantasma. É
exatamente o que a decisão pediu.

---

## 3. A migração

Uma coluna. Só.

```
alembic revision -m "exclusao de conta: marca de anonimizacao"
```

```python
def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("anonymized_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("customers", "anonymized_at")
```

**Por que a coluna, se `is_active=false` já existiria.** Porque `is_active`
já significa outra coisa — conta suspensa, que é reversível e mantém os
dados. Sem a marca própria, "esta conta foi anonimizada" e "esta conta está
bloqueada" viram o mesmo estado, e a primeira pergunta de qualquer auditoria
de LGPD (*"quantas exclusões você atendeu, e quando?"*) deixa de ter
resposta.

`nullable=True` sem default: linha antiga é `NULL`, que é a verdade.

**Nenhuma outra mudança de schema.** Vale registrar o que *não* muda e por
quê, porque as duas tentações existem:

- **não** relaxar o `NOT NULL` de `email`/`phone` — ver 2.3;
- **não** mexer no `NOT NULL` de `coupon_redemptions.customer_id`. Ele é o
  que impede o DELETE, e impedir o DELETE é o desenho, não o obstáculo.

---

## 4. O service

`CustomerAnonymizationService`, arquivo próprio. Não entra no `AuthService`:
o `AuthService` já é o maior do projeto, e isto aqui é uma operação
destrutiva de transação única, que se lê melhor sozinha.

Uma função pública, e as privadas na ordem em que rodam:

```python
class CustomerAnonymizationService:
    def anonymize(self, customer: Customer, password: str) -> None:
        """Apaga a pessoa e mantem a venda. Uma transacao, um commit.

        Nao ha desfazer: quando esta funcao volta, o e-mail e o telefone
        antigos nao existem em lugar nenhum do banco.
        """
        self._ensure_password_matches(customer, password)
        self._ensure_no_order_in_flight(customer)

        self._anonymize_orders(customer)      # a venda fica, a pessoa sai
        self._delete_addresses(customer)
        self._delete_delivery_estimates(customer)
        self._delete_verification_codes(customer)
        self._anonymize_customer(customer)    # POR ULTIMO: ver abaixo
        self.db.commit()
```

Cinco decisões dentro dele:

**Uma transação, um commit no fim.** Se qualquer passo falhar, o rollback
leva tudo junto. O estado intermediário — pedidos já anonimizados e cliente
ainda ativo — seria a pior coisa que poderia sobrar: o lojista perde o
histórico e a pessoa continua logada.

**`_anonymize_customer` por último**, mesmo sendo o principal. Os passos
anteriores acham as linhas *pelo* `customer_id`, e uma falha no meio deixaria
o cliente já anonimizado com endereços intactos apontando para ele. Nesta
ordem, o único estado possível depois de uma falha é "nada aconteceu".

**Confere a senha.** É a mesma exigência de qualquer operação irreversível de
conta. Sem ela, um token vazado (ou um celular esquecido destravado) apaga a
conta de alguém.

**Recusa com pedido em andamento.** Pedido fora de `completed`, `cancelled` e
`rejected` é comida a caminho: anonimizar no meio tira o nome e o telefone de
quem o entregador precisa achar. Responde **409** com a lista de números de
pedido em aberto. É recusa temporária, não permanente.

**Não emite e-mail de confirmação.** Não haveria para onde mandar depois — o
endereço acabou de deixar de existir. Se um aviso for desejado, ele sai
**antes** de qualquer escrita, e vira uma decisão sua (seção 7).

### A rota

```
DELETE /customers/me
body: {"password": "..."}
204 No Content
```

`DELETE` e não `POST /customers/me/delete`: é o verbo do que a operação faz, e
o app do cliente já fala REST no resto do recurso. Corpo em `DELETE` é
incomum mas legal, e a alternativa (senha na querystring) a colocaria no log
do proxy.

Fica ao lado de `GET /customers/me/export` (Fase 1), que é o par natural:
**vejo o que vocês têm** e **apaguem**.

---

## 5. Os testes que provam que o histórico do lojista sobrevive

Esta é a parte que decide se o desenho pode ir para produção. São contra o
Postgres (`@pytest.mark.db`), e o formato é sempre o mesmo: **medir antes,
anonimizar, medir depois, exigir igualdade.**

Cenário base para todos: um restaurante, uma filial, um cliente com **dois
pedidos `completed`** — um com cupom, um sem —, endereço salvo, cashback
lançado e uma estimativa de entrega.

### O que não pode mudar

| Teste | O que compara |
|---|---|
| `test_o_faturamento_do_periodo_nao_muda` | `GET /admin/reports/summary` — o JSON inteiro, antes e depois |
| `test_a_comissao_do_periodo_nao_muda` | `GET /admin/reports/commission` — total **e** o extrato pedido a pedido |
| `test_os_valores_do_pedido_ficam_intactos` | `subtotal`, `delivery_fee`, `service_fee`, `total`, `commission_*` lidos direto do banco |
| `test_os_itens_do_pedido_ficam_intactos` | quantidade de `order_items`, `unit_price_snapshot` e os adicionais |
| `test_o_pedido_continua_aparecendo_no_painel` | `GET /admin/orders` e `GET /admin/orders/{id}` respondem 200 e trazem o pedido |
| `test_o_relatorio_de_produtos_nao_muda` | `GET /admin/reports/products` — o que mais vende continua o mesmo |
| `test_a_redencao_do_cupom_sobrevive` | a linha em `coupon_redemptions` existe, e o cupom de uso único **continua indisponível** para aquele `customer_id` |

O primeiro e o segundo são os que valem mais: eles comparam a **resposta HTTP
inteira**, não campo a campo. Um campo novo que alguém acrescente ao
relatório entra na comparação sozinho, sem ninguém lembrar de atualizar o
teste.

### O que tem que mudar

| Teste | O que exige |
|---|---|
| `test_o_nome_e_o_telefone_somem_do_pedido` | `customer_name_snapshot == "Cliente removido"`, `customer_phone_snapshot` sem os dígitos originais |
| `test_o_endereco_de_entrega_some_do_pedido` | rua, número, complemento, CEP e coordenadas nulos; **bairro e cidade preservados** |
| `test_o_recado_do_pedido_some` | `notes is None` |
| `test_os_enderecos_salvos_somem` | zero linhas em `customer_addresses`; `orders.customer_address_id is None` |
| `test_o_cliente_some_da_lista_do_painel` | `GET /admin/customers` não traz mais o telefone original |
| `test_o_login_antigo_para_de_funcionar` | `POST /auth/login` com e-mail e senha antigos → 401 |
| `test_o_token_em_circulacao_morre` | um JWT emitido **antes** da exclusão → 401 (via `password_changed_at`) |

### O que fecha a decisão

| Teste | O que prova |
|---|---|
| `test_o_email_volta_a_poder_ser_cadastrado` | cadastro novo com o **mesmo e-mail** → 201, e `customer.id` **diferente** do antigo |
| `test_o_telefone_volta_a_poder_ser_cadastrado` | idem para o telefone |
| `test_a_conta_nova_nasce_sem_historico` | `GET /customers/me/orders` da conta nova → lista vazia, com os dois pedidos antigos ainda no painel do lojista |
| `test_duas_exclusoes_nao_colidem` | dois clientes anonimizados na mesma transação — as sentinelas são únicas (é o teste que pega uma sentinela constante) |

### Os que provam a recusa

| Teste | O que exige |
|---|---|
| `test_senha_errada_nao_apaga_nada` | 401, e **todas** as linhas ainda lá |
| `test_pedido_em_andamento_bloqueia` | pedido em `preparing` → 409 com o `order_number` na resposta, e nada apagado |
| `test_falha_no_meio_nao_deixa_estado_pela_metade` | erro forçado no passo do cashback → rollback completo; o cliente continua ativo e os pedidos continuam com nome |

O último é o mais importante dos três e o único que exige injeção de falha.
Sem ele, a transação única é uma afirmação do docstring e não um fato.

---

## 6. O que este desenho NÃO fecha

Nomear os resíduos aqui é o que impede que "a conta foi apagada" seja lido
como mais forte do que é.

**~~`ai_feedback.user_message`~~ — FECHADO em 20/08/2026, por retenção.**
Era o maior buraco que sobrava: a mensagem que a pessoa digitou para o Rapi,
em texto puro, com nome, telefone e endereço dentro, inalcançável a partir da
conta porque a tabela só tem `session_id`.

Das duas saídas levantadas aqui, **`customer_id` na tabela não funcionaria** —
e vale registrar por quê, porque era a opção que parecia mais forte.
`POST /chat` e `POST /chat/feedback` não têm autenticação nenhuma, nem
opcional: o Rapi existe para quem ainda não tem conta. Não há cliente na
requisição para gravar na coluna, então ela nasceria sempre `NULL` e a
anonimização continuaria sem alcançar nada — só que agora atrás de uma coluna
que parece resolver, que é pior que o buraco declarado.

A retenção por `created_at` (90 dias, `chat_service.feedback_retention_cutoff`,
expurgo no container `limpeza`) alcança **mais** que a coluna alcançaria: o
texto de quem nunca criou conta some junto, e esse a anonimização por
definição jamais cobriria. Não precisou de migração — o índice
`idx_ai_feedback_created` já existia no baseline.

**`idempotency_keys.response_body` guarda a resposta de criação do pedido por
até 24h**, incluindo o `tracking_token` em claro. A janela é curta e a
limpeza já roda em container próprio, mas por 24h existe uma cópia fora do
alcance da anonimização.

**O link de acompanhamento continua funcionando.** O pedido não é apagado e
`tracking_token_hash` continua lá, então quem tiver o link antigo vê o
pedido — já anonimizado, sem nome, telefone nem endereço. Considero
aceitável: o que sobra é a nota da venda. Se você preferir que o link morra
junto, é um `UPDATE` a mais e a decisão é sua (seção 7).

**Backup do banco.** A anonimização vale para o banco vivo. Restaurar um
backup anterior à exclusão traz os dados de volta, e nenhuma política de
retenção de backup está escrita hoje.

**Log do container.** Nenhum log leva dado pessoal cru — mensagem de chat é
digest, e-mail do pagador sai como `[email]` —, mas o log guarda `order_id` e
`customer_id`. São pseudônimos, não identificadores diretos, e depois da
anonimização não há mais como resolvê-los para uma pessoa. Fica registrado
como consciente.

---

## 7. As quatro respostas que eu preciso antes de implementar

1. **Bairro e cidade ficam no pedido anonimizado?** Eu proponho que sim (2.2).
   É a diferença entre o lojista manter e perder "de onde vêm meus pedidos".
2. **O link de acompanhamento morre junto?** Eu proponho que não (6). O que
   sobra atrás dele é a nota da venda, sem nada da pessoa.
3. **Aviso por e-mail antes de apagar?** Eu proponho que não haja: a
   confirmação por senha já é a barreira, e um e-mail cria uma janela em que a
   pessoa pediu a exclusão e ela ainda não aconteceu.
4. **`ai_feedback` entra nesta frente ou em outra?** Eu proponho **outra**, e
   proponho que seja a próxima: ela é o maior resíduo (6) e o conserto não
   depende de nada daqui.
   → **Respondida em 20/08/2026: outra frente, e já feita.** Virou retenção
   por data e não `customer_id`; o porquê está na seção 6.

Com essas quatro respondidas, isto vira: uma revisão do Alembic de uma coluna,
um service de ~120 linhas, uma rota, e os 21 testes da seção 5.
