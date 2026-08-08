"""Cadastra um lojista (admin_users).

Nao existe tela de cadastro de lojista, e o primeiro admin nao pode ser
criado pela API — nao ha ninguem autenticado para autorizar a criacao. Este
script resolve o ovo-e-galinha e continua util para adicionar usuarios
enquanto a tela nao existe.

A senha NAO e argumento de linha de comando de proposito: iria parar no
histórico do shell e no `ps` de quem estiver na maquina. Ela e pedida em
prompt oculto, ou lida de stdin quando o terminal nao e interativo.

Uso:

    python scripts/create_admin_user.py \\
        --restaurant-slug junior-da-picanha \\
        --name "Junior" \\
        --email junior@exemplo.com \\
        --role owner

No container:

    docker exec -it pedeaqui-api python scripts/create_admin_user.py ...

O -it e necessario para o prompt de senha aparecer.
"""

import argparse
import getpass
import sys
import uuid
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.core.constants import ADMIN_USER_ROLES
from src.db.session import SessionLocal
from src.models.admin_user_model import AdminUser
from src.repositories.admin_user_repository import AdminUserRepository
from src.repositories.restaurant_repository import RestaurantRepository
from src.utils.normalization import normalize_email
from src.utils.security import PasswordTooLongError, hash_password


# Mesmo piso das senhas de cliente, conferido em auth_service (cadastro e
# reset) e em customer_service (troca de senha). O numero esta repetido nos
# quatro lugares; se um deles mudar, os outros tem que mudar junto.
MIN_PASSWORD_LENGTH = 8


def read_password() -> str:
    if not sys.stdin.isatty():
        # Permite `echo 'senha' | python scripts/create_admin_user.py ...`
        # em automacao, sem deixar a senha na linha de comando.
        return sys.stdin.readline().strip()

    password = getpass.getpass("Senha: ")
    confirmation = getpass.getpass("Confirme a senha: ")
    if password != confirmation:
        raise SystemExit("As senhas nao conferem.")
    return password


def main() -> int:
    parser = argparse.ArgumentParser(description="Cadastra um lojista.")
    parser.add_argument("--restaurant-slug", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", required=True, choices=list(ADMIN_USER_ROLES))
    parser.add_argument(
        "--branch-id",
        default=None,
        help="Restringe o usuario a uma filial. Omitir = todas as filiais.",
    )
    args = parser.parse_args()

    email = normalize_email(args.email)
    branch_id = uuid.UUID(args.branch_id) if args.branch_id else None

    password = read_password()
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"A senha precisa de pelo menos {MIN_PASSWORD_LENGTH} caracteres.")

    with SessionLocal() as db:
        restaurant = RestaurantRepository(db).get_active_by_slug(args.restaurant_slug)
        if restaurant is None:
            raise SystemExit(f"Restaurante ativo nao encontrado para o slug '{args.restaurant_slug}'.")

        repository = AdminUserRepository(db)
        if repository.get_by_email(email) is not None:
            raise SystemExit(f"Ja existe um lojista com o e-mail {email}.")

        try:
            password_hash = hash_password(password)
        except PasswordTooLongError:
            raise SystemExit("Senha longa demais para o bcrypt (limite de 72 bytes).")

        admin_user = AdminUser(
            restaurant_id=restaurant.id,
            branch_id=branch_id,
            name=args.name.strip(),
            email=email,
            password_hash=password_hash,
            role=args.role,
            is_active=True,
        )
        repository.create(admin_user)
        db.commit()
        db.refresh(admin_user)

        print("Lojista criado.")
        print(f"  id:         {admin_user.id}")
        print(f"  restaurant: {restaurant.name} ({restaurant.id})")
        print(f"  email:      {admin_user.email}")
        print(f"  role:       {admin_user.role}")
        print()
        print("Teste o login com:")
        print(
            "  curl -X POST https://api.pederapidex.com/admin/auth/login \\\n"
            '    -H "Content-Type: application/json" \\\n'
            f'    -d \'{{"email": "{admin_user.email}", "password": "..."}}\''
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
