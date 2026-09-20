import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

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
        with patch("grader.grade", side_effect=AssertionError("grading is forbidden")), patch("grader.sp.diff", side_effect=AssertionError("differentiation is forbidden")):
            result = preview("x^2+C")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
