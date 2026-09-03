# Entrar com Google no app do cliente

Branch: `rodada/oauth-google`. Nada na `main`, nada de produção.

---

## 0. Antes de codar

### 0.1 Como o login por e-mail funciona hoje, ponta a ponta

**Onde nasce o cliente.** `AuthService.register`
(`src/services/auth_service.py:162`) é a única porta. Ela normaliza e-mail
(`normalize_email`) e telefone (`normalize_digits`), exige `privacy_accepted`,
hasheia a senha em `_hash_new_password` (`:107` — piso de 8 caracteres, teto de
72 **bytes** por causa do bcrypt) e recusa com **409** quando e-mail ou
telefone já existem (`_registration_conflicts`, `:417` — devolve os **dois**
conflitos, não o primeiro).

A linha em si sai de `CustomerRepository.create`
(`src/repositories/customer_repository.py:38`), com
`email_verified_at=None`, `phone_verified_at=None`, `is_active=True`,
`privacy_accepted_at=utcnow()`. Tudo num `try` com `rollback` e **um** commit
(`src/services/auth_service.py:179-196`).

**Onde o código de verificação é gerado.**
`AuthService._create_email_verification_code` (`:435`):

- `generate_6_digit_code()` — `src/utils/security.py:112`, `secrets.randbelow`;
- gravado **em HMAC**, nunca em claro: `hash_verification_code`
  (`src/utils/security.py:116`) é `_hmac_hex(code, EMAIL_CODE_SECRET)`. Seis
  dígitos são um milhão de possibilidades — a chave é o que compra resistência
  a força bruta sobre um dump (mesma conta da armadilha 19, do outro lado);
- linha em `email_verification_codes`
  (`src/models/customer_model.py:70`) com `expires_at = now + 10 min`
  (`CODE_TTL_MINUTES`, `:59`), `attempts_count=0`, `resend_count`;
- e-mail sai por `EmailService.send_email_verification_code`.

**Onde é conferido.** `AuthService.verify_email_code` (`:205`), em ordem:
cliente existe (404) → existe código não usado (400) → `attempts_count >= 5`
(**429**) → `expires_at` (400) → `verify_verification_code`
(`src/utils/security.py:120`, `hmac.compare_digest`). Código errado **incrementa
`attempts_count` e commita** antes de responder 400 (`:221-224`). Certo:
`customer.email_verified_at = utcnow()`, `code_row.used_at = utcnow()`, um
commit.

Reenvio: `resend_email_code` (`:233`) — **mesma mensagem em todo caminho**,
inclusive e-mail inexistente (armadilha 18), cooldown de 60 s
(`_is_within_resend_cooldown`, `:127`) e teto de 3 na janela de 15 min.

**Como o JWT é emitido.** Só em `AuthService.login` (`:263`). O caminho:
identificador com `@` vira e-mail, sem `@` vira telefone (`:264-267`) →
`verify_password` → `is_active` (403) → **`email_verified_at` nulo devolve
`requires_email_verification=True` e NENHUM token** (`:273-278`) → só então
`create_signed_token` (`src/utils/security.py:248`):

```
sub     = str(customer.id)
purpose = "customer_access"
extra   = {"type": "customer"}
exp     = CUSTOMER_ACCESS_TOKEN_MINUTES (10080 = 7 dias)
alg     = HS256, segredo CUSTOMER_AUTH_SECRET
```

A volta é `get_customer_from_token` (`src/services/auth_service.py:390`):
`decode_signed_token(token, "customer_access")` → confere `type == "customer"`
→ carrega por `sub` → **`token_was_issued_before_password_change`**
(`src/utils/security.py:309`), o marco que mata todo token emitido antes de
`customers.password_changed_at`.

**Campos obrigatórios de `customers`** (`src/models/customer_model.py`):

| Coluna | Linha | Observação |
|---|---|---|
| `name` | `:17` | `NOT NULL` |
| `email` | `:18` | `NOT NULL`, **UNIQUE** |
| `phone` | `:19` | `NOT NULL`, **UNIQUE** |
| `password_hash` | `:25` | `NOT NULL` |
| `birth_date` | `:29` | `NOT NULL` |
| `marketing_opt_in` | `:32` | `NOT NULL`, `default=False` |
| `is_active` | `:34` | `NOT NULL`, `default=True` |

`cpf` (`:24`) é nulável e está morto por decisão da LGPD — nada escreve nele.
`birth_date` é `NOT NULL` **e nada no produto lê aniversário hoje** (é o que o
`CustomerAnonymizationService` diz ao usar `1900-01-01` como sentinela).

---

### 0.2 O que o Google NÃO fornece, e como resolver sem travar o cadastro

O `id_token` do Google entrega `sub`, `email`, `email_verified`, `name`,
`given_name`, `family_name`, `picture`, `locale`. Contra a tabela acima,
**faltam três obrigatórios**:

| Falta | Por que é problema |
|---|---|
| `phone` | `NOT NULL` **e UNIQUE** |
| `birth_date` | `NOT NULL` |
| `password_hash` | `NOT NULL` |

`password_hash` é o fácil e resolve como a anonimização já resolve: **hash de
um segredo sorteado e jogado fora** (`hash_password(secrets.token_urlsafe(32))`,
o mesmo de `_anonymize_customer`). Ninguém conhece essa senha, então o login
por senha simplesmente não abre essa conta — e a pessoa que quiser uma senha
passa por `/auth/forgot-password`, que já funciona e já manda código para o
e-mail que o Google verificou.

**`phone` é o que decide o desenho, e o motivo não está na coluna.** Está em
`OrderService` (`src/services/order_service.py:183`): para cliente logado o
`customer_phone_snapshot` do pedido sai de **`current_customer.phone`**, e o
`payload.customer` é ignorado. Um telefone sentinela — `pendente-<uuid>`, no
estilo de `anonymized_phone` — não fica parado numa coluna: **ele vai para o
pedido, e é o número que o entregador liga quando não acha a casa.** Sem erro,
sem log, e o sintoma aparece na rua.

Três saídas, e por que duas foram descartadas:

1. **sentinela em `phone` + flag "complete o perfil"** — cria a conta na hora
   e devolve o JWT, como o desenho pede ao pé da letra. Descartada: depende de
   o front nunca deixar passar a tela de perfil, e o preço de ele deixar é o
   pedido com telefone falso. Regra da armadilha 46 — quando a garantia depende
   de alguém lembrar de aplicá-la, ela é uma recomendação;
2. **relaxar `phone`/`birth_date` para nulável** — é a armadilha 50 pelo lado
   caro: `phone` é UNIQUE NOT NULL, lido no snapshot do pedido, em
   `_registration_conflicts`, em `LoginCustomerResponse.phone: str` e na
   anonimização. Nulo ali é `ValidationError` (500) em `GET /customers/me` e
   ripple em meia dúzia de schemas de resposta. E o nulo **não tem significado
   de produto** aqui: "ainda não perguntei" não é "esta pessoa não tem
   telefone";
3. **não criar cliente até ter os dois campos.** É a escolhida.

**O que isso significa na prática, e por que não é mudança de desenho.** O
`POST /auth/google` tem três respostas, e **duas delas já não eram JWT** pelo
próprio desenho (o caso (b) responde "confirme por código"). O caso (c) ganha
uma terceira: `status: "profile_required"` mais um **ticket de cadastro** —
token assinado, curto, com o `sub`/`e-mail`/`nome` que o Google já verificou —
e o front manda telefone + data de nascimento em `POST
/auth/google/complete-signup`, que **aí sim** cria o cliente e devolve o MESMO
JWT do login por e-mail. Nenhuma sentinela chega ao banco, o pedido nunca sai
com telefone falso, e o cadastro não trava: é uma tela com dois campos que o
formulário de e-mail **já pede hoje** (`RegisterCustomerRequest` exige `phone`
e `birth_date`).

O ticket é assinado com `CUSTOMER_AUTH_SECRET` e `purpose="google_signup"` —
`purpose` separa usos que compartilham a mesma chave, que é exatamente o que a
armadilha 32 diz que ele serve para fazer. Vale 15 minutos: é uma tela, não uma
sessão.

> **Decisão para revisão.** É a única resposta de 0.2 que não põe telefone
> falso em pedido nem 500 em `GET /customers/me`. Se você preferir a sentinela
> com flag, o conserto que ela exige é uma trava na criação de pedido, e essa
> mexe no fluxo de checkout de todo mundo.

`marketing_opt_in` nasce `False` (é o default e é o consentimento que ninguém
deu) e `privacy_accepted_at` sai do momento em que a pessoa confirma o cadastro
na tela do ticket — o aceite continua explícito, como no cadastro por e-mail.

---

## Andamento

- [x] 0.1 e 0.2 respondidos
- [ ] 1. tabela de identidade social
- [ ] 2. os três casos
- [ ] 3. o que não pode quebrar

---

## 1. Ferramenta antes do conserto: `alcance_da_anonimizacao.py`

A frente acrescenta uma tabela que pende de `customers`. A armadilha 38 cobre
a tabela que **não** pende (retenção é o mecanismo de exclusão, e por isso ela
nasce com prazo) — e não havia nada cobrando a outra metade.

`scripts/alcance_da_anonimizacao.py` cobra que toda tabela com `customer_id`
esteja declarada em `ALCANCE`, de um de três jeitos:

    Alcancada(passo)      o método do service que a alcança — tem que EXISTIR
    Fora(motivo)          por que a linha sobrevive, por decisão
    Indireta(via, passo)  não tem `customer_id`: pende por outra tabela

**A primeira execução já ensinou uma coisa que eu tinha errado**, e é a razão
de `Indireta` existir: `order_reviews` guarda o **comentário** que a pessoa
escreveu e **não tem `customer_id`** — o `UPDATE` chega nela por
`orders.customer_id`. `customer_saved_cards` chega por
`customer_payment_profiles`. Duas tabelas com dado de pessoa que uma varredura
só de coluna não veria.

**Vermelho visto antes do conserto**, com a entrada da tabela nova declarada
e a tabela ainda inexistente:

```
!! customer_social_identities
   esta em ALCANCE como direta e nao tem mais `customer_id` no ORM.
   Entrada de cemiterio ...
1 failed, 8 passed
```

Baseline de hoje: 10 tabelas com `customer_id`, 13 declaradas, **0 achados**.
As quatro `Fora` são `cashback_transactions`, `coupon_redemptions`,
`coupon_claims` e `ai_voice_sessions`, cada uma com o motivo escrito.

`tests/test_alcance_da_anonimizacao.py` tem os três testes que o portão
precisa além do principal (a família do dublê de dado e do teste mudo): que o
varredor **enxerga** tabela e passo de verdade, que ele **acusa** cada um dos
quatro defeitos plantados, e — o par do `pytest.raises` — que ele **deixa
passar** o que está certo.
