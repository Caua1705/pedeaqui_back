"""Cadastra a credencial de pagamento (Mercado Pago) de um restaurante.

Nao existe tela para isso ainda (BLOCO G da Fase 2): a credencial de cada
restaurante fica em restaurant_payment_credentials, cifrada em repouso, e
enquanto nao existir painel esta e a unica forma de cadastrar. Rode uma vez
para a credencial de teste e, quando o Mercado Pago aprovar a conta do
restaurante, rode de novo com --environment production — a linha de teste
continua no banco, so MERCADOPAGO_ENVIRONMENT no .env decide qual das duas
esta ativa.

O access token NAO e argumento de linha de comando, pelo mesmo motivo que a
senha de scripts/create_admin_user.py nao e: ficaria no historico do shell e
no `ps` de quem estiver na maquina, e e credencial da CONTA DO RESTAURANTE,
nao nossa. E pedido em prompt oculto, ou lido de stdin quando o terminal nao
e interativo.

Uso:

    python scripts/register_restaurant_payment_credential.py \\
        --restaurant-slug junior-da-picanha \\
        --environment test \\
        --public-key TEST-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

No container:

    docker exec -it pedeaqui-api python scripts/register_restaurant_payment_credential.py ...

O -it e necessario para o prompt do access token aparecer.
"""

import argparse
import getpass
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.db.session import SessionLocal
from src.repositories.restaurant_payment_credential_repository import (
    RestaurantPaymentCredentialRepository,
)
from src.repositories.restaurant_repository import RestaurantRepository
from src.utils.crypto import encrypt_secret


ENVIRONMENTS = ("test", "production")


def read_access_token() -> str:
    if not sys.stdin.isatty():
        # Permite `echo 'TOKEN' | python scripts/register_restaurant_payment_credential.py ...`
        # em automacao, sem deixar o token na linha de comando.
        return sys.stdin.readline().strip()
    return getpass.getpass("Access token do Mercado Pago (nao aparece na tela): ").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Cadastra a credencial de pagamento de um restaurante.")
    parser.add_argument("--restaurant-slug", required=True)
    parser.add_argument("--environment", required=True, choices=ENVIRONMENTS)
    parser.add_argument(
        "--public-key",
        required=True,
        help="Public key do Mercado Pago para este restaurante e ambiente. Nao e sigilosa.",
    )
    args = parser.parse_args()

    public_key = args.public_key.strip()
    if not public_key:
        raise SystemExit("Public key nao pode ser vazia.")

    access_token = read_access_token()
    if not access_token:
        raise SystemExit("Access token nao pode ser vazio.")

    # Confere a chave de cifra ANTES de tocar no banco: melhor falhar aqui
    # do que descobrir na primeira cobranca que a credencial gravada nao
    # decifra com a chave do ambiente.
    access_token_encrypted = encrypt_secret(access_token)

    with SessionLocal() as db:
        restaurant = RestaurantRepository(db).get_active_by_slug(args.restaurant_slug)
        if restaurant is None:
            raise SystemExit(f"Restaurante ativo nao encontrado para o slug '{args.restaurant_slug}'.")

        repository = RestaurantPaymentCredentialRepository(db)
        credential = repository.upsert(
            restaurant_id=restaurant.id,
            environment=args.environment,
            public_key=public_key,
            access_token_encrypted=access_token_encrypted,
        )
        db.commit()

        print("Credencial de pagamento cadastrada.")
        print(f"  restaurant:  {restaurant.name} ({restaurant.id})")
        print(f"  environment: {credential.environment}")
        print(f"  public_key:  {credential.public_key}")
        print("  access_token: (cifrado, nao exibido)")
        print()
        if args.environment != "test":
            print(
                "Lembrete: cadastrar a credencial de producao nao ativa ela sozinho — "
                "MERCADOPAGO_ENVIRONMENT no .env precisa virar 'production' para o "
                "sistema passar a usa-la."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
