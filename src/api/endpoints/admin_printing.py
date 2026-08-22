from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    AGENTE_DE_IMPRESSAO,
    GERENCIA,
    PESSOAS,
    AdminScope,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.database import get_db
from src.schemas.admin_printing_schema import (
    BranchPrintSettingsResponse,
    BranchPrintSettingsUpdate,
    CategoryPrintingSectorResponse,
    PrintAgentHeartbeatRequest,
    PrintAgentPrintersRequest,
    PrintAgentPrintersResponse,
    PrintAgentStatusResponse,
    PrintingSectorCreate,
    PrintingSectorResponse,
    PrintingSectorUpdate,
    PrintTestRequest,
    PrintTestResponse,
    ProductPrintingSectorRequest,
    ProductPrintingSectorResponse,
)
from src.services.admin_printing_service import AdminPrintingService
from src.services.print_agent_service import PrintAgentService


# Mesma regra das outras rotas /admin: restaurante sai do token. A FILIAL
# aparece no path porque o setor de impressao E de uma filial (a impressora
# esta fisicamente nela) — mas o path nao autoriza nada: o service confronta
# a filial e o setor com o escopo antes de qualquer leitura ou escrita.
router = APIRouter(prefix="/admin", tags=["admin printing"])


@router.get(
    "/branches/{branch_id}/printing-sectors",
    response_model=list[PrintingSectorResponse],
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def list_printing_sectors(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[PrintingSectorResponse]:
    """Setores de impressao da filial, ativos e inativos.

    Traz os desativados pelo mesmo motivo da listagem de categorias: quem
    desligou um setor precisa continuar vendo-o para religar.
    """
    return AdminPrintingService(db).list_sectors(scope, branch_id)


@router.post(
    "/branches/{branch_id}/printing-sectors",
    response_model=PrintingSectorResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def create_printing_sector(
    branch_id: UUID,
    payload: PrintingSectorCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> PrintingSectorResponse:
    """Cadastra uma praca desta filial ("Cozinha", "Chapa", "Bar").

    Nome repetido na mesma filial responde 409: duas impressoras chamadas
    "Cozinha" tornam impossivel saber para onde um produto foi apontado.
    """
    return AdminPrintingService(db).create_sector(scope, branch_id, payload)


@router.patch(
    "/printing-sectors/{sector_id}",
    response_model=PrintingSectorResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def update_printing_sector(
    sector_id: UUID,
    payload: PrintingSectorUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> PrintingSectorResponse:
    """Renomeia, reordena ou DESATIVA o setor (`{"is_active": false}`).

    Nao existe DELETE: `products.printing_sector_id` aponta para esta linha
    por FK, e apagar deixaria o vinculo de cada produto pendurado. Desativar
    e o que o painel chama de excluir — mesma regra do cardapio.
    """
    return AdminPrintingService(db).update_sector(scope, sector_id, payload)


@router.get(
    "/branches/{branch_id}/print-settings",
    response_model=BranchPrintSettingsResponse,
    # PESSOAS: e a mesma tela do balcao que ja lista setores e testa
    # impressora, e "por que sairam duas vias?" e pergunta de quem esta em pe
    # na maquina.
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def get_branch_print_settings(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> BranchPrintSettingsResponse:
    """Rodape e contagem de vias desta filial.

    `receipt_footer_message` e o que ESTA FILIAL gravou (nulo = herdando a
    mensagem do restaurante); `effective_receipt_footer_message` e o que vai
    sair impresso. A tela precisa dos dois — um preenche o campo de edicao, o
    outro mostra o que o cliente vai ler.
    """
    return AdminPrintingService(db).get_print_settings(scope, branch_id)


@router.patch(
    "/branches/{branch_id}/print-settings",
    response_model=BranchPrintSettingsResponse,
    # GERENCIA, e nao SOMENTE_DONO: nao e dinheiro. O que o dono controla
    # sozinho e a mensagem PADRAO da marca, que sai em
    # `PATCH /admin/settings` — e essa continua sendo dele.
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def update_branch_print_settings(
    branch_id: UUID,
    payload: BranchPrintSettingsUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> BranchPrintSettingsResponse:
    """Edicao parcial. So o que vier no corpo e alterado.

    `receipt_footer_message` tem tres estados e os tres sao usados: ausente
    nao mexe, `null` volta a HERDAR a mensagem do restaurante e `""` desliga
    o rodape NESTA loja — sem ele, a filial nao teria como recusar a campanha
    da rede.

    Zero copias e valido: a retirada normalmente nao precisa da via do
    cliente, que e a que iria grampeada na sacola.
    """
    return AdminPrintingService(db).update_print_settings(scope, branch_id, payload)


@router.patch(
    "/products/{product_id}/printing-sector",
    response_model=ProductPrintingSectorResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def set_product_printing_sector(
    product_id: UUID,
    payload: ProductPrintingSectorRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> ProductPrintingSectorResponse:
    """Aponta o produto para um setor, ou desliga a via de producao dele.

    `{"printing_sector_id": null}` nao e "campo vazio": e a instrucao de
    NAO imprimir comanda de producao para este produto — a lata que sai da
    geladeira do balcao e nao passa por praca nenhuma.
    """
    return AdminPrintingService(db).set_product_sector(scope, product_id, payload)


@router.patch(
    "/categories/{category_id}/printing-sector",
    response_model=CategoryPrintingSectorResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def set_category_printing_sector(
    category_id: UUID,
    payload: ProductPrintingSectorRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> CategoryPrintingSectorResponse:
    """Aplica o setor a TODOS os produtos da categoria de uma vez.

    E como a configuracao acontece de verdade: "toda bebida vai para o Bar".
    Produto a produto, um cardapio de 200 itens nunca sai do lugar — e
    cardapio meio configurado imprime comanda pela metade.

    Sobrescreve inclusive quem ja tinha setor; excecoes se reapontam depois
    por `PATCH /admin/products/{id}/printing-sector`. Devolve quantos
    produtos foram alterados.
    """
    return AdminPrintingService(db).set_category_sector(scope, category_id, payload)


# ---------------------------------------------------------------------------
# O agente de impressao
#
# Duas rotas sao chamadas PELO AGENTE (`/admin/print-agent/...`) e tres sao
# lidas pelo PAINEL (`/admin/branches/{id}/...`). A separacao dos prefixos e
# proposital: as do agente nao levam filial no path porque a filial dele sai
# do token — ele nao escolhe de qual loja e a maquina.
#
# E a separacao agora vale tambem no PAPEL, nos dois sentidos:
#
# - as duas do agente sao AGENTE_DE_IMPRESSAO e mais ninguem. Nao ha pessoa
#   com motivo para dizer "a maquina esta viva" ou para reescrever a lista de
#   impressoras dela; um painel que pudesse fazer isso so teria como
#   estragar o dado que a propria tela mostra.
# - as tres do painel nao sao do agente. Ele nao consulta o proprio status.
# ---------------------------------------------------------------------------


@router.post(
    "/print-agent/heartbeat",
    response_model=PrintAgentStatusResponse,
    dependencies=[Depends(exigir_papel(AGENTE_DE_IMPRESSAO))],
)
def print_agent_heartbeat(
    payload: PrintAgentHeartbeatRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> PrintAgentStatusResponse:
    """Sinal de vida do agente, com a versao instalada.

    Devolve o proprio status de volta: e barato e da ao agente como conferir,
    no log dele, que o servidor o esta vendo — o mesmo dado que a tela mostra.

    Responde 400 quando o usuario do agente nao esta preso a uma filial:
    `branch_id` nulo significa "todas as filiais", e nao existe a maquina de
    todas as lojas.
    """
    return PrintAgentService(db).heartbeat(scope, payload)


@router.post(
    "/print-agent/printers",
    response_model=PrintAgentPrintersResponse,
    dependencies=[Depends(exigir_papel(AGENTE_DE_IMPRESSAO))],
)
def report_print_agent_printers(
    payload: PrintAgentPrintersRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> PrintAgentPrintersResponse:
    """As impressoras instaladas naquela maquina, como o Windows as ve.

    A lista SUBSTITUI a anterior. Existe para o painel oferecer um seletor
    em vez de um campo de texto: o nome precisa casar byte a byte com o do
    Windows, e um espaco a mais digitado a mao faz a via nao sair sem
    nenhum erro aparecer.
    """
    return PrintAgentService(db).report_printers(scope, payload)


@router.get(
    "/branches/{branch_id}/print-agent",
    response_model=PrintAgentStatusResponse,
    # PESSOAS: "o agente esta no ar?" e a primeira pergunta de quem esta no
    # balcao com a impressora parada.
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def get_print_agent_status(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> PrintAgentStatusResponse:
    """Ultimo sinal e versao do agente daquela filial.

    Filial que nunca instalou o agente responde 200 com `is_online=false` e
    o resto nulo — nao 404. "Ninguem instalou aqui" e uma resposta que a
    tela precisa poder mostrar.
    """
    return PrintAgentService(db).get_status(scope, branch_id)


@router.get(
    "/branches/{branch_id}/printers",
    response_model=PrintAgentPrintersResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def list_print_agent_printers(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> PrintAgentPrintersResponse:
    """As impressoras que o agente daquela filial reportou, a padrao primeiro."""
    return PrintAgentService(db).list_printers(scope, branch_id)


@router.post(
    "/branches/{branch_id}/print-test",
    response_model=PrintTestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    # PESSOAS: e o botao de quem esta EM PE na impressora tentando descobrir
    # por que a comanda nao saiu. Custa uma tira de bobina e responde a
    # pergunta na hora; exigir o gerente para isso e transformar um teste de
    # dez segundos numa ligacao telefonica.
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def request_print_test(
    branch_id: UUID,
    payload: PrintTestRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> PrintTestResponse:
    """Manda uma via de teste para a maquina daquela filial.

    **202 e nao 200**: o comando foi enfileirado, e quem imprime e o agente
    quando o stream entregar. Se ele estiver desligado, a via sai quando ele
    voltar — por isso a resposta leva `agent_is_online`, para a tela avisar
    antes de o lojista ficar olhando a impressora.
    """
    return PrintAgentService(db).request_print_test(scope, branch_id, payload)
