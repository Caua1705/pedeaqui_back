# Impressão das comandas

Do pedido aceito ao papel saindo na cozinha. Envolve três peças:

| Peça | Onde roda | Arquivo |
|---|---|---|
| Cadastro de setores e vínculo produto → setor | API | `services/admin_printing_service.py` |
| Desenho de cada via | API, sem banco | `services/print_layout.py` |
| Agente que escuta e imprime | **PC da loja** | `print-agent/` (projeto Python separado) |

---

## 1. A divisão de trabalho, e por que ela é assim

**O agente é burro de propósito.** Ele não sabe o que é um adicional, não sabe
que via de produção não leva preço e não sabe que pedido não pago não vai para a
cozinha. A API devolve as vias **já prontas em texto**, quebradas na largura
certa. Ao agente sobra: qual impressora recebe cada via, e como falar ESC/POS com
ela.

A razão: existe **um agente por loja**, instalado à mão, atualizado quando alguém
lembra. Regra que mora nele só se corrige indo até a loja. Regra que mora na API
se corrige com um deploy e vale para todo mundo na mesma hora.

Se a formatação vivesse no agente, cada loja com uma versão instalada imprimiria
uma comanda diferente, e corrigir um bug de layout viraria operação de campo.

---

## 2. Setores de impressão

`printing_sectors` pende de **filial**, não de restaurante: impressora é um objeto
físico dentro de uma loja. "Cozinha" da unidade do Centro e "Cozinha" da Aldeota
são duas máquinas diferentes, ligadas a dois computadores diferentes. Pendurar o
setor no restaurante faria a comanda da Aldeota sair no Centro.

`products.printing_sector_id` é **NULLABLE, e o nulo tem significado**: o produto
não gera via de produção. É o caso da lata de refrigerante, que sai da geladeira
do balcão — imprimir comanda para ela só gasta papel e treina a cozinha a ignorar
comanda.

### Rotas

| Rota | O que faz |
|---|---|
| `GET/POST /admin/branches/{branch_id}/printing-sectors` | lista (ativos **e** inativos) e cria |
| `PATCH /admin/printing-sectors/{sector_id}` | renomeia, reordena, desativa |
| `PATCH /admin/products/{product_id}/printing-sector` | aponta um produto |
| `PATCH /admin/categories/{category_id}/printing-sector` | aplica a **todos** os produtos da categoria |

A rota de categoria é como a configuração inicial acontece de verdade: "tudo que
é bebida vai para o Bar, tudo que é pizza vai para o Forno". Produto a produto,
um cardápio de 200 itens não seria configurado nunca — e cardápio meio
configurado imprime comanda pela metade. Ela **sobrescreve inclusive quem já
tinha setor**; quem quiser exceção reaponta aquele produto depois.

**Nada é apagado.** Setor só é ligado e desligado, porque
`products.printing_sector_id` referencia a linha por FK. Desativar é o que o
painel chama de excluir, e o vínculo dos produtos **não** é desfeito.

UNIQUE em `(branch_id, name)`. O service confere antes para responder 409 com
mensagem em vez de deixar o `IntegrityError` virar 500.

---

## 3. Montagem das vias

`GET /admin/orders/{id}/print-jobs` → `AdminPrintingService.build_print_jobs`.

A leitura do pedido passa pelo `AdminOrderService`, e não por um repositório
direto: mesmo 404 para pedido inexistente, de outro restaurante e de outra
filial, e o mesmo `OrderDetailResponse` que o painel já mostra — inclusive os
adicionais agrupados por grupo de complemento, que são o que faz a troca de
acompanhamento aparecer na comanda da cozinha.

A resposta é uma lista de vias:

```
[ via do cliente        columns=48  font_size=normal  sector_name="Via do cliente" ]
[ via de produção       columns=24  font_size=large   sector_name="Cozinha"       ]
[ via de produção       columns=24  font_size=large   sector_name="Bar"           ]
[ via de produção       columns=24  font_size=large   sector_name="SEM SETOR"     ]
```

Uma entrada por via — e a mesma via **repetida** quando a filial pediu mais de
uma cópia, que é como a configuração da seção 3.1 chega ao agente sem ele
precisar aprender nada.

### Três regras que precisam ser lidas antes de mexer

**1. Pedido não pago não gera via de produção.**

É a mesma regra que `ensure_payment_allows_order_status` já aplica ao status. Se
a comanda saísse assim mesmo, a regra valeria só para quem olha a tela — a praça
prepararia o pedido do mesmo jeito, porque **comanda impressa é ordem de
produção**. A via do **cliente** continua saindo: ela serve de conferência no
balcão e não manda ninguém cozinhar.

**2. Item nunca some da comanda.**

Item cujo setor não serve vai para uma via **`"SEM SETOR"`** e o caso é logado
(`[Impressao] item sem setor utilizavel`), em vez de o item desaparecer
silenciosamente da produção. É sempre configuração errada, nunca operação normal
— sem esse log, alguém descobriria pela comanda estranha no meio do almoço.

**Esta via perdeu um dos dois motivos em 20/08/2026, e o que sobrou não a
dispensa.** Até a revisão `20260820_0026`, o produto pendia de RESTAURANTE e o
setor de FILIAL: o pedido da filial B podia trazer um produto apontando para o
setor da filial A. Hoje o produto também pende de filial, e a FK composta
`(branch_id, printing_sector_id)` torna esse par impossível de gravar — a rota
que o tentaria responde **400** antes.

O motivo que sobrou é o **setor desativado** depois de vinculado, que é estado
e não integridade referencial, mais o produto que não está mais naquele
restaurante. Nenhum dos dois tem chave estrangeira que os feche.

**O único jeito de um item NÃO ser impresso** é o produto ter
`printing_sector_id` nulo, que é decisão explícita do lojista.

**3. A ordem é imposta, não acidental.**

Os **setores** saem na ordem cadastrada (`sort_order`, depois nome) — é a ordem
que o lojista arrumou pensando no fluxo da cozinha, e ela precisa ser a mesma em
todo pedido para quem separa as bobinas não se perder.

Os **itens dentro de cada via** preservam a ordem do pedido, que é a mesma da tela
do painel e a mesma da via do cliente — conferir uma contra a outra só funciona
se as linhas estiverem na mesma sequência.

Os **adicionais** são ordenados alfabeticamente. `order_item_options` não tem
coluna de ordem nem `created_at`, e o id é aleatório: sem `sorted`, a mesma
comanda sairia com os adicionais embaralhados a cada requisição.

---

## 3.1 Personalização por filial (revisão `20260821_0029`)

Duas configurações, uma tela: `GET` e `PATCH
/admin/branches/{branch_id}/print-settings`. Leitura é **PESSOAS** (quem está
em pé na impressora pergunta "por que saíram duas vias?"), escrita é
**GERÊNCIA** — não é dinheiro. A mensagem **padrão da marca** fica em `PATCH
/admin/settings`, que é do dono.

### A mensagem do rodapé

Texto livre do lojista impresso no fim da **via do cliente**. É espaço grátis
no papel que já sai e já chega na mão de quem pediu: o Instagram da loja,
"peça direto e ganhe 5% de volta".

**Só na via do cliente.** A de produção sai em 24 colunas de fonte dupla para
quem está com as mãos ocupadas — propaganda ali é papel gasto e mais uma
linha para ler no aperto.

**Herda do restaurante, e o vazio é uma escolha.** Mesmo regime dos termos
comerciais da revisão `20260818_0025`, porque é da mesma natureza: texto de
marca, escrito uma vez para a rede inteira. Com um terceiro estado que
`min_order_value` não precisa ter:

| `branches.receipt_footer_message` | O que sai |
|---|---|
| `NULL` | a mensagem do restaurante (herda) |
| `''` | **nada** — esta loja recusou a campanha da rede |
| texto | o texto desta loja |

Sem o `''` não haveria como uma filial recusar a mensagem da marca. É o mesmo
caso do `service_fee_enabled = false` da armadilha 35, escrito com texto: um
`or` no lugar do `is not None` faria a campanha voltar a sair justamente na
loja que a desligou. Quem combina os dois é `resolve_receipt_footer`
(`services/branch_operation.py`), no mesmo módulo e no mesmo `_ou_herdado` de
todo o resto — mas **fora** do `BranchOperation`, que é "o que o PEDIDO usa",
e o rodapé não entra em cálculo de pedido nenhum.

**O texto é limpo na ESCRITA, não na impressão.** `normalize_receipt_text`
(`utils/normalization.py`) roda no validador do corpo e é o que separa esta
mensagem de qualquer outro texto do painel:

- **caractere de controle é comando de impressora, não caractere.** O agente
  escreve `content` direto no fluxo ESC/POS e a codepage passa `0x1B` adiante
  intacto — um ESC colado no meio da mensagem deixaria de ser texto e viraria
  `ESC ...`, reprogramando a impressora no meio da comanda. `encode_text` não
  defende contra isso: o trabalho dele é a queda de qualidade do que a
  **tabela** não tem (acento, emoji), e `0x1B` a tabela tem;
- `\t` vira espaço (a tabulação conta 1 caractere e imprime vários, o que
  estoura a coluna sem a conta perceber), `\r\n` vira `\n`, espaço no fim da
  linha some e linha em branco repetida colapsa;
- NFC pelo motivo da armadilha 28: a forma decomposta **não existe na CP850**,
  e a mensagem sairia sem um acento.

Tetos: **240 caracteres** (cinco linhas cheias da bobina) e **6 linhas**. O de
linhas é conferido depois da limpeza, e na escrita — `"a\nb\nc..."` cabe em
240 caracteres e sairia com 120 linhas. Cortar na hora de imprimir faria o
lojista descobrir o limite pela bobina, com o texto já gravado e a tela
mostrando o que ele digitou.

**A quebra de linha do lojista é respeitada**, e por isso `_footer_block`
parte o texto em `\n` **antes** de embrulhar: `textwrap` trata quebra como
espaço, e "Peça no site" + "@nossaloja" sairiam grudados na mesma linha.

### Quantas vias saem, por tipo de pedido

Quatro colunas em `branches`, `NOT NULL DEFAULT 1`, `CHECK BETWEEN 0 AND 5`:

```
print_customer_copies_delivery      print_customer_copies_pickup
print_production_copies_delivery    print_production_copies_pickup
```

**Só da filial, sem herança nenhuma** — ao contrário do rodapé. Elas
descrevem o balcão (quantas impressoras existem, se a comanda vai grampeada
no pacote, se o motoboy leva uma via), não termo negociado pela marca. E todo
o resto da configuração de impressão — setor, `printer_name`, o próprio
agente — já pende de filial sem herdar nada: dar um regime próprio só a estas
quatro colocaria **dois regimes na mesma tela**, que é o jeito mais barato de
fazer alguém preencher o campo errado.

`0` é válido e é o pedido que originou a feature: a retirada normalmente não
precisa da via do cliente, que é a que iria grampeada na sacola. A de
**produção** da retirada continua saindo — ela é a comanda da cozinha, e
pedido de balcão também é preparado.

> **A cópia é ENTRADA REPETIDA na lista de vias, e não um campo `copies`.**
> Esta é a decisão que o resto depende. Duas vias do cliente chegam como dois
> itens idênticos em `jobs`, um atrás do outro. Um campo novo seria mais
> bonito e o agente **não saberia lê-lo**: não existe atualização remota do
> agente (seção 5), e cada versão nova é uma visita por loja. Repetindo a
> entrada, a configuração vale hoje em toda instalação já em campo,
> **inclusive nas anteriores a esta revisão** — o agente sempre imprimiu
> `for job in jobs`.

Consequências que valem conhecer:

- `jobs` pode vir **vazia** (a filial zerou as duas contagens daquele tipo).
  Nada quebra, mas o agente instalado em campo lê lista vazia como
  `"pagamento ainda nao confirmado?"` no log dele — mensagem errada para este
  caso, e que não dá para corrigir sem visitar a loja.
- A contagem **multiplica** o que as outras regras deixaram passar; ela não as
  substitui. Pedido com pagamento online não confirmado continua sem via de
  produção, por mais cópias que estejam configuradas.
- A filial do pedido é lida **sem filtro de `is_active`**
  (`BranchRepository.get_by_id_and_restaurant`): reimprimir o pedido de uma
  loja desativada tem que sair com a configuração daquela loja, e não mudar
  sozinho porque alguém desativou a filial depois.

### A migração não muda operação

As quatro colunas nascem em `1` — inclusive a produção da retirada, que é
justamente o caso que motivou a feature. Nascer em `0` pararia a comanda da
cozinha no deploy, sem uma linha de log: é o defeito do `UPDATE` que faltava
na `20260818_0025`, de cabeça para baixo. **Migração abre a porta para o
lojista mudar; quem muda é ele.**
`tests/test_migracao_comanda_personalizada_db.py` trava isso contra um
Postgres de verdade, inserindo a filial por SQL cru — o `default=1` do model
faria o teste passar verde sobre uma migração sem `server_default`.

---

## 4. O desenho (`print_layout.py`)

Sem banco e sem HTTP, por isso dá para testar linha a linha
(`tests/test_print_layout.py`).

**Larguras:**

```
RECEIPT_WIDTH     = 48    bobina de 80mm em fonte normal (via do cliente)
PRODUCTION_WIDTH  = 24    fonte DUPLA cabe metade das colunas (via de produção)
```

A via de produção sai em fonte dupla para ser lida de longe, no calor, por quem
está com as mãos ocupadas. Montá-la com 48 colunas faria a impressora dobrar
linha sozinha e `"1x FILE COM FRITAS"` sairia cortado no meio da palavra.

**Rótulo fixo não leva acento.** Nome do cliente, endereço e nome do produto saem
como estão no banco, com acento e tudo. Já `ENDERECO` e `TAXA DE SERVICO` ficam
em ASCII porque a codepage da térmica varia de modelo para modelo, e um rótulo
virando `ENDEREO` é um defeito que aparece em **toda** comanda.

**A entrada é o `OrderDetailResponse`**, o mesmo objeto que o painel recebe. Não é
economia de código: é a garantia de que a comanda mostra exatamente o que o
lojista vê na tela. Duas leituras do pedido seriam duas chances de divergir.

---

## 5. O agente (`print-agent/`)

Projeto Python **separado**, com `pytest.ini` e `requirements.txt` próprios. Roda
numa máquina Windows na loja, instalado à mão. Não sobe em container com a API, e
os testes dele não rodam no CI da API — ver a nota em `pytest.ini` da raiz.

> **Pendência que bloqueia outra: não existe atualização remota do agente.**
> Ele é um `.exe` instalado à mão, com exclusão de antivírus e instalador, e
> `INSTALACAO.md` não descreve nenhum caminho de update. Toda mudança no agente
> é uma visita por loja.
>
> Isso já segurou duas features, e as duas pelo mesmo motivo: **elas precisam
> de um comando ESC/POS que o agente não conhece.**
>
> - o **QR de avaliação no rodapé da comanda**, que precisa de `GS ( k` (o
>   agente implementa cinco comandos ESC/POS, e nenhum é QR) e de um campo
>   novo no contrato de impressão. O raciocínio inteiro está em
>   [avaliacao-de-pedido.md](avaliacao-de-pedido.md), seção 6;
> - o **logo da loja na comanda**, que precisa de `GS v 0` (ou do upload de
>   NV bitmap) e de bytes de imagem no contrato — hoje `content` é texto.
>
> A ordem correta é **primeiro a atualização remota, depois as duas.**
>
> A personalização da seção 3.1 escapou dessa fila de propósito: rodapé é
> texto dentro de `content`, e cópia é entrada repetida em `jobs`. Nenhuma
> das duas pede um byte novo ao agente, e por isso as duas valem hoje em toda
> loja instalada. **É esse o teste para saber se uma ideia de comanda cabe
> agora ou entra na fila:** ela precisa de um comando novo da impressora?

```bash
cd print-agent && pytest      # 65 testes
```

### O laço

```
abre GET /admin/orders/stream com Last-Event-ID
   └─ autentica com ticket de 30s (EventSource-style, mas aqui é httpx)
para cada evento order.created | order.status_changed:
   status == trigger_status (padrão "accepted")?
   pedido já está na memória em disco?  → ignora
   GET /admin/orders/{id}/print-jobs
   para cada via: acha a impressora do setor no config.ini, monta ESC/POS, envia
   TODAS as vias saíram?  → marca como impresso
```

`order.created` está na lista de eventos escutados porque um pedido pode nascer
já aceito se a regra de aceite automático mudar um dia — é mais barato escutar os
dois do que descobrir isso com a cozinha parada.

**O pedido só é marcado como impresso quando TODAS as vias saíram.** Meia comanda
impressa é pior que nenhuma: a cozinha prepara o que viu e o resto do pedido
some. Se saiu incompleto, o pedido continua elegível e o agente tenta de novo
quando o servidor repetir o evento.

### O que impede a reimpressão

Duas coisas, e as duas são necessárias:

- o `Last-Event-ID` faz o servidor repetir o que aconteceu durante a queda — o
  que é **desejado** (senão o pedido perdido nunca imprime) e traz repetição
  junto;
- a memória em disco (`PrintedOrders`, retenção padrão de 7 dias) descarta o que
  já saiu. Sem ela, cada reconexão cuspiria a fila do dia inteiro.

### Reconectar é normal, não é falha

O servidor fecha o stream a cada 15 minutos de propósito (`MAX_STREAM_SECONDS`) e
proxies derrubam conexão ociosa — daí o heartbeat de 20s da API.

Por isso o backoff **só cresce quando a conexão cai rápido**: uma que durou mais
que `HEALTHY_CONNECTION_SECONDS` (30s) zera a espera. Senão o agente que roda a
noite inteira chegaria de manhã esperando um minuto entre tentativas por ter
reconectado normalmente centenas de vezes. Há jitter para várias lojas não
voltarem todas no mesmo segundo depois de uma queda da API.

`sync_required` (replay truncado, mais de uma hora fora) **não é recuperável
sozinho**: o agente grita no log pedindo conferência no painel, porque pedidos
aceitos naquele intervalo podem não ter impresso.

### Configuração (`config.ini`)

**Nome de setor é comparado sem acento e sem caixa.** O nome chega da API como o
lojista digitou ("Cozinha", "COZINHA", "Praça Quente") e o `configparser` já
rebaixa a chave para minúsculas. Comparar cru faria a comanda da "Cozinha" não
achar a impressora cadastrada como "cozinha".

Setor sem impressora no `config.ini` e sem um `default` configurado: a via **não
sai**, e o agente grita no log. Acontece quando alguém cria um setor no painel
depois da instalação.

Padrões que valem conhecer:

```
trigger_status          "accepted"   status que dispara a impressão
codepage / encoding     2 / cp850    se imprime "JoÃ£o" no lugar de "João", é aqui
print_attempts          3            tentativas por via
print_retry_seconds     5
reconnect_min/max       1s / 60s     teto baixo: 1 min sem stream = pedido não impresso
state_retention_days    7
```

`ConfigError` sobe até o `__main__` e vira mensagem de uma linha com código de
saída 2 — um agente que sobe com configuração errada é pior que um que não sobe.

### Formato curto do `config.ini`

Para a instalação de balcão existe uma seção `[rapidex]` que cobre o caso comum
(uma loja, uma impressora):

```ini
[rapidex]
api_url  = https://api.pederapidex.com
email    = impressora.centro@exemplo.com
password = ...
printer  = EPSON TM-T20
```

`printer` é exatamente o `default` de `[printers]` — a impressora que recebe
todo setor não mapeado. **Não é mecanismo novo**: é o fallback que já existia,
com um nome que cabe numa instrução por telefone. Os dois formatos convivem, e
onde ambos dizem a mesma coisa o formato longo vence, por ser mais específico.

⚠️ **`token` sozinho não serve para produção.** O token de lojista vale 12h
(`ADMIN_ACCESS_TOKEN_MINUTES`), e um agente que sobe no boot com token fixo para
de imprimir na manhã seguinte. Com `email` + `password` ele refaz o login
sozinho. O agente avisa no log quando sobe só com token.

---

## 6. Empacotamento e instalação na loja

O agente é distribuído como **um executável único** gerado por PyInstaller, para
a máquina do restaurante não precisar de Python instalado.

```bat
cd print-agent
build.bat
```

O `build.bat` roda o PyInstaller e monta `dist/pendrive/` com quatro arquivos:
o `.exe`, o `instalar.bat`, o `desinstalar.bat` e o `INSTALACAO.md`.

| Arquivo | O que faz |
|---|---|
| `instalar.bat` | copia para `%LOCALAPPDATA%\Rapidex Impressao`, cria atalho em `shell:startup`, abre o programa |
| `desinstalar.bat` | fecha o processo, remove o atalho e a pasta |
| [`INSTALACAO.md`](../print-agent/INSTALACAO.md) | passo a passo para quem não é programador |

Nem o instalador nem o agente precisam de administrador: tudo mora no perfil do
usuário, e o agente não precisa de privilégio para falar com a impressora dele.
Reinstalar por cima **não sobrescreve** um `config.ini` existente.

### A armadilha do PyInstaller: `__file__` não é a pasta do lojista

Congelado, `__file__` aponta para dentro do bundle — hoje a subpasta
`_internal/` ao lado do executável (`--onedir`), e antes uma pasta temporária
(`sys._MEIPASS`) que o Windows **apagava quando o processo terminava**.

Se qualquer caminho em disco saísse de `__file__`:

- o `config.ini` procurado nunca seria o que o instalador copiou;
- o log seria gravado onde ninguém vai procurar;
- `pedidos-impressos.json` idem — e no `--onefile` sumia a cada fechamento,
  fazendo o agente **reimprimir a fila inteira do dia** na próxima abertura.

Os três só aparecem depois de empacotar — rodando `python -m print_agent` tudo
funciona. Por isso todo caminho sai de `print_agent/paths.py`:

```python
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent   # a pasta do .exe
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
```

Ao lado do executável ficam `config.ini`, `rapidex-impressao.log` (1 MB × 5
arquivos) e `pedidos-impressos.json`. Sem subpasta, de propósito: o lojista é
instruído por telefone a abrir a pasta e mandar o arquivo `.log`. A única
subpasta é a `_internal/` do PyInstaller, que o `INSTALACAO.md` manda não tocar.

### O console fica visível

`--onedir` sem `--noconsole`. No dia da instalação alguém precisa ver na tela
que conectou e que a comanda saiu; janela escondida vira depuração às cegas.
Virar ícone de bandeja é outra tarefa — exige uma bandeja de verdade, não só
trocar o flag.

### Falso positivo do Windows Defender

Executável do PyInstaller sem assinatura digital costuma ser barrado na primeira
execução ("O Windows protegeu o seu computador") e, em alguns antivírus, movido
para quarentena. **Aconteceu de verdade:** o Defender apagou o `.exe` na frente
do lojista e o `instalar.bat` morreu com "não encontrei o RapidexImpressao.exe".

Três coisas saíram disso, e nenhuma delas resolve sozinha:

- **O build é `--onedir`, não `--onefile`.** O de arquivo único se auto-extrai
  numa pasta temporária a cada abertura, que é justamente a heurística que os
  antivírus marcam. Trocar reduz muito o falso positivo; não elimina.
- **A exclusão de pasta é o item 1 do `INSTALACAO.md`**, antes de copiar o
  programa. Como conserto ela chega tarde: o arquivo já foi apagado, e quem está
  no balcão já viu o antivírus removendo o programa que acabou de instalar.
- **O binário é submetido à Microsoft como falso positivo** a cada versão nova
  (formulário do Microsoft Security Intelligence, gratuito, resposta em alguns
  dias). É o que corrige para *todos* os restaurantes de uma vez, inclusive os já
  instalados, porque a correção desce na atualização de definições.

Assinar o binário com certificado de code signing continua sendo a única solução
definitiva, e custa dinheiro por ano.

`stop()` é atendido no fim do evento em andamento, e o `sleep` é fatiado em
pedaços de 1s: um Ctrl+C que cortasse um `WritePrinter` deixaria meia comanda na
bobina com o pedido marcado como impresso, e um `time.sleep(60)` inteiro faria o
"parar serviço" do Windows bater no timeout e matar o processo.
