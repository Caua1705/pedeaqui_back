"""Le os relatos de erro que os lojistas mandaram pelo painel.

E o outro lado de `POST /admin/error-reports`. Nao existe rota de leitura: o
destinatario do relato e a plataforma, e o painel do lojista nao tem por que
mostrar o texto que outro usuario da mesma loja escreveu.

SO LEITURA. Nenhum INSERT, UPDATE ou DDL — quem apaga relato e a retencao, em
`scripts/cleanup_idempotency_keys.py`, e apagar um a mao aqui seria decidir
por conta propria que um bug relatado deixou de existir.

**O que sai na tela e dado pessoal.** Credencial ja foi mascarada antes de o
registro existir (ver `admin_error_report_service.redigir_credenciais`), mas o
texto livre pode conter nome, telefone e endereco de cliente — o lojista
escreve o que ele ve. Nao cole a saida disto em canal compartilhado, e nao a
mande para lugar nenhum de onde ela nao possa ser apagada em 90 dias.

Uso:

    python scripts/error_reports.py
    python scripts/error_reports.py --limite 5
    docker exec pedeaqui-api python scripts/error_reports.py
"""

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import select

from src.db.session import SessionLocal
from src.models.admin_error_report_model import AdminErrorReport
from src.models.admin_user_model import AdminUser
from src.models.branch_model import Branch
from src.models.restaurant_model import Restaurant
from src.repositories.admin_error_report_repository import AdminErrorReportRepository


LIMITE_PADRAO = 20

SEPARADOR = "-" * 72


def nome_por_id(db, modelo, ids: set) -> dict:
    """`{id: name}` das linhas pedidas, numa consulta so."""
    if not ids:
        return {}
    linhas = db.scalars(select(modelo).where(modelo.id.in_(ids)))
    return {linha.id: linha.name for linha in linhas}


def nomes(db, relatos: list[AdminErrorReport]) -> tuple[dict, dict, dict]:
    """Restaurante, filial e usuario de cada relato, em tres consultas.

    Tres consultas de tamanho fixo, e nao um `joinedload` no repositorio: o
    repositorio serve tambem a rota de escrita e a varredura da retencao, que
    nao querem nome nenhum.
    """
    return (
        nome_por_id(db, Restaurant, {relato.restaurant_id for relato in relatos}),
        nome_por_id(db, Branch, {relato.branch_id for relato in relatos if relato.branch_id}),
        nome_por_id(db, AdminUser, {relato.admin_user_id for relato in relatos if relato.admin_user_id}),
    )


def imprimir(relato: AdminErrorReport, restaurantes, filiais, usuarios) -> None:
    print(SEPARADOR)
    print(f"{relato.created_at:%Y-%m-%d %H:%M} UTC   id={relato.id}")
    print(
        f"  restaurante: {restaurantes.get(relato.restaurant_id, '?')}"
        f"   filial: {filiais.get(relato.branch_id, '(todas)')}"
        f"   quem: {usuarios.get(relato.admin_user_id, '(desligado)')}"
    )
    print(f"  tela: {relato.screen or '-'}   pedido: {relato.order_number or '-'}")
    print()
    print(f"  {relato.description}")
    if relato.error_log:
        print()
        print("  --- log ---")
        for linha in relato.error_log.splitlines():
            print(f"  {linha}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Lista os relatos de erro mandados pelo painel, do mais novo para o mais velho."
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=LIMITE_PADRAO,
        help=f"Quantos relatos mostrar (padrao: {LIMITE_PADRAO}).",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        relatos = AdminErrorReportRepository(db).list_recent(args.limite)
        if not relatos:
            print("Nenhum relato de erro no banco.")
            return 0

        restaurantes, filiais, usuarios = nomes(db, relatos)
        for relato in relatos:
            imprimir(relato, restaurantes, filiais, usuarios)
        print(SEPARADOR)
        print(f"{len(relatos)} relato(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
