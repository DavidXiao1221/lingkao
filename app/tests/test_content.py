"""Release content checks, independent of learner data and the UI state engine."""
import itertools
import json
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sympy as sp
from sympy.calculus.util import continuous_domain
from grader import (C, X, _parse_expression, _real_domain_status, grade,
                    parse_expression, question_html)

COURSE = json.loads((ROOT / "course.json").read_text(encoding="utf-8"))
LEGACY = json.loads((ROOT / "legacy_course.json").read_text(encoding="utf-8"))
QUESTIONS = {q["id"]: q for q in COURSE["questions"]}


class ContentTests(unittest.TestCase):
    def test_question_roles_and_sequence(self):
        self.assertEqual(COURSE["id"], "calculus-integrals")
        self.assertEqual(COURSE["version"], "0.3-alpha.1")
        self.assertEqual([unit["id"] for unit in COURSE["units"]],
                         ["basic", "substitution", "parts", "definite"])
        self.assertEqual(len(COURSE["questions"]), 32)
        self.assertEqual(len(QUESTIONS), 32)
        all_ids = []
        for unit in COURSE["units"]:
            with self.subTest(unit=unit["id"]):
                self.assertEqual(len(unit["diagnostic_order"]), 1)
                self.assertEqual(len(unit["training_order"]), 4)
                self.assertEqual(len(unit["post_order"]), 2)
                self.assertEqual(len(unit["delayed_order"]), 1)
                self.assertEqual([QUESTIONS[qid]["phase"] for qid in unit["training_order"]],
                                 ["guided", "guided", "transfer", "transfer"])
                for role, pool in (("diagnostic_order", "diagnostic"),
                                   ("training_order", "practice"),
                                   ("post_order", "post"), ("delayed_order", "delayed")):
                    for qid in unit[role]:
                        self.assertEqual(QUESTIONS[qid]["pool"], pool)
                        self.assertEqual(QUESTIONS[qid]["skill"], unit["id"])
                        all_ids.append(qid)
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertEqual(set(all_ids), set(QUESTIONS))
        self.assertEqual(sum(q["pool"] == "practice" for q in QUESTIONS.values()), 16)

    def test_all_question_and_example_answers_mathematically_valid(self):
        items = list(QUESTIONS.values()) + [u["example"] for u in COURSE["units"]]
        self.assertEqual(len(items), 36)
        for item in items:
            with self.subTest(qid=item.get("id", "example:" + item["integrand"])):
                answer = parse_expression(item["answer"])
                integrand = parse_expression(item["integrand"])
                if item["kind"] == "indefinite":
                    self.assertEqual(sp.simplify(sp.diff(answer, X) - integrand), 0)
                    self.assertIn(C, answer.free_symbols)
                else:
                    lower, upper = (parse_expression(str(item[key])) for key in ("lower", "upper"))
                    self.assertTrue(lower.is_finite and upper.is_finite)
                    self.assertLess(lower, upper)
                    self.assertFalse(answer.free_symbols)
                    # Verify definite answers independently of stored grading keys.
                    expected = sp.integrate(integrand, (X, lower, upper))
                    self.assertEqual(sp.simplify(answer - expected), 0)
                    interval = sp.Interval(lower, upper)
                    self.assertEqual(continuous_domain(integrand, X, interval), interval)
                result = grade(item["answer"], item)
                self.assertEqual(result["verdict"], "correct", result)
                self.assertTrue(result["answer_complete"])

    def test_all_indefinite_items_are_supported_on_the_whole_real_line(self):
        for item in list(QUESTIONS.values()) + [u["example"] for u in COURSE["units"]]:
            if item["kind"] != "indefinite":
                continue
            for key in ("integrand", "answer"):
                with self.subTest(qid=item.get("id", "example"), field=key):
                    _, requirements = _parse_expression(item[key])
                    self.assertIs(_real_domain_status(requirements), True)

    def test_every_question_rejects_a_wrong_result_and_requires_correct_constant_policy(self):
        for q in QUESTIONS.values():
            with self.subTest(qid=q["id"]):
                if q["kind"] == "indefinite":
                    wrong = q["answer"] + "+x"
                    incomplete = grade(q["answer"].removesuffix("+C"), q)
                    self.assertEqual(incomplete["verdict"], "correct")
                    self.assertTrue(incomplete["constant_missing"])
                    self.assertFalse(incomplete["answer_complete"])
                    equivalent = q["answer"] + "+7"
                else:
                    wrong = q["answer"] + "+1"
                    self.assertEqual(grade(q["answer"] + "+C", q)["verdict"], "invalid")
                    self.assertEqual(grade(q["answer"] + "+x", q)["verdict"], "invalid")
                    equivalent = q["answer"] + "+1-1"
                self.assertEqual(grade(wrong, q)["verdict"], "incorrect")
                self.assertTrue(grade(equivalent, q)["answer_complete"])

    def test_structurally_equivalent_answers_in_all_four_units(self):
        cases = {
            "basic_foundation": "x*(x+1)+C",
            "basic_transfer_polynomial": "x*x*x*x-3*x*x+2*x+C",
            "substitution_transfer_trig": "-cos(x)^2/2+C",
            "substitution_post_log": "ln((x+0.5)^2+0.75)+C",
            "substitution_delayed": "2*(x^2+4)^0.5+C",
            "parts_guided": "x*exp(x)-exp(x)+C",
            "parts_transfer_scaled": "x*exp(2*x)/2-exp(2*x)/4+C",
            "parts_post_repeated": "exp(x)*(x*(x*x-3*x+6)-6)+C",
            "definite_transfer_exponential": "e-1",
            "definite_post_product": "e",
            "definite_post_substitution": "log(2)",
            "definite_delayed": "pi*0.5",
        }
        for qid, answer in cases.items():
            with self.subTest(qid=qid):
                self.assertTrue(grade(answer, QUESTIONS[qid])["answer_complete"])

    def test_published_question_identity_and_mathematical_evidence_are_preserved(self):
        for previous in LEGACY["questions"]:
            current = QUESTIONS[previous["id"]]
            with self.subTest(qid=previous["id"]):
                self.assertEqual(current["version"], LEGACY["version"])
                for key in ("id", "skill", "kind", "integrand", "answer", "source",
                            "hint_steps", "solution_steps", "formula_html", "answer_html"):
                    self.assertEqual(current[key], previous[key], key)
                if previous["id"] != "basic_foundation":
                    self.assertEqual(current["phase"], previous["phase"])
        self.assertEqual(QUESTIONS["basic_foundation"]["phase"], "guided")
        current_example = next(u["example"] for u in COURSE["units"] if u["id"] == "substitution")
        for key, value in LEGACY["unit"]["example"].items():
            self.assertEqual(current_example[key], value)

    def test_content_metadata_and_sources_are_complete_and_honest(self):
        sources = {source["id"]: source for source in COURSE["sources"]}
        self.assertIsNone(COURSE["school"])
        self.assertIsNone(COURSE["term"])
        self.assertIn("通用", COURSE["title"])
        self.assertEqual(COURSE["source_priority"],
                         ["past_exam", "assignment", "slides", "textbook", "original"])
        self.assertTrue(all(s["type"] == "original" for s in sources.values()))
        refs = [q["ref"] for q in QUESTIONS.values()]
        self.assertEqual(len(refs), len(set(refs)))
        for q in QUESTIONS.values():
            with self.subTest(qid=q["id"]):
                self.assertRegex(q["ref"], r"^[a-f0-9]{20}$")
                self.assertNotIn(q["id"], q["ref"])
                self.assertTrue(q["title"].strip())
                self.assertIn(q["difficulty"], ("基础", "进阶"))
                self.assertIs(type(q["minutes"]), int)
                self.assertGreater(q["minutes"], 0)
                self.assertIn("不是任何高校真题", q["source"])
                self.assertTrue(q["source_refs"])
                for ref in q["source_refs"]:
                    self.assertIn(ref["source_id"], sources)
                    self.assertTrue(ref["locator"].strip())
        unit_ids = {u["id"] for u in COURSE["units"]}
        for unit in COURSE["units"]:
            self.assertGreaterEqual(len(unit["knowledge"]), 5)
            self.assertGreaterEqual(len(unit["example"]["steps"]), 3)
            self.assertTrue(set(unit["prerequisites"]).issubset(unit_ids - {unit["id"]}))
            self.assertTrue(unit["goal"] and unit["rule"] and unit["boundary"])

    def test_all_mathml_is_well_formed_and_generated_from_current_math(self):
        fragments = []
        for q in QUESTIONS.values():
            fragments.extend((q["formula_html"], q["answer_html"]))
            self.assertEqual(q["formula_html"], question_html(q))
        for unit in COURSE["units"]:
            example = unit["example"]
            fragments.extend((example["question_html"], example["answer_html"]))
            self.assertEqual(example["question_html"], question_html(example))
        for fragment in fragments:
            with self.subTest(fragment=fragment[:100]):
                root = ET.fromstring(fragment)
                self.assertEqual(root.tag, "{http://www.w3.org/1998/Math/MathML}math")
                self.assertNotRegex(fragment, r"&[A-Za-z]+;")
                self.assertNotRegex(fragment, r"(?i)<script|\bon[a-z]+\s*=")

    def test_explanations_include_layered_hints_and_an_independent_check(self):
        for q in QUESTIONS.values():
            with self.subTest(qid=q["id"]):
                self.assertGreaterEqual(len(q["solution_steps"]), 3)
                self.assertGreaterEqual(len(q["hint_steps"]), 2)
                self.assertTrue(all(isinstance(text, str) and text.strip()
                                    for text in q["solution_steps"] + q["hint_steps"]))
                self.assertIn("求导", "".join(q["solution_steps"]))

    def test_assessment_integrands_are_mathematically_distinct_from_public_content(self):
        # Compare expressions: factor order or polynomial expansion cannot hide
        # a duplicate. Use no real learning records in this content-only test.
        public = [q for q in QUESTIONS.values() if q["pool"] == "practice"]
        public += [u["example"] for u in COURSE["units"]]
        reserved = [q for q in QUESTIONS.values() if q["pool"] != "practice"]
        parsed = {q["integrand"]: parse_expression(q["integrand"])
                  for q in public + reserved}
        for assessment, shown in itertools.product(reserved, public):
            with self.subTest(reserved=assessment["id"], public=shown.get("id", "example")):
                difference = parsed[assessment["integrand"]] - parsed[shown["integrand"]]
                self.assertNotEqual(sp.simplify(difference), 0)
        for first, second in itertools.combinations(reserved, 2):
            with self.subTest(first=first["id"], second=second["id"]):
                self.assertNotEqual(sp.simplify(parsed[first["integrand"]] -
                                               parsed[second["integrand"]]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
