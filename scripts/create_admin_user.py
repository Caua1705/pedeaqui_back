"""Cadastra um lojista (admin_users), ou troca a senha de um existente.

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

Trocar a senha de quem ja existe (e derrubar os tokens dele):

    python scripts/create_admin_user.py --reset-password \\
        --email junior@exemplo.com

O `--reset-password` grava `password_changed_at`, entao todo token de 12h
emitido antes morre na hora — e para isso que ele serve quando a suspeita e
de vazamento. O lojista comum nao precisa deste script: existe
`PATCH /admin/auth/password`. Este caminho e para quem perdeu a senha e para
a rotacao feita pelo operador do servidor.

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

from src.core.constants import ADMIN_USER_ROLES, MIN_ADMIN_PASSWORD_LENGTH
from src.db.session import SessionLocal
from src.models.admin_user_model import AdminUser
from src.repositories.admin_user_repository import AdminUserRepository
from src.repositories.restaurant_repository import RestaurantRepository
from src.utils.normalization import normalize_email
from src.utils.security import PasswordTooLongError, hash_password, utcnow


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


def reset_password(email: str, password: str) -> int:
    """Grava a senha nova e revoga os tokens em circulacao daquele lojista."""
    with SessionLocal() as db:
        repository = AdminUserRepository(db)
        admin_user = repository.get_by_email(email)
        if admin_user is None:
            raise SystemExit(f"Nao existe lojista com o e-mail {email}.")

        try:
            admin_user.password_hash = hash_password(password)
        except PasswordTooLongError:
            raise SystemExit("Senha longa demais para o bcrypt (limite de 72 bytes).")

        # A linha que revoga. Sem ela a senha muda e o token roubado continua
        # abrindo o painel pelas horas que faltam para ele expirar.
        admin_user.password_changed_at = utcnow()
        db.add(admin_user)
        db.commit()

        print("Senha trocada.")
        print(f"  id:    {admin_user.id}")
        print(f"  email: {admin_user.email}")
        print(f"  role:  {admin_user.role}")
        print()
        print("Todo token emitido antes de agora foi revogado.")
        print("Se este usuario e o do agente de impressao, atualize o config.ini")
        print("da maquina do balcao e reinicie o servico.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Cadastra um lojista, ou troca a senha dele.")
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="Troca a senha de um lojista existente e revoga os tokens dele.",
    )
    parser.add_argument("--restaurant-slug")
    parser.add_argument("--name")
    parser.add_argument("--role", choices=list(ADMIN_USER_ROLES))
    parser.add_argument(
        "--branch-id",
        default=None,
        help="Restringe o usuario a uma filial. Omitir = todas as filiais.",
    )
    args = parser.parse_args()

    email = normalize_email(args.email)

    password = read_password()
    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        raise SystemExit(
            f"A senha precisa de pelo menos {MIN_ADMIN_PASSWORD_LENGTH} caracteres."
        )

    if args.reset_password:
        return reset_password(email, password)

    faltando = [
        nome
        for nome, valor in (
            ("--restaurant-slug", args.restaurant_slug),
            ("--name", args.name),
            ("--role", args.role),
        )
        if not valor
    ]
    if faltando:
        raise SystemExit(f"Para criar um lojista faltam: {', '.join(faltando)}.")

    branch_id = uuid.UUID(args.branch_id) if args.branch_id else None

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
