"""Run with:  python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import minilang  # noqa: E402


def compile_(src):
    return minilang.compile_source(src)


def phases(result):
    return [d["phase"] for d in result["diagnostics"]]


class LexerTests(unittest.TestCase):
    def test_kinds_and_positions(self):
        tokens, diags = minilang.tokenize("let x = 10;\nprint(x <= 3);")
        self.assertEqual(diags, [])
        self.assertEqual([t.kind for t in tokens[:5]], ["LET", "IDENT", "ASSIGN", "NUMBER", "SEMI"])
        le = next(t for t in tokens if t.kind == "LE")
        self.assertEqual((le.line, le.col), (2, 9))
        self.assertEqual(tokens[-1].kind, "EOF")

    def test_comments_skipped_and_bad_chars_reported(self):
        tokens, diags = minilang.tokenize("// hi\nlet a = 1 @@ 2;")
        self.assertEqual(tokens[0].kind, "LET")
        self.assertEqual(len(diags), 1)          # adjacent bad chars merged
        self.assertEqual((diags[0].line, diags[0].col, diags[0].length), (2, 11, 2))


class ParserTests(unittest.TestCase):
    def test_precedence_and_associativity(self):
        tree = compile_("let a = 1 + 2 * 3 - 4;")["tree"]
        # (1 + (2 * 3)) - 4
        self.assertIn("value: BinOp -", tree)
        self.assertIn("left: BinOp +", tree)
        self.assertIn("right: BinOp *", tree)

    def test_not_binds_looser_than_equality(self):
        ast = compile_("let a = not 1 == 2;")["ast"]
        value = ast["children"][0]["children"][0]
        self.assertEqual(value["label"], "UnaryOp not")
        self.assertEqual(value["children"][0]["label"], "BinOp ==")

    def test_else_if_chain(self):
        r = compile_("let s = 5; if (s > 9) print(1); else if (s > 3) print(2); else print(3);")
        self.assertTrue(r["success"])
        self.assertEqual(r["output"], ["2"])

    def test_missing_semicolon_points_after_previous_token(self):
        d = compile_("let x = 10\nlet y = 2;")["diagnostics"][0]
        self.assertEqual((d["line"], d["col"]), (1, 11))
        self.assertIn("';'", d["message"])

    def test_panic_mode_reports_multiple_errors(self):
        src = (ROOT / "samples" / "errors.ml").read_text()
        r = compile_(src)
        self.assertGreaterEqual(r["stats"]["errors"], 6)
        self.assertIn("lexical", phases(r))
        self.assertIn("syntax", phases(r))

    def test_unclosed_block(self):
        r = compile_("while (1) { print(1);")
        self.assertEqual(len(r["diagnostics"]), 1)
        self.assertIn("unclosed", r["diagnostics"][0]["message"])

    def test_stray_brace_does_not_hang(self):
        r = compile_("} } let x = 1; print(x);")
        self.assertEqual(r["stats"]["errors"], 2)

    def test_empty_program(self):
        r = compile_("")
        self.assertTrue(r["success"])
        self.assertEqual(r["ast"]["children"], [])


class SemanticTests(unittest.TestCase):
    def test_scope_errors(self):
        r = compile_((ROOT / "samples" / "scope.ml").read_text())
        msgs = [d["message"] for d in r["diagnostics"]]
        self.assertEqual(phases(r), ["semantic"] * 3)
        self.assertTrue(any("undeclared variable 'inner'" in m for m in msgs))
        self.assertTrue(any("already declared" in m for m in msgs))

    def test_shadowing_allowed(self):
        r = compile_("let x = 1; { let x = 2; print(x); } print(x);")
        self.assertEqual(r["output"], ["2", "1"])


class InterpreterTests(unittest.TestCase):
    def test_samples_output(self):
        expected = {
            "factorial.ml": ["120"],
            "precedence.ml": ["14", "20", "3", "4", "6", "true"],
            "elseif.ml": ["3"],
            "primes.ml": ["2", "3", "5", "7", "11", "13", "17", "19", "23", "29"],
            "empty.ml": [],
        }
        for name, out in expected.items():
            with self.subTest(name):
                r = compile_((ROOT / "samples" / name).read_text())
                self.assertTrue(r["success"], r["diagnostics"])
                self.assertEqual(r["output"], out)

    def test_c_style_division(self):
        r = compile_("print(-7 / 2); print(-7 % 2);")
        self.assertEqual(r["output"], ["-3", "-1"])

    def test_runtime_errors(self):
        self.assertIn("division by zero", compile_("print(1 / 0);")["diagnostics"][0]["message"])
        r = compile_("while (true) { }")
        self.assertEqual(phases(r), ["runtime"])


if __name__ == "__main__":
    unittest.main()
