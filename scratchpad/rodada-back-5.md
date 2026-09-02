# Rodada 5 do backend — fonte da verdade

Branch: `rodada/backend-5`, saindo de `rodada/backend-4`. Nunca commitar na
`main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Rodadas anteriores: `rodada-back.md` a `rodada-back-4.md`.

**Nada de produção.** O dono roda hoje os três comandos do Redis (§3 da rodada
3) e o das filiais. O `6fcaccc` continua esperando.

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 3 | A rota de cancelamento pelo cliente, para o front | **feito** — texto colável na §3 |
| 1 | A revisão de alinhamento das colunas, pronta e testada | **feito** — 15 colunas, testada com tabela cheia e com nulo antigo |
| 2 | O que eu faria a seguir, escolhido e justificado | **feito** — varrer o escopo de tenant |

---

## Antes de tudo: são 15 colunas, não 13

O pedido diz "as 13 colunas". **O número certo é 15**, e o 13 saiu de uma conta
minha errada na rodada 3:

- depois de `valid_until` sair, sobraram **15** (16 − 1). Eu escrevi "sobraram
  14";
- daí "1 de código + 13 para a revisão", que fechava com o 14 errado.

Conferido agora contra o Postgres de teste:
`divergencias_orm_schema.py` diz **41 divergências, 15 na primeira classe**, e
`alembic/preparadas/` já lista as 15 certas. A revisão preparada **nunca esteve
errada** — o erro foi só na prosa do scratchpad da rodada 3.

**E `ai_feedback.created_at` continua na lista, apesar do conserto de código.**
O `OR created_at IS NULL` do DELETE de retenção fechou o buraco de LGPD; ele não
tocou no model nem no schema, então a divergência segue lá e a coluna segue
precisando de `SET NOT NULL`. As duas coisas são independentes.

---

## 3. `POST /orders/track/{token}/cancel` — pronto para colar no front

A rota existe desde 25/08/2026 e **o front nunca a chamou**. Enquanto isso,
quem desiste liga para o restaurante e pede que o lojista cancele pelo painel —
e num pix já pago isso atrasa a devolução do dinheiro até alguém atender o
telefone.

O texto abaixo é para copiar inteiro.

---

### PARA COLAR NO REPOSITÓRIO DO APP

> **Assunto: o cliente pode cancelar o próprio pedido, e a rota já existe**
>
> ```
> POST /restaurants/{restaurant_slug}/orders/track/{tracking_token}/cancel
> ```
>
> Responde `OrderDetailResponse` — **o mesmo schema da tela de
> acompanhamento**. Depois de cancelar, dá para renderizar a resposta direto,
> sem um GET a mais.
>
> #### Autenticação: nenhuma, e é de propósito
>
> **Não mande token de cliente.** Quem autoriza é o `tracking_token` da URL —
> o mesmo que já abre o acompanhamento e a avaliação, 256 bits, sem rota de
> reemissão.
>
> Pedido de convidado é caso normal, e exigir conta aqui deixaria justamente o
> convidado sem saída.
>
> #### Quando o botão pode aparecer
>
> **Só com `status` em `pending` ou `accepted`.**
>
> | `status` | Botão |
> |---|---|
> | `pending` | **aparece** — inclui o pix ainda não pago |
> | `accepted` | **aparece** — o lojista aceitou, a comida não começou |
> | `preparing`, `ready`, `out_for_delivery` | **não** — a comida já está sendo feita |
> | `completed`, `cancelled`, `rejected` | **não** — estado final |
>
> A partir de `preparing` o insumo já saiu do estoque, e quem decide quem come
> o prejuízo passa a ser o lojista. **A tela deve trocar o botão por "Falar com
> o restaurante"**, não escondê-lo sem explicação.
>
> Esconder o botão é a defesa da tela; a do servidor é o 409, e ele existe
> porque o status pode mudar entre a tela carregar e o dedo chegar no botão.
>
> #### O corpo é opcional — e ele todo
>
> ```jsonc
> // válido:
> {}
> // também válido — pode não mandar corpo nenhum
> // também válido:
> { "reason": "mudei de ideia" }
> ```
>
> `reason` é `string | null`, **máximo 150 caracteres**, opcional.
>
> **Não faça dele um campo obrigatório na tela.** Exigir justificativa de quem
> desiste de um pedido que nem começou produz um campo preenchido com "a". O
> histórico já registra que foi o cliente.
>
> Se vier, o backend grava `"Cancelado pelo cliente: mudei de ideia"` no
> histórico; sem ele, `"Cancelado pelo cliente"`.
>
> #### O que acontece junto — o app NÃO precisa pedir nada disso
>
> É a **mesma escrita** do cancelamento pelo painel, então tudo isto acontece
> numa chamada só:
>
> | | |
> |---|---|
> | **Cupom** | volta a ficar disponível para o cliente usar de novo |
> | **Cashback resgatado** | volta para o saldo. Sem isto o cliente cancelaria e **perderia** o dinheiro |
> | **Cashback a ganhar** | não é creditado — ele só entra em `completed`, e não houve venda |
> | **Pagamento online** | é **estornado automaticamente** |
>
> **Não chame nenhuma rota de estorno, de cupom ou de cashback depois.** Uma
> segunda chamada não tem o que fazer e só cria caminho para erro.
>
> #### O estorno, e o que a resposta diz sobre ele
>
> O estorno acontece **depois** de o cancelamento estar gravado, e de propósito:
> se o Mercado Pago estiver fora do ar, **o cancelamento vale mesmo assim** e
> uma varredura devolve o dinheiro depois.
>
> Consequência para a tela: no `OrderDetailResponse` que volta, o
> `payment_status` pode estar
>
> - **`refunded`** — o gateway confirmou que o dinheiro saiu; ou
> - **ainda `paid`** — o estorno foi pedido e o gateway ainda não liquidou, ou
>   estava fora do ar.
>
> **Nos dois casos o pedido está cancelado.** Não trate `paid` como falha.
>
> A frase certa é *"cancelado — a devolução aparece na sua fatura em até X
> dias"*, e não *"cancelado, mas o estorno falhou"*: o app não tem como saber a
> diferença entre "ainda não liquidou" e "falhou", e nos dois a varredura
> resolve.
>
> #### Os erros, um a um
>
> | Status | Quando | O que a tela faz |
> |---|---|---|
> | **200** | cancelou | renderiza o `OrderDetailResponse` que voltou |
> | **404** | token errado, pedido de outro restaurante, ou `restaurant_slug` inválido/inativo | *"Não encontramos este pedido."* **Não diga "token inválido"** — as três causas respondem igual de propósito, para a rota não virar um oráculo de tokens válidos |
> | **409** | o pedido saiu da janela | **use o `detail`**: ele cita o status atual (`"Pedido em 'preparing' nao pode mais ser cancelado por voce. Fale com o restaurante."`). Recarregue o pedido e mostre o estado novo |
> | **422** | `reason` acima de 150 caracteres | erro de validação do FastAPI, com `loc` apontando o campo |
> | **429** | limite: **10/min e 60/hora por IP** | *"Muitas tentativas, aguarde um instante."* |
>
> **O 409 é o caso que mais vai acontecer na prática**, e não é erro do app: é
> a corrida normal entre a tela e a cozinha. Trate-o como "o pedido andou",
> recarregue, e mostre o botão certo para o estado novo.
>
> #### Clicar duas vezes
>
> **Não mande `Idempotency-Key`** — a rota não a aceita, e não faz falta: o
> segundo clique chega com o pedido já em `cancelled` e leva **409**. Basta
> desabilitar o botão enquanto a requisição está em voo e tratar o 409 como
> acima.
>
> #### Como testar
>
> 1. crie um pedido e pegue o `tracking_token` da resposta de criação;
> 2. `POST .../track/{token}/cancel` com corpo vazio → **200**, e o
>    `OrderDetailResponse` volta com `status: "cancelled"`;
> 3. repita a mesma chamada → **409**;
> 4. num pedido `preparing` → **409**, com o status citado no `detail`;
> 5. com um token inventado → **404**.
>
> O caso que mais vale testar é o **pix pago e cancelado em seguida**: é o
> motivo de a rota existir, e é onde o cupom, o cashback e o estorno acontecem
> todos na mesma chamada.

---

### O que fica do lado do backend

**Nada.** A rota está no `openapi.json` versionado, com os quatro status
documentados (`200`, `404`, `409`, `422`), e o `429` vem do rate limit global.
Não há deploy pendente para o app começar.

O que existe hoje é uma rota funcionando e ninguém chamando: **todo
cancelamento que passa pelo telefone do restaurante já podia ter sido um
toque.**

---

## 1. A revisão de alinhamento: pronta, e agora testada onde importa

**Nada foi aplicado.** As duas etapas continuam em `alembic/preparadas/`, fora
da cadeia do Alembic, e `tests/test_revisoes_preparadas.py` continua cobrando
isso a cada execução.

### O que já existia, e o que faltava

Desde a rodada 2 havia um teste `db` que roda as duas etapas contra o Postgres
de verdade, dentro de uma transação que volta. Ele provava que a revisão
**executa**.

**Mas ele roda contra o schema de sessão, que está VAZIO — e `VALIDATE
CONSTRAINT` em tabela vazia passa sempre.** Não há linha para contradizer a
regra.

Ou seja: a revisão estava provada apenas no **caminho feliz** — e o caminho
feliz é exatamente o único que não precisa de duas etapas. A operação inteira
da etapa 2 nunca tinha sido exercitada.

### Os três testes novos

Todos contra `ai_feedback`, escolhida por três motivos: tem quatro das 15
colunas, precisa só de um restaurante para existir, e é a única cujo nulo tem
consequência conhecida em produção (a retenção da LGPD que nunca alcançava a
linha).

**1. `test_as_duas_etapas_passam_com_a_tabela_CHEIA`** — cinco linhas de
verdade, as duas etapas rodam, a coluna vira `NOT NULL`, e **as cinco linhas
continuam lá**: o alinhamento não apaga nada.

**2. `test_depois_da_etapa_1_o_nulo_NOVO_ja_e_recusado`** — o "o buraco para de
crescer" da rodada 2 deixa de ser afirmação e vira comportamento medido. Antes
da etapa 1 o nulo entra sem reclamação; depois dela, `IntegrityError` citando
`ck_ai_feedback_user_message_nao_nula`.

É a propriedade que **justifica aplicar a etapa 1 sozinha e deixar assar**. Sem
ela, esperar entre as duas etapas seria só esperar. E é a metade menos óbvia do
que `NOT VALID` significa: ele já cobra as linhas novas mesmo sem ter validado
as antigas.

**3. `test_a_etapa_2_falha_no_VALIDATE_quando_sobra_nulo_antigo`** — o desenho
inteiro existe para falhar aqui. Com uma linha nula antiga, a etapa 2 morre no
**primeiro** comando, com `violated by some row` e o nome da restrição (de onde
sai a coluna). E depois do rollback, **numa conexão nova**, a coluna continua
nulável e a restrição não existe.

A afirmação na conexão nova vale mais do que parece: a transação abortada é a
garantia, mas quem lê o código está lendo o `finally` — e o `finally` é
exatamente o que uma refatoração distraída remove.

### Um detalhe de `SAVEPOINT` que custou uma execução

O teste 2 usava `with conexao.begin_nested():` em volta do `pytest.raises`. O
`raises` engole a exceção **dentro** do `with`, o SQLAlchemy conclui que deu
tudo certo e tenta `RELEASE SAVEPOINT` numa transação já abortada — e o erro
que aparece é o do `RELEASE`, não o da `CHECK` que o teste veio medir.

Fica `begin_nested()` com `rollback()` explícito depois do `raises`. Vale para
todo teste que espera erro de banco dentro de uma transação maior.

### O que a etapa 0 evita, agora demonstrado

`scripts/nulos_nas_colunas_em_desacordo.py` existe para a falha do teste 3
**nunca acontecer em produção**: ele conta os nulos antes, com a API no ar e
sem lock nenhum. O teste 3 é a demonstração do que ele evita.

### O documento foi acertado

`docs/alinhamento-orm-schema.md` dizia 16 em oito lugares, e "de 42 para 26".
Passou a 15 e "de 41 para 26", e a seção sobre `valid_until` mudou de "a única
em que a resposta pode ser relaxe o model" para **"a que saiu desta lista, e
por quê"** — com o critério código × schema que saiu da rodada 3.

E as duas afirmações que o documento fazia sem prova ganharam o teste que as
prova, citado nominalmente.

### Estado final da revisão

| | |
|---|---|
| Colunas | **15** |
| Onde | `alembic/preparadas/`, fora da cadeia |
| Executada | sim, contra o Postgres 17 de teste |
| Com tabela cheia | sim |
| Com nulo antigo | sim — falha no `VALIDATE`, como desenhada |
| Recusa nulo novo depois da etapa 1 | sim |
| Aplicada em produção | **não** |

O que falta é só seu: a **etapa 0** (`nulos_nas_colunas_em_desacordo.py`) contra
o banco de produção. Ela é só leitura e responde se a etapa 2 passaria.

---

## 2. O que eu faria a seguir

**Escolha: varrer o escopo de tenant.** Uma varredura mecânica que responda, em
toda rota `/admin`, se o `restaurant_id` que chega ao repositório vem do
**token** ou de algo que o cliente controla.

### Por que essa, e não as outras

O método que mais rendeu neste trabalho, em três rodadas seguidas, foi sempre o
mesmo: **escrever a varredura de uma classe de defeito e descobrir os casos que
ninguém conhecia.**

| Rodada | Convenção que existia, escrita | O que a varredura achou |
|---|---|---|
| 2 | "não duble model com `SimpleNamespace`" (CLAUDE.md) | **141** dublês, e uma coluna morta há 8 revisões |
| 3 | — | a regra da janela do cupom em **três** cópias |
| 4 | "o que o cliente digita não vai para chave de Redis" (`embedding_key`) | **dois** casos, e o segundo eu não conhecia |

Em **todas** as três, a regra já estava escrita no repositório, por extenso, com
o motivo. E em todas as três ela estava quebrada — porque quem escreveu o código
novo não estava lendo aquele docstring.

**"Escopo de lojista vem do token, nunca da URL" é a próxima regra dessa lista.**
Ela está no CLAUDE.md, tem `AdminScope` para aplicá-la em um lugar só, e tem
dois arquivos de teste feitos à mão (`test_admin_tenant_isolation.py`,
`test_auditoria_isolamento_e2e.py`).

**Teste feito à mão cobre as rotas de que alguém lembrou.** É exatamente a
forma dos outros três casos.

E o custo de estar errado aqui é o maior do sistema: um restaurante lendo
pedido, cliente ou faturamento de outro. Não é 500, não é lentidão — é vazamento
entre lojas, e chega em silêncio.

### O que a varredura faria

Por AST, em `src/api/endpoints/` e `src/services/`:

1. **toda função de endpoint sob `/admin`** — ela recebe `AdminScope`?
2. **todo `restaurant_id=` passado a um método de repositório** — a expressão
   vem de `scope.restaurant_id`, ou de um parâmetro de rota / campo de corpo?
3. **todo método de repositório que aceita `restaurant_id`** — ele está sendo
   chamado em algum lugar **sem** ele?

O terceiro é o que acha o buraco de verdade: uma consulta que *poderia* filtrar
por restaurante e, num caminho, não filtra.

E, como nas outras, ela vira **teste com anti-vacuidade** — um caso plantado
tem que ser acusado, e um caso legítimo (o painel da plataforma, se houver) não
pode virar vermelho.

### O resultado que eu espero, e por que ele vale mesmo assim

**Espero achar pouco ou nada.** O `AdminScope` é recente, foi feito exatamente
para isso, e a rodada 4 mostrou que `_find_coupon` já separava painel de
cliente com cuidado.

Isso não desqualifica o item — inverte o que ele entrega. Hoje a resposta para
"o escopo está certo em todas as rotas?" é *"os testes que escrevemos passam"*.
Depois, é *"as N rotas foram conferidas mecanicamente, e a próxima também
será"*. **Num sistema multi-loja, essa diferença é o que se mostra a um
lojista** — e ela custa uma noite, não uma auditoria.

### As que eu NÃO escolhi, e por quê

**O histórico de chat no Redis** (§5.1 da rodada 2) — é o de maior valor
visível: todo deploy zera a conversa de quem está no meio de um pedido. **Mas
ele está bloqueado, e por um motivo que só existe desde ontem.**

A rodada 4 tirou do Redis toda a coordenada e toda a mensagem em claro. O
histórico de chat é, por definição, **texto em claro do cliente** — é o único
item para o qual não existe digest, porque o modelo precisa ler o que a pessoa
escreveu. Implementá-lo **antes** da resposta sobre `RDB`/`AOF` seria pôr de
volta no Redis, em volume maior, exatamente o que acabei de tirar.

Com persistência desligada, é seguro e eu faria em seguida. Com persistência
ligada, o plano precisa mudar antes de virar código. **É um item que a sua
resposta de hoje desbloqueia** — não um que eu esteja adiando.

**Declarar o Redis no `docs/lgpd-proposta.md`** — o inventário está incompleto,
e agora há o que declarar. Mas metade do conteúdo (o prazo de retenção real do
que fica em disco) depende da mesma resposta. Faria junto com o item acima, na
mesma rodada.

**A latência da busca vetorial** — quase escolhi, e desisti depois de conferir.
O `docs/auditoria-voz-vs-texto.md` marcava como *"não verificado, e importa"* o
fato de cada requisição construir um cliente de embedding novo e pagar handshake
TLS — o que valeria **2,6 s** por busca falada. **Já foi consertado em
15/08/2026**: `get_embeddings_client` é `lru_cache`, medido A/B intercalado,
654 ms → 340 ms de mediana.

O que sobra ali são os ~43 ms de transportar o vetor como texto. Real, e o
documento do ANN aponta para ele — mas são ~43 ms num turno de centenas. Vale
uma tarde, não a próxima noite.

**As 26 divergências restantes** (20 + 6) — 18 são anotação enganosa com
`DEFAULT` no banco (custo: zero hoje), 2 já estão travadas por
`test_models_nunca_instanciados.py`, e as 6 não mapeadas **mudam o que o ORM traz
em todo `SELECT`** — é mudança de comportamento disfarçada de arrumação, e
merece rodada própria com medição, não um item de fim de noite.

**`float` × `Decimal`** — você o deixou fora três vezes. Não vou trazê-lo de
volta por conta própria; se quiser, a parte que **não** depende do front (a
aritmética interna, que a convenção do repositório já proíbe em `float`) é
varrível pelo mesmo método, e eu diria que vale.

---

## Fechamento da rodada 5

**Portão: 2859 testes verdes** (2240 rápidos + 619 `db`), ruff limpo,
`openapi.json` em dia, lock em dia. Nada foi executado contra produção.

### O que continua esperando você

- **os três comandos do Redis** (`RDB`/`AOF`) e o **das filiais** — você disse
  que roda hoje;
- a resposta do primeiro **desbloqueia** o histórico de chat e completa o
  `docs/lgpd-proposta.md`;
- a **etapa 0** do alinhamento (`scripts/nulos_nas_colunas_em_desacordo.py`),
  só leitura, que responde se a etapa 2 passaria;
- o `6fcaccc`;
- os dois textos colável­eis: o de `valid_until` (rodada 4, §3) e o da rota de
  cancelamento (§3 desta).
