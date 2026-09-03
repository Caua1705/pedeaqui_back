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

---

## 2. A tabela: `customer_social_identities` (revisão `20260904_0049`)

    id, customer_id (FK ON DELETE CASCADE), provider, provider_user_id,
    created_at, last_login_at
    UNIQUE (provider, provider_user_id)
    CHECK  provider = ANY (ARRAY['google'])
    INDEX  (customer_id)

**`provider_user_id` é o `sub`.** É ele que liga, nunca o e-mail: e-mail muda,
`sub` não. O `UNIQUE (provider, provider_user_id)` é o que faz o vínculo valer
— uma conta do Google aponta para UM cliente, sempre.

**O e-mail do Google não é gravado.** Não serve para o vínculo, e uma cópia
dele aqui seria dado pessoal numa segunda tabela fora de `customers` — a
mesma coisa que `delete_codes_of` existe para limpar nas tabelas de código.
Uma cópia a menos é um passo a menos que pode ser esquecido na exclusão.

**Não há `UNIQUE (customer_id, provider)`, de propósito.** Dois Google no
mesmo cliente é legítimo (o pessoal e o do trabalho, os dois confirmados por
código); um UNIQUE ali derrubaria a segunda ligação com `IntegrityError` no
fim de um fluxo que deu certo. O `db` prova as duas metades.

### Portões que a tabela acendeu, e o que cada um pegou

- **`alcance_da_anonimizacao`** ficou vermelho na hora
  (`tem customer_id e nao esta em ALCANCE`) → passo `_delete_social_identities`
  no `CustomerAnonymizationService`, com três testes `db`;
- **`test_diagrama_er_cobre_o_schema`** ficou vermelho → a tabela entrou em
  `docs/modelo-de-dados.md`, com o parágrafo do porquê do `sub`;
- **`espelhos_de_enum`** ficou vermelho por um motivo inesperado e melhor:
  `MINIMO_DE_VALORES = 2` fazia o varredor **ignorar** um CHECK de um valor
  só. Ou seja, a coluna que MAIS precisa do portão — `google` esperando
  `apple` — era invisível para ele. Baixado para 1; a varredura contra o banco
  de teste continua em **zero achados**, sem ruído.

**O que quebraria sem o passo da anonimização** (é o que o varredor achou, e
tem dois andares): a conta anonimizada guardaria um ponteiro para o cadastro
da pessoa dentro do Google — e quem excluísse a conta e voltasse pelo Google
cairia no caso (a), `sub` conhecido, **logado numa conta `is_active=False`:
403 para sempre, sem como se recadastrar**. Nenhum teste da suíte falharia.

---

## 3. O cliente do Google (`src/integrations/google_identity_client.py`)

Três conferências, e a segunda é a que separa um token do Google de um token
**para nós**:

1. **assinatura** RS256 contra a chave pública cujo `kid` está no cabeçalho;
2. **`aud` ∈ `GOOGLE_OAUTH_CLIENT_IDS`**;
3. **`iss`** — `accounts.google.com` ou `https://accounts.google.com` (as duas
   formas são emitidas pelo Google e as duas são válidas).

`exp`/`iat` ficam com o PyJWT, e `audience`/`issuer` vão como **argumento do
`decode`**, não como `if` depois dele: o argumento fica ao lado da chave, que
é onde quem lê a chamada olha.

**`email_verified` falso é recusado no cliente, não no service.** Armadilha
46: deixada no service, a regra seria um `if` que a próxima rota a usar este
cliente pode esquecer. Aqui não existe caminho que devolva identidade não
verificada.

### O furo que o `aud` fecha, e a mutação que provou o teste

`test_token_de_outro_aplicativo_e_recusado` é o único teste do arquivo que
cobre uma falha **silenciosa**: um `id_token` legítimo, assinado pelo Google
de verdade, `email_verified` verdadeiro — e emitido para outro aplicativo. O
Google emite milhões por dia. Sem `aud`, quem operasse qualquer um deles
logaria como qualquer cliente dele aqui dentro.

Mutação dirigida (`audience=` trocado por `options={"verify_aud": False}`):
**exatamente 1 de 20 testes falhou, e foi esse.** Com o `aud` de volta, 20/20.

### O cache das chaves

`CHAVES_DO_GOOGLE`, TTL de 1 h, **declarado** em
`scripts/estado_entre_workers.py` — o portão acusou na hora. Com N workers são
N cópias das mesmas chaves **públicas**, que é o correto: não há o que
compartilhar.

**A rotação de chave não depende do TTL.** `kid` desconhecido faz uma segunda
busca imediata; sem isso, uma rotação do Google (que acontece sozinha e sem
aviso) derrubaria todo login por até uma hora. O teste conta as buscas, que é
o que separa "o cache funcionou" de "foi buscar de novo".

### Variáveis de ambiente

| Nome | Obrigatória? | O que faz |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_IDS` | não | client ids separados por vírgula, um por plataforma (web, Android, iOS). **Vazia = as rotas de Google respondem 503**, e nada mais muda |
| `GOOGLE_OAUTH_JWKS_URL` | não | tem padrão |
| `GOOGLE_OAUTH_TIMEOUT_SECONDS` | não | 5 s |
| `GOOGLE_OAUTH_TICKET_MINUTES` | não | 15 min |

**Não há `CLIENT_SECRET`, e a ausência é desenho.** O segredo existe no fluxo
de *código de autorização*, em que o servidor troca um `code` por tokens. Aqui
o app faz o login no aparelho e chega com o `id_token` pronto: tudo o que
conferimos é assinatura, `aud` e `iss`, contra chave **pública**. Uma variável
de segredo sem uso é uma credencial a mais para vazar sem nada em troca.

Opcional e não obrigatória pelo desenho do `PLATFORM_METRICS_KEY`: uma
variável obrigatória derrubaria o boot de todo mundo por causa de uma forma de
login que ainda não está configurada.

---

## 4. As três rotas, os três casos

    POST /auth/google                  os três desfechos, pelo `status`
    POST /auth/google/complete-signup  caso (c): telefone + nascimento -> JWT
    POST /auth/verify-email-code       caso (b): + `google_link_ticket` -> JWT

### O caso (b), que é onde erra

`POST /auth/google` com `sub` novo cujo e-mail já tem conta **não loga e não
liga**: manda o código de seis dígitos e devolve `link_ticket`. Quem liga é
`POST /auth/verify-email-code` quando o código volta certo — **ao cliente que
já existe, nunca criando outro**.

**A rota de código foi reaproveitada, e o fluxo de e-mail não mudou uma
linha.** O que decide o segundo comportamento é um campo **opcional do corpo**
(`google_link_ticket`), e não estado escondido no servidor: o cadastro por
e-mail nunca manda esse campo, então não consegue cair no outro caminho por
acidente. Os campos novos da resposta (`linked_provider`, `access_token`,
`token_type`, `customer`) têm default — campo novo com default é de graça
(armadilha 7).

`TestOFluxoDeEmailNaoMudou` trava essa metade: sem ticket, a rota não devolve
token, não liga identidade e responde a mensagem de sempre.

**Um resíduo conhecido, e ele é do desenho existente:**
`POST /auth/resend-email-code` **não serve** para reenviar o código do caso
(b) — ele desiste em silêncio quando `email_verified_at` já está preenchido,
que é o caso da maioria das contas. O reenvio é chamar `POST /auth/google` de
novo (mesmo cooldown, mesmo teto, ticket novo junto). Está escrito no docstring
da rota. Mexer no `resend_email_code` para cobrir isso **seria mexer no fluxo
de e-mail existente**, que é o gatilho de parada do item 5 — por isso não foi
feito.

### Os tickets, e por que não uma tabela

Uma tabela de "ligações pendentes" custaria migração, prazo de retenção
(armadilha 38: `sub` + e-mail são rastro de pessoa) e mais um passo na exclusão
de conta — para guardar um estado de 15 minutos que pertence a uma pessoa que
está com a tela aberta. O ticket é esse estado, assinado por nós e guardado por
ela, e **não autoriza nada sozinho**: quem autoriza é o código na caixa de
entrada.

`purpose` próprio para cada um (`google_signup`, `google_link`) — armadilha 32,
que é exatamente o caso em que `purpose` serve: separar usos que compartilham a
chave. Testado nos dois sentidos: nenhum dos dois vale como sessão, e nenhum
vale pelo outro.

### Mutação: 8 garantias, 8 mortas

Como as rotas foram escritas antes dos testes, o vermelho veio por mutação
dirigida — apagar a garantia e exigir que um teste caia:

| Mutação | Resultado |
|---|---|
| caso (b) loga direto (auto-link por e-mail) | **4 falhas** |
| `verify_email_code` ignora o `google_link_ticket` | **7 falhas** |
| liga sem conferir de que conta é o ticket | 1 falha |
| cadastro não reconfere se o `sub` já foi ligado | 1 falha |
| cadastro não reconfere e-mail e telefone | 2 falhas |
| a conta do Google nasce com o e-mail não verificado | 1 falha |
| a senha da conta do Google é vazia em vez de inutilizável | 1 falha |
| conta inativa entra pelo Google | 1 falha |

Nenhum mutante sobreviveu. Mais a mutação do `aud` no cliente do Google (1 de
20), são **9 de 9**.
