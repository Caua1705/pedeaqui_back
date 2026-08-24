# Operação

Subir, migrar, ler log e diagnosticar. Para "como eu rodo isso na minha máquina",
o `README.md` da raiz é mais curto e basta.

---

## 1. Deploy

```bash
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
docker logs -f pedeaqui-api
```

**O `GIT_SHA=` na frente não é enfeite, e o `--build` também não.**

O `--build` é o que faz o deploy pegar o código novo: sem ele o compose reusa a
imagem que já existe, e um `up -d` "bem-sucedido" pode deixar em pé exatamente o
que estava antes.

O `GIT_SHA=` carimba na imagem o commit de que ela foi construída. Ele sai em
três lugares — na primeira linha do boot, em `[AI /chat] Nova requisicao` e em
`GET /health` —, e existe por um caso concreto: em 24/08/2026 três commits
ficaram sem `push`, produção seguiu rodando o código anterior, e uma bateria de
medição inteira foi feita contra o build errado. Descobrir isso custou meia hora
comparando frases de log com `git log -S`, porque nenhuma linha dizia a versão.

Esqueceu o `GIT_SHA=`? Não quebra nada, e **a API avisa**: o boot sai como
`WARNING` dizendo `git_sha=nao-carimbado` e repetindo o comando acima.

Confira que subiu o que você acha que subiu, antes de medir qualquer coisa:

```bash
curl -s https://api.pederapidex.com/health
# {"status":"ok","app":"PedeAqui API","git_sha":"874a5b7"}
git rev-parse --short HEAD   # tem que bater
```

O compose **não expõe porta**: o Traefik roteia pela rede externa `n8n_default`,
pelas labels em `docker-compose.yml`. O container roda como usuário sem
privilégios (uid 10001) e com `/app` somente-leitura.

São **dois** serviços: `pedeaqui-api` e `redis`.

### Redis

Guarda os contadores de rate limit e o cache de estimativa de entrega. Sem ele
os dois vivem no processo, e aí o limite efetivo vira **N × o configurado** (um
balde por worker) e o cache do Google é perdido a cada deploy, em chamadas
pagas.

Não expõe porta e não passa pelo Traefik: quem fala com ele é a API, pelo nome
de serviço, dentro da rede.

⚠️ **O compose exige `REDIS_PASSWORD` no `.env` e se recusa a subir sem ela** —
inclusive a API, porque é o mesmo `docker compose up`. Ponha a variável **antes**
de puxar a versão que trouxe o serviço.

A senha existe mesmo sem porta publicada: `n8n_default` é uma rede
**compartilhada** (o n8n mora nela), e Redis sem senha aceita comando de
qualquer container que entre ali, `FLUSHALL` incluído.

Duas variáveis, um segredo só, e elas **têm** que bater:

| Variável | Quem lê |
|---|---|
| `REDIS_PASSWORD` | o `docker-compose.yml`, para subir o container |
| `REDIS_URL` | a API (`redis://:SENHA@redis:6379/0`) |

Errar uma das duas não derruba nada: o rate limiter tem `swallow_errors=True` e
o cache de entrega cai para memória. A API sobe, responde e **fica exatamente
como estava antes do Redis**, com `NOAUTH Authentication required` no log do
Redis e um `redis_write_failed=true` no da API. Confira depois de subir:

```bash
docker exec pedeaqui-redis redis-cli -a "$REDIS_PASSWORD" ping        # PONG
docker exec pedeaqui-redis redis-cli -a "$REDIS_PASSWORD" dbsize      # > 0 com tráfego
```

Sem persistência de propósito (`--save "" --appendonly no`): os dois usos são
cache e janela curta, e um dump em disco só traria contador velho de volta
depois de um restart. `--maxmemory 256mb` com `volatile-lru` — todas as chaves
que a API grava têm TTL.

### O entrypoint migra antes de servir

**Isto mudou e é a coisa mais importante deste documento.**

`docker-entrypoint.sh` roda `alembic upgrade head` e **só então** passa o `CMD`
adiante. Não há `|| true`: se a migração falhar, o container morre e o
`restart: always` tenta de novo.

```sh
set -e
alembic upgrade "${ALEMBIC_TARGET:-head}"
exec "$@"        # uvicorn
```

`ALEMBIC_TARGET` é a exceção, e só serve à migração em duas etapas (§2). Sem
ela — que é o caso de todo deploy normal — o alvo é `head`.

Duas consequências que você precisa ter na cabeça:

1. **Migração ruim vira loop de restart.** Um banco fora do ar ou uma revisão que
   falha deixa o container reiniciando em ciclo, visível no `docker ps`. Isso é
   melhor que uma API de pé respondendo contra o schema errado — mas quando o
   container "não sobe", **olhe a saída do alembic antes de qualquer outra
   coisa**.
2. **`CREATE INDEX` em tabela grande derruba a operação.** Sem `CONCURRENTLY`, o
   comando trava escrita na tabela enquanto roda, e agora essa espera acontece com
   a API fora do ar — nenhum pedido novo é gravado. Aplicar uma revisão dessas em
   horário de pico é derrubar a loja. Rode fora do movimento, ou faça a troca à
   mão com `CREATE INDEX CONCURRENTLY` (que não roda dentro de transação) e depois
   `alembic stamp <revisão>`.

O entrypoint fica como `ENTRYPOINT`, e não embutido no `CMD`, para que
sobrescrever o comando (`docker compose run api bash`) continue migrando primeiro.

### Quebra de linha

`.gitattributes` força LF em `*.sh`, `Dockerfile` e `docker-compose.yml`. O
repositório é editado no Windows com `core.autocrlf=true`, e um shebang terminado
em `\r` vira `exec /app/docker-entrypoint.sh: no such file or directory` dentro do
container.

---

## 2. Migrações

Fonte da verdade: `alembic/versions/` (38 revisões hoje). A pasta `migrations/` é
**arquivo histórico congelado** dos 12 `.sql` aplicados à mão — não rode nada de
lá.

### Banco que já tem o schema mas nunca passou pelo Alembic

Uma vez só, **antes** do primeiro `up`. Não executa DDL:

```powershell
alembic stamp 20260726_0001
alembic current    # deve mostrar 20260726_0001
```

Sem isso, o `upgrade` tenta aplicar da 0002 em diante sobre tabelas que já
existem.

### Dia a dia

```powershell
alembic upgrade head
alembic revision -m "descricao"
alembic revision --autogenerate -m "descricao"
alembic downgrade -1
alembic history --verbose
```

### Migração em duas etapas: `ALEMBIC_TARGET`

O entrypoint vai até `head`, sempre — e é o certo em 99% dos deploys. A exceção
é a revisão **irreversível**, que precisa de uma janela de conferência com a API
nova rodando contra o schema da revisão anterior. Duas revisões na mesma imagem
não dão essa janela: `upgrade head` aplica as duas no mesmo segundo.

`ALEMBIC_TARGET=<revisão>` no `.env` faz o entrypoint parar naquela revisão. É
**estado temporário**: enquanto a variável existir, nenhuma revisão posterior é
aplicada, o deploy passa verde e a API sobe contra um schema velho — que é
exatamente o que o entrypoint existe para impedir. Por isso ele grita no log
enquanto ela estiver lá.

Roteiro de uso real, com o que conferir entre as etapas:
[`deploy-hash-do-tracking-token.md`](deploy-hash-do-tracking-token.md).

### `--autogenerate` quer apagar metade do banco

O schema de produção nasceu antes do Alembic e tem objetos que o ORM não mapeia:
sequences (incluindo a de `order_number`), defaults e índices criados à mão. O
`--autogenerate` compara ORM × banco e propõe `DROP` em tudo que não achar no
ORM.

**Sempre leia o arquivo gerado antes de aplicar, e apague as linhas de drop.**
O aviso está em `alembic/env.py`.

### Índices: `if_not_exists` sim ou não

- Índice sobre tabela **do baseline** (que já existe em produção, possivelmente
  criado à mão): use `if_not_exists=True` no create e `if_exists=True` no drop.
- Índice sobre tabela **criada na própria revisão**: fica estrito. A tabela nasce
  vazia, não há colisão possível, e a permissividade só esconderia erro.

`if_not_exists` casa por **NOME, não por definição**. Dois índices com nomes
diferentes sobre a mesma coluna são, para o Postgres, dois índices — mantidos os
dois em toda escrita. `scripts/audit_indexes.py` procura as duas coisas.

Faça backup antes. É Supabase/Postgres; DDL ruim custa caro.

---

## 3. Ver logs

Todo log da aplicação vai para o logger `uvicorn.error`.

```bash
docker logs -f pedeaqui-api
docker logs pedeaqui-api --since 30m | grep "\[Delivery estimate debug\]"
docker logs pedeaqui-api --since 1h  | grep "\[Idempotency\]"
docker logs pedeaqui-api             | grep "\[Pagamento\]"
docker logs pedeaqui-api             | grep "\[Startup\]"
```

| Prefixo | Investiga |
|---|---|
| `[Startup]` | config faltando ou arriscada no boot |
| `[Delivery estimate debug]` | taxa/prazo errados, passo a passo |
| `[Delivery estimate]` | estimativa reaproveitada ou não, e por quê |
| `[Delivery Google]` | latência e status HTTP do Google |
| `[Idempotency]` | reenvio devolvendo resposta gravada |
| `[AdminAuth]` | quem logou no painel e de qual restaurante |
| `[AdminStream]` | `Last-Event-ID` inválido no stream do painel |
| `[Impressao]` | item que caiu na via "SEM SETOR" — sempre configuração errada |
| `[AI /chat]` | chat: tempo por etapa e cache |
| `[AI reindex]` | índice do Rapi: quantos produtos atrasados e há quanto tempo (container `reindex`) |
| `[Contract validation]` | 422 em rota crítica |
| `[Pagamento][mercadopago]` | latência/status de cada chamada e, no erro, código e causa |
| `[Pagamento]` | cobrança que não saiu, webhook aplicado ou ignorado |
| `[Body limit]` | requisição rejeitada com 413 |

**Não há dado pessoal nesses logs, por escolha.** A mensagem do chat vira digest
sha256, a chave de idempotência também, e o e-mail do pagador sai mascarado como
`[email]` nas linhas do Mercado Pago. Números **não** são mascarados de propósito
— levariam junto o id do pagamento e o código do erro, que são exatamente o que
se lê. Não passe a logar conteúdo cru para depurar.

### Greps que valem ter no radar

```bash
# dinheiro de cliente parado: pedido pago cancelado sem estorno
grep "pedido pago foi .* sem estorno automatico"

# pedidos duplicados: cliente sem Idempotency-Key
grep "requisicao sem Idempotency-Key"

# conta do Google subindo
grep "estimativa nao reaproveitada motivo="

# o prompt escorregando: o modelo cita produto e não seleciona, e o resgate
# no código está segurando a resposta. Frequência alta = hora de mexer no
# prompt, não no código.
grep "citou .* produto(s) no texto e nao selecionou nenhum"

# preço do texto diferente do preço do cartão. Deve ser raríssimo (exige o
# lojista salvar um preço no segundo exato de uma pergunta). Se aparecer com
# frequência, o preço está vindo de algum lugar que não é a linha viva.
grep "preco divergente entre o texto e o cartao"

# o Rapi falando de um cardápio velho: a varredura parou ou está atrás.
# `mais_antigo_s` é a idade da mudança mais antiga ainda não indexada — passar
# de uns poucos minutos significa OpenAI fora, ou o container `reindex` parado.
docker logs pedeaqui-reindex --tail 50 | grep "mais_antigo_s"

# comanda saindo errada
grep "item sem setor utilizavel"

# lojista relatou um erro pelo painel. A linha leva id, loja e tela, e NÃO
# leva o texto — o relato pode ter dado pessoal dentro, e log aqui não
# carrega dado pessoal. Para ler o relato: `scripts/error_reports.py`.
grep "\[relato\] erro relatado pelo lojista"
```

---

## 4. Manutenção

```bash
docker exec pedeaqui-api python scripts/cleanup_idempotency_keys.py --dry-run
docker exec pedeaqui-api python scripts/cleanup_idempotency_keys.py
docker exec pedeaqui-api python scripts/reindex_ai.py
docker exec pedeaqui-api python scripts/audit_indexes.py
docker exec pedeaqui-api python scripts/check_restaurant.py junior-da-picanha
docker exec pedeaqui-api python scripts/error_reports.py
```

- **`cleanup_idempotency_keys.py`** apaga chaves de idempotência **e** estimativas
  de entrega vencidas (e mais quatro tabelas — códigos, feedback do Rapi,
  comentário de avaliação e relato de erro, estas por retenção de dado
  pessoal, não por espaço em disco). Seguro: passado o TTL nenhuma das duas protege ou vale mais
  nada. Bota no cron.
- **`reindex_ai.py`** regenera os embeddings dos produtos. **O dia a dia não
  depende mais dele:** o container `reindex` faz isso sozinho a cada minuto. Rode
  à mão só quando quiser o trabalho todo agora — restaurante novo com o cardápio
  recém-importado, ou troca de `EMBEDDING_MODEL`, que invalida todo vetor já
  gravado.
- **`audit_indexes.py`** procura colisão de nome e duplicata de definição entre
  índices.
- **`error_reports.py`** lista o que os lojistas mandaram pelo botão de
  reportar erro (`POST /admin/error-reports`), do mais novo para o mais velho.
  Só leitura — quem apaga relato é a retenção de 90 dias, no
  `cleanup_idempotency_keys.py`. **A saída tem dado pessoal:** credencial já
  foi mascarada antes de o registro existir, mas o texto livre pode ter nome,
  telefone e endereço de cliente. Não cole em canal compartilhado.
- **`check_restaurant.py <slug>`** responde "este restaurante está pronto?" —
  confere os cinco passos do onboarding que quebram em silêncio e sai com 1 se
  algum estiver em ERRO. Só leitura. Rode antes de considerar um restaurante no
  ar, e de novo depois de qualquer mexida em horário, cardápio ou pagamento:
  [`onboarding-de-restaurante.md`](onboarding-de-restaurante.md).
- **`bench_busca_vetorial.py`** mede a busca do Rapi com e sem índice ANN. Não
  roda em produção — monta um banco descartável no Postgres de teste. É o que
  sustenta a decisão de **não** ter índice `ivfflat`/`hnsw` hoje, e o que diz
  quando ela deixa de valer: [`busca-vetorial-e-indice-ann.md`](busca-vetorial-e-indice-ann.md).

---

## 5. Dependências

**O que a produção instala é `requirements.lock.txt`**, não `requirements.txt`.
São ~70 linhas `nome==versão`, transitivas incluídas, congeladas de um
`pip freeze` do container que está no ar. `requirements.txt` continua sendo a
lista do que o projeto **usa**, com o motivo de cada escolha ao lado —
`Dockerfile` e CI não o instalam mais.

Por que existe: até 23/08/2026 só três pacotes estavam pinados, e o pip
resolvia os outros quarenta **a cada `--build`**. Foi assim que a VPS chegou ao
`starlette 1.6.0` sem ninguém escolher, e o estrago não foi um 500 — foi a
auditoria de papéis das rotas `/admin` desligar em produção, calada, por
semanas. Qualquer um dos quarenta podia fazer o mesmo.

### Atualizar uma dependência

A ordem é sempre **decidir aqui, congelar de lá**. Nunca o contrário: congelar
antes de subir grava uma versão que ninguém serviu.

**1.** Declare no `requirements.txt`: versão nova, pacote novo. Se for pino,
escreva ao lado **por que** aquela versão importa — é a única coisa que aquele
arquivo tem e o lock não.

**2.** Resolva o lock novo num container limpo. É aqui que a versão é
escolhida, e é fora da produção de propósito: um `pip install` na máquina de
desenvolvimento traria o Python e as wheels dela.

```bash
docker run --rm -v "$PWD":/app -w /app python:3.12-slim   sh -c "pip install -q -r requirements.txt && pip freeze" > requirements.lock.txt
```

**3.** Recoloque o marcador de plataforma do `uvloop` — o freeze não o escreve,
e o comentário na linha dele explica. Depois:

```bash
python scripts/check_lockfile.py
```

**4.** Suíte inteira na máquina, com o lock instalado (`pip install -r
requirements.lock.txt -r requirements-dev.txt`). Commit dos **dois** arquivos
juntos: eles nunca viajam separados.

**5.** Deploy normal (`docker compose up -d --build`). Com o serviço de pé,
congele o que de fato subiu e compare com o arquivo commitado:

```bash
docker exec pedeaqui-api pip freeze
```

O passo 5 não é cerimônia. Ele é o que transforma o lock em **registro do que
está no ar** em vez de intenção: se as listas divergirem, quem manda é a
produção, e o arquivo é corrigido para ela — foi errando essa direção que a
primeira tentativa de pino quase rebaixou três frameworks para os da máquina de
desenvolvimento.

**O CI cobra a metade que é fácil de esquecer.** `scripts/check_lockfile.py`
recusa dependência declarada em `requirements.txt` que não esteja no lock — o
sintoma sem ele seria `ModuleNotFoundError` no boot do container, em produção,
depois do deploy.

### Descobrir que existe versão nova

Nada avisa sozinho: um lock que ninguém revisa é uma foto que envelhece. O
comando é este, no container que está no ar:

```bash
docker exec pedeaqui-api pip list --outdated
```

A saída é `pacote  versão-atual  versão-nova  tipo`. Ela **não** é uma lista de
tarefas — a maioria das linhas não vale um deploy. O que vale olhar:

- **`cryptography`, `requests`, `urllib3`, `starlette`, `fastapi`** e qualquer
  coisa que fale rede ou cripto: aqui versão nova pode ser correção de
  segurança, e vale ler o changelog na hora.
- **major nova** (`2.x` → `3.x`) de qualquer coisa: não suba junto com outra
  mudança. Um commit só dela, com a suíte inteira.
- **o resto**: junte e suba de uma vez, de tempos em tempos, com a suíte
  fechando o commit.

Quem quiser o aviso automático liga o **Dependabot** (`.github/dependabot.yml`,
ecossistema `pip`): ele abre PR por atualização e o CI já roda em PR. Ficou de
fora hoje de propósito — PR automático em repositório de uma pessoa só vira
fila que ninguém lê, e a fila silenciosa é pior que o comando acima rodado de
vez em quando.

---

## 6. Quando não sobe

Em ordem.

**1. Leia os logs. A causa quase sempre está escrita.**

```bash
docker logs pedeaqui-api --tail 50
```

**2. Falha do `alembic upgrade head`, antes de qualquer log da aplicação.**
O entrypoint migra antes de servir (§1). Uma revisão que falha ou um banco
inacessível derruba o container em loop. A linha `[entrypoint] alembic upgrade head`
é a última coisa que aparece antes do erro.

**3. `StartupConfigurationError`.**
É a validação de boot recusando de propósito. Quatro coisas derrubam:

- `GOOGLE_MAPS_ROUTES_API_KEY` vazia com `DELIVERY_ESTIMATE_PROVIDER=google_routes`
  (com Routes API **e** Geocoding API habilitadas no projeto GCP);
- `ADMIN_AUTH_SECRET` com o **mesmo valor** de `CUSTOMER_AUTH_SECRET`;
- `PAYMENT_PROVIDER=mercadopago` sem `PAYMENT_CREDENTIALS_ENCRYPTION_KEY`;
- `MERCADOPAGO_ENVIRONMENT` diferente de `test` ou `production`.

**4. `ValidationError` do pydantic-settings.**
Falta variável obrigatória no `.env`. As sem default são: `DATABASE_URL`,
`SUPABASE_URL`, `CUSTOMER_AUTH_SECRET`, `ADMIN_AUTH_SECRET`,
`EMAIL_CODE_SECRET`, `PASSWORD_RESET_SECRET`, `OPENAI_API_KEY`.

Trocar `ADMIN_AUTH_SECRET` num deploy **invalida todo token de lojista em
circulação**: quem estiver no painel volta para a tela de login, e o agente de
impressão configurado com `token =` fixo para de imprimir até receber um token
novo. O agente com `email`/`password` refaz o login sozinho.

**5. Erro de conexão com o banco.**
`DATABASE_URL` precisa do driver: `postgresql+psycopg://...`. Sem o `+psycopg` o
SQLAlchemy tenta `psycopg2` e o comportamento muda. Confira também se o IP do
servidor está liberado no Supabase.

**6. Sobe, mas o Traefik dá 404 / gateway error.**
Não é o app. Confira se o container está na rede externa `n8n_default` e se o DNS
de `api.pederapidex.com` aponta para o servidor. Teste por dentro:
`docker exec pedeaqui-api curl -s localhost:8000/health`.

**7. Sobe e responde, mas `/docs` dá 404.**
Comportamento correto com `APP_ENV=production` — o schema completo entrega a
superfície de ataque inteira a quem só achou o domínio. Para ligar sem sair de
produção: `ENABLE_API_DOCS=true`.

**8. Warnings no boot que NÃO derrubam** — cada um tem consequência real:

| Warning | Consequência |
|---|---|
| `REDIS_URL` vazia | cache de estimativa e rate limit só em memória; com N workers o limite vira N × o configurado (o serviço `redis` do compose existe justamente para isto — ver §1) |
| `SUPABASE_SERVICE_ROLE_KEY` vazia | upload de imagem do painel responde 503; leitura das imagens segue |
| `PAYMENT_WEBHOOK_SECRET` vazia com provider sandbox | webhook responde 503 e nenhum pedido online sai de "aguardando pagamento" |
| `PAYMENT_PROVIDER=sandbox` em produção | cobranças criadas localmente, nenhum dinheiro movimentado de verdade |
| `INTERNAL_API_KEY` presente | depreciada, nenhuma rota usa; pode sair do `.env` |
| `RATE_LIMIT_CLIENT_IP_HEADER` vazio | atrás do proxy, todos os clientes no mesmo balde |

---

## 7. Testes

```powershell
py -m pytest -q                        # 614 testes da API, ~11s
py -m pytest tests/test_idempotency.py -v
cd print-agent && py -m pytest -q      # 65 testes do agente
```

`pytest.ini` da raiz tem `testpaths = tests` de propósito: sem isso um `pytest`
sem argumentos varreria o repositório inteiro e coletaria os testes do
`print-agent/` junto — dois ciclos de vida diferentes, a API sobe em container a
cada merge e o agente é instalado à mão numa máquina Windows.

Os testes **não precisam de banco** (usam fakes em memória), mas precisam de um
`.env` válido, porque `src.core.config` é importado na cadeia e `pydantic-settings`
levanta `ValidationError` se faltar variável obrigatória.

O que os testes **não** cobrem, e você verifica à mão: o `ON CONFLICT DO NOTHING`
real, o bloqueio entre transações concorrentes, o round-trip do JSONB, a limpeza
por TTL, e qualquer chamada de verdade ao Mercado Pago ou ao Google.
