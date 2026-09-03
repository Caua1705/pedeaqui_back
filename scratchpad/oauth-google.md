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

---

## 5. O que não pode quebrar — o que cada um precisa saber

**A resposta curta para os quatro primeiros é a mesma, e é o motivo de o caso
(b) existir: todos pendem de `customers.id`, e o `sub` NÃO substitui o
`customer_id` em lugar nenhum.** Enquanto (a) e (b) preservarem o mesmo id, os
quatro continuam intactos sem uma linha de mudança. Quem os quebraria seria o
caso (b) resolvido como o (c) — e é isso que `TestOCasoB` e
`test_o_historico_do_cliente_sobrevive_a_ligacao` (db) travam.

| O quê | O que precisa saber da identidade social | Estado |
|---|---|---|
| **Cashback** | nada. `cashback_transactions.customer_id` — saldo por (cliente, restaurante). Caso (c) nasce com zero, que é certo | intacto |
| **Cupom por segmento** | nada. O RFV sai de `orders` agregado por `customer_id` | intacto |
| **Cupom de primeira compra** | nada — **e é o que sangra em silêncio se o (b) errar**: cliente antigo em conta nova volta a "nunca comprou" e ganha o desconto de novo. Ninguém reclama de um desconto que recebeu | intacto |
| **Histórico** e **"repetir pedido"** | nada. `GET /customers/me/orders` e `/me/orders/{id}` filtram por `customer_id`; o "repetir" é do front, montado sobre `order_items.product_id` — **não há rota de repetição no backend** (conferido: nenhuma ocorrência em `src/`) | intacto |
| **Exclusão de conta (LGPD)** | **precisava, e não sabia** | **consertado** |
| **`CustomerAnonymizationService`** | **precisava, e não sabia** | **consertado** |

### O que quebrou, e o conserto

**1. A exclusão de conta não alcançava a tabela nova.** Achado pelo portão
(`alcance_da_anonimizacao`), não por leitura. Dois estragos:

- o ponteiro para o cadastro da pessoa **dentro do Google** sobrevivia à
  anonimização — exatamente o que `_delete_saved_cards` já evita com o perfil
  de pagamento;
- quem excluísse a conta e voltasse pelo Google cairia no caso (a) e seria
  logado numa conta `is_active=False`: **403 para sempre, sem como se
  recadastrar pelo Google.**

Conserto: `_delete_social_identities`, dentro da transação única. Três testes
`db` — a linha some, o `sub` fica livre para uma conta nova, e a identidade do
vizinho não é tocada.

**2. A exportação da LGPD não mostrava o que a exclusão apaga.**
`GET /customers/me/export` ganhou `social_identities[]` (provider, `sub`,
`created_at`, `last_login_at`). O critério é o das avaliações: tabela que sai
na exclusão e não aparece no acesso é metade só do mesmo direito. O `sub`
entra — é identificador **da própria pessoa**, devolvido só a ela numa rota
autenticada que não aceita id de terceiro.

**3. Conta do Google não tem senha, e três telas dependem de senha.**
`password_hash` é `NOT NULL`; a conta nasce com hash de um segredo sorteado.
Isso fecha o login (bom) e fecha também **alterar senha** e **excluir a
conta** — a segunda é caminho de LGPD.

Conserto: `unusable_password_hash()` passou a levar o prefixo `!` (convenção do
Django), e `GET /customers/me` ganhou **`password_set: bool`**. O prefixo faz
duas coisas: permite *perguntar* se há senha, e recusa por conta própria
(`!$2b$...` não começa com `$2`, cai no formato antigo e o `split` devolve `!`
como algoritmo). Duas recusas independentes.

O caminho para quem tem `password_set: false` **já existe inteiro e não é
novo**: `POST /auth/forgot-password` → `verify-reset-code` → `reset-password`
manda o código para o e-mail que o Google verificou. Está escrito nos
docstrings de `DELETE /customers/me` e `PATCH /customers/me/password`.

> **Pendência de produto, e é decisão sua.** Excluir a conta continua exigindo
> senha, então quem entrou pelo Google precisa **definir uma antes**. Trocar
> isso — aceitar um `id_token` fresco, ou o código do e-mail, como
> reautenticação da exclusão — é decisão de produto que não está no enunciado,
> e por isso não foi feita. O que foi feito é a tela conseguir avisar em vez de
> responder "Senha incorreta" sem saída.

`social_providers` **não** entrou em `GET /customers/me`, de propósito: exigiria
consulta, e `get_me` é tradução pura do objeto que já chegou (é o que
`test_colunas_em_desacordo.py` usa construindo o service com sessão nenhuma). A
lista está na exportação, que já consulta tudo.

### Portões, depois de tudo

    alcance_da_anonimizacao   0 achados   (12 tabelas com customer_id, 14 declaradas)
    espelhos_de_enum          0 achados   (23 colunas de enum)
    escrita_e_transacao       0 achados
    estado_entre_workers      0 achados   (CHAVES_DO_GOOGLE declarada)
    filtros_por_exclusao      0 achados
    dubles_de_dado            0 achados
    dados_pessoais_em_chave   0 achados
    testes_mudos              0 achados
    divergencias_orm_schema   41          (inalterado — o model novo bate com o DDL)
    leituras_de_coluna_nulavel 201        (era 182; os 19 novos conferidos um a um)
    suíte rápida              2688 passed
    suíte db                  690 passed
    openapi.json              em dia

---

## 6. O contrato para o front — pronto para colar

Tudo `application/json`. Nenhuma destas rotas exige `Authorization`.

### 1. `POST /auth/google`

```jsonc
// manda
{ "id_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6..." }   // o idToken, NAO o accessToken
```

Três respostas possíveis, e o `status` diz qual bloco veio preenchido.

**(a) já conhecido — entra:**

```jsonc
{
  "status": "authenticated",
  "message": "Login realizado com sucesso.",
  "email": "pessoa@gmail.com",
  "access_token": "eyJ...",          // o MESMO de POST /auth/login
  "token_type": "bearer",
  "customer": { "id": "uuid", "name": "...", "email": "...", "phone": "...", "email_verified": true }
}
```

**(b) e-mail já tem conta — mandamos um código, e ninguém entrou:**

```jsonc
{
  "status": "link_confirmation_required",
  "message": "Este e-mail já tem uma conta. Enviamos um código para ele: ...",
  "email": "pessoa@gmail.com",
  "link_ticket": "eyJ..."            // guarde e mande na rota 3
}
```

**(c) cadastro novo — faltam dois campos:**

```jsonc
{
  "status": "profile_required",
  "message": "Falta pouco: informe telefone e data de nascimento ...",
  "email": "pessoa@gmail.com",
  "name": "Pessoa de Teste",         // o nome do Google; pode ser o próprio e-mail
  "signup_ticket": "eyJ..."          // guarde e mande na rota 2
}
```

Erros: **401** `id_token` inválido **ou** e-mail não verificado no Google (a
mensagem distingue) · **403** conta inativa · **502** Google fora do ar (pode
tentar de novo) · **503** o servidor não tem `GOOGLE_OAUTH_CLIENT_IDS`.

### 2. `POST /auth/google/complete-signup` — fecha o caso (c)

```jsonc
// manda
{
  "signup_ticket": "eyJ...",
  "phone": "85999998888",            // aceita máscara; normalizamos para dígitos
  "birth_date": "1990-05-20",
  "privacy_accepted": true,
  "marketing_opt_in": false,         // opcional, default false
  "name": "Maria da Silva"           // opcional; sem ele fica o nome do Google
}

// volta 200 — o MESMO LoginResponse do login por e-mail
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "customer": { "id": "uuid", "name": "...", "email": "...", "phone": "...", "email_verified": true },
  "requires_email_verification": false,
  "email": null,
  "message": null
}
```

Erros: **400** ticket que não vale mais (venceu, foi adulterado, ou é o ticket
da outra rota) ou `privacy_accepted` ausente · **409** telefone já cadastrado,
ou o `sub`/e-mail mudou de estado entre as duas telas — **409 aqui significa
recomeçar em `POST /auth/google`**, não "tente outros dados".

### 3. `POST /auth/verify-email-code` — fecha o caso (b)

**É a rota de código que você já usa.** O campo novo é opcional; sem ele nada
mudou.

```jsonc
// manda
{ "email": "pessoa@gmail.com", "code": "123456", "google_link_ticket": "eyJ..." }

// volta 200
{
  "verified": true,
  "message": "Conta do Google ligada com sucesso.",
  "linked_provider": "google",
  "access_token": "eyJ...",
  "token_type": "bearer",
  "customer": { "id": "uuid", ... }
}
```

Sem `google_link_ticket` a resposta é a de sempre — `{"verified": true,
"message": "E-mail verificado com sucesso."}`, com os quatro campos novos em
`null`.

Erros: **400** código errado/expirado, ou ticket que não vale mais · **403**
conta inativa · **404** cliente não encontrado · **409** essa conta do Google
já está ligada a outra conta · **429** muitas tentativas neste código.

**Reenviar o código do caso (b) é chamar `POST /auth/google` de novo.**
`POST /auth/resend-email-code` **não serve**: ele desiste em silêncio quando o
e-mail já está verificado, que é o caso da maioria das contas existentes. O
cooldown de 60 s e o teto de 3 códigos em 15 min valem igual, e um
`link_ticket` novo vem junto.

### 4. `GET /customers/me` — dois campos novos a olhar

```jsonc
{ "...": "...", "password_set": false }
```

`password_set: false` é a conta que entrou pelo Google e nunca definiu senha.
Com ela:

- **"Alterar senha" não funciona** (não há senha atual) → mostre "Definir
  senha" e mande para `POST /auth/forgot-password`, que já manda o código para
  o e-mail que o Google verificou;
- **"Excluir conta" também não** — a rota exige a senha atual. Avise **antes**
  ("defina uma senha para excluir a conta"), em vez de mostrar um campo que só
  pode responder "Senha incorreta".

`GET /customers/me/export` ganhou `social_identities[]` com `provider`,
`provider_user_id`, `created_at` e `last_login_at`.

### O fluxo inteiro, em uma tela

```
                    POST /auth/google  { id_token }
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
  authenticated          profile_required        link_confirmation_required
        │                        │                        │
   já tem sessão      pede telefone + nascimento    tela de código
        │                        │                        │
        │            POST /auth/google/complete-signup   POST /auth/verify-email-code
        │                        │                    { email, code, google_link_ticket }
        └────────────────────────┴────────────────────────┘
                                 │
                          access_token
```

---

## 7. Variáveis de ambiente

| Nome | Obrigatória | O que acontece se faltar |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_IDS` | **na prática, sim** | as duas rotas de Google respondem **503**; o resto da API sobe e funciona igual |
| `GOOGLE_OAUTH_JWKS_URL` | não | padrão `https://www.googleapis.com/oauth2/v3/certs` |
| `GOOGLE_OAUTH_TIMEOUT_SECONDS` | não | 5 |
| `GOOGLE_OAUTH_TICKET_MINUTES` | não | 15 |

`GOOGLE_OAUTH_CLIENT_IDS` são os client ids do Google Cloud **separados por
vírgula** — um por plataforma (web, Android, iOS), porque o `aud` do `id_token`
é o client id daquela plataforma:

```
GOOGLE_OAUTH_CLIENT_IDS=123-web.apps.googleusercontent.com,456-android.apps.googleusercontent.com
```

**Não há `CLIENT_SECRET`, e não é esquecimento.** O segredo existe no fluxo de
*código de autorização*, em que o servidor troca um `code` por tokens. Aqui o
app faz o login no aparelho e chega com o `id_token` pronto: assinatura, `aud`
e `iss` são conferidos contra chave **pública**. Uma variável de segredo sem
uso seria uma credencial a mais para vazar sem nada em troca. Se um dia o fluxo
de `code` entrar, o segredo entra com ele.

Nada de client id ou secret está no repositório. O `.env` do CI e o
`docker-compose.yml` não foram tocados.

---

## 8. O que ficou pendente

1. **Excluir a conta ainda exige senha.** Quem entrou pelo Google precisa
   definir uma antes (forgot-password → reset-password). O app agora consegue
   avisar (`password_set`), mas trocar a reautenticação da exclusão — aceitar
   um `id_token` fresco, ou o código do e-mail — é **decisão de produto que não
   está no enunciado**, e por isso não foi tomada.
2. **Não há rota para desligar o Google de uma conta.** Não foi pedida. Quando
   for, ela precisa recusar desligar o último método de acesso de uma conta com
   `password_set: false` — senão a pessoa se tranca para fora.
3. **Não há Apple.** A tabela e o `CHECK` já esperam por ela: o provedor entra
   em `SOCIAL_AUTH_PROVIDERS` **e** numa revisão do Alembic, juntos
   (armadilha 15 — e `espelhos_de_enum` agora enxerga esse CHECK, porque o
   corte de um valor foi baixado para 1 nesta rodada).
4. **`resend-email-code` continua sem servir ao caso (b)**, por decisão: mexer
   nele seria mexer no fluxo de e-mail existente, que é gatilho de parada do
   item 5. O reenvio é `POST /auth/google` de novo, documentado na rota.
5. **`GOOGLE_OAUTH_CLIENT_IDS` não derruba o boot quando falta.** É o desenho
   do `PLATFORM_METRICS_KEY`. Se você quiser que falte vire erro de boot,
   `startup_checks.py` é o lugar — mas aí todo ambiente sem Google configurado
   para de subir.
