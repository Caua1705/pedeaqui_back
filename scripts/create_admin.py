"""Cadastra um lojista com todos os dados na linha de comando.

Serve ao caso de provisionamento nao interativo: criar o PRIMEIRO admin de
um restaurante em um `docker exec` sem TTY, ou dentro de um script de
deploy. Nao ha tela para isso e a API tambem nao resolve — nao existe
ninguem autenticado para autorizar a criacao do primeiro usuario.

A diferenca para `create_admin_user.py` e uma so: aqui a senha e argumento
(`--password`), la ela e pedida em prompt oculto. O resto (resolver o slug,
gerar o hash, recusar e-mail repetido) e o mesmo caminho, com as mesmas
funcoes — os dois scripts nao podem divergir em regra nenhuma.

ATENCAO, e a razao de o outro script existir: uma senha em argumento fica
no historico do shell (`~/.bash_history`) e aparece no `ps` de qualquer um
logado na maquina enquanto o comando roda. Duas formas de reduzir isso:

- prefixe o comando com um espaco, se o shell estiver com
  `HISTCONTROL=ignorespace`;
- troque a senha pelo painel depois do primeiro login.

Se o terminal for interativo, prefira `create_admin_user.py`.

Uso:

    python scripts/create_admin.py \\
        --restaurant-slug junior-da-picanha \\
        --name "Junior" \\
        --email junior@exemplo.com \\
        --password 'senha-de-pelo-menos-10' \\
        --role owner

No container:

    docker exec pedeaqui-api python scripts/create_admin.py ...

Sem `-it`: nao ha prompt para responder.
"""

import argparse
import sys
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


# Mesmo piso de create_admin_user.py. Repetido e nao importado porque
# `scripts/` nao e pacote: os dois arquivos rodam como script solto.
MIN_PASSWORD_LENGTH = 8

# Papel do primeiro usuario de um restaurante. Attendant ou manager nao
# serve aqui: quem cria o restaurante precisa enxergar todas as filiais e
# poder cadastrar os outros. Continua trocavel pelo --role.
DEFAULT_ROLE = "owner"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cadastra um lojista com a senha na linha de comando."
    )
    parser.add_argument("--restaurant-slug", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--password",
        required=True,
        help=(
            "Fica no historico do shell e no ps. Prefira "
            "create_admin_user.py quando houver terminal interativo."
        ),
    )
    parser.add_argument(
        "--role",
        default=DEFAULT_ROLE,
        choices=list(ADMIN_USER_ROLES),
        help=f"Padrao: {DEFAULT_ROLE}.",
    )
    args = parser.parse_args()

    email = normalize_email(args.email)
    if len(args.password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"A senha precisa de pelo menos {MIN_PASSWORD_LENGTH} caracteres.")

    with SessionLocal() as db:
        # Slug de restaurante ATIVO: criar lojista para um restaurante
        # desligado renderia um login que autentica e nao enxerga nada.
        restaurant = RestaurantRepository(db).get_active_by_slug(args.restaurant_slug)
        if restaurant is None:
            raise SystemExit(
                f"Restaurante ativo nao encontrado para o slug '{args.restaurant_slug}'."
            )

        repository = AdminUserRepository(db)
        # O e-mail e unico e GLOBAL, nao por restaurante: o login recebe so
        # e-mail e senha, sem slug, entao o mesmo e-mail em dois
        # restaurantes nao teria como ser resolvido.
        if repository.get_by_email(email) is not None:
            raise SystemExit(f"Ja existe um lojista com o e-mail {email}.")

        try:
            # Mesma funcao que o login usa para conferir (verify_password,
            # em src/utils/security.py). Gerar o hash de outro jeito criaria
            # um usuario que nao consegue logar.
            password_hash = hash_password(args.password)
        except PasswordTooLongError:
            raise SystemExit("Senha longa demais para o bcrypt (limite de 72 bytes).")

        admin_user = AdminUser(
            restaurant_id=restaurant.id,
            # Nulo = todas as filiais do restaurante. Para `owner` o campo e
            # ignorado de qualquer forma (ver build_admin_scope); quem
            # precisa prender um manager a uma filial usa o --branch-id de
            # create_admin_user.py.
            branch_id=None,
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
        print(f"  branch_id:  {admin_user.branch_id} (nulo = todas as filiais)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
