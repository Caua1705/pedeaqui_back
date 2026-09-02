# Rodada 4 do backend — fonte da verdade

Branch: `rodada/backend-4`, saindo de `rodada/backend-3`. Nunca commitar na
`main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Rodadas anteriores: `rodada-back.md`, `rodada-back-2.md`, `rodada-back-3.md`.

**Nada de produção.** O comando das filiais não roda — quem roda é o dono. O
`6fcaccc` continua esperando.

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 1 | O 404 do cupom vencido: voltar atrás preservando a defesa | **feito** |
| 2 | Redis: varrer a classe, consertar a chave, e o plano da exclusão | **feito** — 2 achados, 2 consertados, zero agora |
| 3 | A quebra de contrato para o painel e o app | pendente |

---

## 1. "Não existe" × "existe e venceu" voltaram a ser respostas diferentes

Você tem razão, e a razão é específica: **"Cupom não encontrado" para um código
que existe manda o cliente conferir se digitou errado e tentar de novo.** É uma
frase que ele não tem como resolver. "Cupom vencido" encerra o assunto.

### O que mudou

`_find_coupon` passou a devolver **dois valores**: o cupom e se ele está dentro
da janela.

```python
coupon, dentro_da_janela = self._find_coupon(..., agora=agora)
```

**Duas consultas, e a segunda só no caminho raro.** A primeira usa
`filtro_de_janela` — é o cupom aplicável, e é o caminho comum. Só quando ela
volta vazia é que a segunda pergunta, **sem filtro**, se aquele código existe.
É a diferença entre as duas respostas.

A segunda **nunca pede `FOR UPDATE`**: travar linha que já se sabe que não vai
ser aplicada seguraria o cupom de outro pedido por nada. Há teste para isso.

### As mensagens, agora

| Situação | Resposta |
|---|---|
| código não existe naquele restaurante | **404** "Cupom não encontrado para este restaurante" |
| existe e venceu | **400** `expired` |
| existe e ainda não começou | **400** `not_started` |
| preview de qualquer um dos dois | **200**, `valid=false`, `ineligibility_reason` preenchido |

`not_started` e `expired` dizem coisas diferentes ao cliente — "volte depois" e
"acabou" —, e o 404 apagava as duas de uma vez.

### A defesa em profundidade ficou, e agora ela é CONFERÍVEL

Este é o ganho que a volta atrás não custou.

O `dentro_da_janela` sai do **SQL** (`filtro_de_janela`); o
`expired`/`not_started` sai do **Python** (`evaluate`). São as duas formas da
mesma regra, e `lock_and_validate_for_order` **cobra que elas concordem**:

```python
if not dentro_da_janela_pelo_sql:   # e evaluate disse que vale
    logger.error("[Cupom] as duas formas da janela divergiram | ...")
    raise HTTPException(409, "Cupom indisponível neste momento")
```

**409 e não 400**: não há nada que o cliente possa mudar no corpo dele para
consertar isso. É defeito nosso, e o log tem que dizer alto.

Antes, a "defesa em profundidade" era um segundo filtro que ninguém contestava.
Agora são **duas formas que se conferem** — e o defeito que
`src/services/coupon_window.py` inteiro existe para impedir é justamente elas
divergirem. Há teste que força o desacordo e prova o 409.

### Um dublê que mentia, achado no caminho

`FakeCouponRepository.get_by_code_and_restaurant`, em
`tests/test_janela_do_cupom.py`, **ignorava o `code`** e devolvia o cupom para
qualquer string. Com isso, "código que não existe" e "código que venceu"
chegavam ao mesmo lugar — apagando exatamente a distinção que os testes novos
medem. O SQL real compara `lower(code)`; o dublê passou a comparar também.

É a armadilha 52 outra vez, e num arquivo que eu escrevi ontem: o dublê
respondia o que o teste precisava, não o que a aplicação faz.

### Arquivos

`src/services/coupon_service.py` (`_find_coupon` devolve o par, `_buscar_cupom`
novo, a guarda em `lock_and_validate_for_order`, `logger` do módulo) ·
`tests/test_janela_do_cupom.py` · `tests/test_coupons.py`.

**Portão: 2842 testes verdes** (2226 rápidos + 616 `db`), ruff limpo, openapi
em dia. O `openapi.json` **não mudou** — `ineligibility_reason` já era
`str | None` e nenhum status novo entrou em rota (o 409 é uma exceção de
service, como os outros).

---

## 2. Redis: a varredura, o conserto, e o que falta para a exclusão alcançar

### A ferramenta veio antes: `scripts/dados_pessoais_em_chave.py`

Para cada módulo de `src/` que **fala com o Redis de verdade** (`import redis`,
`cliente_redis`, `self.redis`, `redis_client` — e não a palavra "redis" solta,
que aparece em comentário de meio repositório), acha toda montagem de string
**com namespace** — f-string, `%` ou `.format()` cujo pedaço literal contenha
`:` — e classifica cada expressão interpolada:

| Classe | Critério |
|---|---|
| **PESSOAL** | vocabulário de dado pessoal: coordenada, telefone, e-mail, nome, endereço, CPF, mensagem, nascimento |
| pseudônimo | termina em `_id`, ou é `id`/`uuid` — **conferido ANTES do vocabulário** |
| hasheado | passa por `hash`, `digest`, `sha`, `fingerprint` |
| desconhecido | o resto — lista, não acusa |

**Dois refinamentos que a primeira versão não tinha, e cada um tirou ruído
que teria feito o varredor ser ignorado:**

1. **A precedência do pseudônimo.** `payload.address_id` é identificador, e o
   "address" dele ganhava do `_id`. Varredura que grita por nada é varredura
   que se aprende a ignorar.
2. **O `:` obrigatório no literal.** É o que separa uma chave
   (`"cache:v1:{x}"`) de uma formatação de valor (`f"{lat:.4f}"`) — e sem ele
   o varredor acusava a **própria correção dele**, porque a coordenada continua
   sendo formatada, só que para dentro do sha-256. O separador é convenção
   deste repositório e está escrita em `ChatCache.embedding_key`.

### O que a varredura achou: DOIS, e o segundo eu não conhecia

**1. `delivery_estimate_service._cache_key`** — o que você já sabia.

```
delivery-estimate:v1:{slug}:{branch_id}:{lat:.4f}:{lon:.4f}:...
```

**2. `chat_cache.retrieval_key`** — a **mensagem do cliente**, normalizada e em
claro, na chave do cache de busca.

Esse segundo **vive só em memória** (`self._retrievals` é um `dict` do
processo), então não aparece em `KEYS` nem em dump. Ordem de grandeza menor.
**Mas é a mesma classe, a poucas linhas de virar Redis** — que foi exatamente o
que aconteceu com o cache de embedding, por um bom motivo, e teria levado a
pergunta do cliente junto sem ninguém notar.

Consertei os dois. Zero agora.

### E a metade que o varredor não faz: o VALOR

`SETEX chave valor` expõe o **valor** no `MONITOR` tanto quanto a chave, e um
dump guarda os dois. Auditoria manual das quatro escritas:

| Escrita | Valor | Veredito |
|---|---|---|
| `delivery-estimate` | `DeliveryEstimateResult` em JSON, **com `latitude` e `longitude`** | **era exposição** — corrigido |
| `emb:...` | o vetor de 1536 floats, binário | derivado, não reversível na prática, não identifica pessoa — **ok** |
| `MenuGeneration` | um inteiro | **ok** |
| rate limit (`slowapi`) | contador | ok — a exposição ali é a **chave** (IP), e é por desenho |

### Os consertos

**A chave da estimativa: `v1` → `v2`, tudo em digest.**

```
delivery-estimate:v2:{branch_id}:{sha256(material)[:32]}:{bucket}
```

- **o digest não muda o comportamento do cache** — é determinístico, a mesma
  coordenada cai na mesma entrada, o acerto continua igual;
- **`branch_id` fica legível de propósito**: é identificador interno, não diz
  nada sobre a pessoa, e sem ele não há como varrer as chaves de uma filial
  para depurar cache. Hashear tudo trocaria privacidade que já se tem por
  operação que se perde;
- **as quatro casas continuam sendo a granularidade** — elas entram no digest,
  não na chave. Arredondar mais grosso trocaria precisão de cache por uma
  privacidade que o digest já dá inteira;
- **o `v2` é obrigatório**: sem ele as chaves `v1` que estão no Redis agora
  continuariam sendo lidas, e quem voltasse a imagem antiga leria `v1`.
  Versionar deixa os dois conjuntos inalcançáveis um pelo outro;
- **de graça:** sha-256 não tem `:`, então nenhum pedaço pode mais empurrar o
  formato da chave.

**O valor: as coordenadas não são mais gravadas.** `estimate` resolve
`destination` **antes** de consultar o cache — é dele que a chave é derivada —,
então ele as recoloca na volta com `dataclasses.replace`.

**E recolocar é mais correto que devolver o guardado:** a chave agrupa por
quatro casas decimais, então um acerto pode vir de uma coordenada **vizinha**.
O pedido tem que gravar a do endereço que ele está usando, não a do vizinho que
consultou antes.

**A chave da busca: `normalize_message(message)` → `message_digest(message)`.**
Custo zero — é o mesmo digest que a chave de embedding já calcula no mesmo
turno.

### O que ficou travado

`tests/test_dado_pessoal_no_redis.py`, 13 testes rápidos:

- **nenhuma chave com dado pessoal** — e dois testes de anti-vacuidade: o
  varredor **acusa** uma chave plantada, e **não acusa** digest nem
  identificador (se acusasse, a regra deixaria de ser seguível);
- a coordenada não aparece na chave, **e o cache continua separando endereços
  diferentes e sendo determinístico** — o digest não podia ter custado a função
  do cache;
- a coordenada não é gravada no valor, **e o resto do resultado continua
  inteiro** (taxa, distância, prazo — o motivo de o cache existir);
- **valor no formato antigo devolve `None`, nunca levanta.** O `v2` torna as
  entradas velhas do Redis inalcançáveis, mas o cache de **memória** não tem
  versão, e um deploy no meio de uma requisição pode deixar as duas formas
  convivendo. O preço de errar aqui seria 500 no checkout por causa de cache;
- a mensagem do cliente não aparece na chave de busca.

No CI: passo `if: failure()` que imprime a saída legível. **Vermelho, não
aviso** — aqui o número certo é zero, e zero que cresce não é dívida herdada, é
regressão.

### O que a exclusão de conta precisa passar a fazer — escrito, não implementado

**Hoje ela não alcança nada do Redis**, e depois destes consertos **continua não
alcançando** — o que mudou é que sobrou muito menos a alcançar.

O que resta no Redis ligado a uma pessoa, depois dos consertos:

| O quê | Ligado à pessoa? | Vida |
|---|---|---|
| estimativa de entrega | só pelo **digest** da coordenada — irreversível | 600 s |
| embedding | pelo digest da pergunta — irreversível | 3600 s |
| busca (memória) | pelo digest da pergunta | 1200 s |
| rate limit | **pelo IP, em claro** | janela do limite |

**A pergunta que decide o desenho: dá para apagar por pessoa?**

**Não, e não por falta de código.** Nenhuma dessas chaves carrega
`customer_id` — de propósito, porque carregá-lo seria criar o vínculo que hoje
não existe. Para apagar por pessoa seria preciso **primeiro** ligar cada chave
a um cliente, e isso **aumenta** a superfície de LGPD para poder depois
diminuí-la. É a troca errada.

**O que eu faria, na ordem:**

1. **Nada de novo no código, e declarar por quê.** Depois destes consertos, o
   que sobra ligado a uma pessoa é digest irreversível com TTL de minutos. Isso
   é pseudonimização, e a LGPD trata dado pseudonimizado de forma diferente do
   dado direto. A exclusão de conta não precisa alcançar o que não identifica
   ninguém.
2. **O IP do rate limit é a exceção, e é caso à parte.** Ele é inerente ao
   mecanismo — sem identificar quem chama não há limite por cliente —, a janela
   é de minutos, e o conserto não é apagar: é **declarar**. Entra no
   `docs/lgpd-proposta.md` como base legal de legítimo interesse (segurança da
   aplicação), com o prazo.
3. **Se ainda assim se quiser alcance por pessoa** — e há um argumento honesto
   para querer, que é auditabilidade —, o desenho é **um índice reverso e não
   uma varredura**: `SADD lgpd:cliente:{customer_id} {chave}` a cada escrita, e
   a exclusão faz `SMEMBERS` + `DEL` + `DEL` do próprio conjunto. Uma escrita a
   mais por operação, e **o índice em si vira o vínculo que hoje não existe** —
   ou seja, o remédio cria a doença. Por isso é o item 3 e não o 1.
4. **O que NUNCA fazer:** `KEYS`/`SCAN` com padrão para achar as chaves de uma
   pessoa. Não funciona (as chaves não a identificam), e `KEYS` trava o Redis
   inteiro.

**A pergunta que continua sendo sua, e a única que muda essa conclusão:** se o
Redis de produção tiver `RDB`/`AOF` ligado, o TTL deixa de ser garantia e o
raciocínio do item 1 muda de figura — dado pseudonimizado com TTL de 10 minutos
é uma coisa; o mesmo dado num dump de backup é outra. Os três comandos estão na
§3 da rodada 3.

### Arquivos

`scripts/dados_pessoais_em_chave.py` (novo) ·
`src/services/delivery_estimate_service.py` (`_cache_key`, `DeliveryEstimateCache.set`
e `.get`, e o `replace` no acerto) · `src/ai/services/chat_cache.py`
(`retrieval_key`) · `tests/test_dado_pessoal_no_redis.py` (novo) ·
`.github/workflows/ci.yml`.
