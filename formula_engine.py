"""
Safe AST Formula, Filter, Rule, Limit, and Weight Expression Engine for Knapset v2.

Supports:
- Row formulas:           "deal_pct = (price - ref.msrp) / ref.msrp * 100"
- Row/set predicates:     "vram >= 12 and ref.score > 20000"
                          "cpu.socket == mobo.socket and cpu.score / gpu.score < 1"
- Aggregate limits:       "sum(price) <= 1000", "sum(weight_g) >= 200", "count <= 5", "avg(rating) >= 4.2"
- Signed weight terms:    "ref.score:+0.7, price:-0.3"
"""

from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

_BINARY_OPS: Dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: Dict[type, Callable[[Any], Any]] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Not: operator.not_,
}

_CMP_OPS: Dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def _safe_contains(haystack: Any, needle: Any) -> bool:
    if haystack is None or needle is None:
        return False
    return str(needle).lower() in str(haystack).lower()


def _safe_lower(val: Any) -> str:
    return "" if val is None else str(val).lower()


def _safe_upper(val: Any) -> str:
    return "" if val is None else str(val).upper()


def _safe_clamp(val: Any, lo: Any, hi: Any) -> float:
    return max(float(lo), min(float(hi), float(val)))


def _safe_coalesce(*args: Any) -> Any:
    for arg in args:
        if arg is not None and arg != "":
            return arg
    return None


def _safe_sqrt(val: Any) -> float:
    v = float(val)
    if v < 0:
        raise ValueError("sqrt domain error")
    return math.sqrt(v)


def _safe_log(val: Any, base: Any = math.e) -> float:
    v = float(val)
    if v <= 0:
        raise ValueError("log domain error")
    return math.log(v, float(base))


_SAFE_FUNCS: Dict[str, Callable[..., Any]] = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "len": len,
    "float": float,
    "int": int,
    "str": str,
    "bool": bool,
    "sqrt": _safe_sqrt,
    "log": _safe_log,
    "clamp": _safe_clamp,
    "coalesce": _safe_coalesce,
    "contains": _safe_contains,
    "lower": _safe_lower,
    "upper": _safe_upper,
}

_LIMIT_RE = re.compile(
    r"^\s*(?:(?P<agg>sum|count|avg|mean|min|max)\s*\(\s*(?P<inner>[^)]*)\s*\)|(?P<bare_count>count))\s*(?P<op><=|>=|==|<|>)\s*(?P<rhs>.+?)\s*$",
    re.IGNORECASE,
)


def to_number(val: Any) -> Optional[float]:
    """Converts a scalar value into a finite float when possible; returns None otherwise."""
    if val is None or isinstance(val, bool):
        if isinstance(val, bool):
            return 1.0 if val else 0.0
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            f = float(s)
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    return None


def resolve_field_path(ctx: Dict[str, Any], parts: Sequence[str]) -> Any:
    """
    Resolves a dotted attribute chain against a row/set dictionary.
    Supports:
    - direct top-level / nested lookup: e.g. `price`, `ref.msrp`, `cpu.score`, `cpu.ref.score`
    - fallback into `.spec` sub-dict on candidate objects for v1 blueprint compatibility
    """
    if not parts:
        return None
    curr: Any = ctx
    for idx, part in enumerate(parts):
        if not isinstance(curr, dict):
            return None
        if part in curr:
            curr = curr[part]
        elif idx > 0 and isinstance(curr.get("spec"), dict) and part in curr["spec"]:
            curr = curr["spec"][part]
        elif idx == 0 and isinstance(curr.get("spec"), dict) and part in curr["spec"]:
            curr = curr["spec"][part]
        else:
            return None
    return curr


def _extract_attr_chain(node: ast.AST) -> List[str]:
    chain: List[str] = []
    curr = node
    while isinstance(curr, ast.Attribute):
        if curr.attr.startswith("_"):
            raise ValueError(f"Prohibited attribute '{curr.attr}' in expression")
        chain.append(curr.attr)
        curr = curr.value
    if isinstance(curr, ast.Name):
        if curr.id.startswith("_"):
            raise ValueError(f"Prohibited identifier '{curr.id}' in expression")
        chain.append(curr.id)
        chain.reverse()
        return chain
    raise ValueError("Invalid attribute chain in expression")


class SafeExpression:
    """Compiled zero-dependency sandbox-safe AST expression."""

    def __init__(self, expression: str):
        self.expression = (expression or "").strip()
        if not self.expression:
            raise ValueError("Expression must not be empty.")
        try:
            self.tree = ast.parse(self.expression, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"Invalid expression syntax '{self.expression}': {exc}") from exc
        self.roots_referenced: Tuple[str, ...] = self._validate_ast(self.tree.body)
        self._compiled: Callable[[Dict[str, Any], Optional[Dict[str, Callable[..., Any]]]], Any] = self._compile_node(self.tree.body)

    def _validate_ast(self, root: ast.AST) -> Tuple[str, ...]:
        roots: List[str] = []

        def _visit(node: ast.AST) -> None:
            if isinstance(node, ast.Attribute):
                chain = _extract_attr_chain(node)
                top = chain[0]
                if top not in _SAFE_FUNCS and top not in roots:
                    roots.append(top)
                return
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_FUNCS:
                    fn_name = getattr(node.func, "id", type(node.func).__name__)
                    raise ValueError(
                        f"Prohibited function call '{fn_name}'. Allowed: {', '.join(sorted(_SAFE_FUNCS))}"
                    )
                if node.keywords:
                    raise ValueError("Keyword arguments are not allowed in expressions.")
                for arg in node.args:
                    _visit(arg)
                return
            if isinstance(node, ast.Name):
                if node.id.startswith("_"):
                    raise ValueError(f"Prohibited identifier '{node.id}' in expression")
                if node.id not in ("True", "true", "False", "false", "None", "null") and node.id not in _SAFE_FUNCS:
                    if node.id not in roots:
                        roots.append(node.id)
                return
            if not isinstance(
                node,
                (
                    ast.Expression,
                    ast.Constant,
                    ast.List,
                    ast.Tuple,
                    ast.Set,
                    ast.UnaryOp,
                    ast.BinOp,
                    ast.BoolOp,
                    ast.Compare,
                    ast.IfExp,
                    ast.Load,
                    ast.operator,
                    ast.unaryop,
                    ast.boolop,
                    ast.cmpop,
                ),
            ):
                raise ValueError(f"Prohibited construct '{type(node).__name__}' in expression")
            for child in ast.iter_child_nodes(node):
                _visit(child)

        _visit(root)
        return tuple(roots)

    def evaluate(self, ctx: Dict[str, Any], extra_funcs: Optional[Dict[str, Callable[..., Any]]] = None) -> Any:
        return self._compiled(ctx, extra_funcs)

    def _compile_node(
        self,
        node: ast.AST,
    ) -> Callable[[Dict[str, Any], Optional[Dict[str, Callable[..., Any]]]], Any]:
        if isinstance(node, ast.Constant):
            val = node.value
            return lambda _ctx, _ef, v=val: v
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            elt_fns = tuple(self._compile_node(e) for e in node.elts)
            if isinstance(node, ast.Tuple):
                return lambda ctx, ef, fns=elt_fns: tuple(f(ctx, ef) for f in fns)
            if isinstance(node, ast.Set):
                return lambda ctx, ef, fns=elt_fns: {f(ctx, ef) for f in fns}
            return lambda ctx, ef, fns=elt_fns: [f(ctx, ef) for f in fns]
        if isinstance(node, ast.IfExp):
            test_fn = self._compile_node(node.test)
            body_fn = self._compile_node(node.body)
            else_fn = self._compile_node(node.orelse)
            return lambda ctx, ef, tf=test_fn, bf=body_fn, el=else_fn: (bf(ctx, ef) if tf(ctx, ef) else el(ctx, ef))
        if isinstance(node, ast.UnaryOp):
            op_fn = _UNARY_OPS.get(type(node.op))
            if not op_fn:
                raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
            arg_fn = self._compile_node(node.operand)
            if isinstance(node.op, ast.Not):
                return lambda ctx, ef, af=arg_fn: not bool(af(ctx, ef))
            return lambda ctx, ef, af=arg_fn, fn=op_fn: (None if (v := af(ctx, ef)) is None else fn(v))
        if isinstance(node, ast.BinOp):
            op_fn = _BINARY_OPS.get(type(node.op))
            if not op_fn:
                raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
            l_fn = self._compile_node(node.left)
            r_fn = self._compile_node(node.right)
            is_div = isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod))
            is_pow = isinstance(node.op, ast.Pow)

            def _binop(ctx: Dict[str, Any], ef: Optional[Dict[str, Callable[..., Any]]]) -> Any:
                left = l_fn(ctx, ef)
                right = r_fn(ctx, ef)
                if left is None or right is None:
                    return None
                if is_div and right == 0:
                    return None
                if is_pow and isinstance(right, (int, float)) and abs(right) > 1000:
                    raise ValueError("Exponent magnitude too large in expression.")
                try:
                    res = op_fn(left, right)
                    if isinstance(res, float) and not math.isfinite(res):
                        return None
                    return res
                except (ZeroDivisionError, TypeError, ValueError):
                    return None

            return _binop
        if isinstance(node, ast.BoolOp):
            val_fns = tuple(self._compile_node(v) for v in node.values)
            if isinstance(node.op, ast.And):
                def _and(ctx: Dict[str, Any], ef: Optional[Dict[str, Callable[..., Any]]]) -> bool:
                    for f in val_fns:
                        if not f(ctx, ef):
                            return False
                    return True
                return _and
            if isinstance(node.op, ast.Or):
                def _or(ctx: Dict[str, Any], ef: Optional[Dict[str, Callable[..., Any]]]) -> bool:
                    for f in val_fns:
                        if f(ctx, ef):
                            return True
                    return False
                return _or
            raise ValueError(f"Unsupported boolean operator: {type(node.op).__name__}")
        if isinstance(node, ast.Compare):
            l_fn = self._compile_node(node.left)
            steps = []
            for op, comp in zip(node.ops, node.comparators):
                cmp_fn = _CMP_OPS.get(type(op))
                if not cmp_fn:
                    raise ValueError(f"Unsupported comparison operator: {type(op).__name__}")
                steps.append((isinstance(op, ast.Eq), isinstance(op, ast.NotEq), cmp_fn, self._compile_node(comp)))
            steps_t = tuple(steps)

            if len(steps_t) == 1:
                is_eq, is_ne, cmp_fn, r_fn = steps_t[0]

                def _cmp1(ctx: Dict[str, Any], ef: Optional[Dict[str, Callable[..., Any]]]) -> bool:
                    left = l_fn(ctx, ef)
                    right = r_fn(ctx, ef)
                    if left is None or right is None:
                        return (left is right) if is_eq else ((left is not right) if is_ne else False)
                    try:
                        return bool(cmp_fn(left, right))
                    except TypeError:
                        return False

                return _cmp1

            def _cmp_multi(ctx: Dict[str, Any], ef: Optional[Dict[str, Callable[..., Any]]]) -> bool:
                left = l_fn(ctx, ef)
                for is_eq, is_ne, cmp_fn, r_fn in steps_t:
                    right = r_fn(ctx, ef)
                    if left is None or right is None:
                        ok = (left is right) if is_eq else ((left is not right) if is_ne else False)
                    else:
                        try:
                            ok = bool(cmp_fn(left, right))
                        except TypeError:
                            ok = False
                    if not ok:
                        return False
                    left = right
                return True

            return _cmp_multi
        if isinstance(node, ast.Call):
            fn_name = node.func.id  # validated above
            static_fn = _SAFE_FUNCS.get(fn_name)
            arg_fns = tuple(self._compile_node(a) for a in node.args)
            check_none = fn_name != "coalesce"

            def _call(ctx: Dict[str, Any], ef: Optional[Dict[str, Callable[..., Any]]]) -> Any:
                fn = (ef.get(fn_name) if ef else None) or static_fn
                if not fn:
                    raise ValueError(f"Unknown function '{fn_name}'")
                args = [f(ctx, ef) for f in arg_fns]
                if check_none and any(a is None for a in args):
                    return None
                try:
                    return fn(*args)
                except (ValueError, TypeError, ZeroDivisionError):
                    return None

            return _call
        if isinstance(node, ast.Attribute):
            chain = tuple(_extract_attr_chain(node))
            if len(chain) == 2:
                r0, r1 = chain
                def _attr2(ctx: Dict[str, Any], _ef: Optional[Dict[str, Callable[..., Any]]]) -> Any:
                    sub = ctx.get(r0)
                    if isinstance(sub, dict):
                        if r1 in sub:
                            return sub[r1]
                        sp = sub.get("spec")
                        if isinstance(sp, dict) and r1 in sp:
                            return sp[r1]
                        return None
                    return resolve_field_path(ctx, chain)
                return _attr2
            return lambda ctx, _ef, ch=chain: resolve_field_path(ctx, ch)
        if isinstance(node, ast.Name):
            nid = node.id
            if nid in ("True", "true"):
                return lambda _ctx, _ef: True
            if nid in ("False", "false"):
                return lambda _ctx, _ef: False
            if nid in ("None", "null"):
                return lambda _ctx, _ef: None

            def _name(ctx: Dict[str, Any], _ef: Optional[Dict[str, Callable[..., Any]]]) -> Any:
                if nid in ctx:
                    return ctx[nid]
                sp = ctx.get("spec")
                if isinstance(sp, dict) and nid in sp:
                    return sp[nid]
                return None

            return _name
        raise ValueError(f"Unsupported AST construct '{type(node).__name__}'")


@dataclass(frozen=True)
class FormulaSpec:
    """Named row or set-level derived formula `name = expression`."""
    name: str
    expression: str
    compiled: SafeExpression

    @classmethod
    def parse(cls, spec: str) -> FormulaSpec:
        if "=" not in spec or spec.strip().startswith("="):
            raise ValueError(f"Invalid formula '{spec}'. Expected 'target_name = expression'.")
        # Ensure we split on assignment '=' rather than '=='
        m = re.match(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*=(?!=)\s*(.+)$", spec)
        if not m:
            raise ValueError(f"Invalid formula '{spec}'. Expected '<identifier> = <expression>'.")
        name, expr = m.group(1), m.group(2).strip()
        return cls(name=name, expression=expr, compiled=SafeExpression(expr))


def parse_formulas(formulas: Optional[Sequence[str] | Dict[str, str]]) -> List[FormulaSpec]:
    """Converts either a dict `{name: expr}` or list of `'name = expr'` strings into FormulaSpec list."""
    if not formulas:
        return []
    out: List[FormulaSpec] = []
    if isinstance(formulas, dict):
        for k, v in formulas.items():
            out.append(FormulaSpec(name=str(k).strip(), expression=str(v).strip(), compiled=SafeExpression(str(v))))
    else:
        for item in formulas:
            out.append(FormulaSpec.parse(str(item)))
    return out


def apply_row_formulas(rows: List[Dict[str, Any]], formulas: Sequence[FormulaSpec]) -> List[Dict[str, Any]]:
    """Computes derived columns on each row copy sequentially (later formulas may read earlier ones)."""
    if not formulas:
        return [dict(r) for r in rows]
    enriched: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for f in formulas:
            val = f.compiled.evaluate(item)
            if isinstance(val, float) and math.isfinite(val):
                item[f.name] = round(val, 6)
            else:
                item[f.name] = val
        enriched.append(item)
    return enriched


def filter_rows(rows: List[Dict[str, Any]], where_clauses: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    """Filters rows keeping only items satisfying all `--where` expressions."""
    if not where_clauses:
        return list(rows)
    compiled = [SafeExpression(w) for w in where_clauses if str(w).strip()]
    if not compiled:
        return list(rows)
    return [r for r in rows if all(bool(expr.evaluate(r)) for expr in compiled)]


@dataclass(frozen=True)
class WeightTerm:
    """Weighted scoring criterion: positive weight maximizes, negative weight minimizes."""
    expression: str
    weight: float
    compiled: SafeExpression


def parse_weights(spec: str | Dict[str, Any] | Sequence[str]) -> List[WeightTerm]:
    """
    Parses signed weight definitions:
      - "score:+0.6, price:-0.4"
      - ["score:0.6", "price:-0.4", "-latency", "+fps"]
      - {"score": 0.6, "price": -0.4}
    """
    items: List[Tuple[str, float]] = []
    if isinstance(spec, dict):
        for k, v in spec.items():
            w = float(v)
            if w != 0.0:
                items.append((str(k).strip(), w))
    else:
        raw_parts: List[str] = []
        if isinstance(spec, str):
            raw_parts = [p.strip() for p in spec.split(",") if p.strip()]
        else:
            for elem in spec:
                for p in str(elem).split(","):
                    if p.strip():
                        raw_parts.append(p.strip())
        for part in raw_parts:
            if ":" in part:
                expr_s, w_s = part.rsplit(":", 1)
                expr_s = expr_s.strip()
                w = float(w_s.strip())
            elif "=" in part and not any(op in part for op in ("==", "!=", "<=", ">=")):
                expr_s, w_s = part.rsplit("=", 1)
                expr_s = expr_s.strip()
                w = float(w_s.strip())
            elif part.startswith("-"):
                expr_s, w = part[1:].strip(), -1.0
            elif part.startswith("+"):
                expr_s, w = part[1:].strip(), 1.0
            else:
                expr_s, w = part.strip(), 1.0
            if w != 0.0:
                items.append((expr_s, w))

    terms: List[WeightTerm] = []
    seen: Dict[str, int] = {}
    for expr_s, w in items:
        if not math.isfinite(w):
            raise ValueError(f"Weight for '{expr_s}' must be finite.")
        if expr_s in seen:
            idx = seen[expr_s]
            terms[idx] = WeightTerm(expression=expr_s, weight=w, compiled=terms[idx].compiled)
        else:
            seen[expr_s] = len(terms)
            terms.append(WeightTerm(expression=expr_s, weight=w, compiled=SafeExpression(expr_s)))
    return terms


@dataclass(frozen=True)
class LimitSpec:
    """
    Resource / aggregate constraint on a basket or assembled set:
      e.g. "sum(price) <= 1000", "sum(weight_g) >= 200", "count <= 5", "avg(rating) >= 4.0".
    """
    raw: str
    agg: str  # "sum" | "count" | "avg" | "min" | "max"
    inner_expr: Optional[str]
    inner_compiled: Optional[SafeExpression]
    op: str  # "<=", "<", ">=", ">", "=="
    rhs: float

    @classmethod
    def parse(cls, expr: str) -> LimitSpec:
        m = _LIMIT_RE.match(expr or "")
        if not m:
            raise ValueError(
                f"Invalid limit '{expr}'. Examples: 'sum(price) <= 1000', 'sum(tdp) <= 500', 'count <= 4', 'avg(rating) >= 4.0'"
            )
        agg = (m.group("agg") or "count").lower()
        if agg == "mean":
            agg = "avg"
        inner_raw = (m.group("inner") or "").strip()
        op = m.group("op")
        rhs = float(SafeExpression(m.group("rhs")).evaluate({}))
        inner_compiled: Optional[SafeExpression] = None
        if agg != "count":
            if not inner_raw:
                raise ValueError(f"Aggregate '{agg}()' requires a column or expression inside parentheses.")
            inner_compiled = SafeExpression(inner_raw)
        elif inner_raw and inner_raw != "*":
            inner_compiled = SafeExpression(inner_raw)
        return cls(
            raw=expr.strip(),
            agg=agg,
            inner_expr=inner_raw or None,
            inner_compiled=inner_compiled,
            op=op,
            rhs=rhs,
        )

    def _compare(self, value: float) -> bool:
        if self.op == "<=":
            return value <= self.rhs + 1e-9
        if self.op == "<":
            return value < self.rhs - 1e-9
        if self.op == ">=":
            return value >= self.rhs - 1e-9
        if self.op == ">":
            return value > self.rhs + 1e-9
        return abs(value - self.rhs) <= 1e-6

    def evaluate_items(self, items: Sequence[Dict[str, Any]], counts: Optional[Sequence[int]] = None) -> Tuple[bool, float]:
        """Evaluates the aggregate constraint against a sequence of items (or item * count pairs)."""
        qtys = list(counts) if counts is not None else [1] * len(items)
        total_count = sum(qtys)
        if self.agg == "count":
            if self.inner_compiled is None:
                val = float(total_count)
            else:
                val = float(sum(qty for it, qty in zip(items, qtys) if qty > 0 and bool(self.inner_compiled.evaluate(it))))
            return self._compare(val), val

        assert self.inner_compiled is not None
        vals: List[Tuple[float, int]] = []
        for it, qty in zip(items, qtys):
            if qty <= 0:
                continue
            num = to_number(self.inner_compiled.evaluate(it))
            if num is not None:
                vals.append((num, qty))

        if not vals:
            agg_val = 0.0
        elif self.agg == "sum":
            agg_val = sum(v * q for v, q in vals)
        elif self.agg == "avg":
            denom = sum(q for _, q in vals)
            agg_val = sum(v * q for v, q in vals) / denom if denom > 0 else 0.0
        elif self.agg == "min":
            agg_val = min(v for v, _ in vals)
        else:  # max
            agg_val = max(v for v, _ in vals)
        return self._compare(agg_val), round(agg_val, 6)

    def evaluate_set(self, set_dict: Dict[str, Dict[str, Any]], slot_names: Optional[Sequence[str]] = None) -> Tuple[bool, float]:
        """Evaluates a limit against an assembled multi-slot dictionary."""
        slots = list(slot_names) if slot_names is not None else list(set_dict.keys())
        if self.inner_compiled is not None and any(r in set_dict for r in self.inner_compiled.roots_referenced):
            val = to_number(self.inner_compiled.evaluate(set_dict))
            if val is None:
                return False, 0.0
            return self._compare(val), round(val, 6)
        items = [set_dict[s] for s in slots if s in set_dict]
        return self.evaluate_items(items)

    def is_upper_monotone(self) -> bool:
        """True if adding items with non-negative values can only increase the left-hand side vs `<=`/`<`."""
        return self.op in ("<=", "<") and self.agg in ("sum", "count", "max")


@dataclass(frozen=True)
class RuleSpec:
    """Declarative boolean compatibility/constraint rule on a candidate set or slot assignment."""
    name: str
    expression: str
    compiled: SafeExpression
    slots_involved: Tuple[str, ...]

    @classmethod
    def parse(cls, raw: str, name: Optional[str] = None) -> RuleSpec:
        compiled = SafeExpression(raw)
        return cls(
            name=name or raw.strip(),
            expression=raw.strip(),
            compiled=compiled,
            slots_involved=compiled.roots_referenced,
        )

    def is_evaluable(self, partial_ctx: Dict[str, Any]) -> bool:
        if not self.slots_involved:
            return True
        return all(s in partial_ctx for s in self.slots_involved)

    def evaluate(self, ctx: Dict[str, Any]) -> bool:
        return bool(self.compiled.evaluate(ctx))
