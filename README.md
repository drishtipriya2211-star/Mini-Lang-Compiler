# MiniLang Compiler Explorer

MiniLang is a small imperative language with a hand-written compiler front-end in Python 3,
plus a FastAPI backend and a web UI for exploring each compilation stage.

```
source ─▶ Lexer ─▶ tokens ─▶ Parser ─▶ AST ─▶ Semantic checks ─▶ text tree · DOT · interpreter
```

- **Lexer:** one regex of named groups, with line/column tracking and bad-character diagnostics.
- **Parser:** recursive descent for statements and table-driven precedence climbing for expressions.
  Panic-mode error recovery lets it report every error in one run.
- **Semantic analysis:** block scopes, declare-before-use, and no redeclaration in the same scope.
- **Visualisation:** an indented text tree and Graphviz DOT export. The UI also has an interactive tree and an SVG graph.
- **Interpreter:** a tree-walking evaluator, so programs actually run.
- **Diagnostics:** rustc-style caret messages with the phase, line, column and helpful notes.

## Project layout

```
Mini-Lang-Compiler/
├── minilang.py          # the compiler: lexer, parser, AST, semantic, DOT, interpreter, CLI
├── server.py            # FastAPI backend (also serves the UI)
├── static/index.html    # single-file frontend (no build step, works offline)
├── samples/*.ml         # sample programs
├── tests/test_minilang.py
└── requirements.txt
```

## Setup (Windows PowerShell)

```powershell
cd "C:\Users\Drishti Priya\Documents\Mini-Lang-Compiler"

# 1. (optional) create a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. install the two dependencies
pip install -r requirements.txt

# 3. start the server
python server.py
```

Then open **http://127.0.0.1:8000** in your browser.
The interactive API docs (Swagger) are at **http://127.0.0.1:8000/docs**.

> On macOS/Linux, use `source .venv/bin/activate` in step 1. Everything else is the same.

## Using the UI

| Area | What it shows |
|---|---|
| Editor | Syntax highlighting, line numbers, red squiggles and gutter dots on error lines. **Ctrl+Enter** compiles; with *Live compile* on, it recompiles as you type. |
| **Tokens** | Kind, lexeme, line and column for each token. Filter the table, or click a row to jump to that token in the editor. |
| **AST** | *Interactive* (collapsible) tree, *Graph* (SVG, zoomable and downloadable), *Text tree*, and *Graphviz DOT* (copy or download). |
| **Diagnostics** | Every error with its phase badge, caret snippet and notes. Click the location to jump to it. |
| **Output** | What the program prints, when it compiles without errors. |
| **Architecture & Docs** | Pipeline, EBNF grammar, precedence table, error recovery and API reference. |

## Command line

```powershell
python minilang.py samples\factorial.ml --tokens --tree --run
python minilang.py samples\errors.ml                 # shows every diagnostic
python minilang.py samples\precedence.ml --dot ast.dot
dot -Tpng ast.dot -o ast.png                         # if Graphviz is installed
python minilang.py samples\factorial.ml --json       # same JSON the API returns
```

## Tests

```powershell
python -m unittest discover tests
```

## The language in one example

```
// comments start with //
let n = 5;
let result = 1;
while (n > 1) {
    result = result * n;
    n = n - 1;
}
if (result >= 100 and not (result == 0)) {
    print(result);
} else {
    print(0);
}
```

Values are integers and booleans (`true` / `false`). Operators, from loosest to tightest binding:
`or` < `and` < `== !=` / `not` < `< > <= >=` < `+ -` < `* / %` < unary `-`.

## Samples

| File | Demonstrates |
|---|---|
| `factorial.ml` | while loop, assignment (prints 120) |
| `precedence.ml` | precedence and associativity of every operator level |
| `elseif.ml` | `else if` chains as nested `if` statements |
| `primes.ml` | nested loops, `and`, `%`, booleans |
| `errors.ml` | several syntax errors plus one lexical error, all reported in one run (panic-mode recovery) |
| `unclosed.ml` | unclosed `{` reported at the opening brace |
| `scope.ml` | semantic errors: out-of-scope use, redeclaration, undeclared assignment |
| `empty.ml` | an empty program is valid |

## API

`POST /api/compile` with `{"source": "...", "filename": "x.ml", "run": true}` returns:

```json
{
  "success": false,
  "tokens": [{"kind": "LET", "lexeme": "let", "line": 1, "col": 1}],
  "ast": {"type": "Program", "label": "Program", "line": 1, "col": 1, "children": []},
  "tree": "Program\n└── ...",
  "dot": "digraph AST { ... }",
  "diagnostics": [{"phase": "syntax", "message": "expected ';' after let statement, found 'let'",
                   "line": 2, "col": 11, "length": 1, "source_line": "let x = 10",
                   "caret": "          ^", "notes": [], "rendered": "error[syntax]: ..."}],
  "executed": false,
  "output": null,
  "stats": {"lines": 3, "tokens": 12, "ast_nodes": 5, "errors": 1, "timings_ms": {"lex": 0.02, "parse": 0.04}}
}
```

`GET /api/samples` lists the built-in samples. `GET /api/health` is a health check.
