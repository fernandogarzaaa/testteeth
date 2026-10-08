from __future__ import annotations

import ast
import textwrap

import pytest

from testteeth.operators import ALL_OPERATORS, apply_mutant, generate_mutants, iter_functions


def mutants_of(source: str, operators: set[str] | None = None):
    source = textwrap.dedent(source).lstrip("\n")
    return source, generate_mutants(source, "mod.py", frozenset(operators) if operators else None)


def replacements(source: str, operator: str) -> list[tuple[str, str]]:
    _, ms = mutants_of(source, {operator})
    return [(m.original, m.replacement) for m in ms]


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("a < b", "(a <= b)"), ("a <= b", "(a < b)"), ("a > b", "(a >= b)"), ("a >= b", "(a > b)"),
        ("a == b", "(a != b)"), ("a != b", "(a == b)"), ("a is b", "(a is not b)"),
        ("a is not b", "(a is b)"), ("a in b", "(a not in b)"), ("a not in b", "(a in b)"),
    ],
)
def test_comparison_flips(expr, expected):
    assert replacements(f"def f(a, b):\n    return {expr}\n", "comparison") == [(expr, expected)]


def test_chained_comparison_mutates_each_operator_once():
    got = replacements("def f(a, b, c):\n    return a < b <= c\n", "comparison")
    assert got == [("a < b <= c", "(a <= b <= c)"), ("a < b <= c", "(a < b < c)")]


def test_boolean_and_or_and_not():
    got = replacements("def f(a, b):\n    return not (a and b) or a\n", "boolean")
    assert ("not (a and b) or a", "(not (a and b) and a)") in got
    assert ("not (a and b)", "(a and b)") in got
    assert ("a and b", "(a or b)") in got
    assert len(got) == 3


@pytest.mark.parametrize(
    ("op", "swapped"), [("+", "-"), ("-", "+"), ("*", "/"), ("/", "*"), ("//", "*"), ("%", "//"), ("**", "*")]
)
def test_arithmetic_swaps(op, swapped):
    got = replacements(f"def f(a, b):\n    return a {op} b\n", "arithmetic")
    assert got == [(f"a {op} b", f"(a {swapped} b)")]


def test_augmented_assignment_swap_keeps_statement():
    source, ms = mutants_of("def f(x):\n    x += 2\n    return x\n", {"arithmetic"})
    assert [m.replacement for m in ms] == ["x -= 2"]
    assert apply_mutant(source, ms[0]) == "def f(x):\n    x -= 2\n    return x\n"


def test_unsupported_operators_are_ignored():
    assert replacements("def f(a, b):\n    return a @ b | a\n", "arithmetic") == []
    assert replacements("def f(x):\n    x |= 1\n", "arithmetic") == []


def test_constants_off_by_one_and_bool_flip():
    got = replacements("def f():\n    return [0, 7, 2.5, True, False, None, 'txt']\n", "constant")
    assert got == [("0", "1"), ("7", "8"), ("2.5", "3.5"), ("True", "False"), ("False", "True")]


def test_return_value_replaced_with_none_but_not_when_already_none():
    got = replacements("def f(x):\n    if x:\n        return None\n    return x\n", "return-value")
    assert got == [("return x", "return None")]
    assert replacements("def f():\n    return\n", "return-value") == []


def test_remove_raise_records_exception():
    _, ms = mutants_of("def f(x):\n    if x < 0:\n        raise ValueError('neg')\n    raise\n", {"remove-raise"})
    assert [(m.replacement, m.exception, m.context) for m in ms] == [
        ("pass", "ValueError", "if x < 0:"),
        ("pass", "", ""),
    ]
    assert "bare re-raise" in ms[1].description


def test_swallow_exception_replaces_whole_handler_body():
    source, ms = mutants_of(
        """
        def f(g):
            try:
                return g()
            except (KeyError, TimeoutError) as exc:
                log_it = 1
                raise RuntimeError('boom') from exc
        """,
        {"swallow-exception"},
    )
    (m,) = ms
    assert m.exception == "(KeyError, TimeoutError)"
    assert m.context == "except (KeyError, TimeoutError):"
    mutated = apply_mutant(source, m)
    assert "raise RuntimeError" not in mutated and "log_it" not in mutated
    assert "        pass\n" in mutated
    compile(mutated, "m", "exec")


def test_trivial_handlers_are_not_swallowed():
    src = "def f(g):\n    try:\n        g()\n    except KeyError:\n        pass\n    try:\n        g()\n    except:\n        ...\n"
    assert replacements(src, "swallow-exception") == []


def test_remove_call_skips_logging_and_print():
    src = """
    import logging
    logger = logging.getLogger()
    def f(cache, key):
        print("x")
        logger.info("x")
        logging.warning("x")
        cache.add(key)
        retry_later(key)
    """
    _, ms = mutants_of(src, {"remove-call"})
    assert [m.call for m in ms] == ["cache.add", "retry_later"]
    assert all(m.replacement == "pass" for m in ms)


def test_remove_call_handles_await():
    _, ms = mutants_of("async def f(c):\n    await c.flush()\n", {"remove-call"})
    assert [m.call for m in ms] == ["c.flush"]


def test_docstrings_annotations_fstrings_and_main_guard_are_skipped():
    src = '''
    """Module doc 1."""
    LIMIT: int = 3

    class A:
        """Class doc 2."""
        size: int = 4

        def m(self, x: int = 5) -> int:
            """Method doc 6."""
            return f"{x + 7}"

    if __name__ == "__main__":
        print(1 + 8)
    '''
    _, ms = mutants_of(src)
    replaced = {m.original for m in ms}
    assert replaced == {"3", "4", "5", 'return f"{x + 7}"'}
    method = [m for m in ms if m.original == "5"][0]
    assert method.function == "A.m"


def test_function_and_context_attribution():
    _, ms = mutants_of(
        """
        X = 1
        class Box:
            def outer(self, n):
                def inner(k):
                    return k > 1
                while n > 2:
                    n -= 1
                for i in range(n):
                    pass
                else:
                    n = 3
                return inner(n)
        """,
        {"comparison", "constant"},
    )
    where = {(m.original, m.function, m.context) for m in ms}
    assert ("1", "<module>", "") in where
    assert ("k > 1", "Box.outer.inner", "") in where
    assert ("n > 2", "Box.outer", "") in where
    assert ("3", "Box.outer", "") in where
    funcs = {m.function: m.function_line for m in ms}
    assert funcs["Box.outer.inner"] == 4 and funcs["Box.outer"] == 3


def test_else_branch_context():
    _, ms = mutants_of("def f(x):\n    if x:\n        y = 1\n    else:\n        y = 2\n    return y\n", {"constant"})
    assert [m.context for m in ms] == ["if x:", "else branch of `if x:`"]


def test_ids_are_sequential_and_file_scoped():
    _, ms = mutants_of("def f(a):\n    return a + 1 < 2\n")
    assert [m.id for m in ms] == [f"mod.py#{i}" for i in range(1, len(ms) + 1)]


def test_apply_mutant_handles_multibyte_characters_before_span():
    source = "def f(x):\n    s = 'héllo ✓'; return x < 3\n"
    ms = generate_mutants(source, "u.py", frozenset({"comparison"}))
    assert apply_mutant(source, ms[0]) == "def f(x):\n    s = 'héllo ✓'; return (x <= 3)\n"


def test_multiline_expression_mutation_stays_valid():
    source = "def f(a, b):\n    return (a\n            + b)\n"
    (m,) = generate_mutants(source, "m.py", frozenset({"arithmetic"}))
    mutated = apply_mutant(source, m)
    assert "a - b" in mutated
    ns: dict = {}
    exec(compile(mutated, "m", "exec"), ns)
    assert ns["f"](5, 3) == 2


def test_mutants_that_do_not_compile_are_dropped():
    # Swapping the operator of a walrus-free aug-assign is fine; replacing a `return` value inside a
    # generator with `return None` stays valid; nothing here should produce invalid code.
    source = "def g():\n    yield 1\n    return 2\n"
    for m in generate_mutants(source, "g.py"):
        compile(apply_mutant(source, m), "g", "exec")


def test_invalid_mutant_is_filtered(monkeypatch):
    from testteeth import operators

    real = operators.apply_mutant
    monkeypatch.setattr(operators, "apply_mutant", lambda s, m: "def (:" if m.operator == "constant" else real(s, m))
    ms = generate_mutants("def f(x):\n    return x < 1\n", "f.py")
    assert {m.operator for m in ms} == {"comparison", "return-value"}


def test_lambda_and_comprehension_are_walked():
    src = "def f(xs):\n    g = lambda v: v + 1\n    return [x for x in xs if x > 0]\n"
    got = {(m.operator, m.original) for m in generate_mutants(src, "c.py")}
    assert ("arithmetic", "v + 1") in got and ("comparison", "x > 0") in got


def test_all_operators_listed():
    assert set(ALL_OPERATORS) == {
        "comparison", "boolean", "arithmetic", "constant", "return-value", "remove-raise",
        "swallow-exception", "remove-call",
    }


def test_iter_functions_qualnames():
    tree = ast.parse(
        "def a():\n    def b():\n        pass\nclass C:\n    async def d(self):\n        pass\nif True:\n    def e():\n        pass\n"
    )
    assert [name for name, _ in iter_functions(tree)] == ["a", "a.b", "C.d", "e"]
