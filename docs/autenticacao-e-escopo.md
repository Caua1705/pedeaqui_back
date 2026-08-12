# Autenticação e escopo

Três credenciais diferentes circulam por esta API: token de cliente, token de
lojista e ticket de stream. Todos JWT HS256, todos com um campo `purpose` que os
separa. **Não há refresh token**: expirou, loga de novo.

---

## 1. Token de cliente

| | |
|---|---|
| Emitido em | `POST /auth/login` → `services/auth_service.py` |
| Segredo | `CUSTOMER_AUTH_SECRET` |
| Claims | `sub` = customer_id, `purpose` = `customer_access`, `type` = `customer` |
| Validade | `CUSTOMER_ACCESS_TOKEN_MINUTES`, padrão 10080 min = **7 dias** |
| Verificado em | `api/dependencies/customer_auth.py` |

**Obrigatório** (`get_current_customer`): todas as rotas `/customers/me/*`
(perfil, endereços, histórico, detalhe de pedido, cashback) e
`POST /restaurants/{slug}/coupons/preview`. Sem token → 401; conta inativa → 403.

**Opcional** (`get_optional_current_customer`): criação de pedido, estimativa de
entrega e listagem de cupons disponíveis. Token ruim aqui **não dá erro** —
devolve `None` e a rota segue como visitante.

> É por isso que um token expirado no checkout produz o sintoma "meu cupom
> sumiu" e não "sessão expirada": sem cliente, o cupom recusa com 401 lá no
> fundo e a lista de cupons omite os que exigem login.

O token também é conferido contra `customers.password_changed_at`: JWT emitido
antes da última troca de senha é recusado, mesmo dentro da validade. É o que
permite ao cliente expulsar quem entrou na conta dele — antes, um token roubado
sobrevivia à troca de senha por até 7 dias.

O que o token de cliente **não** permite: nada em `/admin/*`. O `purpose` é
conferido dentro de `decode_signed_token`.

---

## 2. Token de lojista

| | |
|---|---|
| Emitido em | `POST /admin/auth/login` → `services/admin_auth_service.py` |
| Segredo | `ADMIN_AUTH_SECRET` — obrigatório e diferente do de cliente |
| Claims | `sub` = admin_user_id, `purpose` = `admin_access`, `type` = `admin`, + `restaurant_id` e `role` (**informativos**) |
| Validade | `ADMIN_ACCESS_TOKEN_MINUTES`, padrão 720 min = **12h**, um turno |
| Verificado em | `api/dependencies/admin_auth.py` |

A verificação, em ordem: assinatura e expiração, `purpose = admin_access`,
`type = admin`, `sub` é UUID válido, **usuário recarregado do banco**, usuário
ainda ativo.

**Recarregar do banco a cada requisição é o ponto importante.** O `restaurant_id`
que autoriza é o da linha em `admin_users`, não o do token. Desativar um lojista
ou movê-lo de restaurante tem efeito imediato — o `restaurant_id` dentro do token
existe só para o painel se orientar.

### O login não vaza quais e-mails existem

`verify_password` roda **mesmo sem usuário encontrado**, senão o tempo do bcrypt
denunciaria quais e-mails estão cadastrados. Não achou e senha errada devolvem
401 com a mensagem idêntica.

O mesmo cuidado existe em `POST /auth/forgot-password`, do lado do cliente:
resposta sempre igual, mesmo perfil de consultas ao banco nos dois caminhos, e um
piso de latência para não vazar a existência do e-mail pelo tempo de resposta.
Falhas internas são logadas, não propagadas.

---

## 3. Ticket de stream

`POST /admin/orders/stream-ticket` → JWT com `purpose = admin_stream_ticket`,
válido por **30 segundos**.

Existe porque o `EventSource` do navegador **não envia cabeçalho**: o stream só
pode ser autenticado pela URL, e o token de 12h não pode ir para a querystring
(log de proxy, `Referer`, histórico do navegador).

---

## 4. Dois segredos, e o `purpose` só separa dentro de cada um

**Cliente e lojista assinam com chaves diferentes**, e é obrigatório que seja
assim: `ADMIN_AUTH_SECRET` não tem default, e valor igual ao de
`CUSTOMER_AUTH_SECRET` derruba o boot.

Havia fallback para o segredo de cliente, e ele era falha real: os dois públicos
compartilhando chave fazem um token forjado de um lado valer do outro. O
`purpose` não salva — ele viaja **dentro** do token que o atacante assina.

O que o `purpose` faz é separar os usos que **compartilham** a mesma chave: o
token de lojista de 12h e o ticket de stream de 30s são os dois assinados com
`ADMIN_AUTH_SECRET`, e um não vale pelo outro.

---

## 5. Escopo do lojista

Duas camadas, e as duas são necessárias.

### Camada 1 — por restaurante

**O `restaurant_id` vem sempre do token, nunca da URL.** Nenhuma rota `/admin`
aceita restaurante como parâmetro hoje. A listagem de pedidos aceitava um slug na
URL e isso foi corrigido: uma rota que *recebe* restaurante é uma rota que pode
esquecer de conferi-lo.

Divergência responde **404, não 403** — um 403 confirmaria que aquele
`restaurant_id` existe.

### Camada 2 — por filial

Mora em **um lugar só**: `api/dependencies/admin_scope.py`, e chega às rotas como
`AdminScope`.

```
owner                   → vê e edita todas as filiais, mesmo com branch_id preenchido
manager / attendant     → presos à filial quando branch_id está preenchido
                          branch_id nulo = todas as filiais
```

`AdminScope` oferece dois métodos:

- `ensure_branch_allowed(branch_id)` — barra a filial que existe mas não é deste
  lojista;
- `resolve_branch_filter(requested_branch_id)` — a filial a usar no `WHERE` de uma
  listagem. O filtro que o painel manda na querystring **só restringe, nunca
  amplia**: um lojista preso a uma filial que pedir outra recebe 404.

O que **não tem filial** continua sendo do restaurante inteiro, para qualquer
papel: cardápio (`categories`/`products` só têm `restaurant_id`), cupons e
`restaurant_settings`. Quem precisar de restrição por filial ali precisa antes de
uma coluna de filial nessas tabelas.

### Duas armadilhas de estar em um lugar só

1. **O stream SSE não usa `Depends(get_admin_scope)`.** Ele autentica por ticket
   na querystring e por isso chama `build_admin_scope` na mão
   (`api/endpoints/admin_orders.py`). Se a regra de escopo mudar, os dois caminhos
   precisam continuar iguais — foi para isso que `build_admin_scope` foi separada
   da dependência.
2. **A filial no path não autoriza nada.** Em `/admin/branches/{branch_id}/...`, o
   `branch_id` da URL é conferido contra o escopo dentro do service
   (`AdminSettingsService._get_branch`, `AdminPrintingService._get_branch`), com
   duas checagens distintas: o repositório barra a filial de **outro restaurante**
   (pela junção), e `ensure_branch_allowed` barra a filial que **este lojista** não
   enxerga. Mesmo 404 para as duas.

---

## 6. Rate limit

`api/rate_limit.py`. O IP sai do header `x-real-ip`
(`RATE_LIMIT_CLIENT_IP_HEADER`), não do socket — atrás do Traefik o socket é o
proxy.

| Rota | Limite |
|---|---|
| `POST /auth/login` | `10/minute` |
| `POST /admin/auth/login` | `10/minute;60/hour` |
| `POST /auth/forgot-password` | `5/minute;20/hour` |
| `POST /restaurants/{slug}/orders` | `10/minute;60/hour` |
| consulta pública de pedido | `30/minute` |
| iniciar pagamento | `15/minute;60/hour` |
| `POST /chat` | `20/minute;200/hour` |
| `POST /chat/feedback` | `30/minute` |

**As rotas `/admin` não têm rate limit**, exceto o login. Elas só exigem JWT — o
login de lojista já é limitado, que é a porta de força bruta.

Sem `REDIS_URL` o contador é em memória do processo: com N workers o limite
efetivo vira N × o configurado. Avisado no boot.

---

## 7. Criar um lojista

Não há tela de cadastro, e o primeiro admin não pode nascer pela API — não há
ninguém autenticado para autorizar.

```bash
docker exec -it pedeaqui-api python scripts/create_admin_user.py \
  --restaurant-slug junior-da-picanha \
  --name "Junior" \
  --email junior@exemplo.com \
  --role owner
```

O `-it` é necessário: a senha é pedida em prompt oculto e **não** é argumento de
linha de comando de propósito — iria para o histórico do shell e para o `ps`.

Mínimo de **8 caracteres** (`MIN_PASSWORD_LENGTH` em `scripts/create_admin_user.py`).
Papéis: `owner`, `manager`, `attendant`.

Local, com o venv ativo:
`py scripts/create_admin_user.py --restaurant-slug ... --role owner`.

**Não há gestão de usuários do painel por rota.** O `role` existe e delimita
filial, mas nenhuma rota é restrita *por papel* — attendant escreve o mesmo que
owner dentro do escopo dele.
