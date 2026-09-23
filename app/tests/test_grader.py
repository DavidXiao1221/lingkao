import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import sympy as sp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grader import C, X, InputError, grade, parse_expression, preview

COURSE = json.loads((ROOT / "course.json").read_text(encoding="utf-8"))
QUESTIONS = {q["id"]: q for q in COURSE["questions"]}
# Minimal mathematical regression cases are shipped with the tests, so a fresh
# checkout does not depend on the private development workspace or old prototype.
REGRESSION = json.loads((Path(__file__).parent / "fixtures/grader_regression.json").read_text(encoding="utf-8"))
REGRESSION_QUESTIONS = {q["id"]: q for q in REGRESSION["questions"]}


class GraderTests(unittest.TestCase):
    def test_original_question_results(self):
        for question in REGRESSION["questions"]:
            with self.subTest(qid=question["id"]):
                result = grade(question["answer"], question)
                self.assertEqual(result["verdict"], "correct")
                self.assertTrue(result["answer_complete"])

    def test_original_examples(self):
        for index, example in enumerate(REGRESSION["examples"]):
            with self.subTest(example=index):
                self.assertEqual(grade(example["answer"], example)["verdict"], "correct")

    def test_equivalent_antiderivative(self):
        q = REGRESSION_QUESTIONS["parts_guided"]
        self.assertEqual(grade("x*exp(x)-exp(x)+7+C", q)["verdict"], "incorrect")
        self.assertEqual(grade("2*x*e^x-2*e^x+7+C", q)["verdict"], "correct")

    def test_new_equivalent_results(self):
        cases = {
            "substitution_guided_polynomial": "(x*x+x+1)^3/3+5+C",
            "substitution_transfer_trig": "-cos(x)^2/2+C",
            "substitution_post_log": "log(x*x+x+1)+C",
            "basic_foundation": "x*(x+1)+C",
            "substitution_transfer": "e^(x*x)/2+c",
        }
        for qid, answer in cases.items():
            with self.subTest(qid=qid, answer=answer):
                result = grade(answer, QUESTIONS[qid])
                self.assertEqual(result["verdict"], "correct")
                self.assertTrue(result["answer_complete"])

    def test_logarithm_domain_and_legal_equivalents(self):
        question = QUESTIONS["substitution_post_log"]
        for expression in ("ln(x^2+x+1)+C", "ln((x^2+x+1)^2)/2+C",
                           "-ln(1/(x^2+x+1))+C", "ln((x+0.5)^2+0.75)+C"):
            with self.subTest(expression=expression):
                result = grade(expression, question)
                self.assertEqual(result["verdict"], "correct")
                self.assertTrue(result["answer_complete"])
        result = grade("ln(-x^2-x-1)+C", question)
        self.assertEqual(result["verdict"], "incorrect")
        self.assertFalse(result["answer_complete"])

    def test_simplification_cannot_erase_invalid_original_domains(self):
        question = QUESTIONS["basic_foundation"]
        additions = ("ln(-x^2-1)-ln(x^2+1)",
                     "ln(-x^2-x-1)-ln(-x^2-x-1)",
                     "0*ln(-x^2-x-1)", "0*ln(x)", "x/x-1",
                     "0*sqrt(x)", "0*x^(-2)", "0*x^0.5",
                     "0*exp(ln(-x^2-x-1))", "sqrt(-1)^2+1")
        for addition in additions:
            with self.subTest(addition=addition):
                result = grade("x^2+x+C+" + addition, question)
                self.assertIn(result["verdict"], ("invalid", "incorrect"))
                self.assertFalse(result["answer_complete"])

    def test_unknown_domain_is_ungraded_even_after_cancellation(self):
        result = grade("x^2+x+C+0*tan(x)", QUESTIONS["basic_foundation"])
        self.assertEqual(result["verdict"], "ungraded")
        self.assertFalse(result["answer_complete"])

    def test_domain_restricted_question_requires_interval_contract(self):
        question = {"kind": "indefinite", "integrand": "1/x", "answer": "ln(x)+C"}
        result = grade(question["answer"], question)
        self.assertEqual(result["verdict"], "ungraded")
        self.assertFalse(result["answer_complete"])

    def test_numeric_literals_are_exact_before_any_ast_float_rounding(self):
        cases = {"1.0000000000000001": sp.Rational(10000000000000001, 10**16),
                 "1e-100": sp.Rational(1, 10**100), "0.125": sp.Rational(1, 8),
                 ".5": sp.Rational(1, 2), "2.5E-2": sp.Rational(1, 40),
                 "10000.": sp.Integer(10000)}
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertEqual(parse_expression(expression), expected)

    def test_tiny_nonzero_errors_never_count_as_symbolic_equivalence(self):
        question = QUESTIONS["substitution_transfer"]
        for expression in ("1.0000000000000001*exp(x^2)/2+C",
                           "exp(x^2)/2+C+1e-100*x"):
            with self.subTest(expression=expression):
                result = grade(expression, question)
                self.assertEqual(result["verdict"], "incorrect")
                self.assertFalse(result["answer_complete"])
        self.assertEqual(grade("0.5*exp(x^2)+C", question)["verdict"], "correct")

    def test_numeric_precision_bounds_fail_explicitly(self):
        for expression in ("1e-400", "1e-101", "0e101", "0." + "0" * 40 + "1",
                           "1.00000000000000000000000000000000000000001", "0x10", "1_000"):
            with self.subTest(expression=expression):
                with self.assertRaises(InputError):
                    parse_expression(expression)
                self.assertFalse(preview(expression)["valid"])
        result = grade("exp(x^2)/2+C+1e-400*x", QUESTIONS["substitution_transfer"])
        self.assertEqual(result["verdict"], "invalid")
        self.assertFalse(result["answer_complete"])

    def test_missing_constant_is_mathematically_correct_but_incomplete(self):
        result = grade("exp(x^2)", QUESTIONS["substitution_diagnostic"])
        self.assertEqual(result["verdict"], "correct")
        self.assertTrue(result["constant_missing"])
        self.assertFalse(result["answer_complete"])

    def test_cancelled_constant_does_not_count(self):
        for suffix in ("+C-C", "+0*C", "+7", "+C^2", "+sin(C)"):
            with self.subTest(suffix=suffix):
                result = grade("exp(x^2)" + suffix, QUESTIONS["substitution_diagnostic"])
                self.assertFalse(result["answer_complete"])

    def test_additive_nonzero_multiple_of_constant(self):
        result = grade("exp(x^2)-2*C+7", QUESTIONS["substitution_diagnostic"])
        self.assertTrue(result["answer_complete"])

    def test_wrong_sign(self):
        self.assertEqual(grade("x^3+cos(x)+C", REGRESSION_QUESTIONS["basic_transfer"])["verdict"], "incorrect")

    def test_wrong_coefficient(self):
        self.assertEqual(grade("exp(x^2)+C", QUESTIONS["substitution_transfer"])["verdict"], "incorrect")

    def test_definite_no_variable(self):
        for answer in ("x+C", "3+C"):
            self.assertEqual(grade(answer, REGRESSION_QUESTIONS["definite_guided"])["verdict"], "invalid")

    def test_invalid_syntax(self):
        for expression in ("2x", "sin x", "x+", "", "   ", None, 42):
            with self.subTest(expression=expression):
                self.assertEqual(grade(expression, QUESTIONS["substitution_diagnostic"])["verdict"], "invalid")

    def test_expression_security(self):
        bad = [
            '__import__("os").system("whoami")', "x.__class__", "[x for x in []]",
            "lambda:1", "9^9^9", "sin(x,y)", "sin(x=1)", "1/0", "a+1", "x=2",
            "1000000000000", "True", "open(1)", "sqrt(-1)", "ln(0)", "x//2",
            "x%2", "(1,2)", "x[0]", "sin(*x)", "sin(**x)", "x if x else 0",
            "e^101", "exp(exp(10))", "2^x", "1e1000", "x\x00",
        ]
        for expression in bad:
            with self.subTest(expression=expression):
                with self.assertRaises(InputError):
                    parse_expression(expression)

    def test_complexity_bounds(self):
        for expression in ("x" * 181, "+".join(["x"] * 50), "sin(" * 15 + "x" + ")" * 15):
            with self.subTest(expression=expression):
                with self.assertRaises(InputError):
                    parse_expression(expression)

    def test_unicode_math_input(self):
        result = grade("sin（x^3）＋C", QUESTIONS["substitution_guided"])
        self.assertTrue(result["answer_complete"])
        self.assertEqual(parse_expression("2×x÷2−π+pi"), X)

    def test_numerical_coincidence_never_proves_a_result(self):
        question = {"kind": "indefinite", "integrand": "0", "answer": "0"}
        result = grade("(x-1/3)^2*(x-2/3)^2*(x-4/3)^2*(x-5/3)^2*(x-7/3)^2", question)
        self.assertNotEqual(result["verdict"], "correct")

    def test_constant_dependent_difference_never_counts_as_correct(self):
        question = {"kind": "indefinite", "integrand": "2*x", "answer": "x^2+C"}
        self.assertNotEqual(grade("x^2+C*x", question)["verdict"], "correct")

    def test_preview_is_standalone_mathml(self):
        for expression in ("exp(x^2)+C", "2*x+pi", "sin(x)^2/2+C", "ln(x^2+x+1)"):
            with self.subTest(expression=expression):
                result = preview(expression)
                self.assertTrue(result["valid"])
                root = ET.fromstring(result["mathml"])
                self.assertEqual(root.tag, "{http://www.w3.org/1998/Math/MathML}math")
                self.assertNotIn("&InvisibleTimes;", result["mathml"])

    def test_preview_never_grades_or_differentiates(self):
        with patch("grader.grade", side_effect=AssertionError("grading is forbidden")), patch("grader.sp.diff", side_effect=AssertionError("differentiation is forbidden")), patch("grader._real_domain_status", side_effect=AssertionError("domain grading is forbidden")):
            result = preview("ln(x^2+x+1)+C")
        self.assertEqual(set(result), {"valid", "mathml"})

    def test_preview_invalid_contains_no_raw_html(self):
        result = preview('<script>alert(1)</script>')
        self.assertFalse(result["valid"])
        self.assertNotIn("<script>", result["message"])
        self.assertNotIn("mathml", result)

    def test_cli_utf8_operations(self):
        cases = [
            {"operation": "preview", "expression": "sin（x^2）＋C"},
            {"operation": "grade", "answer": "exp(x^2)+C", "question": QUESTIONS["substitution_diagnostic"]},
        ]
        for payload in cases:
            with self.subTest(operation=payload["operation"]):
                result = subprocess.run([sys.executable, str(ROOT / "grader.py")], input=json.dumps(payload, ensure_ascii=False), text=True, encoding="utf-8", capture_output=True, timeout=10, env=dict(os.environ, PYTHONIOENCODING="ascii"))
                self.assertEqual(result.returncode, 0, result.stderr)
                parsed = json.loads(result.stdout)
                self.assertTrue(parsed["valid"] if payload["operation"] == "preview" else parsed["answer_complete"])

    def test_adversarial_math_in_real_subprocess_with_existing_deadline(self):
        cases = [("ln(-x^2-x-1)+C", QUESTIONS["substitution_post_log"], "incorrect"),
                 ("ln((x^2+x+1)^2)/2+C", QUESTIONS["substitution_post_log"], "correct"),
                 ("1.0000000000000001*exp(x^2)/2+C", QUESTIONS["substitution_transfer"], "incorrect"),
                 ("exp(x^2)/2+C+1e-400*x", QUESTIONS["substitution_transfer"], "invalid")]
        for answer, question, verdict in cases:
            with self.subTest(answer=answer):
                payload = {"operation": "grade", "answer": answer, "question": question}
                result = subprocess.run([sys.executable, str(ROOT / "grader.py")], input=json.dumps(payload),
                                        text=True, encoding="utf-8", capture_output=True, timeout=4)
                self.assertEqual(result.returncode, 0, result.stderr)
                parsed = json.loads(result.stdout)
                self.assertEqual(parsed["verdict"], verdict)
                self.assertEqual(parsed["answer_complete"], verdict == "correct")


if __name__ == "__main__":
    unittest.main(verbosity=2)
