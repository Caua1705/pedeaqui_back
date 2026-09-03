"""Cadastra o canal de WhatsApp (Cloud API) de uma filial, ou do restaurante.

Nao existe tela para isso: enquanto a configuracao for na mao — e ela e, ate
virarmos Tech Provider —, este script e a unica forma de gravar o numero e o
token. Quando o onboarding automatico chegar, muda de onde o token CHEGA; a
linha continua sendo a mesma.

## Com ou sem --branch-slug, e a diferenca e a heranca

    --branch-slug centro     o numero DAQUELA filial
    (sem a opcao)            o numero do RESTAURANTE, usado por toda filial
                             que nao tem o seu

E o regime da armadilha 35. O Junior, que tem dois numeros (um por filial, na
mesma Business Manager), roda duas vezes COM `--branch-slug`. Um restaurante
de uma loja so roda uma vez SEM, e funciona como "um numero por restaurante".

## O token nao e argumento de linha de comando

Mesmo motivo da senha de `create_admin_user.py` e do access token do Mercado
Pago: ficaria no historico do shell e no `ps` de quem estiver na maquina. E
credencial da Business Manager DO LOJISTA, nao nossa. Vem por prompt oculto,
ou por stdin (uma linha) quando o terminal nao e interativo.

**Use o token do USUARIO DO SISTEMA, nao o de 24 horas.** O que aparece em
*WhatsApp -> Configuracao da API* no painel da Meta e temporario: com ele os
avisos param sozinhos no dia seguinte, sem nada errado do nosso lado. Ver
`scratchpad/rodada-whatsapp.md`, parte II-C.

## Rodar de novo troca o token do mesmo numero

O conflito e pelo `phone_number_id`. Recadastrar e a rotacao de token — o
caso que de fato se repete. Trocar o NUMERO de uma loja e outra coisa: apague
a linha antiga antes, senao a loja fica com dois numeros cadastrados e um
deles inalcancavel.

Uso:

    python scripts/register_whatsapp_channel.py \\
        --restaurant-slug junior-da-picanha \\
        --branch-slug centro \\
        --waba-id 1234567890 \\
        --phone-number-id 9876543210 \\
        --display-phone-number "+55 85 99999-0000"

No container (o -it e necessario para o prompt do token aparecer):

    docker exec -it pedeaqui-api python scripts/register_whatsapp_channel.py ...
"""

import argparse
import getpass
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.db.session import SessionLocal
from src.models.branch_model import Branch
from src.repositories.branch_repository import BranchRepository
from src.repositories.restaurant_repository import RestaurantRepository
from src.repositories.whatsapp_repository import WhatsAppChannelRepository
from src.utils.crypto import encrypt_whatsapp_token


def read_hidden(prompt_label: str) -> str:
    if not sys.stdin.isatty():
        # Permite automacao via `printf 'TOKEN\\n' | python
        # scripts/register_whatsapp_channel.py ...`, sem deixar o token na
        # linha de comando.
        return sys.stdin.readline().strip()
    return getpass.getpass(prompt_label).strip()


def find_branch(branches: list[Branch], slug: str) -> Branch:
    for branch in branches:
        if branch.slug == slug:
            return branch
    disponiveis = ", ".join(sorted(branch.slug for branch in branches)) or "(nenhuma)"
    raise SystemExit(
        f"Filial ativa nao encontrada para o slug '{slug}'. Disponiveis: {disponiveis}."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cadastra o numero de WhatsApp de uma filial (ou o do restaurante)."
    )
    parser.add_argument("--restaurant-slug", required=True)
    parser.add_argument(
        "--branch-slug",
        help="A filial dona deste numero. OMITIR grava o numero do RESTAURANTE, "
        "que e o usado por toda filial que nao tem o seu.",
    )
    parser.add_argument("--waba-id", required=True)
    parser.add_argument(
        "--phone-number-id",
        required=True,
        help="O 'ID do numero de telefone' da Meta. E por ele que o webhook acha a filial.",
    )
    parser.add_argument(
        "--display-phone-number",
        required=True,
        help="O numero como uma pessoa o le. Nao roteia nada; existe para a tela e o log.",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        restaurant = RestaurantRepository(db).get_active_by_slug(args.restaurant_slug)
        if restaurant is None:
            raise SystemExit(
                f"Restaurante ativo nao encontrado para o slug '{args.restaurant_slug}'."
            )

        branch = None
        if args.branch_slug:
            branch = find_branch(
                BranchRepository(db).list_active_by_restaurant(restaurant.id),
                args.branch_slug,
            )

        token = read_hidden("Access token da Meta (nao aparece na tela): ")
        if not token:
            raise SystemExit("Access token nao pode ser vazio.")

        channel = WhatsAppChannelRepository(db).upsert(
            restaurant_id=restaurant.id,
            branch_id=branch.id if branch is not None else None,
            waba_id=args.waba_id,
            phone_number_id=args.phone_number_id,
            display_phone_number=args.display_phone_number,
            access_token_encrypted=encrypt_whatsapp_token(token),
        )
        db.commit()

    print("Canal de WhatsApp cadastrado.")
    print(f"  restaurante: {restaurant.name} ({restaurant.id})")
    if branch is not None:
        print(f"  filial:      {branch.name} ({branch.slug})")
    else:
        print("  filial:      (nenhuma) — este e o numero do RESTAURANTE, herdado")
        print("               por toda filial que nao tiver o seu.")
    print(f"  waba_id:          {channel.waba_id}")
    print(f"  phone_number_id:  {channel.phone_number_id}")
    print(f"  numero:           {channel.display_phone_number}")
    print("  access_token:     (cifrado, nao exibido)")
    print()
    print(
        "Lembrete: cadastrar o canal nao liga os avisos sozinho — "
        "WHATSAPP_NOTIFICATIONS_ENABLED no .env precisa ser 'true', e os "
        "templates precisam estar aprovados na Meta."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
