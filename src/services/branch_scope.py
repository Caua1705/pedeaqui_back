"""A filial dentro do escopo do token, conferida UMA vez para toda a plataforma.

Era escrita SEIS vezes — `admin_settings_service`, `admin_printing_service`,
`admin_courier_service`, `admin_cashback_service`, `admin_menu_service` e
`admin_whatsapp_service` —, e duas dessas cópias já tinham divergido. É o
recorte de tenant, e `scripts/escopo_das_rotas.py` diz o que ele custa quando
falha: *"um restaurante lendo pedido, cliente ou faturamento de outro. Não é
500, não é lentidão — chega em silêncio e sem log."*

## As duas conferências são necessárias, e são diferentes

`ensure_branch_allowed` barra a filial que existe mas não é a deste lojista; o
repositório barra a filial de OUTRO restaurante. Uma não substitui a outra, e o
motivo é sutil: `ensure_branch_allowed` só recusa quando `scope.branch_id` está
preenchido, e para o **dono** ele é sempre nulo por desenho. Numa rota que só o
chamasse, o dono do restaurante A alcançaria a filial do restaurante B.

As duas respondem o MESMO 404. Um 403 confirmaria que aquela filial existe, e a
rota viraria oráculo de quais filiais existem na plataforma.

## Qual leitura virou a canônica, e por quê

`get_active_by_id_and_restaurant` — a das cinco cópias. A sexta
(`admin_whatsapp_service`) usava `get_by_id_and_restaurant`, que devolve a
filial **ativa ou não**, e isso mudou aqui: conectar um canal de WhatsApp a uma
filial desativada passa a responder 404.

**A decisão não é de gosto: ela está escrita no próprio repositório**, no
docstring de `get_by_id_and_restaurant`, que existe por causa da reimpressão de
comanda —

> *"Não serve para configurar nem para vender: quem escreve continua passando
> pela versão ativa, que é onde a filial desativada tem que sumir mesmo."*

Conectar um canal É configurar. A sexta cópia estava do lado errado dessa
frase, e ninguém tinha decidido isso — foi decidido por qual cópia alguém
copiou, que é exatamente o que a auditoria §3.1 chamou de invisível.

**O caminho que a filial desativada ainda precisa** é a leitura, e ele não passa
por aqui: `_find_in_scope`, no mesmo service, acha o canal pelo id e confere o
restaurante direto na linha dele. Desconectar um canal de uma loja que fechou
continua funcionando.

## Por que uma CLASSE com um método, e não uma função de módulo

Parece cerimônia e não é. `scripts/escopo_das_rotas.py` monta um grafo de
chamadas e se recusa, **de propósito**, a seguir função de módulo chamada por
nome puro — está escrito lá: *"sem receptor, não dá para saber de qual módulo
ela é [...] seguir pelo nome era exatamente o defeito que a colisão de
`list_orders` mostrou"*.

Uma função de módulo aqui deixaria a varredura sem conseguir seguir a cadeia
justamente na conferência que ela existe para vigiar — e ela reporta isso como
achado, não como "ok". `self.branch_scope.get(...)` é `self.atributo.metodo`,
que o grafo resolve pelo `__init__`.

## Os argumentos são SOMENTE por nome

`admin_menu_service` chamava `_get_branch(branch_id, scope)` — a ordem
invertida das outras cinco. Nenhum type checker separa `AdminScope` de `UUID`
num `self._get_branch(a, b)` mal copiado, e os dois seguiam sendo posicionais.

`*` na assinatura fecha essa porta de vez: a chamada trocada deixa de compilar.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.models.branch_model import Branch
from src.repositories.branch_repository import BranchRepository


class BranchScope:
    def __init__(self, db: Session):
        self.branch_repository = BranchRepository(db)

    def get(self, *, scope: AdminScope, branch_id: uuid.UUID) -> Branch:
        """A filial ATIVA daquele restaurante, se o token alcança essa loja."""
        scope.ensure_branch_allowed(branch_id)
        branch = self.branch_repository.get_active_by_id_and_restaurant(
            branch_id, scope.restaurant_id
        )
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filial não encontrada"
            )
        return branch
