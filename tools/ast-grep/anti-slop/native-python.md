# Python anti-slop pack — native layer and provenance

## Rule-by-rule TypeScript ancestry

| Python rule (this pack) | Family | TypeScript ancestor (`install-anti-slop`) |
| --- | --- | --- |
| `anti-slop-python-no-any-parameters` | F1 | `no-unknown-parameters` |
| `anti-slop-python-no-any-returns` | F1 | `no-unknown-returns` |
| `anti-slop-python-no-any-type-aliases` | F1 | `no-unknown-type-aliases` |
| `anti-slop-python-no-unsafe-dictionary-type` | F2 | `no-unsafe-dictionary-type` |
| `anti-slop-python-no-any-kwargs` | F1 (variadic) | `no-unknown-parameters`, extended to `*args`/`**kwargs`, which TS rest params don't need split out but Python's separate splat-parameter grammar does |
| `anti-slop-python-require-safety-comment-for-cast` | F3 | `require-safety-comment-for-type-assertion` |
| `anti-slop-python-no-blanket-type-ignore` | F3 (sibling) | no 1:1 ancestor; Python's escape hatch is a suppression *comment* (`# type: ignore`) rather than a cast expression, so it extends F3 with the comment-based escape hatch TS doesn't have (TS assertions are always expressions) |
| `anti-slop-python-no-runtime-type-comparison` | F4 | `no-runtime-typeof` |
| `anti-slop-python-no-isinstance-dispatch` | F4 (dispatch variant) | `no-runtime-typeof` (the `isinstance`/`elif` chain is Python's `typeof`-switch equivalent) |
| `anti-slop-python-no-dynamic-attribute-access` | F5 | `no-reflect-get`, `no-reflect-apply` |
| `anti-slop-python-no-module-mocking` | F6 | `no-module-mocking` |
| `anti-slop-python-no-shape-in-symbol-names` | F7 | `no-shape-in-symbol-names` |
| `anti-slop-python-no-conditional-empty-dict-spread` | F9 | `no-conditional-empty-object-spread` |

13 of 13 assigned rules shipped; every shipped rule fires reliably against `fixtures/python/bad.py` and produces zero findings against `fixtures/python/good.py` (see the repo-root `SKILL.md`/lead report for the verbatim scan transcripts).

## Dropped family

**F8 — inline object-literal parameter types (`no-object-parameters` in TS).** Not ported; no Python syntax exists to port. TypeScript lets a parameter's type be an *anonymous* structural literal written directly in the signature, e.g. `function f(opts: { a: string; b: number })`. Python's annotation grammar has no anonymous-structural-type literal: `def f(opts: {a: str, b: int})` is not valid type syntax (a `{...}` in annotation position parses as a dict/set *expression*, not a type, and no PEP gives it type meaning). The only ways to describe a multi-field parameter shape in Python already require a **named** declaration — a `TypedDict`, `dataclass`, `NamedTuple`, or `Protocol` — which is precisely the outcome F8 pushes TypeScript authors toward. Porting this rule would mean either false-positiving on `dict[str, X]` parameters (already covered by F2/`no-unsafe-dictionary-type`) or matching nothing at all. Per the brief, a rule that cannot fire reliably is not shipped.

## Known heuristic limits (documented, not silent)

- **`no-any-type-aliases`** bare-assignment form (`X = Any`) is heuristically scoped to assignments whose left-hand identifier starts uppercase (PEP 8 alias/class convention), to avoid flagging an ordinary lowercase variable that happens to be named `Any`-adjacent. A lowercase `x = Any` will not be flagged; that pattern is vanishingly rare in real code and is not how type aliases are named.
- **`require-safety-comment-for-cast`** locates the `# SAFETY:` comment via tree-sitter sibling adjacency (`follows`/`precedes` on the enclosing statement), not byte-offset line/column math like the TS rule's `SourceCode` API. Two consequences, both accepted as reasonable tradeoffs for a pure ast-grep rule with no line-distance predicate available:
  - A comment that is the **first line of a block** (immediately after a `def`/`if`/`for` colon) attaches to the compound statement node in tree-sitter's grammar, not to the block's first child statement, so a SAFETY comment placed there is not seen as covering that first statement. Fixtures avoid this shape; write the guarding check (or any statement) before the `cast` if the comment must precede it, or use the trailing-same-line form instead.
  - A trailing comment on statement *N* and a "line above" comment for statement *N+1* are structurally identical (both are the sibling directly between the two statements), so in the rare case a `# SAFETY:` comment trails one statement and the very next statement also contains an unguarded `cast()`, the rule will (incorrectly) treat the next `cast()` as justified. Real code rarely stacks two casts like this without justifying both.
- **`no-module-mocking`** matches by callee name (`patch`, `mock.patch`, `patch.object`, `monkeypatch.setattr`), not by resolving `patch`/`mock` back to an `unittest.mock`/`pytest` import the way the TS rule resolves `vi`/`jest` through scope analysis (ast-grep has no cross-file/scope binding resolution). A project-owned function literally named `patch` would false-positive; none of the fixtures do this, and the convention of naming a non-mocking function `patch` is itself unusual enough to warrant a second look anyway.
- **`no-dynamic-attribute-access`** only flags `getattr`/`setattr` when the name argument is a string **literal** and there is no default argument (2-arg `getattr`, 3-arg `setattr`) — i.e., only the case where the coded field name proves a name is known at write-time and reflection is unnecessary. `getattr(obj, name)` (dynamic name) and `getattr(obj, "field", default)` (default-by-necessity) are left unflagged by design, per the assignment's own guidance.

## Optional native layer

ast-grep is syntax-only: it cannot resolve imports, run type inference, or know whether an `Any` came from an untyped third-party stub. Layer these on top for repositories that already run ruff/pyright or mypy; they catch what a syntax engine structurally cannot.

### `pyproject.toml` — ruff

```toml
[tool.ruff.lint]
extend-select = [
  "ANN001",  # missing-type-function-argument — adds: catches *missing* annotations, not just Any; ast-grep only sees annotations that are present and spelled Any.
  "ANN201",  # missing-return-type-undocumented-public-function — adds: same, for return types on public functions.
  "ANN401",  # any-type — adds: flags `Any` used *anywhere* in an annotation (nested in a Union, a generic argument, a Callable signature, an overload), not just top-level parameter/return/alias positions the way this ast-grep pack does.
  "PGH003",  # blanket-type-ignore — adds: ruff's own AST/typed pass; overlaps `no-blanket-type-ignore` but stays correct if ast-grep's regex fixture assumptions ever drift from tokenizer edge cases (e.g. type: ignore embedded in an f-string or triple-quoted block).
  "PGH005",  # invalid-mock-access — adds: catches invalid `mock.<method>` call assertions (e.g. `mock.assert_called` without parens), a mocking-hygiene bug this pack doesn't attempt.
]

[tool.ruff.lint.flake8-annotations]
allow-star-arg-any = false          # keep *args/**kwargs: Any flagged by ANN401 too, matching no-any-kwargs
mypy-init-return = true
suppress-none-returning = true
```

### `pyproject.toml` — pyright (or mypy) strict mode

```toml
[tool.pyright]
typeCheckingMode = "strict"
reportMissingTypeArgument = true      # adds: flags a bare `dict`/`list`/`Mapping` with no type arguments at all — a wider hole than `no-unsafe-dictionary-type`'s `X[str, Any]` shape, which requires the Any to already be spelled out.
reportUnknownMemberType = "error"     # adds: flags a member access whose *type* is unknown because it flows from an untyped import or a dynamic attribute pyright cannot resolve — catches reflection-adjacent risk ast-grep can't see because it never runs type inference.
reportUnknownVariableType = "error"   # adds: same, for a variable whose inferred type includes Any that a syntax pass would never see (e.g. Any laundered through a third-party stub-less import).
reportUntypedFunctionDecorator = "error"  # adds: flags a decorator that erases the wrapped function's signature to Any — a boundary-erasure path with no `Any` token anywhere in the source for ast-grep to match.
```

```ini
# mypy.ini equivalent
[mypy]
strict = True
disallow_any_explicit = True   # adds: bans every explicit `Any`, including ones this pack's four rules don't target (e.g. inside a Callable[..., Any] or a Union member).
disallow_any_expr = True       # adds: bans Any at *expression* level after inference, not just at declared-annotation sites — the deepest version of F1, unreachable from pure syntax matching.
warn_unused_ignores = True     # adds: flags a `# type: ignore[code]` whose code no longer applies (stale suppression) — no-blanket-type-ignore only checks the comment has *a* code, not that the code is still truthful.
```
