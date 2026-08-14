# LGPD — o que falta, e o que eu proponho fazer

> **Estado em 12/08/2026, depois das decisões.**
>
> | Fase | Decisão | Estado |
> |---|---|---|
> | 1 | executar | **feita**, menos o item 3 (ver abaixo) |
> | 2 — exclusão de conta | anonimizar **liberando** e-mail e telefone para recadastro; queimar para sempre é incômodo real e não é exigência da lei | **desenhada, não implementada** — ver [`lgpd-fase2-exclusao-de-conta.md`](lgpd-fase2-exclusao-de-conta.md) |
> | 3 — CPF | não há plano de nota fiscal; o CPF sai do cadastro e os gravados são anulados | **feita** (revisão `20260812_0019`) |
> | 2.6 — convidado | canal fora do produto: e-mail de contato na política | decidido, depende da política |
>
> **O item 1.3 está resolvido no repositório.** A limpeza deixou de depender
> de cron na máquina: virou o serviço `limpeza` do `docker-compose.yml`, um
> container próprio que roda `scripts/cleanup_idempotency_keys.py` em laço de
> 24h (e retenta em 5 min quando falha). Isso é melhor que cron por três
> razões — sobe junto com o resto, não depende de crontab de servidor que
> ninguém versiona, e o `entrypoint: []` garante que só a API roda
> `alembic upgrade head`, sem dois containers migrando o mesmo banco.
>
> Como a retenção dos códigos de verificação entrou nesse mesmo script, ela
> passa a acontecer sozinha assim que o serviço subir.
>
> **O que sobra é operacional, não de código:** conferir que o serviço está
> de pé no servidor. Enquanto ele não subir, nenhuma retenção acontece —
> nem a antiga (chaves e estimativas), nem a nova.
>
> O resto do documento é o levantamento original, mantido como estava.

---

**Status: proposta. Nada aqui foi implementado.**

Este documento é para ser lido e decidido, não aplicado. Ele não é parecer
jurídico: é o levantamento de engenharia de onde o código guarda dado
pessoal, o que a LGPD exige que exista e não existe, e quanto custa cada
conserto. Onde a decisão é de negócio ou de advogado, está marcado.

Levantado em 12/08/2026, contra a árvore em `962eaf1`.

---

## 1. O que a plataforma guarda hoje

Verificado no código, não presumido.

| Onde | O quê | Prazo hoje |
|---|---|---|
| `customers` | nome, e-mail, telefone, **CPF**, data de nascimento, hash de senha, `marketing_opt_in`, `privacy_accepted_at` | para sempre |
| `customer_addresses` | rua, número, bairro, complemento, referência, CEP, **latitude/longitude** | até o cliente apagar |
| `orders` | `customer_name_snapshot`, `customer_phone_snapshot`, endereço da entrega | para sempre |
| `delivery_estimates` | `customer_id`, latitude/longitude | apagado pelo container `limpeza` após o TTL |
| `idempotency_keys` | corpo inteiro da resposta, **incluindo `tracking_token` em claro** | 24h, apagado pelo container `limpeza` |
| `email_verification_codes`, `password_reset_codes` | e-mail | para sempre |
| `cashback_transactions`, `coupon_redemptions` | `customer_id` | para sempre |

Os dois `cleanup` rodam pelo mesmo script (`scripts/cleanup_idempotency_keys.py`),
que hoje roda pelo serviço `limpeza` do `docker-compose.yml` — um container
próprio, em laço de 24h. Antes dependia de cron na máquina; a mudança é das
correções de segurança de 11/08/2026. Se o serviço não estiver de pé no
servidor, as duas tabelas crescem para sempre.

**Para onde o dado sai:**

- **Mercado Pago** — só o e-mail do pagador (`_resolve_payer_email`). CPF não vai.
- **Google Routes** — origem e destino como coordenadas. O destino é o
  endereço residencial do cliente.
- **Supabase** — banco e Storage (imagens do cardápio; sem dado pessoal).
- **OpenAI** — mensagens do chat. O histórico é dicionário em memória do
  worker (armadilha 20) e não é persistido.

---

## 2. Os buracos, do mais grave ao menos

### 2.1 Não existe apagar a conta — e não existe caminho manual seguro

`is_active` existe em `customers` e o login o respeita (`auth_service.py:228`),
mas **nada no código o coloca em `False`**. Não há rota, não há script.

Um cliente que peça exclusão hoje só pode ser atendido com `UPDATE` à mão no
Supabase — que é exatamente o que a armadilha 33 proíbe, e que além disso não
resolve: `orders` tem `customer_name_snapshot` e `customer_phone_snapshot`, e
`order_items` referencia `products` por FK. Apagar a linha de `customers`
quebra histórico que o próprio cliente consulta.

É o item mais grave porque é o único sem **nenhuma** saída operacional.

### 2.2 O consentimento de marketing é via de mão única

`marketing_opt_in` é coletado no cadastro (`RegisterCustomerRequest`), sai em
`/customers/me` — e **não há como desmarcar**. `UpdateCurrentCustomerRequest`
tem `extra="forbid"` e aceita só `name`, `email`, `phone`, `birth_date`.

O cliente marca a caixa uma vez e não tem, em lugar nenhum do produto, como
voltar atrás. A LGPD trata revogação de consentimento como procedimento
gratuito e facilitado (Art. 8, §5).

É o buraco mais barato de fechar de todos, e o mais difícil de defender se
alguém perguntar.

### 2.3 O CPF é obrigatório e não serve para nada além de barrar cadastro repetido

Rastreei todos os usos. Depois do cadastro, o CPF aparece em exatamente dois
lugares: `_registration_conflict` (para recusar CPF já cadastrado) e a
resposta de `/customers/me`. **Não vai para o Mercado Pago, não emite nota, não
entra em relatório.**

Ou seja: a plataforma exige, guarda para sempre e em texto puro o documento de
identidade de todo cliente para cumprir uma função que e-mail e telefone —
ambos já únicos na tabela — cumprem igual.

Isso é o oposto da minimização (Art. 6, III). E o custo de um vazamento com CPF
é de outra ordem que um com e-mail.

**Decisão de negócio, não minha:** se existe plano de emitir nota fiscal, o CPF
tem base legal e a conversa muda — mas aí ele deveria ser pedido no checkout de
quem quer nota, não no cadastro de todo mundo.

### 2.4 Não existe exportar os próprios dados

`/customers/me`, `/me/orders` e `/me/addresses` já entregam quase tudo em
pedaços. Falta o pacote único (Art. 18, II e V).

Barato justamente por isso: é montagem do que já existe, não consulta nova.

### 2.5 `privacy_accepted_at` não diz *o que* foi aceito

Grava o instante, não a versão da política. Quando o texto mudar, não haverá
como responder "quem aceitou esta versão" — nem como saber a quem pedir aceite
de novo.

Uma coluna `privacy_policy_version` resolve daqui para a frente. O que já está
gravado fica ambíguo para sempre; não há conserto retroativo.

### 2.6 O pedido de convidado não tem titular alcançável

Pedido de convidado grava nome, telefone e endereço sem conta. Essa pessoa não
tem login, então não tem como exercer direito nenhum pelo produto — e o único
identificador que ela possui é o `tracking_token`, que some do lado dela se
limpar o navegador (armadilha 19: o token só sai da API uma vez).

Não tenho proposta boa aqui. As duas saídas que enxergo — canal fora do produto
(e-mail de contato) ou obrigar cadastro — são decisão de negócio.

### 2.7 Resíduo já conhecido

`idempotency_keys.response_body` guarda o `tracking_token` em claro por até 24h
(armadilha 19, já documentada). Um dump vazado entrega os pedidos das últimas
24h. Está aqui só para constar no inventário — a decisão de conviver com isso
já foi tomada e continua defensável.

---

## 3. O que eu proponho, em três fases

Fases por **custo e risco**, não por gravidade — a 1 é toda aditiva, a 3 muda
schema e produto.

### Fase 1 — o que não quebra nada (proponho fazer primeiro)

1. **`marketing_opt_in` editável.** Um campo a mais em
   `UpdateCurrentCustomerRequest`. Fecha o 2.2.
2. **`GET /customers/me/export`.** Monta e-mail, telefone, endereços, pedidos e
   cashback num JSON só, do próprio token. Fecha o 2.4.
3. **Conferir que o serviço `limpeza` está de pé em produção.** Não é código:
   o container já existe no `docker-compose.yml`. Enquanto ele não subir,
   nenhuma retenção acontece — nem a antiga nem a nova.
4. **Estender o cleanup a `email_verification_codes` e `password_reset_codes`.**
   Confirmei que nada os apaga: as duas tabelas guardam o e-mail de todo mundo
   que já pediu um código, para sempre, muito depois de o código ter vencido e
   deixado de servir para qualquer coisa. É a mesma justificativa do cleanup
   que já existe ("passado o TTL a linha só ocupa espaço"), aplicada às tabelas
   que ficaram de fora — só que aqui o que ela ocupa é dado pessoal.

Nada disso muda contrato existente nem schema. Custo: baixo. Risco: baixo.

### Fase 2 — exclusão de conta (precisa de decisão antes)

> **O desenho detalhado está em
> [`lgpd-fase2-exclusao-de-conta.md`](lgpd-fase2-exclusao-de-conta.md).**
> Ele corrige três premissas desta seção que não se sustentaram quando os
> modelos vizinhos foram abertos: existe FK apontando para
> `customer_addresses`, o DELETE do cliente é **impossível** (não uma
> escolha — `coupon_redemptions.customer_id` é `NOT NULL`), e um DELETE
> levaria o extrato de cashback junto por `CASCADE`.

Proponho **anonimização, não DELETE**, e vale discutir:

- `customers`: `is_active=False`, e-mail/telefone/CPF substituídos por valor
  neutro derivado do id, `password_hash` invalidado;
- `customer_addresses`: apagados de verdade (não há FK apontando para eles);
- `orders`: os snapshots viram `"Cliente removido"`. **O pedido não é apagado**
  — há obrigação fiscal e contábil sobre a venda, e o restaurante precisa do
  histórico dele.

O ponto que precisa da sua decisão: **isso queima o e-mail, o telefone e o CPF
para sempre**, porque os três são `UNIQUE`. A pessoa que apagar a conta e quiser
voltar não conseguirá se recadastrar com os mesmos dados, a menos que a
anonimização libere os valores em vez de substituí-los — e aí ela deixa de ser
irreversível. Escolher aqui é escolher entre dois incômodos reais.

Custo: uma revisão do Alembic, um service, e testes que provem que o histórico
do restaurante sobrevive.

### Fase 3 — minimização do CPF (só depois de responder 2.3)

Se não houver plano de nota fiscal: tornar o CPF opcional, parar de exigi-lo no
cadastro, e decidir o que fazer com os que já estão gravados. Se houver: manter,
mas mover a coleta para onde ele é usado.

Não proponho mexer antes dessa resposta.

---

## 4. O que fica de fora, e por quê

- **Criptografar CPF/telefone em repouso.** Antes de criptografar, decidir se o
  CPF precisa existir (2.3). Criptografar um campo que não deveria estar lá é
  pagar caro para manter o problema.
- **Registro de acesso do lojista a dado de cliente.** É um controle real e caro;
  só faz sentido depois da Fase 1.
- **Contrato com operadores (Mercado Pago, Google, OpenAI, Supabase) e política
  de privacidade.** Não é engenharia. É o que um advogado precisa ver, e é onde
  eu paro.

---

## 5. As três respostas que eu preciso

1. **CPF:** existe plano de nota fiscal? (define a Fase 3 inteira)
2. **Exclusão:** anonimizar queimando e-mail/telefone/CPF para sempre, ou
   liberá-los para recadastro?
3. **Convidado:** canal fora do produto, ou passa a exigir cadastro?

Com a 1 e a 2 respondidas eu executo as Fases 1 e 2. A Fase 1 eu consigo
começar sem resposta nenhuma.
