# Usuários do painel

**Status: implementado em 23/08/2026.** Este documento nasceu como proposta,
foi decidido, e agora descreve o que existe. As três decisões que ele pedia
estão respondidas na seção 8, com o que foi feito de cada uma.

Levantado em 23/08/2026 contra a árvore em `4a1f4c1`; implementado nas
revisões `20260823_0037` (a coluna) e nos commits das quatro rotas.

O que ele responde, porque foi o que se pediu: **como a senha nasce**, **como
se desativa alguém** e **o que acontece com o `print_agent`**, que é conta de
máquina e não deveria conseguir entrar no painel.

## O que existe, em uma tabela

| Rota | Papel | O que faz |
|---|---|---|
| `GET /admin/users` | `SOMENTE_DONO` | a equipe do restaurante, sem a conta de máquina |
| `POST /admin/users` | `SOMENTE_DONO` | cadastra e devolve a senha temporária **uma vez** |
| `PATCH /admin/users/{id}` | `SOMENTE_DONO` | nome, papel, filial, `is_active` |
| `POST /admin/users/{id}/reset-password` | `SOMENTE_DONO` | outra temporária, e revoga os tokens |

Não há `DELETE`, e a ausência é da seção 4.

---

## 1. O que já existe — verificado no código, não presumido

Mais do que parece. A parte cara desta frente já está construída; o que falta
é a porta.

| Peça | Onde | Estado |
|---|---|---|
| Tabela `admin_users` | `models/admin_user_model.py` | completa: `restaurant_id`, `branch_id`, `name`, `email`, `password_hash`, `role`, `is_active`, `password_changed_at` |
| Os quatro papéis | `core/constants.ADMIN_USER_ROLES` + CHECK da tabela | `owner`, `manager`, `attendant`, `print_agent` |
| Escopo de filial | `dependencies/admin_scope.py` | aplicado em todas as rotas `/admin`, num lugar só |
| Matriz de papel por rota | `dependencies/admin_scope.exigir_papel` | auditada por `tests/test_papeis_das_rotas.py` — rota nova sem papel é teste vermelho |
| Listar os usuários de um restaurante | `AdminUserRepository.list_by_restaurant` | **já escrito, e não é chamado por ninguém** |
| Hash e verificação de senha | `utils/security.py` | bcrypt, com piso de latência no login |
| Envio de e-mail | `services/email_service.py` (Resend) | funcionando — é por onde sai o código de verificação do cliente |
| Criação por linha de comando | `scripts/create_admin_user.py` | é o que se usa hoje |

**Duas propriedades que mudam a conta desta frente, e as duas já valem:**

**`is_active` já é aplicado a cada requisição, não só no login.**
`AdminAuthService._load_admin_from_token` recarrega o usuário do banco em
TODA chamada e responde 403 se `is_active` for falso — e o mesmo vale para o
ticket do stream SSE. Ou seja: **desativar alguém tem efeito na requisição
seguinte**, sem esperar as 12h do token, sem lista de revogação e sem código
novo. A metade difícil de "desativar" está pronta.

**`password_changed_at` revoga tudo o que foi emitido antes dele.** É o
mecanismo que `PATCH /admin/auth/password` e o `--reset-password` do script
usam. Serve de graça para o reset feito pelo dono.

---

## 2. O que faltava, e o que ainda falta

O levantamento original listava quatro buracos. **Três foram fechados**, e
ficam registrados porque explicam por que as rotas são como são:

1. ~~**Nenhuma rota.**~~ Todo cadastro era `docker exec` +
   `scripts/create_admin_user.py`, com `--branch-id` colado de uma consulta SQL
   feita antes. Hoje são as quatro rotas da tabela no topo. O script continua
   existindo, e continua sendo o único caminho para o **primeiro** usuário de
   um restaurante — não há ninguém autenticado para autorizar essa criação — e
   para a conta de máquina.
2. ~~**O `role` não passava por revisão de ninguém.**~~ O script aceita
   `--role owner` sem cerimônia, e continua aceitando: quem tem `docker exec`
   já tem o banco. Pela API, criar e editar usuário é `SOMENTE_DONO`, e as
   guardas da seção 4 impedem que o restaurante fique sem dono ativo.
3. ~~**`print_agent` na mesma lista dos outros três.**~~ `PAPEIS_DE_PESSOA`
   existe em `core/constants.py`, e é dela que o painel monta o seletor de
   cargo — o `role` do corpo é um `Literal` com esses três valores, então a
   lista sai no `/openapi.json` e a tela não precisa da segunda cópia.

**O que continua não existindo, e é decisão:**

4. **Não há rastro de quem criou ou desativou quem.** `admin_users` não tem
   `created_by`, e não há tabela de auditoria do painel. O `logger.info` de
   `AdminUserService` registra quem agiu, o que é rastro de operação e não
   auditoria consultável. É frente própria — ver seção 7.

---

## 3. Como a senha nasce

Três caminhos possíveis. **A diferença decisiva entre eles não é segurança —
é quantos repositórios cada um mexe.**

### Opção A — convite por e-mail

O dono cadastra nome, e-mail, papel e filial; o servidor manda um link
assinado; a pessoa abre e escolhe a própria senha.

- **A favor:** a senha nunca existe fora da cabeça de quem a usa. Não trafega
  por WhatsApp nem é ditada em voz alta no balcão. É o padrão da indústria.
- **A infraestrutura já está pronta do lado de cá:** `EmailService` (Resend)
  funciona, e `create_signed_token`/`decode_signed_token` já emitem token com
  `purpose` próprio e prazo — um `purpose="admin_invite"` de 48h não precisa
  de tabela nova.
- **Contra, e é o que pesa:** exige **uma página pública nova no
  `rapidex-admin`** ("defina sua senha"), fora da área autenticada, com
  roteamento próprio. É outro repositório e outro deploy, e enquanto ela não
  existir o convite chega num link que não abre nada.
- **Contra, operacional:** pressupõe que o atendente do balcão tem e-mail e o
  consulta. Nem sempre é verdade, e quando não é, o dono acaba criando a conta
  com o próprio e-mail — que é pior que qualquer uma das outras opções.
- **Modo de falha novo:** Resend fora do ar no minuto do cadastro. Hoje
  `_send_email` levanta 500; num fluxo de convite isso deixaria o usuário
  criado e ninguém capaz de entrar nele.

### Opção B — senha temporária gerada pelo servidor, mostrada uma vez *(recomendada)*

O dono cadastra; a resposta do `POST` traz uma senha aleatória; a tela a
mostra uma vez, com um botão de copiar; a pessoa entra com ela e o painel
**obriga** a trocar antes de qualquer outra coisa.

- **A favor, e é o motivo da recomendação:** não precisa de página nova em
  lugar nenhum. A pessoa entra pela tela de login que já existe, e a troca
  obrigatória cai na rota `PATCH /admin/auth/password`, que também já existe.
  A frente inteira cabe no backend mais um formulário na área autenticada do
  painel.
- **A favor:** funciona para quem não tem e-mail à mão, que é o caso real do
  balcão.
- **A favor:** a senha é gerada pelo servidor, então ninguém escolhe
  `mesa123`. `MIN_ADMIN_PASSWORD_LENGTH` é 12 e a gerada pode ser bem maior.
- **Contra:** a senha temporária atravessa um canal informal (WhatsApp, papel,
  voz). O prejuízo é limitado pela troca obrigatória, mas a janela existe.
- **Precisa de uma coluna nova:** `must_change_password boolean NOT NULL
  DEFAULT false`. **Não dá para reaproveitar `password_changed_at IS NULL`**
  como sinal: hoje NULL significa "nunca trocou desde a revisão 0013", e todo
  lojista antigo está assim — reusá-lo poria a plataforma inteira numa tela de
  troca de senha no dia do deploy.
- **A senha temporária sai na resposta do `POST`, uma vez, e não é
  recuperável.** Segunda via = gerar outra. Isso é propriedade, não limitação:
  uma rota "me mostra de novo" seria uma rota que devolve a senha de outra
  pessoa.

### Opção C — o dono digita a senha

Descartada. Reproduz o script na tela: o dono conhece a senha da pessoa, e o
`changed_by` do histórico de pedidos (`admin:{email}`) deixa de identificar
quem de fato agiu. Só faz sentido como último recurso, não como padrão.

### O que eu proponho

**B agora, A depois** — e A fica barata no dia em que o `rapidex-admin`
ganhar sua primeira página pública, porque a metade do backend (token
assinado com `purpose`, envio por Resend) já está escrita. Adotar B não fecha
a porta de A: as duas terminam no mesmo lugar, um usuário com senha própria e
`must_change_password` falso.

---

## 4. Como se desativa

**Nunca apagando.** Três razões concretas, e nenhuma é preferência:

1. `print_agents.created_by_admin_user_id` é FK para `admin_users.id`. Um
   `DELETE` violaria a constraint, e resolver com `ON DELETE SET NULL`
   apagaria a resposta de quem instalou aquele agente.
2. `order_status_history.changed_by` grava `admin:{email}` como texto. O
   registro sobrevive ao `DELETE`, mas se o e-mail for reaproveitado por outra
   pessoa depois, o histórico passa a **apontar para a pessoa errada** —
   silenciosamente, e sem jeito de descobrir.
3. `uq_admin_users_email` é único e **global**, sobre `lower(email)`, não por
   restaurante (o login recebe só e-mail e senha, sem slug). E-mail liberado é
   e-mail que pode reaparecer em outro restaurante.

**Implementado:** `PATCH /admin/users/{id}` com `is_active: false`. Efeito na
requisição seguinte, pelo motivo da seção 1. E, junto no mesmo commit,
**gravar `password_changed_at = now()` ao desativar** — sem isso, reativar a
pessoa depois ressuscita tokens antigos que ainda estejam dentro das 12h.

Três guardas que a rota precisa, e que não são simetria decorativa:

- **ninguém se desativa sozinho.** É o jeito mais fácil de um restaurante
  ficar sem dono nenhum, e a saída seria `docker exec` — exatamente o que esta
  frente existe para eliminar;
- **não dá para desativar o último `owner` ativo do restaurante.** Mesmo
  buraco, por outro caminho;
- **não dá para rebaixar o último `owner` ativo.** O terceiro caminho para o
  mesmo buraco, e o mais fácil de esquecer.

**`DELETE` não deve existir na API.** Se um dia for preciso apagar de verdade
(pedido de titular sob LGPD), é script, com o mesmo cuidado do
`customer_anonymization_service`.

---

## 5. O `print_agent`

É conta de **máquina**. A senha dela fica em texto puro no `config.ini` do
computador do balcão, e é por isso que o papel existe separado: ele alcança as
oito rotas de que o agente precisa (`AGENTE_DE_IMPRESSAO` /
`PESSOAS_E_AGENTE`) e mais nenhuma. Com `attendant`, quem lesse aquele arquivo
levava junto o preço do cardápio, a lista de clientes com telefone e o
faturamento.

**Ele já não entra no painel** — não por uma regra sobre login, mas porque
nenhuma rota de tela o aceita. Isso está certo e não precisa mudar. O que a
frente nova pode estragar é justamente isso, de três jeitos:

1. **Oferecer `print_agent` no seletor de papel.** A tela vai querer a lista
   de papéis; se ela vier de `ADMIN_USER_ROLES`, "agente de impressão" aparece
   como cargo de gente. **Proponho uma constante nova — `PAPEIS_DE_PESSOA =
   ("owner", "manager", "attendant")` —, e que o `POST`/`PATCH` de usuário
   RECUSE `print_agent` no corpo**, com 422. A tela é conveniência; a recusa é
   a regra.
2. **Deixar criar `print_agent` sem `branch_id`.** O script já barra isso, com
   o motivo escrito: não existe a máquina de todas as lojas, e sem filial o
   heartbeat responde 400 e o painel nunca mostra o agente como online. Se um
   dia a rota aceitar o papel, essa validação tem que vir junto — mas o item 1
   torna a questão discutível.
3. **Listar as contas de máquina misturadas com as pessoas.**
   `GET /admin/users` deve trazer as pessoas. Os agentes já têm tela própria
   em `/admin/printing`, que é onde eles fazem sentido — com nome de
   impressora e estado de heartbeat ao lado.

**Onde o `print_agent` continua nascendo, então?** No script. E isso é
decisão, não omissão: criar um agente é parte de uma instalação física —
alguém está na loja, com a máquina na frente, editando o `config.ini`. Uma
tela que crie a conta sem que ninguém esteja lá cria conta órfã.

---

## 6. As rotas

Todas `SOMENTE_DONO`, com a linha correspondente em
`tests/test_papeis_das_rotas.py` — que é o teste que faz rota nova sem papel
nascer vermelha.

| Rota | O que faz |
|---|---|
| `GET /admin/users` | lista as pessoas do restaurante (`print_agent` fora). O repositório já existe |
| `POST /admin/users` | cria com nome, e-mail, papel e filial; devolve a senha temporária **uma vez** |
| `PATCH /admin/users/{id}` | nome, papel, filial, `is_active` |
| `POST /admin/users/{id}/reset-password` | gera nova temporária e revoga os tokens da pessoa |

**Por que `SOMENTE_DONO` inclusive para ler.** A lista diz quem tem acesso ao
faturamento do restaurante. É o mapa de quem atacar, e não é informação que o
balcão precise para atender.

**Escopo:** `restaurant_id` sai do token, sempre. `{id}` de outro restaurante
responde **404, não 403** — 403 confirmaria que aquele usuário existe.

O corpo nunca traz `restaurant_id` nem `password_hash`. E a resposta nunca
traz `password_hash` — vale escrever, porque o jeito rápido de montar o
schema é `ConfigDict(from_attributes=True)` com os campos da tabela.

**Custo de contrato:** quatro rotas novas no `/openapi.json`, que o painel
consome. Nada renomeado, nada removido — é adição pura.

---

## 7. O que fica de fora, e por quê

- **Convite por e-mail** — seção 3, opção A. Continua fora: fica para quando o
  painel tiver sua primeira página pública.
- **Auditoria de quem criou/desativou quem.** É desejável, e é uma tabela
  nova; misturar com esta frente dobra o tamanho dela. Vale como frente
  própria, junto com o resto da auditoria do painel.
- **2FA.** Fora de escopo, e a conversa muda inteira por causa do
  `print_agent`, que não tem segundo fator possível.
- **Papel novo** (um "gerente de pessoas" que cadastre usuário sem ser dono).
  Enquanto o dono é uma pessoa por restaurante, é complexidade sem demanda.

---

## 8. As três decisões, e o que foi feito de cada uma

Decididas em 23/08/2026.

### 1. Senha temporária (B), não convite por e-mail (A)

**Decidido: B.** O argumento de escopo decidiu — A exige uma página pública em
outro repositório, com deploy próprio, para uma frente que cabe na área
autenticada. E o caso do "quem não tem e-mail" é real: atendente de balcão
muitas vezes não tem e-mail que ele mesmo acesse, e o dono acabaria cadastrando
com o próprio.

Os dois detalhes que vinham junto foram confirmados e implementados:

- **`must_change_password` é coluna nova** (revisão `20260823_0037`), e não
  `password_changed_at IS NULL`. Aquele nulo é o estado de todo lojista antigo;
  reusá-lo poria a plataforma inteira numa tela de troca de senha no dia do
  deploy.
- **Segunda via é gerar outra**, nunca "me mostra de novo" — que seria uma rota
  que devolve a senha de outra pessoa.

**Além do que a proposta dizia:** a obrigação de trocar é aplicada no
**backend**, não só na tela. Com `must_change_password` verdadeiro, só
`GET /admin/auth/me` e `PATCH /admin/auth/password` abrem; o resto responde
403. Sem isso a limitação seria enfeite — quem interceptasse a senha no
WhatsApp chamaria a API direto. O sinal que o painel obedece é o
`must_change_password` que o login e o `/me` devolvem; o 403 é a rede embaixo.

**A porta de A continua aberta.** As duas terminam no mesmo lugar (um usuário
com senha própria e `must_change_password` falso), e metade do backend de A —
token assinado com `purpose`, envio por Resend — já está escrita.

### 2. `print_agent` nasce só pelo script

**Decidido: sim**, e a rota recusa o papel no corpo com 422 que diz o motivo.
`PAPEIS_DE_PESSOA` é constante própria em `core/constants.py`, separada de
`ADMIN_USER_ROLES` — a primeira espelha uma decisão de produto, a segunda
espelha o CHECK da tabela.

A conta de máquina também **não aparece** no `GET /admin/users` e responde
**404** no `PATCH` e no `reset-password`: não aparecer na lista mas ser
editável por id seria a porta lateral da mesma decisão.

### 3. `GET /admin/users` é `SOMENTE_DONO` inclusive para ler

**Decidido: sim.** E a pergunta que dependia disso foi respondida: **o gerente
não precisa ver a equipe.** Com um restaurante de duas filiais, quem cadastra é
o dono.

Se um dia virar necessidade, é **rota diferente com outro recorte** — filial em
vez de restaurante, e sem e-mail na resposta. Afrouxar o papel destas entregaria
o restaurante inteiro para resolver uma pergunta sobre uma loja.
