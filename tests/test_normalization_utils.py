"""Caracterizacao de `utils/normalization.py`.

TESTE DE CARACTERIZACAO: descreve o que o codigo faz HOJE, nao o que deveria
fazer. Onde o comportamento atual e esquisito, o esquisito esta registrado —
com um comentario apontando o problema — em vez de corrigido. Corrigir aqui
contaminaria a rede: a partir dai ninguem sabe se uma refatoracao preservou o
comportamento ou preservou a correcao.

O que este arquivo NAO cobre, de proposito: NFC/NFD em `normalize_text` e a
imunidade do `slugify` as duas formas ja estao em `test_unicode_normalization.py`,
que e o arquivo daquele assunto. Aqui ficam o e-mail, o CPF e as bordas do slug.
"""

import pytest

from src.utils.normalization import (
    is_valid_cpf,
    is_valid_email,
    normalize_digits,
    normalize_email,
    slugify,
)


# ---------------------------------------------------------------------------
# normalize_email / normalize_digits
# ---------------------------------------------------------------------------


class TestNormalizeEmail:
    def test_it_strips_and_lowercases(self):
        assert normalize_email("  JOAO@Exemplo.COM ") == "joao@exemplo.com"

    def test_it_leaves_an_already_normalized_address_alone(self):
        assert normalize_email("joao@exemplo.com") == "joao@exemplo.com"

    def test_none_raises_attribute_error(self):
        """ESQUISITO, e registrado como esta.

        `normalize_digits` trata `None` como string vazia (`value or ""`) e
        `normalize_email` estoura. As duas moram no mesmo modulo, tem a mesma
        assinatura `(value: str)` e discordam sobre o mesmo caso.

        Nenhuma das duas deveria receber `None` — o type hint diz `str` —, mas
        uma delas perdoa e a outra nao, e quem chama nao tem como saber qual
        sem abrir o arquivo. Nao e corrigido aqui: escolher qual das duas muda
        e decisao separada.
        """
        with pytest.raises(AttributeError):
            normalize_email(None)


class TestNormalizeDigits:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("(85) 99999-9999", "85999999999"),
            ("529.982.247-25", "52998224725"),
            ("abc", ""),
            ("", ""),
        ],
    )
    def test_it_keeps_only_digits(self, entrada, esperado):
        assert normalize_digits(entrada) == esperado

    def test_none_becomes_empty_string(self):
        """O `value or ""` da funcao. Ver o teste irmao em NormalizeEmail."""
        assert normalize_digits(None) == ""


# ---------------------------------------------------------------------------
# is_valid_email
# ---------------------------------------------------------------------------


class TestIsValidEmail:
    @pytest.mark.parametrize(
        "email",
        [
            "joao@exemplo.com",
            "  JOAO@Exemplo.COM ",  # normaliza antes de conferir
            # Era "a@b.c" e passou a ser recusado: TLD de uma letra nao
            # existe, e o regex agora exige duas ou mais.
            "a@b.co",
            "joao.silva+tag@exemplo.com.br",
        ],
    )
    def test_accepted(self, email):
        assert is_valid_email(email) is True

    @pytest.mark.parametrize(
        "email",
        [
            "",
            "a@b",  # sem ponto no dominio
            "@b.c",  # sem parte local
            "a b@c.d",  # espaco no meio
            "a@@b.c",  # dois @
            "semarroba.com",
        ],
    )
    def test_rejected(self, email):
        assert is_valid_email(email) is False

    @pytest.mark.parametrize(
        "email",
        [
            "a@b..c",  # ponto duplo no dominio
            "a@b.-",  # TLD que e um hifen
            "a@-.-",  # dominio inteiro de hifens
            "a@b.c.",  # termina em ponto
            ".@b.c",  # parte local comeca com ponto
            "a.@b.co",  # parte local termina com ponto
            "a..b@c.co",  # ponto duplo na parte local
            "a@.com",  # rotulo vazio
            "a@b-.com",  # rotulo terminando em hifen
            "a@b.1",  # TLD numerico
        ],
    )
    def test_malformed_addresses_are_refused(self, email):
        """O regex antigo exigia so "um arroba e um ponto depois dele", e os
        cinco primeiros casos desta lista passavam.

        Nao era bug de seguranca — o e-mail so vale depois que o codigo de
        verificacao chega. Era pior de outro jeito: endereco invalido aceito
        no cadastro nao da erro nenhum na hora. Ele vira o codigo que nunca
        chega, e o cliente fica sem conseguir entrar sem saber por que.
        """
        assert is_valid_email(email) is False

    @pytest.mark.parametrize(
        "email",
        [
            "joana+ifood@exemplo.com",  # etiqueta: recusar seria recusar cliente
            "joana.souza@sub.exemplo.com.br",
            "nome-com-hifen@dominio-com-hifen.com.br",
            "J.Souza@Exemplo.COM",
        ],
    )
    def test_the_legitimate_shapes_still_pass(self, email):
        assert is_valid_email(email) is True

    def test_an_absurdly_long_address_is_refused(self):
        """Teto do RFC 5321. Sem ele o campo aceita uma string de megabytes,
        que atravessa validacao, banco e o corpo do e-mail de verificacao."""
        assert is_valid_email("a@" + "x" * 300 + ".com") is False


# ---------------------------------------------------------------------------
# is_valid_cpf — o algoritmo dos dois digitos verificadores, inteiro
# ---------------------------------------------------------------------------


class TestIsValidCpfAccepts:
    @pytest.mark.parametrize(
        "cpf",
        [
            "52998224725",
            "529.982.247-25",  # a pontuacao cai no normalize_digits
            "  529.982.247-25  ",  # espaco nas pontas
            "529 982 247 25",
        ],
    )
    def test_the_same_number_in_every_punctuation(self, cpf):
        assert is_valid_cpf(cpf) is True

    def test_first_check_digit_of_ten_becomes_zero(self):
        """O ramo `if first_digit == 10: first_digit = 0`.

        Sem um CPF que caia nele, metade da regra dos digitos nunca executa.
        Neste numero o resto do primeiro digito da exatamente 10.
        """
        assert is_valid_cpf("52601815906") is True

    def test_second_check_digit_of_ten_becomes_zero(self):
        """O mesmo ramo, para o segundo digito."""
        assert is_valid_cpf("76842684650") is True


class TestIsValidCpfRejects:
    @pytest.mark.parametrize("digito", list("0123456789"))
    def test_all_eleven_digits_the_same(self, digito):
        """`len(set(digits)) == 1`. 111.111.111-11 passa na conta dos digitos
        verificadores e mesmo assim nao e CPF — por isso o caso e barrado antes
        da conta, e nao por ela."""
        assert is_valid_cpf(digito * 11) is False

    @pytest.mark.parametrize(
        "cpf",
        [
            "",
            "1234567890",  # 10 digitos
            "123456789012",  # 12 digitos
            "529982247",  # 9 digitos
        ],
    )
    def test_wrong_length(self, cpf):
        assert is_valid_cpf(cpf) is False

    def test_wrong_first_check_digit(self):
        assert is_valid_cpf("52998224715") is False

    def test_wrong_second_check_digit(self):
        assert is_valid_cpf("52998224724") is False

    def test_letters_only(self):
        assert is_valid_cpf("abcdefghijk") is False


class TestIsValidCpfRejectsAnythingThatIsNotWrittenAsACpf:
    """Era aqui que a validacao aceitava lixo.

    `normalize_digits` joga fora TODO caractere que nao e digito ANTES da
    conta, entao qualquer texto com onze digitos escondidos no meio passava
    como CPF. Um campo de CPF que aceita texto arbitrario grava lixo na
    coluna, e a conferencia manual depois nao bate com nada.

    Agora a pontuacao aceita e explicita: digito, ponto, hifen e espaco.
    """

    @pytest.mark.parametrize(
        "entrada",
        [
            "a5b2c9d9e8f2g2h4i7j2k5",  # o caso do relatorio
            "529982247-25abc",
            "CPF: 529.982.247-25",
            "529982247/25",
            "<529.982.247-25>",
        ],
    )
    def test_garbage_around_the_digits_is_refused(self, entrada):
        assert is_valid_cpf(entrada) is False

    def test_the_conventional_punctuation_still_passes(self):
        """Ponto, hifen e espaco continuam valendo: e como CPF se escreve, e
        recusa-los quebraria quem cola do documento."""
        for entrada in ("529.982.247-25", "  529.982.247-25  ", "529 982 247 25"):
            assert is_valid_cpf(entrada) is True, entrada

    def test_none_is_false_and_not_an_exception(self):
        """Continua devolvendo False, como antes: o retorno cedo por `not cpf`
        cobre None e vazio, e nao o `fullmatch`, que estouraria."""
        assert is_valid_cpf(None) is False
        assert is_valid_cpf("") is False


# ---------------------------------------------------------------------------
# slugify — as bordas
# ---------------------------------------------------------------------------


class TestSlugify:
    @pytest.mark.parametrize(
        ("nome", "slug"),
        [
            ("Pizza Calabresa 30cm", "pizza-calabresa-30cm"),
            ("X-Burger  Clássico", "x-burger-classico"),
            ("Açaí 500ml!!!", "acai-500ml"),
            ("ÁÉÍÓÚ", "aeiou"),
            ("a_b.c", "a-b-c"),
        ],
    )
    def test_accents_spaces_and_punctuation_become_one_hyphen(self, nome, slug):
        assert slugify(nome) == slug

    def test_it_does_not_leave_hyphens_at_the_edges(self):
        assert slugify("-ja-") == "ja"
        assert slugify("!!! Pizza !!!") == "pizza"

    @pytest.mark.parametrize(
        "nome",
        [
            "",
            "   ",
            "---",
            "\U0001f356",  # nome so de emoji
            "Ø",  # letra sem decomposicao ASCII no NFKD
        ],
    )
    def test_a_name_with_nothing_usable_becomes_an_empty_slug(self, nome):
        """Documentado no docstring da funcao: "quem chama decide o que fazer".

        O caso do "Ø" e o menos obvio dos cinco — ele e uma LETRA, parece
        aproveitavel, e mesmo assim some: o NFKD nao o decompoe em "O" +
        acento, entao o `encode("ascii", "ignore")` o descarta inteiro.
        """
        assert slugify(nome) == ""
