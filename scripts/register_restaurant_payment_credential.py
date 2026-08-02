"""Cadastra a credencial de pagamento (Mercado Pago) de um restaurante.

Nao existe tela para isso ainda (BLOCO G da Fase 2): a credencial de cada
restaurante fica em restaurant_payment_credentials, cifrada em repouso, e
enquanto nao existir painel esta e a unica forma de cadastrar. Rode uma vez
para a credencial de teste e, quando o Mercado Pago aprovar a conta do
restaurante, rode de novo com --environment production — a linha de teste
continua no banco, so MERCADOPAGO_ENVIRONMENT no .env decide qual das duas
esta ativa.

O access token e o segredo do webhook NAO sao argumento de linha de
comando, pelo mesmo motivo que a senha de scripts/create_admin_user.py nao
e: ficariam no historico do shell e no `ps` de quem estiver na maquina, e
sao credencial da CONTA DO RESTAURANTE, nao nossa. Sao pedidos em prompt
oculto, ou lidos de stdin (uma linha cada, nesta ordem) quando o terminal
nao e interativo.

O segredo do webhook e OPCIONAL no cadastro completo: a "Assinatura
secreta" so existe depois de cadastrar a Notification URL no painel do
Mercado Pago, um passo que pode acontecer depois deste script (Enter em
branco pula e mantem o que ja estava la, se houver). Sem ela cadastrada, o
webhook deste restaurante responde 503 ate ser preenchido.

Uso (cadastro completo):

    python scripts/register_restaurant_payment_credential.py \\
        --restaurant-slug junior-da-picanha \\
        --environment test \\
        --public-key TEST-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

Para atualizar SO o segredo do webhook de uma credencial ja cadastrada
(sem re-informar access_token nem public_key):

    python scripts/register_restaurant_payment_credential.py \\
        --restaurant-slug junior-da-picanha \\
        --environment test \\
        --webhook-secret-only

No container:

    docker exec -it pedeaqui-api python scripts/register_restaurant_payment_credential.py ...

O -it e necessario para o prompt do access token/segredo aparecer.
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


def read_hidden(prompt_label: str) -> str:
    if not sys.stdin.isatty():
        # Permite automacao via `printf 'TOKEN\\nWEBHOOK_SECRET\\n' | python
        # scripts/register_restaurant_payment_credential.py ...`, uma linha
        # por segredo pedido, sem deixar nenhum na linha de comando.
        return sys.stdin.readline().strip()
    return getpass.getpass(prompt_label).strip()


def register_full_credential(repository: RestaurantPaymentCredentialRepository, restaurant, args) -> None:
    public_key = (args.public_key or "").strip()
    if not public_key:
        raise SystemExit("--public-key e obrigatorio para o cadastro completo.")

    access_token = read_hidden("Access token do Mercado Pago (nao aparece na tela): ")
    if not access_token:
        raise SystemExit("Access token nao pode ser vazio.")
    access_token_encrypted = encrypt_secret(access_token)

    webhook_secret = read_hidden(
        "Segredo do webhook do Mercado Pago (Enter para pular, nao aparece na tela): "
    )
    # None (nao string vazia) e o que faz RestaurantPaymentCredentialRepository.upsert
    # NAO mexer no campo — reaproveita o que ja estava cadastrado, se houver.
    webhook_secret_encrypted = encrypt_secret(webhook_secret) if webhook_secret else None

    credential = repository.upsert(
        restaurant_id=restaurant.id,
        environment=args.environment,
        public_key=public_key,
        access_token_encrypted=access_token_encrypted,
        webhook_secret_encrypted=webhook_secret_encrypted,
    )
    print("Credencial de pagamento cadastrada.")
    print(f"  restaurant:  {restaurant.name} ({restaurant.id})")
    print(f"  environment: {credential.environment}")
    print(f"  public_key:  {credential.public_key}")
    print("  access_token: (cifrado, nao exibido)")
    if webhook_secret_encrypted is not None:
        print("  webhook_secret: (cifrado, nao exibido)")
    else:
        print(
            "  webhook_secret: NAO alterado (Enter em branco) — se este "
            "restaurante ainda nao tem um cadastrado, o webhook dele "
            "responde 503 ate ser preenchido com --webhook-secret-only."
        )
    print()
    if args.environment != "test":
        print(
            "Lembrete: cadastrar a credencial de producao nao ativa ela sozinho — "
            "MERCADOPAGO_ENVIRONMENT no .env precisa virar 'production' para o "
            "sistema passar a usa-la."
        )


def register_webhook_secret_only(repository: RestaurantPaymentCredentialRepository, restaurant, args) -> None:
    webhook_secret = read_hidden("Segredo do webhook do Mercado Pago (nao aparece na tela): ")
    if not webhook_secret:
        raise SystemExit("Segredo do webhook nao pode ser vazio.")
    webhook_secret_encrypted = encrypt_secret(webhook_secret)

    credential = repository.update_webhook_secret(
        restaurant_id=restaurant.id,
        environment=args.environment,
        webhook_secret_encrypted=webhook_secret_encrypted,
    )
    if credential is None:
        raise SystemExit(
            f"Nenhuma credencial cadastrada para {restaurant.name} ({args.environment}). "
            "Cadastre a credencial completa primeiro, sem --webhook-secret-only."
        )

    print("Segredo do webhook atualizado.")
    print(f"  restaurant:  {restaurant.name} ({restaurant.id})")
    print(f"  environment: {credential.environment}")
    print("  webhook_secret: (cifrado, nao exibido)")
    print("  access_token e public_key: nao alterados.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Cadastra a credencial de pagamento de um restaurante.")
    parser.add_argument("--restaurant-slug", required=True)
    parser.add_argument("--environment", required=True, choices=ENVIRONMENTS)
    parser.add_argument(
        "--public-key",
        help="Public key do Mercado Pago para este restaurante e ambiente. Nao e sigilosa. "
        "Obrigatorio no cadastro completo; ignorado com --webhook-secret-only.",
    )
    parser.add_argument(
        "--webhook-secret-only",
        action="store_true",
        help="Atualiza SO o segredo do webhook de uma credencial ja cadastrada, sem mexer "
        "em access_token nem em public_key. Use para preencher o segredo de uma credencial "
        "cadastrada antes deste campo existir, ou para trocar um segredo vazado.",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        restaurant = RestaurantRepository(db).get_active_by_slug(args.restaurant_slug)
        if restaurant is None:
            raise SystemExit(f"Restaurante ativo nao encontrado para o slug '{args.restaurant_slug}'.")

        repository = RestaurantPaymentCredentialRepository(db)

        if args.webhook_secret_only:
            register_webhook_secret_only(repository, restaurant, args)
        else:
            register_full_credential(repository, restaurant, args)

        db.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
