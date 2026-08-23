"""O corpo do cadastro de usuário do painel. Sem banco.

O que se prova aqui é a recusa da conta de máquina, que é a regra: a tela é
conveniência, e uma tela que ofereça "agente de impressão" como cargo de gente
não pode ser corrigida pedindo à tela que não ofereça.
"""

import unittest

from pydantic import ValidationError

from src.core.constants import ADMIN_USER_ROLES, PAPEIS_DE_PESSOA, PAPEL_DE_MAQUINA
from src.schemas.admin_user_schema import (
    RECUSA_DE_MAQUINA,
    AdminUserCreate,
    AdminUserUpdate,
)


def criar(**overrides) -> AdminUserCreate:
    valores = {
        "name": "Fulano",
        "email": "fulano@exemplo.com",
        "role": "attendant",
    }
    valores.update(overrides)
    return AdminUserCreate(**valores)


class PapeisTests(unittest.TestCase):
    def test_a_constante_de_pessoa_e_a_da_tabela_menos_a_maquina(self):
        self.assertEqual(
            set(PAPEIS_DE_PESSOA) | {PAPEL_DE_MAQUINA},
            set(ADMIN_USER_ROLES),
        )

    def test_o_literal_do_schema_espelha_a_constante(self):
        """As duas formas do mesmo fato — a tupla para o codigo, o Literal para
        o OpenAPI — nao podem divergir. Divergindo, o painel monta o seletor a
        partir de uma lista que o backend nao aceita."""
        do_openapi = AdminUserCreate.model_json_schema()["properties"]["role"]["enum"]
        self.assertEqual(sorted(do_openapi), sorted(PAPEIS_DE_PESSOA))

    def test_os_tres_papeis_de_pessoa_passam(self):
        for papel in PAPEIS_DE_PESSOA:
            self.assertEqual(criar(role=papel).role, papel)


class ContaDeMaquinaTests(unittest.TestCase):
    def test_criar_com_print_agent_e_recusado(self):
        with self.assertRaises(ValidationError) as capturado:
            criar(role="print_agent")
        self.assertIn("instalacao fisica", str(capturado.exception))

    def test_editar_para_print_agent_e_recusado(self):
        """A porta lateral: criar como atendente e promover a maquina depois."""
        with self.assertRaises(ValidationError) as capturado:
            AdminUserUpdate(role="print_agent")
        self.assertIn("instalacao fisica", str(capturado.exception))

    def test_a_recusa_diz_o_MOTIVO_e_nao_so_a_lista(self):
        """O Literal sozinho responderia "Input should be 'owner', 'manager'..."
        — verdadeiro e inutil para quem tentou justamente o quarto valor."""
        self.assertIn("scripts/create_admin_user.py", RECUSA_DE_MAQUINA)


class CorpoTests(unittest.TestCase):
    def test_o_email_e_normalizado(self):
        """O UNIQUE do banco e sobre `lower(email)`: a checagem de duplicado do
        service precisa comparar a mesma coisa que o indice compara."""
        self.assertEqual(criar(email="  Fulano@Exemplo.COM ").email, "fulano@exemplo.com")

    def test_o_nome_perde_os_espacos_das_pontas(self):
        self.assertEqual(criar(name="  Fulano  ").name, "Fulano")

    def test_nome_so_de_espacos_e_recusado(self):
        """`min_length=1` confere o valor CRU; o vazio nasce do strip."""
        with self.assertRaises(ValidationError):
            criar(name="   ")

    def test_restaurant_id_no_corpo_e_recusado(self):
        """O restaurante sai do token. Aceita-lo aqui criaria o campo que a
        rota teria que desobedecer."""
        with self.assertRaises(ValidationError):
            criar(restaurant_id="00000000-0000-0000-0000-000000000000")

    def test_password_no_corpo_e_recusado(self):
        """Quem escolhe a senha e o servidor, e por isso nao ha onde manda-la."""
        with self.assertRaises(ValidationError):
            criar(password="uma-senha-qualquer")

    def test_is_active_nao_entra_na_criacao(self):
        """Criar alguem ja desativado nao e caso de uso; desativar e o PATCH."""
        with self.assertRaises(ValidationError):
            criar(is_active=False)

    def test_o_patch_distingue_campo_ausente_de_nulo(self):
        """`branch_id: null` significa "todas as filiais", e nao "nao mexa".

        E o `exclude_unset` do service que separa os dois; sem essa distincao,
        soltar a filial de alguem seria impossivel pela API.
        """
        self.assertEqual(AdminUserUpdate().model_dump(exclude_unset=True), {})
        self.assertEqual(
            AdminUserUpdate(branch_id=None).model_dump(exclude_unset=True),
            {"branch_id": None},
        )


if __name__ == "__main__":
    unittest.main()
