import json
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sympy as sp
from grader import X, grade, parse_expression

COURSE = json.loads((ROOT / "course.json").read_text(encoding="utf-8"))
QUESTIONS = {q["id"]: q for q in COURSE["questions"]}


class ContentTests(unittest.TestCase):
    def test_question_roles_and_sequence(self):
        self.assertEqual(len(COURSE["diagnostic_order"]), 1)
        self.assertEqual(len(COURSE["training_order"]), 4)
        self.assertEqual(len(COURSE["post_order"]), 2)
        self.assertEqual([QUESTIONS[qid]["phase"] for qid in COURSE["training_order"]], ["guided", "guided", "transfer", "transfer"])
        ids = COURSE["diagnostic_order"] + COURSE["training_order"] + COURSE["post_order"] + [COURSE["foundation"]["question_id"]]
        self.assertEqual(len(set(ids)), 8)
        self.assertEqual(set(ids), set(QUESTIONS))

    def test_all_question_answers_mathematically_valid(self):
        for question in QUESTIONS.values():
            with self.subTest(qid=question["id"]):
                derivative = sp.diff(parse_expression(question["answer"]), X)
                self.assertEqual(sp.simplify(derivative - parse_expression(question["integrand"])), 0)
                result = grade(question["answer"], question)
                self.assertEqual(result["verdict"], "correct")
                self.assertTrue(result["answer_complete"])

    def test_example_is_mathematically_valid(self):
        example = COURSE["unit"]["example"]
        self.assertEqual(sp.simplify(sp.diff(parse_expression(example["answer"]), X) - parse_expression(example["integrand"])), 0)
        self.assertTrue(grade(example["answer"], example)["answer_complete"])

    def test_logarithm_argument_is_positive(self):
        self.assertEqual(sp.expand((X + sp.Rational(1, 2)) ** 2 + sp.Rational(3, 4)), X ** 2 + X + 1)
        self.assertTrue(((X + sp.Rational(1, 2)) ** 2 + sp.Rational(3, 4)).is_positive)

    def test_all_mathml_is_well_formed_and_has_no_named_entities(self):
        fragments = []
        for question in QUESTIONS.values():
            fragments.extend([question["formula_html"], question["answer_html"]])
        fragments.extend([COURSE["unit"]["example"]["question_html"], COURSE["unit"]["example"]["answer_html"]])
        for fragment in fragments:
            with self.subTest(fragment=fragment[:100]):
                root = ET.fromstring(fragment)
                self.assertEqual(root.tag, "{http://www.w3.org/1998/Math/MathML}math")
                self.assertNotRegex(fragment, r"&[A-Za-z]+;")

    def test_each_question_has_a_complete_explanation_and_layered_hints(self):
        for question in QUESTIONS.values():
            with self.subTest(qid=question["id"]):
                self.assertGreaterEqual(len(question["solution_steps"]), 3)
                self.assertGreaterEqual(len(question["hint_steps"]), 2)
                self.assertTrue(all(isinstance(text, str) and text.strip() for text in question["solution_steps"] + question["hint_steps"]))
                self.assertIn("求导", "".join(question["solution_steps"]))
        self.assertGreaterEqual(len(COURSE["unit"]["knowledge"]), 5)

    def test_post_questions_are_disjoint_from_training_and_example(self):
        shown = [QUESTIONS[qid]["integrand"] for qid in COURSE["training_order"] + COURSE["diagnostic_order"]]
        shown.append(COURSE["unit"]["example"]["integrand"])
        for qid in COURSE["post_order"]:
            self.assertNotIn(QUESTIONS[qid]["integrand"], shown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
