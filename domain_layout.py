"""
Domain Layout Module.

Defines the architectural layout and structural blueprint of a multi-component set:
- Component Sections
- Cross-section Compatibility Rules & Constraints
- Display & Table Layout Formatters
- Dynamic Currency Integration
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from currency import canonical_currency, convert_currency, format_price, get_active_currency


@dataclass
class SectionDefinition:
    """
    Defines a structural slot/section in a synthesized set (e.g. GPU, CPU; or Rod, Reel).
    """
    id: str
    name: str
    label: str
    description: str = ""
    required: bool = True
    budget_share_min: float = 0.0
    budget_share_max: float = 1.0
    pareto_limit: int = 24
    spec_fields: Tuple[str, ...] = ()
    row_formatter: Optional[Callable[[Dict[str, Any], str], Tuple[str, str, str, str, str]]] = None

    def format_row(
        self,
        candidate: Dict[str, Any],
        discount_fmt: Callable[[float], str],
    ) -> Tuple[str, str, str, str, str]:
        """
        Formats a row tuple: (label, name, price_str, spec1, spec2).
        Delegates to row_formatter if provided, otherwise uses standard fallback.
        """
        if self.row_formatter:
            return self.row_formatter(candidate, discount_fmt)

        label = self.label
        name = candidate.get("model") or candidate.get("name") or candidate.get("title", "Unknown")
        price = float(candidate.get("price", 0.0))
        disc = float(candidate.get("discount_pct", 0.0))
        deal_s = discount_fmt(disc)
        curr = candidate.get("currency") or get_active_currency()
        price_str = f"{format_price(price, curr)} ({deal_s})" if price > 0 else format_price(0, curr)

        spec = candidate.get("spec") or {}
        spec1 = str(spec.get("spec1") or candidate.get("spec1", ""))
        spec2 = str(spec.get("spec2") or candidate.get("spec2", ""))
        return (label, name, price_str, spec1, spec2)


_SAFE_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_SAFE_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}

_SAFE_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b if isinstance(b, (list, tuple, set, str, dict)) else False,
    ast.NotIn: lambda a, b: a not in b if isinstance(b, (list, tuple, set, str, dict)) else True,
}

_SAFE_DSL_FUNCS = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "len": len,
    "float": float,
    "int": int,
    "str": str,
}


def _resolve_candidate_field(cand: Dict[str, Any], field_path: Sequence[str]) -> Any:
    """
    Resolves a dotted field path on a candidate item.
    Automatically checks top-level keys first, and falls back to `cand['spec']`
    so expressions like `cpu.socket` work seamlessly whether `socket` is top-level
    or nested inside `cpu['spec']['socket']`.
    """
    curr: Any = cand
    for idx, key in enumerate(field_path):
        if key.startswith("_"):
            raise ValueError(f"Access to private/dunder attribute '{key}' is prohibited in Rule DSL")
        if isinstance(curr, dict):
            if key in curr:
                curr = curr[key]
            elif idx == 0 and isinstance(curr.get("spec"), dict) and key in curr["spec"]:
                curr = curr["spec"][key]
            else:
                return None
        else:
            return None
    return curr


def _extract_attr_chain(node: ast.AST) -> List[str]:
    chain: List[str] = []
    curr = node
    while isinstance(curr, ast.Attribute):
        if curr.attr.startswith("_"):
            raise ValueError(f"Prohibited attribute '{curr.attr}' in Rule DSL")
        chain.append(curr.attr)
        curr = curr.value
    if isinstance(curr, ast.Name):
        if curr.id.startswith("_"):
            raise ValueError(f"Prohibited identifier '{curr.id}' in Rule DSL")
        chain.append(curr.id)
        chain.reverse()
        return chain
    raise ValueError("Invalid attribute chain in Rule DSL")


class _SafeRuleEvaluator:
    """Zero-dependency, sandbox-safe AST evaluator for declarative CompatibilityRule expressions."""

    def __init__(self, expression: str):
        self.expression = expression.strip()
        try:
            self.tree = ast.parse(self.expression, mode="eval")
        except SyntaxError as e:
            raise ValueError(f"Invalid Rule DSL syntax in '{expression}': {e}") from e
        self.sections_involved: Tuple[str, ...] = self._validate_and_discover_sections(self.tree.body)

    def _validate_and_discover_sections(self, root: ast.AST) -> Tuple[str, ...]:
        secs: List[str] = []

        def _visit(node: ast.AST) -> None:
            if isinstance(node, ast.Attribute):
                if node.attr.startswith("_"):
                    raise ValueError(f"Prohibited attribute '{node.attr}' in Rule DSL")
                if isinstance(node.value, ast.Name):
                    sec_name = node.value.id
                    if sec_name.startswith("_"):
                        raise ValueError(f"Prohibited identifier '{sec_name}' in Rule DSL")
                    if sec_name not in _SAFE_DSL_FUNCS and sec_name not in secs:
                        secs.append(sec_name)
            elif isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_DSL_FUNCS:
                    fn_name = getattr(node.func, "id", type(node.func).__name__)
                    raise ValueError(
                        f"Prohibited function call '{fn_name}' in Rule DSL. "
                        f"Allowed: {', '.join(sorted(_SAFE_DSL_FUNCS))}"
                    )
            elif isinstance(node, ast.Name):
                if node.id.startswith("_"):
                    raise ValueError(f"Prohibited identifier '{node.id}' in Rule DSL")
            elif not isinstance(
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
                    ast.Load,
                    ast.operator,
                    ast.unaryop,
                    ast.boolop,
                    ast.cmpop,
                ),
            ):
                raise ValueError(f"Prohibited AST construct '{type(node).__name__}' in Rule DSL")

            for child in ast.iter_child_nodes(node):
                _visit(child)

        _visit(root)
        return tuple(secs)

    def evaluate(self, set_dict: Dict[str, Dict[str, Any]]) -> bool:
        return bool(self._eval_node(self.tree.body, set_dict))

    def _eval_node(self, node: ast.AST, set_dict: Dict[str, Dict[str, Any]]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            elts = [self._eval_node(e, set_dict) for e in node.elts]
            return tuple(elts) if isinstance(node, ast.Tuple) else (set(elts) if isinstance(node, ast.Set) else elts)
        if isinstance(node, ast.UnaryOp):
            op_fn = _SAFE_UNARY_OPS.get(type(node.op))
            if not op_fn:
                raise ValueError(f"Unsupported unary operator in Rule DSL: {type(node.op).__name__}")
            val = self._eval_node(node.operand, set_dict)
            if val is None and not isinstance(node.op, ast.Not):
                return 0.0
            return op_fn(val)
        if isinstance(node, ast.BinOp):
            op_fn = _SAFE_BINARY_OPS.get(type(node.op))
            if not op_fn:
                raise ValueError(f"Unsupported binary operator in Rule DSL: {type(node.op).__name__}")
            left = self._eval_node(node.left, set_dict)
            right = self._eval_node(node.right, set_dict)
            if left is None or right is None:
                return 0.0
            return op_fn(left, right)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(bool(self._eval_node(v, set_dict)) for v in node.values)
            if isinstance(node.op, ast.Or):
                return any(bool(self._eval_node(v, set_dict)) for v in node.values)
            raise ValueError(f"Unsupported boolean operator: {type(node.op).__name__}")
        if isinstance(node, ast.Compare):
            left = self._eval_node(node.left, set_dict)
            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval_node(comparator, set_dict)
                cmp_fn = _SAFE_CMP_OPS.get(type(op))
                if not cmp_fn:
                    raise ValueError(f"Unsupported comparison operator: {type(op).__name__}")
                if left is None or right is None:
                    if isinstance(op, ast.Eq):
                        if left is not right:
                            return False
                    elif isinstance(op, ast.NotEq):
                        if left is right:
                            return False
                    else:
                        return False
                elif not cmp_fn(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_DSL_FUNCS:
                raise ValueError("Only safe math/type functions (min, max, abs, round, len, float, int, str) are allowed in Rule DSL")
            fn = _SAFE_DSL_FUNCS[node.func.id]
            args = [self._eval_node(a, set_dict) for a in node.args]
            if any(a is None for a in args):
                return 0.0
            return fn(*args)
        if isinstance(node, ast.Attribute):
            chain = _extract_attr_chain(node)
            sec_id = chain[0]
            field_path = chain[1:]
            cand = set_dict.get(sec_id)
            if not isinstance(cand, dict):
                return None
            return _resolve_candidate_field(cand, field_path)
        if isinstance(node, ast.Name):
            if node.id in ("True", "true"):
                return True
            if node.id in ("False", "false"):
                return False
            if node.id in ("None", "null"):
                return None
            if node.id in set_dict:
                return set_dict[node.id]
            raise ValueError(f"Unknown identifier '{node.id}' in Rule DSL")
        raise ValueError(f"Prohibited AST construct '{type(node).__name__}' in Rule DSL")


@dataclass
class CompatibilityRule:
    """
    A constraint or physics/engineering validation rule between set sections.
    Supports both Python callables (`validator`) and zero-code declarative expressions
    via `CompatibilityRule.from_expression(...)` or `DomainLayout.from_dict(...)`.
    """
    name: str
    description: str
    validator: Callable[[Dict[str, Dict[str, Any]]], Tuple[bool, Optional[str]]]
    sections_involved: Tuple[str, ...] = ()
    critical: bool = True
    expression: Optional[str] = None

    @classmethod
    def from_expression(
        cls,
        name: str,
        expression: str,
        description: str = "",
        error_message: Optional[str] = None,
        sections_involved: Optional[Sequence[str]] = None,
        critical: bool = True,
    ) -> CompatibilityRule:
        """
        Creates a CompatibilityRule from a safe declarative expression string, e.g.:
          - "cpu.socket == mobo.socket and mobo.ddr_type == ram.generation"
          - "psu.wattage >= (gpu.tdp_watts + cpu.tdp_watts) * 1.25"
          - "lure.weight_g >= rod.test_min and lure.weight_g <= rod.test_max"
        Automatically infers `sections_involved` for Branch-and-Bound early pruning.
        """
        evaluator = _SafeRuleEvaluator(expression)
        inferred_secs = tuple(sections_involved) if sections_involved else evaluator.sections_involved
        err_msg = error_message or description or f"Rule '{name}' failed: {expression}"

        def _validator(set_dict: Dict[str, Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
            try:
                ok = evaluator.evaluate(set_dict)
                return (True, None) if ok else (False, err_msg)
            except Exception as exc:
                return False, f"{err_msg} ({exc})"

        return cls(
            name=name,
            description=description or expression,
            validator=_validator,
            sections_involved=inferred_secs,
            critical=critical,
            expression=expression,
        )

    def is_evaluable(self, partial_set: Dict[str, Dict[str, Any]]) -> bool:
        """Returns True if all required sections for this rule are present in the partial set."""
        if not self.sections_involved:
            return True
        return all(s in partial_set for s in self.sections_involved)

    def evaluate(self, set_dict: Dict[str, Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
        """Runs the validator callable and returns (is_compatible, failure_reason)."""
        return self.validator(set_dict)


@dataclass
class DomainLayout:
    """
    The structural blueprint of a specific domain (PC build, fishing set, etc.).
    """
    domain_id: str
    title: str
    description: str = ""
    sections: List[SectionDefinition] = field(default_factory=list)
    compatibility_rules: List[CompatibilityRule] = field(default_factory=list)
    default_budget: float = 600.0
    currency: str = "USDT"
    headline_formatter: Optional[Callable[[Dict[str, Any], int, float, float, Any], Tuple[str, str, str, str, str, str]]] = None

    def get_section(self, section_id: str) -> Optional[SectionDefinition]:
        for sec in self.sections:
            if sec.id == section_id:
                return sec
        return None

    def section_ids(self) -> List[str]:
        return [sec.id for sec in self.sections]

    def validate_combination(
        self,
        set_dict: Dict[str, Dict[str, Any]],
        strict: bool = True,
    ) -> Tuple[bool, List[str]]:
        """
        Validates all compatibility rules against a set combination.
        Returns (is_valid, list_of_errors).
        """
        errors = []
        for rule in self.compatibility_rules:
            if not rule.is_evaluable(set_dict):
                if strict:
                    errors.append(f"Missing required sections for rule: {rule.name}")
                continue
            ok, reason = rule.evaluate(set_dict)
            if not ok:
                errors.append(reason or f"Failed rule: {rule.name}")
        return len(errors) == 0, errors

    def validate_partial(
        self,
        partial_set: Dict[str, Dict[str, Any]],
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluates early-pruning rules that only depend on the already selected sections.
        Allows branch-and-bound optimization to prune dead branches instantly.
        """
        for rule in self.compatibility_rules:
            if rule.sections_involved and rule.is_evaluable(partial_set):
                ok, reason = rule.evaluate(partial_set)
                if not ok:
                    return False, reason
        return True, None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DomainLayout:
        """
        Constructs a complete DomainLayout from a declarative JSON/dictionary blueprint.
        Supports declarative Rule DSL expressions in `compatibility_rules`.
        """
        sections: List[SectionDefinition] = []
        for s_data in data.get("sections", []):
            sections.append(
                SectionDefinition(
                    id=str(s_data["id"]),
                    name=str(s_data.get("name", s_data["id"])),
                    label=str(s_data.get("label", str(s_data["id"]).upper())),
                    description=str(s_data.get("description", "")),
                    required=bool(s_data.get("required", True)),
                    budget_share_min=float(s_data.get("budget_share_min", 0.0)),
                    budget_share_max=float(s_data.get("budget_share_max", 1.0)),
                    pareto_limit=int(s_data.get("pareto_limit", 24)),
                    spec_fields=tuple(s_data.get("spec_fields", ())),
                )
            )

        rules: List[CompatibilityRule] = []
        for r_data in data.get("compatibility_rules", []):
            expr = r_data.get("expression") or r_data.get("rule")
            if expr:
                rules.append(
                    CompatibilityRule.from_expression(
                        name=str(r_data.get("name", "rule")),
                        expression=str(expr),
                        description=str(r_data.get("description", expr)),
                        error_message=r_data.get("error_message"),
                        sections_involved=r_data.get("sections_involved"),
                        critical=bool(r_data.get("critical", True)),
                    )
                )

        return cls(
            domain_id=str(data["domain_id"]),
            title=str(data.get("title", data["domain_id"])),
            description=str(data.get("description", "")),
            sections=sections,
            compatibility_rules=rules,
            default_budget=float(data.get("default_budget", 600.0)),
            currency=str(data.get("currency", "USDT")),
        )
