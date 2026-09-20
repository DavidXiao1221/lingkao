"""Bounded AST expression parsing, symbolic result checking and MathML preview.

No user input is passed to eval, sympify, or SymPy's string parser. The server
runs this module in a separate process with a deadline and resource limits.
"""
from __future__ import annotations

import ast
import html
import json
import math
import re
import sys

import sympy as sp
from sympy.printing.mathml import mathml

X = sp.Symbol("x", real=True)
C = sp.Symbol("C", real=True)
FUNCS = {"sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "exp": sp.exp,
         "ln": sp.log, "log": sp.log, "sqrt": sp.sqrt}
NAMES = {"x": X, "C": C, "c": C, "e": sp.E, "E": sp.E, "pi": sp.pi}
MAX_LENGTH = 180


class InputError(ValueError):
    pass


def parse_expression(text: str) -> sp.Expr:
    if not isinstance(text, str) or not text.strip():
        raise InputError("请先输入表达式；不会做可以选择跳过。")
    if len(text) > MAX_LENGTH:
        raise InputError("表达式过长，请只填写最终结果（180字以内）。")
    text = text.strip().translate(str.maketrans({"−": "-", "×": "*", "÷": "/", "π": "pi", "（": "(", "）": ")", "＋": "+"})).replace("^", "**")
    if re.search(r"[^A-Za-z0-9_+*/().\-\s]", text):
        raise InputError("支持 x^2、exp(x)、sin(x)、cos(x)、ln(x)、sqrt(x)、+C；乘号请写 *。")
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, RecursionError, ValueError):
        raise InputError("格式无法识别。例如：sin(x^2)+C。乘法请写 *，函数请加括号。") from None
    if len(list(ast.walk(tree))) > 75:
        raise InputError("表达式过于复杂，请填写简化后的最终结果。")

    def visit(node, depth=0):
        if depth > 13:
            raise InputError("括号或运算嵌套过多。")
        if isinstance(node, ast.Expression):
            return visit(node.body, depth + 1)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if not math.isfinite(node.value) or abs(node.value) > 10000:
                raise InputError("数字超出此练习允许的范围。")
            return sp.Rational(str(node.value))
        if isinstance(node, ast.Name) and node.id in NAMES:
            return NAMES[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            val = visit(node.operand, depth + 1)
            return val if isinstance(node.op, ast.UAdd) else -val
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            left, right = visit(node.left, depth + 1), visit(node.right, depth + 1)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                if right == 0:
                    raise InputError("出现了除以零。")
                return left / right
            if left == sp.E:
                return checked_function(sp.exp, right)
            if not right.is_Rational or abs(right) > 6:
                raise InputError("此版本支持 -6 到 6 的常数幂；指数函数请写 exp(x)。")
            if left.is_number and abs(left) > 100:
                raise InputError("幂运算数值过大。")
            return left ** right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS:
            if len(node.args) != 1 or node.keywords:
                raise InputError("函数只能接受一个参数。")
            return checked_function(FUNCS[node.func.id], visit(node.args[0], depth + 1))
        raise InputError("存在不支持的写法。只接受数学表达式，不接受代码、赋值或额外变量。")

    def checked_function(function, arg):
        if not arg.free_symbols and arg.is_real and abs(arg) > 100:
            raise InputError("函数参数过大。")
        return function(arg)

    value = visit(tree)
    if value.has(sp.zoo, sp.oo, -sp.oo, sp.nan, sp.I):
        raise InputError("结果包含未定义值或复数；当前练习限实数范围。")
    return value


def _has_arbitrary_constant(value: sp.Expr) -> bool:
    """Recognize an additive free constant; cancelled or x-dependent C is not enough."""
    coefficient = sp.simplify(sp.diff(value, C))
    return C in value.free_symbols and coefficient.is_number and coefficient.is_real is True and coefficient.is_zero is False


def grade(answer: str, question: dict) -> dict:
    try:
        value = parse_expression(answer)
        if question["kind"] == "indefinite":
            difference = sp.simplify(sp.diff(value, X) - parse_expression(question["integrand"]))
        else:
            if value.free_symbols:
                raise InputError("这是一道定积分，最终结果应为常数，不能带 x 或 C。")
            difference = sp.simplify(value - parse_expression(question["answer"]))
        if difference == 0:
            missing = question["kind"] == "indefinite" and not _has_arbitrary_constant(value)
            message = ("原函数求导核对通过；完整书写请补上独立的积分常数 +C。" if missing else
                       "原函数求导核对通过。此判定不评价推导过程。" if question["kind"] == "indefinite" else
                       "定积分结果核对通过。此判定不评价推导过程。")
            return {"verdict": "correct", "answer_complete": not missing, "constant_missing": missing, "message": message}
        # Numerical checks only disprove an answer; coincidence never proves it.
        for point in (sp.Rational(1, 3), sp.Rational(2, 3), sp.Rational(4, 3), sp.Rational(5, 3), sp.Rational(7, 3)):
            delta = difference.subs({X: point, C: 0}).evalf()
            if delta.is_real and delta.is_finite and abs(delta) > sp.Rational(1, 100000000):
                return {"verdict": "incorrect", "answer_complete": False, "constant_missing": False,
                        "message": "结果未通过核对。检查系数、符号与复合函数；系统不据此判断你具体哪一步出错。"}
        return {"verdict": "ungraded", "answer_complete": False, "constant_missing": False,
                "message": "当前校验器无法可靠判断这一写法；已标为待核对，不记为答错。"}
    except InputError as exc:
        return {"verdict": "invalid", "answer_complete": False, "constant_missing": False, "message": str(exc)}
    except (ValueError, TypeError, OverflowError, RecursionError):
        return {"verdict": "ungraded", "answer_complete": False, "constant_missing": False,
                "message": "当前校验器无法可靠判断；请尝试等价的简洁写法或交给人工核对。"}


def _presentation(value: sp.Expr) -> str:
    # SymPy emits XML entities such as &InvisibleTimes; without declaring them.
    # Decode known entities to Unicode so these fragments are standalone MathML.
    return html.unescape(mathml(value, printer="presentation"))


def expression_html(text: str) -> str:
    return '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block">' + _presentation(parse_expression(text)) + '</math>'


def question_html(question: dict) -> str:
    integrand = _presentation(parse_expression(question["integrand"]))
    if question["kind"] == "indefinite":
        sign = '<mo>∫</mo>'
    else:
        sign = '<msubsup><mo>∫</mo>' + _presentation(parse_expression(str(question["lower"]))) + _presentation(parse_expression(str(question["upper"]))) + '</msubsup>'
    return '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><mrow>' + sign + integrand + '<mspace width="0.2em"/><mi mathvariant="normal">d</mi><mi>x</mi></mrow></math>'


def preview(expression: str) -> dict:
    """Parse only: no question, expected answer, derivative or verdict is used."""
    try:
        return {"valid": True, "mathml": expression_html(expression)}
    except InputError as exc:
        return {"valid": False, "message": str(exc)}
    except (ValueError, TypeError, OverflowError, RecursionError):
        return {"valid": False, "message": "暂时无法预览，请换用更简洁的表达式。"}


def main() -> None:
    # Explicit encoding is also set by the parent process on Windows.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    operation = "grade"
    try:
        payload = json.loads(sys.stdin.read(16384))
        operation = payload.get("operation", "grade")
        if operation == "preview":
            result = preview(payload.get("expression"))
        elif operation == "grade":
            result = grade(payload.get("answer"), payload["question"])
        else:
            raise ValueError("Unsupported operation")
    except Exception:
        result = ({"valid": False, "message": "预览未完成，请稍后重试。"} if operation == "preview" else
                  {"verdict": "ungraded", "answer_complete": False, "constant_missing": False, "message": "校验未完成，标为待核对。"})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
