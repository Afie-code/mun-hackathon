"""A small, allowlisted Python-like language for user-authored gantry programs."""

from __future__ import annotations

import ast
import operator
import time
from typing import Callable

from gantry import GantryController, GantryError


MAX_STATEMENTS = 5_000
MAX_LOOP_ITERATIONS = 1_000
MAX_RUNTIME_SECONDS = 30
MAX_CALL_DEPTH = 20
MAX_NUMBER_MAGNITUDE = 1_000_000_000


class ProgramError(Exception):
    """A syntax, language, or gantry error suitable for display in the editor."""


class _Return(Exception):
    def __init__(self, value: object):
        self.value = value


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class SafeProgram:
    def __init__(self, controller: GantryController, log: Callable[[str], None]):
        self.controller = controller
        self.log = log
        self.functions: dict[str, ast.FunctionDef] = {}
        self.scopes: list[dict[str, object]] = [{}]
        self.steps = 0
        self.deadline = time.monotonic() + MAX_RUNTIME_SECONDS
        self.call_depth = 0

    def run(self, source: str) -> None:
        try:
            tree = ast.parse(source, mode="exec")
        except SyntaxError as exc:
            raise ProgramError(
                f"Syntax error on line {exc.lineno}: {exc.msg}"
            ) from exc
        try:
            self._execute_block(tree.body)
            self.controller.check_running()
        except GantryError as exc:
            raise ProgramError(str(exc)) from exc
        except (ArithmeticError, TypeError, ValueError, IndexError) as exc:
            raise ProgramError(f"Program error: {exc}") from exc
        except _Return as exc:
            raise ProgramError("return can only be used inside a function") from exc
        except (_Break, _Continue) as exc:
            raise ProgramError("break and continue can only be used inside a loop") from exc

    def _tick(self) -> None:
        self.steps += 1
        if self.steps > MAX_STATEMENTS:
            raise ProgramError(f"Program exceeded the {MAX_STATEMENTS}-step limit")
        if time.monotonic() > self.deadline:
            raise ProgramError(f"Program exceeded the {MAX_RUNTIME_SECONDS}-second limit")
        self.controller.check_running()

    def _execute_block(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            self._tick()
            if isinstance(statement, ast.FunctionDef):
                self._define_function(statement)
            elif isinstance(statement, ast.Assign):
                value = self._evaluate(statement.value)
                for target in statement.targets:
                    self._assign(target, value)
            elif isinstance(statement, ast.AnnAssign):
                if statement.annotation is not None:
                    raise ProgramError("Type annotations are not supported")
                if statement.value is not None:
                    self._assign(statement.target, self._evaluate(statement.value))
            elif isinstance(statement, ast.Expr):
                self._evaluate(statement.value)
            elif isinstance(statement, ast.If):
                branch = statement.body if self._evaluate(statement.test) else statement.orelse
                self._execute_block(branch)
            elif isinstance(statement, ast.For):
                self._execute_for(statement)
            elif isinstance(statement, ast.While):
                self._execute_while(statement)
            elif isinstance(statement, ast.Return):
                raise _Return(
                    self._evaluate(statement.value) if statement.value is not None else None
                )
            elif isinstance(statement, ast.Break):
                raise _Break()
            elif isinstance(statement, ast.Continue):
                raise _Continue()
            elif isinstance(statement, ast.Pass):
                continue
            else:
                raise ProgramError(
                    f"{type(statement).__name__} is not supported on line {statement.lineno}"
                )

    def _define_function(self, node: ast.FunctionDef) -> None:
        if node.decorator_list or node.returns or node.args.defaults or node.args.kw_defaults:
            raise ProgramError("Function decorators, annotations, and default values are not supported")
        if node.args.vararg or node.args.kwarg or node.args.kwonlyargs or node.args.posonlyargs:
            raise ProgramError("Functions must use plain positional parameters")
        if node.name in self._api_functions() or node.name == "range":
            raise ProgramError(f"{node.name} is a reserved function name")
        self.functions[node.name] = node

    def _execute_for(self, node: ast.For) -> None:
        if node.orelse or not isinstance(node.iter, ast.Call) or not isinstance(node.iter.func, ast.Name):
            raise ProgramError("for loops must use range(...) and cannot have an else block")
        if node.iter.func.id != "range" or node.iter.keywords:
            raise ProgramError("for loops must use range(...)")
        args = [self._evaluate(argument) for argument in node.iter.args]
        if not 1 <= len(args) <= 3 or any(type(arg) is not int for arg in args):
            raise ProgramError("range() needs one to three whole-number arguments")
        values = range(*args)
        if len(values) > MAX_LOOP_ITERATIONS:
            raise ProgramError(f"range() is limited to {MAX_LOOP_ITERATIONS} iterations")
        for value in values:
            self._tick()
            self._assign(node.target, value)
            try:
                self._execute_block(node.body)
            except _Continue:
                continue
            except _Break:
                break

    def _execute_while(self, node: ast.While) -> None:
        if node.orelse:
            raise ProgramError("while loops cannot have an else block")
        iterations = 0
        while self._evaluate(node.test):
            self._tick()
            iterations += 1
            if iterations > MAX_LOOP_ITERATIONS:
                raise ProgramError(f"while loops are limited to {MAX_LOOP_ITERATIONS} iterations")
            try:
                self._execute_block(node.body)
            except _Continue:
                continue
            except _Break:
                break

    def _evaluate(self, node: ast.expr) -> object:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (str, bool, int, float)) or node.value is None:
                return node.value
            raise ProgramError("This value type is not supported")
        if isinstance(node, ast.Name):
            return self._lookup(node.id)
        if isinstance(node, ast.Tuple):
            return tuple(self._evaluate(item) for item in node.elts)
        if isinstance(node, ast.List):
            return [self._evaluate(item) for item in node.elts]
        if isinstance(node, ast.Subscript):
            container = self._evaluate(node.value)
            index = self._evaluate(node.slice)
            if not isinstance(container, (tuple, list)) or type(index) is not int:
                raise ProgramError("Only integer indexing into tuples and lists is supported")
            return container[index]
        if isinstance(node, ast.UnaryOp):
            value = self._evaluate(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            if type(value) not in (int, float):
                raise ProgramError("Unary +/- requires a number")
            if isinstance(node.op, ast.USub):
                return self._bounded(operator.neg(value))
            if isinstance(node.op, ast.UAdd):
                return self._bounded(operator.pos(value))
            raise ProgramError("This unary operator is not supported")
        if isinstance(node, ast.BinOp):
            left, right = self._evaluate(node.left), self._evaluate(node.right)
            operations = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: operator.truediv,
                ast.FloorDiv: operator.floordiv,
                ast.Mod: operator.mod,
            }
            operation = operations.get(type(node.op))
            if operation is None or type(left) not in (int, float) or type(right) not in (int, float):
                raise ProgramError("Arithmetic operators require numbers")
            return self._bounded(operation(left, right))
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result: object = True
                for item in node.values:
                    result = self._evaluate(item)
                    if not result:
                        return result
                return result
            result = False
            for item in node.values:
                result = self._evaluate(item)
                if result:
                    return result
            return result
        if isinstance(node, ast.Compare):
            left = self._evaluate(node.left)
            comparisons = {
                ast.Eq: operator.eq,
                ast.NotEq: operator.ne,
                ast.Lt: operator.lt,
                ast.LtE: operator.le,
                ast.Gt: operator.gt,
                ast.GtE: operator.ge,
            }
            for operator_node, comparator in zip(node.ops, node.comparators):
                right = self._evaluate(comparator)
                comparison = comparisons.get(type(operator_node))
                if comparison is None:
                    raise ProgramError("This comparison operator is not supported")
                if not comparison(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ProgramError("Attribute and computed function calls are not supported")
            if node.keywords:
                raise ProgramError("Functions must use positional arguments")
            args = [self._evaluate(argument) for argument in node.args]
            return self._call(node.func.id, args)
        raise ProgramError(f"{type(node).__name__} is not supported in expressions")

    def _call(self, name: str, args: list[object]) -> object:
        self._tick()
        arities = {
            "move": 2,
            "speed": 1,
            "home": 0,
            "plant": 0,
            "harvest": 0,
            "get_position": 0,
            "wait": 1,
            "log": 1,
        }
        if name in arities:
            if len(args) != arities[name]:
                raise ProgramError(f"{name}() takes {arities[name]} argument(s)")
            if name == "move":
                self.controller.move(*args)
            elif name == "speed":
                self.controller.set_speed(args[0])
            elif name == "home":
                self.controller.home()
            elif name == "plant":
                self.controller.plant()
            elif name == "harvest":
                self.controller.harvest()
            elif name == "get_position":
                return self.controller.get_position()
            elif name == "wait":
                self.controller.wait(args[0])
            else:
                self.log(str(args[0]))
            return None
        function = self.functions.get(name)
        if function is None:
            raise ProgramError(f"Unknown function: {name}()")
        parameters = [argument.arg for argument in function.args.args]
        if len(args) != len(parameters):
            raise ProgramError(f"{name}() takes {len(parameters)} argument(s)")
        if self.call_depth >= MAX_CALL_DEPTH:
            raise ProgramError(f"Function calls are limited to {MAX_CALL_DEPTH} levels")
        self.call_depth += 1
        self.scopes.append(dict(zip(parameters, args)))
        try:
            try:
                self._execute_block(function.body)
            except _Return as result:
                return result.value
            return None
        finally:
            self.scopes.pop()
            self.call_depth -= 1

    def _assign(self, target: ast.expr, value: object) -> None:
        if isinstance(target, ast.Name):
            self.scopes[-1][target.id] = value
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            if not isinstance(value, (tuple, list)) or len(target.elts) != len(value):
                raise ProgramError("Unpacked values must have the same number of items")
            for item, item_value in zip(target.elts, value):
                self._assign(item, item_value)
            return
        raise ProgramError("Assignments must use variable names or tuple unpacking")

    def _lookup(self, name: str) -> object:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        raise ProgramError(f"Unknown variable: {name}")

    @staticmethod
    def _bounded(value: object) -> object:
        if type(value) in (int, float):
            if abs(value) > MAX_NUMBER_MAGNITUDE:
                raise ProgramError(f"Numbers are limited to +/-{MAX_NUMBER_MAGNITUDE}")
        return value

    @staticmethod
    def _api_functions() -> set[str]:
        return {
            "move",
            "speed",
            "home",
            "plant",
            "harvest",
            "get_position",
            "wait",
            "log",
        }


def run_program(source: str, controller: GantryController, log: Callable[[str], None]) -> None:
    SafeProgram(controller, log).run(source)
