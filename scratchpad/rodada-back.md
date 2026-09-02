# Rodada de pendências do backend — fonte da verdade

Branch: `rodada/backend`. Nunca commitar na `main`. Um commit por item, verde,
com push.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa.**

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 1 | `tracking_token` — plano das duas etapas | **feito** (só plano; nada executado) |
| 2 | `print_agent` — criar a conta | **feito** (comando escrito; conta NÃO criada — exige o banco) |
| 3 | Custo de IA por restaurante | **feito** |
| 4.1 | Quebrar a tabela `branches` | **PARADO** — levantamento e plano prontos; espera duas decisões suas |
| 4.2 | README | **feito** |
| 4.3 | Diagrama ER | **feito** |

Fora desta rodada, e anotado se cruzar: `float × Decimal`, entregadores,
máquina de estados do pedido, histórico de chat sem Redis, assistente de voz,
merge na `main`.

---

## 1. `tracking_token` — plano do deploy em duas etapas

**Nada foi executado.** Esta seção é o roteiro; quem digita é você.

O roteiro longo já existe em `docs/deploy-hash-do-tracking-token.md` e continua
válido. O que está aqui é o que ele **não** responde: quanto tempo trava, o que
acontece se falhar no meio, o que fazer no meio da noite, e — o mais
importante — a suspeita de que este deploy **já foi feito**.

### 1.0 A primeira coisa: isto provavelmente já está aplicado

Antes de planejar o deploy, confira se ele ainda existe. Três indícios apontam
para "já foi", e nenhum deles é prova:

1. **A imagem em produção é recente.** A armadilha 49 registra um pedido real
   de R$ 0,01 no Júnior da Picanha em **28/08/2026**, com **cartão salvo** —
   funcionalidade da revisão `20260825_0040`. Se aquilo aconteceu em produção,
   o banco está em `0040` ou depois, e `0016`/`0017` ficaram para trás no
   caminho.
2. **O model não mapeia `orders.tracking_token`.** Antes da `0016` essa coluna
   é `NOT NULL`. Se o banco estivesse em `0015`, **todo pedido novo falharia no
   INSERT** — e pedidos estão entrando.
3. O `docker-entrypoint.sh` roda `alembic upgrade head` em todo deploy. Só um
   `ALEMBIC_TARGET` esquecido no `.env` seguraria a fila, e o entrypoint grita
   `ATENCAO` no log quando isso acontece.

**O comando que responde, e é só leitura:**

```bash
docker exec pedeaqui-api python scripts/estado_da_producao.py
```

Ele diz o `git_sha` da imagem, o alvo do alembic e a revisão do banco contra o
que existe no repositório. Se a revisão do banco for `20260828_0043` (o head de
hoje), **não há deploy pendente e o resto desta seção não se aplica** — pare
aqui.

A conferência que não depende da tabela `alembic_version` (é o schema
respondendo, e também é só leitura):

```bash
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    print(db.execute(text('''
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'orders'
           AND column_name IN ('tracking_token','tracking_token_hash')
         ORDER BY column_name
    ''')).scalars().all())
"
```

| Saída | Onde você está |
|---|---|
| `['tracking_token_hash']` | **as duas já rodaram.** Nada a fazer |
| `['tracking_token','tracking_token_hash']` | entre as duas etapas |
| `['tracking_token']` | antes da `0016` — o roteiro abaixo vale inteiro |

### 1.1 As duas etapas, na ordem, e o que cada uma faz

**Etapa 1 — revisão `20260812_0016`.** Numa transação só:

1. `ALTER TABLE orders ADD COLUMN tracking_token_hash text` (nullable);
2. backfill em Python: lê 1000 linhas por vez e manda **um `UPDATE` por
   linha** com o sha-256 do token que já está gravado;
3. `CREATE UNIQUE INDEX ix_orders_tracking_token_hash` (sem `CONCURRENTLY`);
4. `ALTER TABLE orders ALTER COLUMN tracking_token DROP NOT NULL`.

Nenhum token é regerado: link que já está no WhatsApp de cliente continua
funcionando. **Esta etapa só acrescenta.**

**Etapa 2 — revisão `20260812_0017`.** Também numa transação só:

1. roda o backfill de novo (varre o que tenha entrado por container com código
   antigo);
2. conta `tracking_token_hash IS NULL` — **se sobrar qualquer linha, levanta e
   a transação inteira volta, antes do DROP**;
3. `ALTER TABLE orders ALTER COLUMN tracking_token_hash SET NOT NULL`;
4. `ALTER TABLE orders DROP COLUMN tracking_token`.

O passo 4 é o ponto sem volta.

### 1.2 O que fica travado, e por quanto tempo

**O `docs/deploy-hash-do-tracking-token.md` erra por baixo aqui, e vale
corrigir na sua cabeça antes de abrir a janela.** Ele diz que "o `CREATE
UNIQUE INDEX` trava escrita em `orders` enquanto constrói". Trava mais que
isso:

- `alembic/env.py` abre **uma transação para o upgrade inteiro**
  (`context.begin_transaction()` em volta de `run_migrations`, sem
  `transaction_per_migration`);
- o **primeiro** comando da `0016` é `ALTER TABLE ... ADD COLUMN`, que toma
  **`ACCESS EXCLUSIVE`** sobre `orders`;
- lock de transação só é solto no `COMMIT`.

Logo: **`orders` fica em `ACCESS EXCLUSIVE` do primeiro comando até o commit**
— não só durante o índice, e não só para escrita. `ACCESS EXCLUSIVE` bloqueia
**`SELECT` também**.

Quem sente isso, já que a API está fora do ar durante a migração (o entrypoint
migra antes do Uvicorn):

- os containers `limpeza`, `estorno` e `reindex`, que **continuam de pé** e
  falam com o mesmo banco. O `estorno` roda a cada 15 min e escreve em
  `orders`; ele vai **esperar no lock** (ou estourar o timeout do statement, se
  houver um configurado no Supabase) e o laço dele tenta de novo em 5 min. Não
  é perigoso — é ruído no log que você precisa reconhecer;
- qualquer sessão sua aberta no SQL editor do Supabase contra `orders`.

**Duração — o que domina é o backfill, não o índice.** O backfill é
`N` round-trips ao banco (um `UPDATE` por pedido) mais `N/1000` `SELECT`s.
Round-trip para o Supabase de dentro do container é o que manda:

| pedidos em `orders` | a 3 ms/round-trip | a 20 ms/round-trip | a 60 ms/round-trip |
|---|---|---|---|
| 1 000 | ~3 s | ~20 s | ~1 min |
| 10 000 | ~30 s | ~3,5 min | ~10 min |
| 50 000 | ~2,5 min | ~17 min | ~50 min |
| 100 000 | ~5 min | ~35 min | ~1 h 40 |

O `CREATE UNIQUE INDEX` em cima disso é ruído: btree sobre um `text` de 64
caracteres, ordem de **segundos** até uns 100 mil registros.

**Nenhum desses números é medido — eu não consulto produção nesta sessão.** Os
dois que faltam, e os dois comandos que os dão (os dois são só leitura):

```bash
# 1. quantas linhas o backfill vai percorrer
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    print(db.execute(text('SELECT count(*) FROM orders')).scalar_one(), 'pedidos')
    print(db.execute(text(
        'SELECT pg_size_pretty(pg_total_relation_size(\'orders\'))'
    )).scalar_one())
"

# 2. quanto custa UM round-trip daqui até o banco
docker exec pedeaqui-api python -c "
from time import perf_counter
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    db.execute(text('SELECT 1'))          # aquece a conexao
    inicio = perf_counter()
    for _ in range(200):
        db.execute(text('SELECT 1'))
    print(f'{(perf_counter()-inicio)/200*1000:.2f} ms por round-trip')
"
```

Multiplique um pelo outro e você tem a janela, com uma folga de 2x para o
índice, o commit e o `alembic upgrade` das revisões que vierem junto.

### 1.3 Se a etapa falhar no meio

**Etapa 1 (`0016`) falhando:** transação única, então **volta inteira** — sem
coluna, sem índice, sem meia-linha preenchida. O container morre, o
`restart: always` sobe de novo, o entrypoint tenta a migração de novo, morre de
novo: **loop de restart visível em `docker ps`**, com a API fora do ar. É o
comportamento desenhado (armadilha 5) e é preferível a uma API de pé contra o
schema errado. Nada foi perdido; leia a saída do alembic antes de qualquer
outra coisa.

O modo de falha que **não** volta sozinho: você derruba o processo no meio
(Ctrl-C, `docker stop`, OOM do container). A transação é abortada pelo
Postgres quando a conexão cai, então o schema volta igual — mas o
`pg_advisory_xact_lock` do `alembic/env.py` também é de transação e sai junto.
Não fica lock órfão.

**Etapa 2 (`0017`) falhando:** três lugares, três consequências diferentes:

1. falha **no backfill ou na contagem** (`RuntimeError: N pedido(s) sem
   tracking_token_hash`) — a transação volta, `orders.tracking_token`
   **continua lá**, nada foi perdido. É o desenho: falhar antes do DROP;
2. falha **no `SET NOT NULL`** — mesma coisa, volta inteira;
3. falha **depois do `DROP COLUMN`, antes do commit** — volta inteira também.
   Não existe "meio DROP".

O que **não** volta é o commit bem-sucedido. Depois dele, a coluna em claro não
existe em lugar nenhum e sha-256 não se desfaz.

**E há um risco na etapa 2 que o doc não destaca o suficiente:** tirar o
`ALEMBIC_TARGET` do `.env` não libera só a `0017`. Libera **da `0017` até o
head**, hoje `20260828_0043` — 27 revisões, **na mesma transação**, incluindo a
`20260820_0026` (cardápio por filial), que **copia dados** e cuja ordem interna
é sensível a banco com conteúdo. Se produção estiver mesmo em `0015`, a etapa 2
não é "apagar uma coluna": é o resto do repositório inteiro de uma vez. Nesse
cenário, faça a etapa 2 em duas partes, com `ALEMBIC_TARGET=20260812_0017`
primeiro e só depois sem variável nenhuma.

### 1.4 Como voltar de cada etapa

**Da etapa 1: dá para voltar, com uma ressalva que morde.**

```bash
docker exec pedeaqui-api alembic downgrade 20260812_0015
# e volte a imagem antiga (o código novo não escreve mais a coluna em claro)
```

A ressalva: o `downgrade` da `0016` só recoloca o `NOT NULL` em
`tracking_token` **se nenhuma linha estiver nula**. Todo pedido criado enquanto
o código novo esteve no ar tem `tracking_token` nulo — o model não escreve mais
essa coluna. Então:

- a coluna volta **nullable**, com um `RAISE NOTICE` explicando;
- e o código antigo, que busca por `tracking_token`, **não acha os pedidos
  criados na janela**. Esses clientes perdem o acompanhamento, e não há rota de
  reemissão. Quanto mais tempo a etapa 1 ficar no ar, maior esse buraco no
  rollback.

Tradução operacional: o rollback da etapa 1 é barato **nas primeiras horas** e
vai ficando caro. Não é uma saída que envelhece bem.

**Da etapa 2: NÃO HÁ VOLTA. Digo isso em voz alta.** O `downgrade` da `0017`
recria a coluna **vazia** — o schema volta, o acesso não. Os tokens em claro
deixaram de existir e sha-256 não se desfaz. O único caminho de volta é
**restaurar backup do banco**, e restaurar backup significa perder tudo o que
entrou desde o backup (pedidos, pagamentos, cashback). Na prática: **depois do
commit da etapa 2, você não volta.**

Por isso: **faça backup do banco imediatamente antes da etapa 2**, e confira
que o arquivo existe e tem tamanho plausível antes de rodar qualquer coisa.

### 1.5 Qual a janela segura

**Não tenho resposta segura para isto, e não vou inventar uma.** O horário de
funcionamento mora em `branch_business_hours`, no banco de produção, e eu não
consulto produção nesta sessão. Nada no repositório guarda o horário do Júnior:
grep por `junior`/`matriz`/`varjota` em `docs/` só devolve exemplo de
onboarding.

O comando que responde (só leitura), e ele já traz as duas filiais:

```bash
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    for linha in db.execute(text('''
        SELECT b.name, h.weekday, h.opens_at, h.closes_at, h.is_closed
          FROM branch_business_hours h
          JOIN branches b ON b.id = h.branch_id
          JOIN restaurants r ON r.id = b.restaurant_id
         WHERE r.slug = 'junior-da-picanha'
         ORDER BY b.name, h.weekday, h.opens_at
    ''')):
        print(linha)
"
```

**Cuidado ao ler:** `weekday` aqui é o do Python — **0 = segunda, 6 = domingo**
(armadilha 1). E faixa que vira a noite (18:00–02:00) pertence ao dia em que
**começa**: uma loja que fecha às 02:00 de domingo aparece como faixa do
sábado.

O que dá para dizer sem consultar: a janela tem que caber **depois do último
pedido do dia e antes da primeira abertura seguinte**, com folga de 2x a
estimativa da seção 1.2, e não pode ser madrugada de sexta ou sábado — que em
churrascaria é o pico, e é justamente quando a faixa vira a noite.

### 1.6 O passo a passo, comando por comando

Tudo daqui é o que **você** digita, no servidor. Nada disto foi executado.

```bash
# ── PASSO 0 — onde a produção está (só leitura) ──────────────────────────
docker exec pedeaqui-api python scripts/estado_da_producao.py
# Se a revisão do banco já for 20260828_0043 → PARE, não há deploy pendente.

# ── PASSO 0b — dimensionar a janela (só leitura) ─────────────────────────
#   os dois comandos da seção 1.2 (contagem de pedidos e ms por round-trip)

# ── PASSO 0c — BACKUP. Não pule. ─────────────────────────────────────────
#   pelo painel do Supabase, ou pg_dump da DATABASE_URL.
#   Confira o tamanho do arquivo antes de seguir.

# ── ETAPA 1 ──────────────────────────────────────────────────────────────
cd /caminho/do/projeto
echo "ALEMBIC_TARGET=20260812_0016" >> .env
grep ALEMBIC_TARGET .env                     # confira que ficou exatamente isso

GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
docker logs -f pedeaqui-api
#   esperado, nesta ordem:
#     [entrypoint] alembic upgrade 20260812_0016
#     [entrypoint] ATENCAO: ALEMBIC_TARGET=... — o banco NAO esta em head.
#     [entrypoint] iniciando: uvicorn ...
#   o ATENCAO é esperado NESTA etapa.

# ── CONFERÊNCIA (a parte que não se pula) ────────────────────────────────
# 1. todo pedido tem hash — tem que dizer "0 sem hash"
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    sem = db.execute(text('SELECT count(*) FROM orders WHERE tracking_token_hash IS NULL')).scalar_one()
    total = db.execute(text('SELECT count(*) FROM orders')).scalar_one()
    print(f'{sem} sem hash, de {total} pedidos')
"

# 2. um link ANTIGO de verdade (do WhatsApp da loja, de antes do deploy) abre:
#    GET https://api.pederapidex.com/restaurants/{slug}/orders/track/{token}
#    Tem que responder 200. Pedido criado DEPOIS do deploy não serve —
#    ele nasceu com hash e passaria mesmo com o backfill errado.
#    Se der 404: PARE. Não faça a etapa 2.

# 3. feche um pedido de teste e abra o link que ele devolveu.

# 4. deixe assar. UM DIA INTEIRO de operação normal, no mínimo.

# ── ETAPA 2 (ponto sem volta) ────────────────────────────────────────────
#   refaça a conferência 1 imediatamente antes. E o backup de novo.
sed -i '/^ALEMBIC_TARGET=/d' .env
grep ALEMBIC_TARGET .env                     # não pode sobrar nada
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d
docker logs -f pedeaqui-api
#   esperado: [entrypoint] alembic upgrade head   e NENHUM "ATENCAO".

# ── CONFERÊNCIA FINAL ────────────────────────────────────────────────────
docker exec pedeaqui-api python scripts/estado_da_producao.py
#   a coluna em claro tem que ter sumido; a revisão do banco tem que ser o
#   head do repositório (hoje 20260828_0043), não a 0017.
```

### 1.7 O que continua sem resposta segura

- **quantas linhas tem `orders` hoje** — não consultei produção; comando na
  seção 1.2;
- **quanto custa um round-trip até o Supabase** — idem;
- **o horário de fechamento das duas filiais** — idem, seção 1.5;
- **se o Supabase tem `statement_timeout` configurado** — se tiver, e for menor
  que a duração do backfill, a etapa 1 morre no meio (volta inteira, sem
  estrago, mas você fica sem saber por quê). Confira com
  `SHOW statement_timeout;` antes;
- **se existe backup automático e qual a idade dele** — a etapa 2 depende
  disso e eu não tenho como ver.

Plano incompleto é melhor que plano confiante e errado: os cinco acima são
buracos conhecidos, e cada um tem o comando que o fecha.

---

## 2. `print_agent` — a conta que destrava o cliente

**A conta NÃO foi criada.** Criar exige escrever no banco de produção, e o
comando é seu. Está tudo escrito abaixo, na ordem de digitar.

### 2.1 Uma conta por filial. Duas, portanto.

O Júnior tem Matriz e Varjota, e cada uma tem sua máquina de balcão. São
**duas contas**, e o motivo não é organização — é que uma conta só **não
funciona**:

- **`--branch-id` é obrigatório** para `--role print_agent`, e o script recusa
  sem ele. Não existe a conta "de todas as filiais": com `branch_id` nulo o
  `AdminScope` significa "todas", e `PrintAgentService._branch_of_agent`
  responde **400** com `SEM_FILIAL`.
- **A filial vem do TOKEN, nunca do corpo.** Um agente que pudesse escolher a
  filial se anunciaria como a loja do lado.
- **`print_agents` é uma linha por filial**, upsert pelo `branch_id`. Duas
  máquinas com a mesma conta escreveriam na mesma linha: o `last_seen_at` e o
  `agent_version` do painel passariam a ser "o da última máquina que bateu", e
  a tela de "agente online" viraria loteria.
- **`print_agent_printers` é substituída inteira a cada `report_printers`**,
  também por `branch_id`. Com conta compartilhada, a lista de impressoras da
  Varjota apagaria a da Matriz a cada 30 s, e o botão de teste do painel
  mandaria a via para a impressora da outra loja.
- **O stream é recortado pelo escopo.** Conta presa à Matriz recebe pedido da
  Matriz. Com uma conta só, **a Varjota nunca imprimiria os pedidos dela** — e
  imprimiria os da Matriz, o dia inteiro, sem erro nenhum no log.
- E o de sempre: a senha fica em texto puro no `config.ini` de cada balcão.
  Uma conta para as duas lojas faz um `config.ini` lido comprar as duas.

### 2.2 Passo 1 — descobrir o `branch_id` de cada filial (só leitura)

```bash
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    for linha in db.execute(text('''
        SELECT b.id, b.name, b.slug
          FROM branches b JOIN restaurants r ON r.id = b.restaurant_id
         WHERE r.slug = 'junior-da-picanha'
         ORDER BY b.name
    ''')):
        print(linha)
"
```

Anote os dois uuids: o da Matriz e o da Varjota.

### 2.3 Passo 2 — criar as duas contas

Uma por vez. **O `-it` é obrigatório**: a senha é pedida em prompt oculto, de
propósito — argumento de linha de comando pararia no histórico do shell e no
`ps` de quem estiver na máquina.

```bash
# MATRIZ
docker exec -it pedeaqui-api python scripts/create_admin_user.py \
  --restaurant-slug junior-da-picanha \
  --name "Impressora Matriz" \
  --email impressora.matriz@juniordapicanha.com.br \
  --role print_agent \
  --branch-id <uuid-da-matriz>

# VARJOTA
docker exec -it pedeaqui-api python scripts/create_admin_user.py \
  --restaurant-slug junior-da-picanha \
  --name "Impressora Varjota" \
  --email impressora.varjota@juniordapicanha.com.br \
  --role print_agent \
  --branch-id <uuid-da-varjota>
```

Ele pede `Senha:` e `Confirme a senha:`. **Senhas diferentes nas duas contas** —
senão vazar um balcão vaza os dois. O mínimo de caracteres é
`MIN_ADMIN_PASSWORD_LENGTH`; o script recusa e diz o número.

**O e-mail é só identificador de login.** Nenhuma mensagem é enviada para
`admin_users` em lugar nenhum do sistema — `EmailService` só fala com cliente.
O domínio não precisa existir. O que ele precisa é ser **único**: o script
recusa e-mail repetido.

### 2.4 O que o comando gera

Ele imprime, para cada conta:

```
Lojista criado.
  id:         <uuid do admin_user>
  restaurant: Júnior da Picanha (<uuid>)
  email:      impressora.matriz@juniordapicanha.com.br
  role:       print_agent
```

E grava uma linha em `admin_users` com `role='print_agent'`,
`branch_id=<a filial>`, `is_active=true` e `must_change_password=false` — o
script não marca troca obrigatória, e ainda bem: se marcasse, a máquina bateria
403 em tudo e nunca imprimiria.

**Ele NÃO gera token.** O agente faz o login sozinho, com e-mail e senha, e
refaz quando o token de 12 h vence. É por isso que a instalação com senha é a
recomendada: a com `token =` fixo para de imprimir em silêncio depois de 12 h.

### 2.5 Onde colar no computador do restaurante

Na **máquina daquela filial** — a conta da Matriz no balcão da Matriz, a da
Varjota no da Varjota. Trocar as duas imprime o pedido da loja errada, o dia
inteiro, sem nenhum sintoma.

1. Tecla **Windows + R**, digitar e dar Enter:

   ```
   %LOCALAPPDATA%\Rapidex Impressao
   ```

2. Dois cliques em `config.ini` (abre no Bloco de Notas).
3. Preencher as duas linhas de credencial — `printer` já está preenchido desde
   a instalação:

   ```ini
   [rapidex]
   api_url  = https://api.pederapidex.com
   email    = impressora.matriz@juniordapicanha.com.br
   password = <a senha que você digitou no prompt>
   printer  = EPSON TM-T20
   ```

   No formato completo — o que tem `[api]`, `[printers]`, `[printing]` — as
   mesmas duas linhas ficam na seção `[api]`, e `token =` continua comentado.

4. **Ctrl + S** e fechar.
5. Fechar e reabrir `%LOCALAPPDATA%\Rapidex Impressao\RapidexImpressao.exe`.

**Encoding (armadilha 29):** o Bloco de Notas salva em ANSI nas máquinas
antigas, que são justamente as de balcão. O programa lê os dois (UTF-8
primeiro, CP1252 depois), então salvar em ANSI é seguro — **mas não troque o
Bloco de Notas por um editor que salve em UTF-16**, que nenhum dos dois lê.

Confira no painel, em Configurações → Setores de impressão: o agente daquela
filial tem que aparecer **online** em até 30 s (é o intervalo do heartbeat; o
painel o dá por offline depois de 90 s sem sinal).

### 2.6 Confirmado: a conta não abre o painel

Não é opinião — está travado por teste. `tests/test_papeis_das_rotas.py` audita
**toda** rota `/admin` contra uma tabela, e
`test_o_agente_de_impressao_alcanca_exatamente_as_rotas_de_que_precisa` afirma
que o conjunto de rotas que aceitam `print_agent` é **exatamente**:

| Rota | Para quê |
|---|---|
| `POST /admin/orders/stream-ticket` | credencial de 30 s do stream |
| `GET /admin/orders/{id}/print-jobs` | as vias já formatadas |
| `POST /admin/print-agent/heartbeat` | sinal de vida |
| `POST /admin/print-agent/printers` | a lista de impressoras da máquina |

Mais quatro que não exigem papel nenhum, e por isso também são alcançadas:

| Rota | Por que está aberta |
|---|---|
| `POST /admin/auth/login` | porta de entrada de todos os papéis, inclusive o da máquina |
| `GET /admin/orders/stream` | confere o papel **dentro** da função (`ensure_role(PESSOAS_E_AGENTE)`), porque autentica por ticket na querystring |
| `GET /admin/auth/me` | identidade — não devolve nada que quem tem o `config.ini` já não saiba |
| `PATCH /admin/auth/password` | troca da própria senha |

**Todo o resto do painel responde 403** — cardápio, preços, clientes,
faturamento, comissão, cupons, cashback, usuários, configurações. Rota nova que
nasça sem decisão de papel deixa esse teste **vermelho**, então isso não
apodrece sozinho.

Uma frase honesta sobre o que "não loga no painel" significa: a conta
**autentica** (tem que autenticar, senão não imprime) e recebe um token de 12 h
válido. O que ela não faz é **abrir tela nenhuma**: toda rota que desenha o
painel a recusa com 403.

### 2.7 O que ainda falta restringir (NÃO mexi, como combinado)

1. **`PATCH /admin/auth/password` — a máquina pode trocar a própria senha.**
   O `api_client.py` do agente nunca chama essa rota; ele só faz login. Quem
   ler o `config.ini` do balcão pode trocar a senha e **parar a impressão da
   loja em silêncio** — e o dono só destrava com
   `create_admin_user.py --reset-password` no servidor. É negação de serviço,
   não vazamento, mas é barato de fechar: `print_agent` sai da rota, e a senha
   da máquina passa a ser girada só por quem tem o servidor. O custo é uma
   linha saindo de `SEM_EXIGENCIA_DE_PAPEL` e entrando em `PAPEL_ESPERADO`
   como `PESSOAS`.

2. **`GET /admin/auth/me` — alcançável e não usada.** Não vaza nada novo (id,
   restaurante, filial, e-mail, papel — tudo já está no `config.ini` ou é
   derivável dele). Fecha junto com a de cima se você quiser a lista mínima de
   verdade; sozinha não paga o commit.

3. **O que NÃO dá para restringir, e você precisa saber.** Com o `config.ini`
   na mão, um atacante tem o **fluxo de pedidos daquela filial em tempo real**
   e as **vias formatadas** de cada um — nome, telefone e endereço residencial
   de todo cliente que pedir naquela loja. Isso é o agente de impressão
   funcionando, não um furo: a comanda **é** esse dado. O que limita o estrago
   já foi feito — o papel próprio (com `attendant`, aquele arquivo comprava o
   cardápio inteiro, a base de clientes e o faturamento do restaurante) e a
   prisão à filial (a Varjota não alcança a Matriz). A defesa que sobra é
   física: a pasta `%LOCALAPPDATA%\Rapidex Impressao` não deve estar em máquina
   compartilhada com Wi-Fi de cliente nem em pasta sincronizada com nuvem.

**Nada disso foi alterado nesta sessão** — item 5 do briefing e a instrução
explícita de não mexer no painel.

---

## 3. Custo de IA por restaurante

**Implementado.** Desenho completo em [`docs/custo-de-ia.md`](../docs/custo-de-ia.md);
aqui fica o resumo e o que eu decidi por conta própria.

### 3.1 O que passou a ser medido

Uma linha por chamada em `ai_usage_events` (revisão `20260902_0044`), com
restaurante, filial, modelo, tokens de entrada (e a fatia que veio do cache),
tokens de saída e **o custo em dólar já calculado**.

| Superfície | Uma linha por | Gancho |
|---|---|---|
| `text` | turno do `/chat` | `ChatService._invoke_llm` |
| `voice` | **sessão** de voz | `VoiceSessionService.encerrar` |

A voz é por sessão e não por turno porque não existe turno visível daqui: o
áudio vai do navegador direto para a OpenAI. O número chega uma vez só, no
aviso de fim — **e esse aviso chega duas vezes** (vai com `keepalive`, o
navegador reenvia ao fechar a aba). Por isso a linha carrega
`voice_session_id` com UNIQUE parcial: a segunda chegada corrige a primeira em
vez de dobrar o custo da conversa.

### 3.2 Como eu leio

```bash
curl -s -H "X-Internal-Key: $PLATFORM_METRICS_KEY" \
  "https://api.pederapidex.com/internal/ai-usage?start_date=2026-08-01&end_date=2026-08-31" \
  | python -m json.tool
```

Sem parâmetro nenhum: últimos 30 dias, um bloco por restaurante. Aceita
`start_date`, `end_date` (o dia entra INTEIRO) e `restaurant_id`.

**Antes de ligar o servidor, ponha a chave no `.env`:**

```bash
echo "PLATFORM_METRICS_KEY=$(openssl rand -hex 32)" >> .env
docker compose up -d
```

Sem ela, só essa rota responde **503** — o resto da API sobe igual. Foi
deliberado: obrigatória, ela derrubaria o boot de todo mundo por causa de um
relatório que uma pessoa lê.

**Leia `calls_without_price` antes do total.** Ela conta chamadas cujo modelo
não estava na tabela de preços: somam token e não somam dólar. Número grande
ali é "falta preço", não "custou pouco".

### 3.3 As decisões que eu tomei sozinho, e o porquê

1. **A rota não é do painel.** É `/internal/ai-usage`, com chave própria e
   `include_in_schema=False`. Quanto o assistente custa **à plataforma** é a
   nossa margem; publicá-la no painel a poria na mesa de negociação da
   comissão, com o lojista vendo o número antes de sentar. É o raciocínio da
   armadilha 17 (`platform_commission_percent` fora de todo schema do painel),
   e a armadilha 16 fecha a outra metade: o painel consome o `/openapi.json`, e
   rota que não é dele não entra no contrato dele.

2. **Segredo novo, não a `INTERNAL_API_KEY`.** Aquela está depreciada e o
   `startup_checks` pede para removê-la do `.env`. Reaproveitá-la faria uma
   chave "que nenhuma rota usa" voltar a valer alguma coisa em silêncio — é o
   que a armadilha 32 registra. Comparação com `hmac.compare_digest`
   (armadilha 18).

3. **Dólar, não real.** O preço é publicado em dólar e a fatura vem em dólar.
   Converter exigiria uma cotação que não temos, e qual cotação (do dia? do
   fechamento do mês? do pagamento do cartão?) é decisão de contabilidade, não
   de schema.

4. **`Decimal` de 6 casas, não `float` de 2.** Um turno do `/chat` custa da
   ordem de US$ 0,0005 — com duas casas, **todo turno seria zero**. Isto não
   contradiz a armadilha 34 (que manda não converter schema isolado enquanto a
   decisão `float × Decimal` não é tomada com o front): esta resposta é nova,
   não tem consumidor e não entra no documento.

5. **Modelo sem preço grava custo `NULL`, nunca zero.** Zero é um número que
   soma, e um restaurante inteiro apareceria de graça no relatório que existe
   para dizer quanto ele custa. Os tokens ficam gravados, então dá para
   reprocessar quando a tabela de preços for atualizada. É o critério da
   armadilha 49: número desatualizado degrada a mensagem, não a correção.

6. **Medir não derruba o que está sendo medido.** As duas gravações engolem
   falha e seguem, com `custo_nao_gravado=true` no log. Um `/chat` que responde
   500 porque a contabilidade não gravou seria trocar a operação pela planilha.

### 3.4 Os preços

Conferidos em **02/09/2026** em `developers.openai.com/api/docs/pricing`, e
gravados em `src/ai/custo.py` (USD por milhão de tokens):
`gpt-5-mini` 0,25 / 0,025 / 2,00 · `gpt-5` 1,25 / 0,125 / 10,00 · `gpt-5-nano`
0,05 / 0,005 / 0,40 · `gpt-realtime-mini` texto 0,60 e áudio 10,00 na entrada,
2,40 e 20,00 na saída · `gpt-realtime` 4,00/32,00 e 16,00/64,00.

**A tabela é uma cópia e envelhece.** Quando um preço mudar, é ali que se
mexe — e as linhas antigas continuam com o valor do dia em que foram gravadas,
de propósito: relatório que muda sozinho de valor não é confiável.

### 3.5 O que ficou de fora, com o número que justifica

- **Embedding não é medido.** `text-embedding-3-small` custa US$ 0,02 por
  milhão de tokens, e uma pergunta de cliente tem ~20 tokens: **um milhão de
  buscas custa US$ 0,40**, contra ~US$ 0,0005 por turno do `/chat`.
  Instrumentar exigiria contar token com `tiktoken` no caminho quente da
  busca, para medir um arredondamento. Se o modelo de embedding mudar de faixa
  de preço, a conta muda.
- **A transcrição da voz** não é ligada pelo backend (armadilha 43); quem liga
  é a bancada, e ela é cobrada à parte por minuto.
- **Painel, alerta e limite** — pedido explicitamente para ficar de fora.
  Anotado o que cada um exigiria decidir: a que número alertar, quem recebe, e
  o que acontece quando o teto bate (o assistente cala? responde sem busca? o
  lojista paga o excedente?).

### 3.6 Arquivos

Migração `20260902_0044` · `src/ai/custo.py` (preços e as duas contas) ·
`src/models/ai_usage_event_model.py` · `src/repositories/ai_usage_repository.py` ·
`src/services/ai_usage_service.py` · `src/schemas/ai_usage_schema.py` ·
`src/api/endpoints/internal_metrics.py` · `docs/custo-de-ia.md`.

Ganchos: `ChatLLMService` (o `usage` deixou de morrer no log),
`ChatService._invoke_llm`, `VoiceSessionService.encerrar`.

Testes: `tests/test_custo_de_ia.py` (14, rápidos: a aritmética, o cache como
subconjunto, modelo sem preço, e a porta da rota) e
`tests/test_custo_de_ia_db.py` (13, contra o Postgres: a idempotência da voz, os
dois CHECK, a agregação e a janela meio-aberta).

**Portão: 2785 testes verdes** (rápida + `db`), `ruff` limpo,
`export_openapi.py --check` em dia — a rota não entra no documento, então o
`openapi.json` não mudou.

---

## 4.1 Quebrar a tabela `branches` — **PARADO, com plano**

**Nada foi executado.** Toda quebra desta tabela termina em `DROP COLUMN`, que
é destrutivo, e o briefing manda parar e trazer o plano. Este é o plano — e ele
vem com uma recomendação que contraria o pedido, com o motivo escrito.

### 4.1.1 O que ela guarda hoje: 48 colunas, 6 grupos

| Grupo | Colunas | O que são |
|---|---|---|
| **Identidade** (9) | `id`, `restaurant_id`, `name`, `slug`, `display_name`, `is_main`, `is_active`, `created_at`, `updated_at` | quem é a loja |
| **Contato** (3) | `email`, `phone`, `whatsapp` | |
| **Endereço — o conjunto vivo** (7) | `address`, `neighborhood`, `city`, `state` (**NOT NULL**), `zipcode`, `latitude`, `longitude` | é o que o painel escreve |
| **Endereço — o conjunto morto** (6) | `address_street`, `address_number`, `address_neighborhood`, `address_city`, `address_state`, `address_zipcode` | **nada no código escreve nelas** |
| **Precificação de entrega** (10) | `delivery_base_fee`, `delivery_fee_per_km`, `delivery_min_fee`, `delivery_max_fee`, `delivery_max_distance_km`, `default_delivery_fee`, `estimated_delivery_time_min/max`, `free_delivery_enabled`, `free_delivery_min_order_value` | termo comercial; `NULL` = herda |
| **Estado do dia** (5) | `is_open`, `accepts_delivery`, `accepts_pickup`, `delivery_paused_until`, `delivery_pause_reason` | o que o balcão aperta; NOT NULL, não herda |
| **Termo comercial** (3) | `min_order_value`, `service_fee_enabled`, `service_fee_amount` | `NULL` = herda |
| **Impressão** (5) | `print_customer_copies_delivery/pickup`, `print_production_copies_delivery/pickup`, `receipt_footer_message` | descrevem o balcão |

### 4.1.2 O que deveria sair, e isso é o achado da rodada

**As seis colunas `address_*` são código morto no banco — e não são inertes.**

- **Ninguém escreve nelas.** `AdminBranchUpdate` (o `PATCH /admin/branches/{id}`)
  aceita `address`, `neighborhood`, `city`, `state`, `zipcode` e mais nada. Grep
  em `src/`, `scripts/` e `docs/` não acha um único `branch.address_street = `.
  Elas vêm do schema pré-Alembic, criado à mão no Supabase (estão no
  `schema_baseline.sql`, e nenhuma revisão as toca).
- **Mas alguém LÊ, e lê antes.** `RestaurantService._build_address` resolve
  assim:

  ```python
  street       = branch.address_street       or branch.address
  neighborhood = branch.address_neighborhood or branch.neighborhood
  city         = branch.address_city         or branch.city
  ...
  ```

  O conjunto morto **vence**. Ele é a resposta de `GET /restaurants/{slug}` —
  o endereço que o cliente vê no app.

**O defeito, em uma frase:** numa filial cujas `address_*` estejam preenchidas,
**o lojista corrige o endereço no painel e o app do cliente continua mostrando o
antigo.** Sem erro, sem log, sem tela onde conferir — o painel exibe o valor
novo (ele lê `branch.address`) e o cliente vê o velho. É a família da armadilha
35: a coluna crua responde "o que está sobrescrito", que quase nunca é a
pergunta.

**Eu não sei se isso está acontecendo hoje**, porque não consulto produção nesta
sessão. O comando que responde é só leitura:

```bash
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    for linha in db.execute(text('''
        SELECT name,
               address        AS painel_rua,
               address_street AS morta_rua,
               neighborhood   AS painel_bairro,
               address_neighborhood AS morta_bairro,
               (address_street IS NOT NULL
                 OR address_neighborhood IS NOT NULL
                 OR address_city IS NOT NULL) AS o_conjunto_morto_vence
          FROM branches ORDER BY name
    ''')):
        print(linha)
"
```

- Se **`o_conjunto_morto_vence` for falso em todas as filiais**: o defeito é
  latente, e o conserto é limpeza pura.
- Se for **verdadeiro em alguma**: aquela filial está mostrando ao cliente um
  endereço que o painel não consegue corrigir. **Isso é um chamado esperando
  acontecer**, e vale conferir de olho se o texto bate com o endereço real da
  loja.

#### As duas saídas, e a que eu recomendo

| | O que faz | Reversível? |
|---|---|---|
| **A. Só código** | `_build_address` passa a ler só o conjunto vivo; as seis colunas ficam no banco, órfãs | **sim** — é um revert |
| **B. Código + migração** | o mesmo, e uma revisão faz `DROP COLUMN` nas seis depois de conferir que ninguém lê | o DROP **não** volta com dado |

**Recomendo A agora e B depois** — exatamente a forma das revisões 0016/0017: a
etapa reversível primeiro, a janela de conferência no meio, o irreversível
depois. E A já fecha o defeito inteiro; B só devolve espaço e tira a armadilha
do caminho de quem ler o model.

**Mas A é decisão de produto, e por isso está parada aqui.** Se alguma filial
tiver as duas preenchidas com valores diferentes, A **muda o endereço que o
cliente vê**. Qual dos dois é o certo é uma pergunta sobre a loja, não sobre o
código, e não está no briefing. Rode o comando acima e me diga; com o resultado
na mão, A é meia hora.

### 4.1.3 Os outros dois cortes possíveis, e por que eu NÃO os recomendo

O pedido era "quebrar a tabela". Os dois candidatos naturais, para registro:

- **`branch_print_settings`** (`print_*_copies_*` + `receipt_footer_message`, 5
  colunas). Já tem rota própria (`GET/PATCH
  /admin/branches/{id}/print-settings`), e o resto da configuração de impressão
  já mora fora (`printing_sectors`).
- **`branch_delivery_settings`** (as 10 de precificação + as 3 de termo
  comercial, 13 colunas).

**Contra os dois, e o argumento é medido, não estético:**

1. **`resolve_branch_operation` lê 15 colunas de `branches` numa chamada só**, e
   ela roda no caminho mais quente do sistema: `/menu`, tela de escolha de
   filial, criação de pedido, `/chat` e `/voice`. Quebrar em duas satélites põe
   **dois JOINs** ali para arrumar o nome das coisas.
2. **48 colunas não são um problema do Postgres.** A linha é estreita (texto
   curto, numeric, boolean), são duas filiais hoje, e nenhuma consulta é lenta
   por causa disso. Não há sintoma a consertar — o único sintoma real desta
   tabela é o endereço duplicado do 4.1.2, e ele **não** se resolve quebrando.
3. **Toda quebra termina em `DROP COLUMN`** e exige a mesma dança de duas
   etapas, com o mesmo `ACCESS EXCLUSIVE` da seção 1.2 desta rodada. É custo de
   deploy sem defeito do outro lado.
4. **A tabela já tem um vocabulário que funciona**, e ele está documentado no
   model e em `docs/operacao-por-filial.md`: *estado do dia* (NOT NULL, não
   herda) contra *termo comercial* (`NULL` = herda). Os comentários agrupados
   já separam os blocos para quem lê. Uma satélite acrescentaria uma terceira
   coisa a lembrar sem tirar nenhuma.

**Se ainda assim você quiser a quebra**, o roteiro é o das duas etapas: revisão
que CRIA a satélite e COPIA (reversível) → deploy com o código lendo da satélite
e escrevendo nas duas → janela de conferência → revisão que faz o `DROP COLUMN`
(irreversível). Com uma diferença importante da 0016: aqui a tabela é pequena e
o backfill é um `INSERT ... SELECT`, não um `UPDATE` por linha, então a janela é
de segundos e não de minutos.

**Estado: parado.** Preciso de duas decisões suas — qual endereço é o certo (se
divergirem) e se a quebra em satélites vale mesmo o deploy.
