# Swift anti-slop pack — native layer and dropped rules

## Optional additive layer: SwiftLint

`ast-grep` is the only required engine for this pack. SwiftLint is optional and
additive — it catches a few things ast-grep's syntax-only view cannot (the
`Selector(...)` string-literal spelling case, and semantic checks that need a
type checker). Add this fragment to `.swiftlint.yml` if SwiftLint is already in
the project; do not add SwiftLint as a new dependency solely for this pack.

```yaml
opt_in_rules:
  - force_cast
  - force_try
  - force_unwrapping
  - legacy_objc_type
  - discouraged_optional_collection
```

What each adds beyond the ast-grep pack:

- `force_cast` — same surface as `anti-slop-swift-no-force-cast` (`as!`). Keep
  both: SwiftLint gets IDE-inline squiggles, ast-grep gets a portable CI gate
  that doesn't need Xcode.
- `force_try` — same surface as `anti-slop-swift-no-force-try` (`try!`). Same
  rationale as `force_cast`.
- `force_unwrapping` — flags every postfix `!` force-unwrap. The ast-grep pack
  deliberately does **not** ship this check (see `require-safety-comment-for-force-unwrap`
  below) because a blanket "no `!`" ban has a much higher false-positive rate
  on idiomatic Swift (`@IBOutlet` implicitly-unwrapped properties, `first!`
  after a guarded non-empty check, etc.) than the syntax-only pack can safely
  encode. If a project wants force-unwrap banned outright, SwiftLint's
  semantic-aware `force_unwrapping` (with its `excluded`/inline-disable
  support) is the better tool for that policy, not this pack.
- `legacy_objc_type` — flags `NSDictionary`, `NSArray`, `NSString`, etc. used
  where a native Swift collection/`String` would work. Overlaps with
  `anti-slop-swift-no-unsafe-dictionary-type`'s `NSDictionary` arm but also
  catches `NSArray`/`NSMutableDictionary`/etc., which this pack intentionally
  does not (out of scope: this pack targets *type erasure*, not bridging
  style).
- `discouraged_optional_collection` — flags `[T]?`/`[K: V]?` (an optional
  collection instead of an empty collection). Different failure mode from
  anything shipped here: it is about API ergonomics, not type erasure or
  runtime dispatch, but it is a genuinely useful companion for the same
  "boundary hygiene" goal.

## Dropped rule: `require-safety-comment-for-force-unwrap`

Attempted last, as instructed, and dropped. What was tried:

**Finding the operator.** tree-sitter-swift gives a clean, unambiguous node
for the postfix force-unwrap: `postfix_expression` with a `bang` child. This
is structurally distinct from `as!` (`as_expression` > `as_operator` "as!")
and `try!` (`try_expression` > `try_operator` "try" "!"), confirmed with
`ast-grep run --debug-query`:

```
let forced = optionalValue!
  →  postfix_expression
       simple_identifier optionalValue
       bang
         !
```

So detecting the operator itself is not the problem.

**Finding the comment.** Comments parse as ordinary sibling nodes of kind
`comment` in tree-sitter-swift's CST (confirmed: a rule matching bare
`kind: comment` finds both leading and same-line-trailing comments). The
problem is *locating* a `// SAFETY:` comment relative to a specific
force-unwrap using only ast-grep's relational rules (`precedes`/`follows`/
`inside`, with `stopBy: neighbor|end`):

- Anchoring on the `postfix_expression` node itself and using
  `follows`/`precedes` with `stopBy: end` fails outright: `stopBy: end` walks
  further **siblings within the same parent**, not up through ancestors, and
  the comment is never a sibling of the bang expression (it is a sibling of
  the enclosing statement, several levels up). This produced zero correct
  matches — every force-unwrap in the test fixture was flagged regardless of
  an adjacent `SAFETY:` comment.

- Anchoring on the enclosing statement (`property_declaration`) and checking
  `precedes`/`follows` for an immediate sibling `comment` node (default
  `stopBy: neighbor`) does correctly recognize both a comment on the line
  above and a same-line trailing comment as "safe". But it introduces a
  worse failure mode: a same-line trailing comment on statement *N* becomes
  the immediately **preceding** sibling of statement *N+1* in document order
  (tree-sitter interleaves comments into the sibling list purely by source
  position, with no line-number awareness). Test case:

  ```swift
  let b = maybeB! // SAFETY: checked above
  let c = maybeC!
  ```

  Line 2 (`maybeB!`) is correctly recognized as safe. Line 3 (`maybeC!`, with
  **no** comment of its own) was silently swallowed as "safe" too, because
  its immediately-preceding sibling — the trailing comment that actually
  belongs to line 2 — matched the `SAFETY:` regex. This is a real, load-bearing
  false negative, not an edge case: any file with one properly-commented
  force-unwrap followed by an uncommented one produces a silent miss on the
  second. ast-grep's relational rules have no way to additionally constrain
  "on the same source line as" or "within N lines of", so there is no way to
  disambiguate a trailing comment on the previous statement from a leading
  comment on this one using only the rule schema.

Given the explicit contract that "a silently non-matching rule is worse than
an absent one" — and this one is worse than non-matching, it actively
launders real violations as clean — it is dropped rather than shipped. A
correct version would need either a custom scan (comparing byte offsets/line
numbers of the comment and the force-unwrap node directly, outside the
declarative rule schema) or an upstream ast-grep feature for same-line
trailing-comment attachment. Projects that want this check should reach for
SwiftLint's `force_unwrapping` with inline `// swiftlint:disable:this
force_unwrapping` instead, which is a linter-level opt-out already anchored
to the correct line.

## Family mapping (shared anti-slop taxonomy → this pack)

| Family | Status | Swift rule(s) | TS ancestor(s) |
|---|---|---|---|
| F1 type erasure at boundaries | Ported | `no-any-parameters`, `no-any-returns`, `no-any-typealias` | `no-unknown-parameters`, `no-unknown-returns`, `no-unknown-type-aliases` (Swift has no `unknown`; `Any`/`AnyObject`/`[Any]`/`Any?` are the erasure surface) |
| F2 unsafe dictionary types | Ported | `no-unsafe-dictionary-type` | `no-unsafe-dictionary-type` (`[String: Any]`/`[String: AnyObject]`/`NSDictionary`/`Dictionary<String, Any>` instead of `Record<string, unknown>`) |
| F3 cast/assertion escape hatches | Partially ported | `no-force-cast` (`as!`), `no-force-try` (`try!`, no TS equivalent — JS has no fallible `try` expression form) | `no-widen-then-assert`, `no-known-value-widening` closest cousins |
| F3 (cont.) | Dropped | — | `no-chained-type-assertions` — Swift's `as!`/`as?` chain grammar doesn't produce the same "assert away, assert back" shape TS permits (`x as unknown as T`); a single `as!` is already caught by `no-force-cast`, and stacking two force-casts (`x as! A as! B`) is vanishingly rare and not worth a dedicated pattern that would mostly false-positive on legitimate `(x as? A)?.field as? B` optional chains |
| F3 (cont.) | Attempted, dropped | — | `require-safety-comment-for-type-assertion` → attempted as `require-safety-comment-for-force-unwrap`; see write-up above |
| F4 runtime type dispatch | Ported | `no-runtime-type-dispatch` (`if x is T`, `guard x is T`, `case is T`, `type(of:)`) | `no-runtime-typeof` |
| F5 reflection | Ported | `no-reflection` (`Mirror(reflecting:)`, `.value(forKey:)`, `.setValue(_:forKey:)`, `.perform(Selector(...))`, `NSClassFromString`) | `no-reflect-get`, `no-reflect-apply` (merged into one rule — Swift's reflection surface is Objective-C-bridged KVC/`Mirror`/`NSClassFromString`, not a `Reflect.get`/`Reflect.apply` pair) |
| F6 module mocking in tests | Dropped (not applicable) | — | `no-module-mocking` — Swift has no dynamic module-mocking mechanism analogous to `jest.mock()`/`vi.mock()` (no ambient module registry to intercept). The idiomatic Swift equivalent — protocol-based dependency injection with a test double conforming to the same protocol — is not an anti-pattern to *flag*, it's the recommended replacement, so there is no AST shape to ban here |
| F7 shape in symbol names | Ported | `no-shape-in-symbol-names` (`let`/`var`/`func`/parameter names ending `Dict`, `Array`, `Obj`, `Str`, `Map`) | `no-shape-in-symbol-names` (TS bans the substring `"shape"`; the Swift task spec instead lists concrete storage-shape suffixes, so the port targets those suffixes rather than the literal word) |
| F8 inline object-literal parameter types | Dropped (not applicable) | — | `no-object-parameters` — Swift has no structural/inline object-literal type annotation (TS's `(x: { id: number }) => …`). A Swift parameter type is always a named nominal type (struct/class/enum/protocol) or a tuple; there is no anonymous-shape literal to write in parameter position, so the anti-pattern is structurally impossible |
| F9 conditional empty-object spread | Dropped (not applicable) | — | `no-conditional-empty-object-spread` — Swift has no object/spread-literal syntax at all (no `{ ...cond ? x : {} }` equivalent); nothing to port |
