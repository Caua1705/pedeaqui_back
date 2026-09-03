# Rodada WhatsApp (Cloud API oficial) — fonte da verdade

Branch: `rodada/whatsapp`, saindo da `main`. Nunca commitar na `main`. Um
commit por item, verde. Portão sem pipe, e o resultado nunca como argumento
de `&&`.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 0 | O que EU faço no painel da Meta, e o que colo no `.env` | **escrito** (parte II deste arquivo) |
| 1 | A estrutura: canal por filial, token cifrado | **feito** — 3 tabelas, revisao `0051`, `scripts/register_whatsapp_channel.py` |
| 2 | O webhook: assinatura da Meta e roteamento pelo `phone_number_id` | **feito** — `GET`+`POST /webhooks/whatsapp` |
| 3 | Mandar mensagem: janela de 24h × template aprovado | **feito** — `WhatsAppSender` |
| 4 | Os avisos de status do pedido: aceito, saiu, entregue | pendente |

---

## Decisões que vieram do enunciado — não redecidir

1. **O número é POR FILIAL, com queda no restaurante.** Filial com número
   usa o dela; filial sem número usa o do restaurante. Restaurante de uma
   filial só deixa o campo vazio. É o regime da armadilha 35: `NULL`
   significa "herda", e só `NULL`.
2. **O webhook é ÚNICO para a aplicação inteira.** A Meta manda tudo para o
   mesmo endereço; o roteamento é pelo `phone_number_id` do payload. Webhook
   por restaurante é o que fica caro mudar depois.
3. **Hoje a configuração é na mão; amanhã é Tech Provider.** A estrutura
   nasce pronta para isso: o token mora numa coluna por canal, então a
   migração é trocar de onde ele CHEGA, não o desenho.
4. **Nada de chatbot nesta rodada.** Só os avisos de status.
5. **Não tenho acesso à Business Manager do Júnior.** Conectar número,
   verificar e aprovar template é operação, e é minha.

---

# Parte I — o desenho

## 1. Onde o token mora, e como este repositório já guarda segredo

O repositório **já tem esse problema resolvido uma vez**, e a resposta é
copiar aquilo, não inventar:

`restaurant_payment_credentials` guarda o `access_token` do Mercado Pago de
cada restaurante numa coluna `access_token_encrypted`, cifrada com **Fernet**
(`src/utils/crypto.py`), com a chave em `.env`
(`PAYMENT_CREDENTIALS_ENCRYPTION_KEY`) e **nunca** no banco — cifrar a coluna
é inútil se a chave fica ao lado dela. Fernet e não hash porque a aplicação
precisa do valor ORIGINAL a cada chamada (o `Authorization` do Graph), então
tem que dar para decifrar; e Fernet é autenticado, então um bloco alterado no
banco quebra a verificação em vez de decifrar em lixo.

O token do WhatsApp é **a mesma natureza de segredo**: credencial de um
terceiro (a Business Manager do lojista), que precisa ser enviada em texto a
cada requisição, e que não é nossa. Então:

> **O token vai numa coluna `access_token_encrypted` da tabela
> `whatsapp_channels`, cifrado com Fernet, decifrado na hora do envio e
> nunca guardado em atributo de objeto de vida longa nem em log.**

O que **não** vai para o banco, e o que **não** vai para o `.env`:

| Segredo | Onde mora | De quem é |
|---|---|---|
| `access_token` do número | banco, cifrado | do lojista (BM dele) |
| **App Secret** da Meta | `.env` `WHATSAPP_APP_SECRET` | **nosso** — assina o webhook de TODOS os números |
| Verify token do webhook | `.env` `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | **nosso** — eu invento |
| Chave que cifra o token | `.env` `WHATSAPP_TOKEN_ENCRYPTION_KEY` | nossa |

**A chave de cifra é NOVA e não reaproveita a do pagamento.** Armadilha 32,
"segredo novo por público novo": são credenciais de terceiros diferentes, e
rotacionar uma por causa de um incidente do gateway obrigaria a recifrar as
outras no mesmo minuto. **Sem fallback de uma para a outra** — o fallback
entre segredos foi exatamente o que a armadilha 32 registra como o furo.

**Por que o App Secret é global e o token é por linha.** O app da Meta é
NOSSO e é um só para a plataforma inteira (parte II, A1): é ele que assina o
`X-Hub-Signature-256` de todo webhook que chega, venha do número que vier.
O token é a credencial de acesso ao WABA do lojista. Isso inverte a ordem do
webhook em relação à do pagamento — ver o item 2.

## 2. A estrutura: três tabelas

### `whatsapp_channels` — o número

Uma linha por número conectado.

| Coluna | |
|---|---|
| `restaurant_id` | `NOT NULL` |
| `branch_id` | **nullable — `NULL` é o número do RESTAURANTE (a queda)** |
| `waba_id` | `NOT NULL` |
| `phone_number_id` | `NOT NULL`, **`UNIQUE`** — é a chave de roteamento do webhook |
| `display_phone_number` | `NOT NULL` — o número legível, para a tela e para o log |
| `access_token_encrypted` | `NOT NULL`, Fernet |
| `is_active` | `NOT NULL`, default `true` |

**Por que tabela e não colunas em `branches` + `restaurant_settings`.** O
enunciado diz "é o mesmo padrão da taxa de entrega", e a SEMÂNTICA é essa
mesma — filial sobrescreve, restaurante é a queda. Mas a taxa de entrega é
lida a partir da filial que já se conhece, e aqui a pergunta principal é a
**inversa**: o webhook chega com um `phone_number_id` e precisa achar a
filial. Com as colunas espalhadas em duas tabelas, esse `SELECT` vira dois, e
— o que importa mais — **não existe `UNIQUE` que atravesse duas tabelas**: o
mesmo `phone_number_id` cadastrado nos dois lugares roteia para dois destinos
e ninguém percebe até o dia em que os dois divergem. Numa tabela só,
`UNIQUE (phone_number_id)` é o banco garantindo que a resposta é uma.

As três travas que a tabela leva, e o que cada uma impede:

- `UNIQUE (phone_number_id)` — um número roteia para um destino, sempre;
- `UNIQUE (branch_id)` — uma filial tem no máximo um número. `NULL` é
  distinto de `NULL` no Postgres, então isto **não** atrapalha as linhas de
  restaurante;
- índice único **parcial** `(restaurant_id) WHERE branch_id IS NULL` — um
  restaurante tem no máximo UMA queda. É o que o `UNIQUE` comum não pega,
  justamente por causa da regra dos nulos acima;
- FK composta `(restaurant_id, branch_id) → branches (restaurant_id, id)` —
  o mesmo cinto do cardápio por filial (armadilha 36), impedindo cadastrar a
  filial de um restaurante debaixo de outro. Com `branch_id` nulo a FK
  composta não é conferida (`MATCH SIMPLE`), que é exatamente o que se quer:
  a linha de restaurante não aponta para filial nenhuma.

### `whatsapp_contact_windows` — a janela de 24h

`(channel_id, phone_e164)` com `window_expires_at`. Escrita quando uma
mensagem do cliente CHEGA no webhook; lida na hora de enviar para decidir
entre texto livre e template.

**Ela guarda telefone, e telefone é dado pessoal numa tabela que não pende de
`customers`.** Armadilha 38: nasce com prazo, e o prazo é o mecanismo de
exclusão. A linha com `window_expires_at` no passado não serve para mais
nada — o expurgo entra em `scripts/cleanup_idempotency_keys.py`, junto das
outras seis.

### `whatsapp_messages` — o que saiu, e se chegou

`order_id`, `channel_id`, `kind` (qual aviso), `wamid` (o id da Meta),
`status`, `error_code`.

Não é enfeite: **sem ela, "o cliente foi avisado" é uma suposição.** O
`status` chega pelo próprio webhook (`statuses[]`: `sent`, `delivered`,
`read`, `failed`), e é o que separa "mandei" de "chegou". `UNIQUE (order_id,
kind)` fecha o aviso repetido de graça.

Sem telefone nela: quem tem o telefone é o pedido.

## 3. O webhook, e a ordem que ele NÃO herda do pagamento

`POST /webhooks/whatsapp` e `GET /webhooks/whatsapp` (a verificação da Meta,
que devolve o `hub.challenge`).

A ordem aqui é **o contrário** da do `handle_webhook` do pagamento, e a razão
merece ficar escrita porque parece incoerência:

| | pagamento | WhatsApp |
|---|---|---|
| de quem é o segredo da assinatura | do RESTAURANTE | **nosso** (App Secret) |
| dá para verificar antes de saber a origem? | não | **sim** |
| ordem | achar o pedido → achar a credencial → conferir | **conferir → rotear** |

No Mercado Pago, saber qual segredo usar exige antes saber de qual
restaurante é o pagamento — por isso lá a assinatura é conferida no meio do
caminho. Aqui o segredo é um só para a aplicação inteira, então **nada
acontece antes da assinatura**: `X-Hub-Signature-256`, HMAC-SHA256 sobre os
**bytes crus** do corpo, comparado com `hmac.compare_digest` (armadilha 18).
Corpo reserializado não bate — por isso a rota é `async def`, como a de
pagamento, para pegar `await request.body()`.

**`phone_number_id` desconhecido: 200, com log, e nada mais.** Não é
invenção — é o que o repositório já faz com `unknown_payment`, e os motivos
são os mesmos com um a mais:

- **reenviar não conserta.** A Meta retenta em cima de qualquer resposta que
  não seja 2xx, e desativa o webhook do app depois de falha sustentada.
  Devolver 5xx para um número que não é nosso é trocar um evento inútil por
  um canal derrubado — e o canal é o mesmo para TODOS os restaurantes;
- **não há o que proteger**: sem canal não há filial, não há pedido e não há
  estado a mudar;
- **um número novo do próprio Júnior cai aqui.** É o sintoma esperado de
  "conectei o número na Meta e esqueci de cadastrar do nosso lado", e o log
  precisa dizer isso com todas as letras, com o `display_phone_number` que
  vem no próprio payload (`metadata`) — que é público, é o número comercial
  da loja, e é o que me deixa cadastrar sem abrir o painel da Meta.

O corpo pode trazer **vários** `entry[]`/`changes[]`, de números diferentes,
no mesmo POST: o roteamento é por `change`, não por requisição.

## 4. Enviar: a janela de 24h

Duas formas, e a Meta só aceita a primeira dentro da janela:

- **texto livre** — só se houver janela aberta, isto é, se o cliente mandou
  mensagem para AQUELE número nas últimas 24h;
- **template aprovado** — sempre permitido, e é o único caminho fora da
  janela.

**Os avisos de status desta rodada são SEMPRE template**, e isso não é
detalhe de implementação: o cliente pediu pelo app e nunca escreveu no
WhatsApp da loja, então a janela está fechada em praticamente 100% dos
pedidos. Um envio de texto livre ali volta `131047` e o cliente não é
avisado — sem erro do nosso lado, porque a resposta da Meta é um 400 que
alguém precisa estar lendo.

A função de envio recebe o que mandar e **decide sozinha**: janela aberta e
pedido de texto livre → manda texto; janela fechada → recusa o texto livre em
vez de mandar e falhar. A regra não pode morar em quem chama (armadilha 46:
regra que depende de alguém lembrar de aplicá-la é recomendação).

## 5. Os avisos

Três, nos três estados que o enunciado pediu: `accepted`,
`out_for_delivery`, `completed`.

**Onde eles entram: `OrderStatusChangeService.apply`, depois do commit, fora
do `try`** — no mesmo lugar e pelo mesmo motivo do estorno. Falar com a Meta
não pode fazer parte da transação do pedido: um timeout deles desfaria a
mudança de status, e quem clicou veria um erro com o pedido já na cozinha. E
o `except` largo é o mesmo: a mudança **já está gravada**, e transformar uma
falha de notificação em 500 diria ao lojista que o aceite não aconteceu.

É esse ponto único que faz o aviso valer para as QUATRO portas de escrita de
status (painel, cancelamento, cliente, entregador) sem quatro cópias.

**`completed` de pedido de RETIRADA não manda "foi entregue".** O texto
mentiria, e `out_for_delivery` nem existe naquele fluxo. O aviso que a
retirada quer é "seu pedido está pronto" (status `ready`), e ele **fica de
fora desta rodada** — não foi pedido, e um template a mais é uma aprovação a
mais na Meta.

**O telefone nunca vai para o log.** A linha de log carrega `pedido=#N` e o
`wamid`; o telefone é dado pessoal e o repositório já não loga nem e-mail de
pagador.

**A conversão para E.164 não inventa.** `normalize_digits` deixa só dígitos, e
a armadilha 27 registra o resíduo: `+55 85 9...` vira `5585...` e
`85 9...` vira `859...`. A regra é fechada — 11 dígitos ganham o `55`; 13
dígitos começando com `55` passam como estão; **qualquer outra coisa não é
enviada e vira log**. Chutar um DDI é mandar a mensagem do cliente para o
telefone de outra pessoa.

---

## Item 1 — feito. O que ficou, e o que a implementação ensinou

| Onde | O que |
|---|---|
| `alembic/versions/20260904_0051_*` | as três tabelas |
| `src/models/whatsapp_model.py` | os três models, com o porquê de cada trava |
| `src/repositories/whatsapp_repository.py` | as duas perguntas inversas |
| `scripts/register_whatsapp_channel.py` | o cadastro (token por prompt oculto) |
| `src/utils/crypto.py` | a segunda chave Fernet, sem queda para a primeira |
| `tests/test_migracao_whatsapp_db.py` | as travas do banco, por SQL cru |
| `tests/test_whatsapp_canal_db.py` | a herança e o roteamento |
| `tests/test_whatsapp_token_cifrado.py` | as duas chaves não se substituem |

**Duas coisas apareceram fazendo, e não estavam previstas.**

**1. O `UNIQUE (branch_id)` não trava a queda.** No Postgres `NULL` é distinto
de `NULL`: duas linhas de restaurante do MESMO restaurante entram pelo UNIQUE
sem reclamar, e a filial sem número herdaria uma das duas — a que o `ORDER BY`
escolhesse. Quem fecha é um índice único **parcial**
(`WHERE branch_id IS NULL`). Conferido por mutação contra o banco de teste:
derrubado o índice dentro de uma transação revertida, as duas linhas entram. O
teste não é vácuo.

**2. O número da filial DESLIGADO não cai no do restaurante.** A regra não
estava no enunciado e precisou ser decidida: `resolve_for_branch` só herda
quando a filial **não tem linha nenhuma**. Cair seria a loja passando a falar
por outro número sem ninguém ter pedido — e o que se espera de um número
desligado é que a loja pare de mandar.

**E um portão que eu não conhecia:** `tests/test_diagrama_er_cobre_o_schema.py`
cobra que toda tabela do ORM apareça num `erDiagram` de
`docs/modelo-de-dados.md`. Tabela nova sem desenho é vermelho — foi o único
vermelho desta etapa que não veio de código meu.

---

## Item 2 — feito. O webhook

| Onde | O que |
|---|---|
| `src/integrations/whatsapp_client.py` | assinatura e leitura do corpo (só transporte) |
| `src/services/whatsapp_webhook_service.py` | roteamento, janela, status |
| `src/api/endpoints/whatsapp.py` | as duas rotas, registradas em `main.py` |
| `tests/test_whatsapp_webhook_integracao.py` | assinatura e parser |
| `tests/test_whatsapp_webhook_db.py` | número desconhecido, janela, status |
| `tests/test_whatsapp_webhook_rota.py` | o `hub.challenge` e o corpo cru |

**A ordem é o contrário da do pagamento, e isso não é incoerência.** Lá o
segredo é do RESTAURANTE, então não dá para conferir a assinatura antes de
descobrir de quem é o pagamento. Aqui é o App Secret do nosso app, um só:
**nada acontece antes da assinatura** — nem leitura do corpo, nem banco.

**Três coisas apareceram fazendo:**

1. **Um POST pode trazer vários números.** `entry[].changes[]`, cada um com o
   seu `phone_number_id`. Rotear por requisição creditaria as mensagens de
   uma loja à outra, sem erro nenhum. O roteamento é por `change`.
2. **A Meta reenvia fora de ordem.** A janela usa `GREATEST` (um reenvio
   antigo não encurta a que a mensagem nova abriu) e o status só AVANÇA (um
   `sent` depois de um `read` faria a linha mentir). Nenhuma das duas estava
   prevista.
3. **A Meta tem status que nós não temos** (`deleted`, `warning`). Gravá-los
   morreria no CHECK e derrubaria o POST inteiro — são ignorados, com log.

**E dois portões acusaram, os dois com razão:**
`escrita_e_transacao.py` não conhecia o verbo `extend` (entrou no vocabulário
de escrita), e `filtros_por_exclusao.py` viu a negação sobre
`WHATSAPP_MESSAGE_STATUSES` (entrou em `ESPERADOS` como *negação de
permitidos*: status novo é recusado, que é o lado que fecha).

### O que ainda não existe, e por quê

O canal só ROTEIA. Nada é enviado ainda, e a janela de 24h é escrita sem ter
leitor — ela ganha um no item 3.

---

## Item 3 — feito. O envio

| Onde | O que |
|---|---|
| `src/integrations/whatsapp_client.py` | `send_template_message` e `send_text_message` |
| `src/services/whatsapp_send_service.py` | `WhatsAppSender` e `to_whatsapp_phone` |
| `tests/test_whatsapp_envio.py` | telefone e a chamada ao Graph (transporte dublado) |
| `tests/test_whatsapp_envio_db.py` | a janela decide, e a Meta nem é chamada |

**A decisão mora no envio, não em quem chama.** `send_text` recusa fora da
janela ANTES de existir chamada. Tentar e falhar custaria um `131047` da Meta
e o mesmo cliente não avisado, com o motivo escondido na resposta deles.

**Conferido por mutação:** com `is_open` respondendo sempre `True`, os três
testes da janela ficam vermelhos e a chamada acontece. A trava é real.

**Duas decisões que o enunciado não trazia:**

1. **A conversão para E.164 recusa o que não dá para afirmar.** 10 ou 11
   dígitos ganham o `55`; 12 ou 13 começando com `55` passam; **o resto não é
   enviado**. É a armadilha 27 pelo lado da saída: chutar um DDI é mandar a
   mensagem de um cliente para o telefone de outra pessoa.
2. **200 sem `wamid` conta como recusa.** Sem ele o webhook de status não
   acha a linha depois — chamar aquilo de sucesso gravaria um envio que
   ninguém consegue acompanhar.

**Portão, de novo:** `escrita_e_transacao.py` não conhecia o prefixo `is_`.
Entrou no vocabulário de LEITURA, ao lado de `has` e `exists`.

---

# Parte II — o que EU faço, na ordem

Nada abaixo é código. É o que só eu consigo fazer, e o que o backend espera
encontrar pronto.

## A. O app da Meta — uma vez, e ele é NOSSO

Um app para a plataforma inteira, não um por restaurante. É a mesma decisão
do webhook único, e pela mesma razão.

1. `developers.facebook.com` → **Meus Apps** → **Criar app** → tipo
   **Empresa (Business)** → vincular à **MINHA** Business Manager (não à do
   Júnior).
2. No app: **Adicionar produto** → **WhatsApp** → Configurar.
3. **Configurações do app → Básico → Chave Secreta do App** → *Mostrar*.
   Esse valor é o `WHATSAPP_APP_SECRET`. **Guarde agora** — ele fica escondido
   atrás de nova confirmação de senha depois.

## B. O WABA do Júnior chega até o app

4. **Júnior** (admin da BM dele): *Configurações do Negócio* → *Contas* →
   *Contas do WhatsApp* → escolhe o WABA → **Parceiros** → *Adicionar
   parceiro* → informa o **ID da minha Business Manager** → conceder
   **controle total** sobre o WABA.
   - Alternativa: ele me adicionar como **pessoa** (admin) na BM dele.
     Funciona, e é pior: amarra o acesso à minha conta pessoal do Facebook
     em vez da empresa.
5. Na **minha** BM: *Contas do WhatsApp* → o WABA do Júnior aparece →
   **Adicionar recursos** → o app do passo 1.
6. Os **dois números** precisam estar conectados ao WABA na Cloud API, cada
   um com **verificação em duas etapas** (PIN de 6 dígitos) definida.

> ⚠️ **O número sai do aplicativo WhatsApp Business do celular.** Um número
> na Cloud API não atende ao mesmo tempo no app do celular. Se hoje alguém
> responde cliente por ali, isso PARA no minuto da migração. É a decisão de
> operação que mais atrasa piloto: ou o Júnior aceita, ou entra um chip novo
> só para os avisos. Decidir isto ANTES do passo 6, porque migrar de volta é
> trabalhoso.

## C. O token — e ele não vai para o `.env`

7. Na BM que tem o WABA: *Configurações do Negócio* → *Usuários* →
   **Usuários do sistema** → *Adicionar* → nome `pedeaqui-api`, papel
   **Administrador**.
8. Nesse usuário: **Adicionar recursos** → o **app** (passo 1, permissão de
   gerenciar) e o **WABA** (controle total).
9. **Gerar novo token** → escolher o app → validade **Nunca expira** →
   marcar `whatsapp_business_messaging` e `whatsapp_business_management` →
   copiar.

> ⚠️ O token de **24 horas** que aparece em *WhatsApp → Configuração da API*
> serve só para teste. Com ele, os avisos param sozinhos no dia seguinte, sem
> nada errado do nosso lado.

O token **não entra no `.env`**: é do Júnior, é por número, e mora cifrado no
banco. Ele entra por um script de cadastro (item 1 desta rodada), do mesmo
jeito que a credencial do Mercado Pago entra hoje.

## D. Os ids de cada número

10. *WhatsApp → Configuração da API*: o seletor mostra, **por número**:
    - **ID do número de telefone** → `phone_number_id`
    - **ID da conta do WhatsApp Business** → `waba_id` (igual nos dois)
    - o número em si → `display_phone_number`

    Anotar os **dois pares**, dizendo qual é de qual filial. É isto que eu
    passo para o cadastro junto do token.

## E. O webhook

11. Inventar o verify token (só meu, a Meta não dá):
    `openssl rand -hex 16` → `WHATSAPP_WEBHOOK_VERIFY_TOKEN`.
12. **Subir a API com `WHATSAPP_APP_SECRET` e
    `WHATSAPP_WEBHOOK_VERIFY_TOKEN` no ambiente ANTES do passo 13.** A Meta
    faz um `GET` no instante do clique; sem a variável a rota responde 503 e
    o painel só diz que não conseguiu validar.
13. App → **WhatsApp → Configuração → Webhook → Editar**:
    - URL de callback: `https://<domínio da API>/webhooks/whatsapp`
    - Token de verificação: o do passo 11
    - **Verificar e salvar**
14. Ainda ali → **Gerenciar** → assinar o campo **`messages`**. É um campo
    só, e ele traz as duas coisas: mensagem recebida e status de entrega.
    **Sem ele o webhook nunca é chamado** — e nada no painel avisa.

## F. Os três templates

*WhatsApp Manager → Modelos de mensagem → Criar modelo.* Nos três:
categoria **Utilidade**, idioma **Português (BR)**, sem cabeçalho e sem
botão.

> Categoria **Utilidade** e não Marketing de propósito: aviso transacional
> é utilidade, custa menos, e Marketing pode ser bloqueado pela preferência
> do próprio usuário — o aviso do pedido dele sumiria.

| Nome do modelo | Corpo |
|---|---|
| `pedido_aceito` | `Olá, {{1}}! Seu pedido #{{2}} foi aceito e já está sendo preparado pelo {{3}}. Avisamos por aqui quando ele sair para entrega.` |
| `pedido_saiu_para_entrega` | `Olá, {{1}}! Seu pedido #{{2}} saiu para entrega e está a caminho. Qualquer dúvida, fale com o {{3}}.` |
| `pedido_entregue` | `Olá, {{1}}! Seu pedido #{{2}} foi entregue. Obrigado por pedir no {{3}}!` |

`{{1}}` nome do cliente · `{{2}}` número do pedido · `{{3}}` nome da loja.

**Os três com as mesmas três variáveis, na mesma ordem, de propósito:** um
caminho de código só monta os três. O formulário pede exemplos para cada
variável — usar `Maria`, `5471`, `Júnior da Picanha`.

A aprovação leva de minutos a horas. Enquanto não aprova, o envio volta
`132001` (modelo não existe) — o nosso lado loga e não derruba nada.

## G. O `.env`, depois de tudo

```
WHATSAPP_APP_SECRET=<passo 3>
WHATSAPP_WEBHOOK_VERIFY_TOKEN=<passo 11>
WHATSAPP_TOKEN_ENCRYPTION_KEY=<gerar, ver abaixo>
WHATSAPP_NOTIFICATIONS_ENABLED=true
```

A chave de cifra:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> **Perder essa chave torna os tokens do banco ilegíveis** e obriga a
> recadastrar cada número. Ela fica no mesmo lugar que
> `PAYMENT_CREDENTIALS_ENCRYPTION_KEY`, e **não é a mesma**.

`WHATSAPP_NOTIFICATIONS_ENABLED` **nasce falsa** — o mesmo desenho de
`cashback_rules.enabled` (armadilha 26). É o freio que não depende de
escrita no banco: um dia de aviso duplicado se apaga com uma variável e um
restart, sem `UPDATE` de madrugada.

## H. O que ainda depende de mim, e não é da Meta

- **O opt-in.** A Meta exige que o cliente tenha consentido em receber
  mensagem no WhatsApp. Na prática do piloto isso é uma linha no checkout do
  app — *"vamos te avisar pelo WhatsApp sobre este pedido"* — e é do lado do
  app, não do backend. Sem ela, o risco é qualidade do número caindo por
  denúncia, que é o que derruba o canal do Júnior.
- **Contar ao Júnior o que muda no celular dele** (o aviso do passo 6).

---

# Parte III — o que ficou registrado e NÃO foi feito

- **Aviso de "pronto para retirada"** (`ready`): o aviso que falta para o
  pedido de retirada. Pede um quarto template.
- **Chatbot / atendimento pelas mensagens que chegam.** Esta rodada guarda a
  janela de 24h e descarta o conteúdo da mensagem recebida de propósito:
  guardar texto de cliente é dado pessoal com prazo, e não há quem leia.
- **Onboarding automático (Tech Provider / Embedded Signup).** Muda só de
  onde o token chega; a coluna é a mesma.
- **Reenvio de aviso que falhou.** Hoje o `status=failed` fica gravado em
  `whatsapp_messages` e ninguém retenta. A varredura, se um dia existir, lê
  essa tabela.
