# Rodada 7 do backend — fonte da verdade

Branch: `rodada/backend-7`, saindo de `rodada/backend-6`. Nunca commitar na
`main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Rodadas anteriores: `rodada-back.md` a `rodada-back-6.md`.

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 1 | Rodar os três comandos do Redis | **não rodei** — são de produção, e não tenho a máquina |
| 2 | Histórico de chat: módulo + backend de Redis | **feito**, dois commits |
| 3 | O próximo item, escolhido pelo critério da regra sem ferramenta | **feito** — varredura de escrita, commit e camadas |

---

## 1. Os três comandos: por que não rodei

Os três são `docker exec pedeaqui-api ...` e `docker logs pedeaqui-api ...` —
**produção**, e a regra do rodapé do pedido ("Nada de produção") continua
valendo.

E não é só a regra: **esta máquina não tem os containers.** `docker ps -a` não
lista nenhum, e o `.env` local não define `REDIS_URL`. Mesmo rodando o Python
direto, `cliente_redis()` devolveria `None`.

Eles estão prontos na **§1 de `scratchpad/rodada-back-6.md`**. O primeiro é o
decisivo: se a única chave for `ai:menu_generation:<uuid>` com `ttl=-1` e valor
inteiro > 0, o caminho de escrita está provado e o assunto fecha como **falta
de movimento**.

**O que o assunto já fechou, e valeu para esta rodada:** `aof_enabled=0` e RDB
desligado. Sem persistência — e é isso que desbloqueou o item 2.

---

## 2. O histórico de chat

Dois commits, na ordem que eu tinha proposto na rodada 2 §5.1.

### Commit 1 — o módulo, sem mudar comportamento

`src/ai/services/chat_history.py`. Mesmo teto de 20 mensagens, mesmo TTL de uma
hora, mesma varredura. O que mudou é o `ChatService` deixar de saber **onde** a
conversa mora.

**A interface é de dois métodos, e isso foi escolha:**

```python
ler(session_id, agora)
gravar(session_id, pergunta, resposta, agora)
```

A limpeza das sessões vencidas **não** é um terceiro método: ela acontece dentro
do `ler`, com o mesmo `agora`. Como terceiro método, obrigaria o backend de
Redis a ter uma **função vazia** — e função vazia numa interface é o convite
para alguém "consertar" chamando-a de outro lugar.

`agora` entra de fora pela armadilha 51: duas leituras do relógio no mesmo turno
podem cair em lados diferentes da virada, e a sessão seria limpa e regravada no
mesmo pedido.

**Dois testes trocaram de forma, e a troca é o ponto.** Eles liam
`_SESSION_HISTORY["s1"]["last_interaction"]` — detalhe do backend de memória,
que o de Redis **não tem**. Passaram a afirmar a consequência: conversar de novo
mantém a sessão viva.

### Commit 2 — o backend de Redis

**A chave vai hasheada:** `chat:hist:v1:{sha256(session_id)[:32]}`.

O `session_id` vem do **cliente** e `POST /chat` não tem autenticação nenhuma —
nada impede um front de usar o telefone da pessoa como identificador de sessão.
É a regra da armadilha 56, aplicada **antes** de ser quebrada desta vez.

**O digest é do `session_id` cru, sem normalizar** — ao contrário de
`ChatCache.message_digest`. Lá normalizar é certo (duas perguntas iguais com
caixa diferente devem colidir); aqui seria errado: `abc` e `ABC` são duas
sessões, e fazê-las colidir **misturaria a conversa de duas pessoas**.

**O valor é texto em claro, e não há como não ser** — o modelo precisa ler o que
a pessoa escreveu. É o único item do Redis sem digest possível. As três defesas:

1. **TTL no próprio Redis**, via `SETEX`, renovado a cada turno. Não é a
   varredura da memória, que só roda quando alguém conversa: sessão parada de
   madrugada expira sozinha;
2. **nenhuma persistência** — conferido hoje;
3. **a chave não liga à pessoa**, então não há o que a exclusão de conta precise
   alcançar.

**`SETEX` e não `set` + `expire`:** separados, um erro entre os dois deixaria a
conversa no Redis **para sempre** — e o `maxmemory-policy volatile-lru` do
compose só descarta chave com expiração, então ela seria imune ao despejo
também. Há teste que exige `setex` e proíbe `set`.

**Falha do Redis não derruba o turno.** Nenhum caminho levanta: leitura que
falha devolve histórico vazio (resposta pior, não 500), escrita que falha é um
turno fora do histórico.

**Não há nível de memória por baixo, e é escolha.** Ele traria de volta o
"depende de qual worker atendeu" que este backend existe para eliminar, num
caminho que só roda quando o Redis cai — raramente exercitado e fácil de estar
errado sem ninguém saber.

### O aviso do boot inverteu, e isso é o achado do item

`src/core/startup_checks.py` avisava quando `--workers > 1`, e o texto dizia:

> `REDIS_URL` **NÃO** resolve este caso, ao contrário do rate limit e do cache
> de entrega: o histórico do chat não tem caminho de Redis.

**Dizia certo, e passou a estar errado neste commit.** Agora resolve. O aviso:

- ganhou o segundo termo na condição (`workers > 1 and not REDIS_URL`);
- passou a dizer **"DEFINA REDIS_URL"**.

Mantê-lo incondicional seria pior que não tê-lo: apareceria em todo boot de uma
configuração **correta**, e aviso que sempre aparece é aviso que ninguém lê —
inclusive no dia em que ele estiver certo.

O teste que afirmava a frase antiga (`test_o_aviso_diz_que_redis_nao_resolve`)
virou dois: um exige `DEFINA REDIS_URL` **e proíbe** `NAO resolve` — as duas
frases nunca podem conviver, porque uma delas está sempre errada —, e outro
exige que o aviso **suma** com `REDIS_URL` definido.

### Os testes

`tests/test_historico_do_chat.py`, 19 testes, com o cliente de Redis dublado
por `SimpleNamespace` e classes à mão — **uso certo** dele (CLAUDE.md): cliente
de biblioteca externa é colaborador, não dado.

O grupo que mais vale: **`OsDoisBackendsConcordamTests`**, que roda a mesma
sequência nos dois e exige a mesma conversa. É o que o plano da rodada 2 pedia,
e o que impede os dois de divergirem em silêncio — o de memória é o que a suíte
exercita em todo lugar, e o de Redis é o que roda em produção.

E quatro do grupo da falha, incluindo o caminho composto: `gravar` **lê** antes
de escrever, e sem a leitura defensiva um valor estragado travaria a sessão no
lixo até o TTL.

### O que continua fora, e está escrito no módulo

- não deduplica conversa entre abas (o `session_id` é do cliente — é o desenho);
- não sobrevive a reinício do Redis, e não deve;
- não vale para a voz — o histórico dela é da sessão do Realtime, na OpenAI.

### Arquivos

`src/ai/services/chat_history.py` (novo) · `src/services/chat_service.py` ·
`src/core/startup_checks.py` · `tests/test_historico_do_chat.py` (novo) ·
`tests/test_chat_service.py` · `tests/test_startup_checks.py` ·
`tests/test_chat_saudacao.py` · `tests/test_chat_saudacao_enlatada.py` ·
`tests/test_git_sha_no_log_e_no_health.py`.

**Portão: 2892 testes verdes** (2273 rápidos + 619 `db`), ruff limpo,
`openapi.json` em dia.

### O que falta para isso valer em produção

**Nada de código.** `REDIS_URL` já existe no `.env` de produção (é o mesmo que
o rate limit e os dois caches usam), então o backend de Redis entra sozinho no
próximo deploy — e o `[AI historico] backend=redis` no boot é como se confere.

---

## 3. O item que eu escolhi: escrita, commit e camadas

### Por que este, e não outro

O critério era seu: **"o método que mais rendeu foi varredura sobre regra
escrita sem ferramenta. Se sobrar alguma regra nessa classe, ela vem antes de
qualquer coisa nova."** Então a primeira coisa foi *conferir*, e não lembrar,
quais regras escritas ainda não têm varredor. Sobraram três candidatas:

| Regra escrita | Tem ferramenta? |
|---|---|
| "log não leva dado pessoal" | **tem** — `tests/test_log_sem_dado_pessoal.py`, que proíbe por *origem* (`address`, `customer`, `payer`…) e não por nome de campo |
| "nada é apagado no cardápio" | não tem |
| **`endpoint -> service -> repository`, e quem commita é o service** | **não tinha** |

Escolhi a terceira, e o desempate foi o custo do defeito. "Nada é apagado no
cardápio" erra alto: some um produto e o lojista vê. A regra da transação erra
**calada** — escrita sem commit não levanta e não loga, e o sintoma chega dias
depois como "o pedido não registrou o cashback".

### O que o `grep` já respondia, e o que ele não responde

Antes de escrever qualquer coisa eu conferi as três metades fáceis, e **as três
já estavam de pé**: nenhum repositório chama `db.commit()`, nenhum levanta
`HTTPException`, nenhum endpoint importa repositório. O varredor confere as três
assim mesmo — custa nada, e o dia em que uma cair tem que ser vermelho e não
descoberta.

A metade que o grep **não** responde é a que vale, e são duas perguntas que
precisam de **ordem** e de **cadeia de chamadas**:

1. **escrita sem commit em lugar nenhum da cadeia** — a linha nunca chega ao
   banco;
2. **commit ENTRE duas escritas do mesmo método** — as duas deixam de ser
   atômicas; uma falha no meio grava a primeira e perde a segunda. No caminho do
   pedido isso é cupom consumido sem pedido.

### Como ele classifica, e a porta que isso fecha

Por **vocabulário de prefixo, explícito nos dois lados** (`create`, `mark`,
`delete`… escrevem; `get`, `list`, `count`… leem). O que não casa com nenhuma
das duas listas vira **"não classifiquei"** e sai como achado — **nunca como
leitura**.

Essa escolha é o coração do script. Deduzir leitura por exclusão faria um método
de repositório com nome novo virar leitura por acidente, e uma escrita sem commit
ficaria invisível para sempre — exatamente o buraco que ele existe para fechar.

### O resultado, e as três vezes que ele estava errado antes de chegar nele

**Zero achados.** Mas as três execuções anteriores diziam outra coisa, e as três
eram defeito **meu**, sempre na direção de acusar código correto:

1. **15 achados** — cobrava commit de **helper privado**. `_delete_addresses`
   escreve e não commita, e está certo: quem commita é o `anonymize` que o chama,
   uma vez, no fim. Cobrar commit dele seria pedir o **contrário** da regra, que
   manda as várias escritas caírem na mesma transação. Corrigido para auditar só
   **ponto de entrada** — quem não é chamado por ninguém.
2. **1 achado** — `CashbackService.expire_balance`. Quem commita é
   `scripts/expire_cashback.py`, no laço, uma vez por pessoa (de propósito: o
   `FOR UPDATE` da conferência só é liberado no fim da transação, e segurar o
   lock de quem não venceu travaria o checkout dessa pessoa). O índice só olhava
   `src/`. Passou a incluir `scripts/` e `src/ai`.
3. **ainda 1 achado** — `_chamados_por_alguem` varria só `_Indice.metodos`, e
   `_expirar` é **função de módulo**, não método. Faltava `.funcoes`.

É a quarta varredura seguida cujo primeiro número estava errado, e sempre para o
mesmo lado. É por isso que o teste de anti-vacuidade não é acessório.

### A anti-vacuidade, e a mutação que provou que ela vale

`tests/test_escrita_e_transacao.py`, 19 testes sobre uma **árvore plantada em
disco** (`auditar(raiz)` existe para isso, como o `diretorios` do
`escopo_das_rotas`). Em disco, e não com `ast.Module` injetado, porque ler
arquivo é parte do que se testa — índice, diretórios, casamento por prefixo.

Cada grupo tem os **dois lados**: a isca que ele tem que acusar, e o padrão
legítimo que ele **não pode** acusar. O segundo lado é o que impede o conserto
preguiçoso — um varredor que acusa tudo passa em toda isca e é inútil na mesma
medida. Os legítimos incluem os dois falsos positivos de cima, virados em teste:
o helper privado e o commit que mora num script de manutenção.

**19 verdes de primeira não provam nada**, então eu quebrei o varredor de nove
maneiras e conferi quem morre:

| Mutação | Vermelhos |
|---|---|
| tudo vira leitura | as 2 iscas + a de vocabulário |
| nada é repositório | as 2 iscas + a de vocabulário |
| commit sempre visto | as 2 iscas + a de vocabulário |
| grep dos repositórios cego | as 2 iscas de camada |
| grep dos endpoints cego | a isca de endpoint |
| **tudo vira escrita** | os legítimos: só-leitura, vocabulário, `src/` |
| **ninguém é chamado** | helper privado, script de manutenção, `src/` |
| **commit nunca visto** | commit em cadeia, commit no fim, `src/` |
| `_eventos` sem o `sorted` | o teste de ordem, e `src/` |

A última achou um teste **vacuoso meu**: o caso que eu tinha escrito para a
ordem era simétrico, e sobrevivia a qualquer embaralhamento. Troquei por um em
que `ast.walk` mente de verdade — escrita no topo, escrita **dentro de um
`if`**, commit no topo. A largura entrega os nós de profundidade 1 primeiro, e
o método legítimo é lido como escrita/commit/escrita. Sem o `sorted` por
`lineno`, esse teste fica vermelho; é assim que ele foi conferido.

### No portão: falha, e não aviso

Igual aos dublês e ao Redis, e pelo mesmo argumento da rodada 6. Aqui **nunca
houve dívida herdada** — as quatro metades da regra já estavam de pé quando o
varredor nasceu. Zero que cresce é **regressão**.

Quem cobra é `OEstadoDeHojeTests.test_o_repositorio_esta_limpo`, na suíte
rápida. O passo do CI é `if: failure()` e só imprime a saída legível, como os
outros três.

### O que ele NÃO faz, e está escrito no docstring

- **não enxerga `flush`.** Vários repositórios usam `db.flush()` para obter o id
  antes do commit do service, e isso é **correto** — há teste exigindo que
  `flush` não seja acusado. O script fala de COMMIT;
- **não segue chamada que não resolve.** Reusa o índice do `escopo_das_rotas`,
  que resolve `self.metodo`, `self.atributo.metodo` e `Classe(db).metodo`. O
  resto para — e a parada é reportada, nunca engolida.

### Arquivos

`scripts/escrita_e_transacao.py` (novo) · `tests/test_escrita_e_transacao.py`
(novo) · `.github/workflows/ci.yml`.

**Portão: 2912 testes verdes** (2293 rápidos + 619 `db`), ruff limpo,
`openapi.json` em dia, lockfile em dia. Nada de produção: o script só lê código,
não abre banco.
