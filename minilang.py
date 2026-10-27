"""
MiniLang - a small imperative language with a hand-written compiler front-end.

Pipeline:
    source text
      -> Lexer      (regex-based, tracks line/column)        -> tokens
      -> Parser     (recursive descent + table-driven         -> AST
                     precedence climbing, panic-mode recovery)
      -> Semantic   (scope checking: declare-before-use,      -> diagnostics
                     no redeclaration in the same scope)
      -> Visualise  (indented text tree, Graphviz DOT)
      -> Interpret  (tree-walking evaluator, optional)        -> program output

Usage (CLI):
    python minilang.py samples/factorial.ml --tokens --tree --run
    python minilang.py samples/errors.ml
    python minilang.py samples/precedence.ml --dot ast.dot
    python minilang.py samples/factorial.ml --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@dataclass
class Diagnostic:
    phase: str            # lexical | syntax | semantic | runtime
    message: str
    line: int
    col: int
    length: int = 1
    severity: str = "error"
    notes: list[str] = field(default_factory=list)

    def caret_lines(self, source_lines: list[str]) -> tuple[str, str]:
        text = source_lines[self.line - 1] if 0 < self.line <= len(source_lines) else ""
        # Preserve tabs so the caret lines up with the source as displayed.
        pad = "".join("\t" if ch == "\t" else " " for ch in text[: self.col - 1])
        pad += " " * max(0, self.col - 1 - len(text))
        return text, pad + "^" * max(1, self.length)

    def render(self, source_lines: list[str], filename: str = "<input>") -> str:
        text, caret = self.caret_lines(source_lines)
        gutter = " " * len(str(self.line))
        out = [
            f"{self.severity}[{self.phase}]: {self.message}",
            f"{gutter}--> {filename}:{self.line}:{self.col}",
            f"{gutter} |",
            f"{self.line} | {text}",
            f"{gutter} | {caret}",
        ]
        out += [f"{gutter} = note: {n}" for n in self.notes]
        return "\n".join(out)

    def to_dict(self, source_lines: list[str], filename: str = "<input>") -> dict:
        text, caret = self.caret_lines(source_lines)
        return {
            "severity": self.severity,
            "phase": self.phase,
            "message": self.message,
            "line": self.line,
            "col": self.col,
            "length": self.length,
            "source_line": text,
            "caret": caret,
            "notes": self.notes,
            "rendered": self.render(source_lines, filename),
        }


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

KEYWORDS = {
    "let": "LET", "if": "IF", "else": "ELSE", "while": "WHILE", "print": "PRINT",
    "and": "AND", "or": "OR", "not": "NOT", "true": "TRUE", "false": "FALSE",
}

OPERATORS = {
    "==": "EQ", "!=": "NE", "<=": "LE", ">=": "GE", "<": "LT", ">": "GT",
    "=": "ASSIGN", "+": "PLUS", "-": "MINUS", "*": "STAR", "/": "SLASH", "%": "PERCENT",
    "(": "LPAREN", ")": "RPAREN", "{": "LBRACE", "}": "RBRACE", ";": "SEMI",
}

# Order matters: longer operators must come before their prefixes.
TOKEN_SPEC = [
    ("COMMENT", r"//[^\n]*"),
    ("NEWLINE", r"\n"),
    ("SKIP", r"[ \t\r]+"),
    ("NUMBER", r"\d+"),
    ("NAME", r"[A-Za-z_][A-Za-z0-9_]*"),
    ("OP", r"==|!=|<=|>=|[+\-*/%<>=(){};]"),
    ("MISMATCH", r"."),
]
TOKEN_REGEX = re.compile("|".join(f"(?P<{name}>{pattern})" for name, pattern in TOKEN_SPEC))

LEXICAL_HINTS = {
    "!": "MiniLang uses 'not' for logical negation and '!=' for inequality",
    "&": "MiniLang uses the keyword 'and' for logical conjunction",
    "|": "MiniLang uses the keyword 'or' for logical disjunction",
    '"': "string literals are not supported; print() takes an expression",
}


@dataclass
class Token:
    kind: str
    lexeme: str
    line: int
    col: int

    def to_dict(self) -> dict:
        return {"kind": self.kind, "lexeme": self.lexeme, "line": self.line, "col": self.col}


def tokenize(source: str) -> tuple[list[Token], list[Diagnostic]]:
    tokens: list[Token] = []
    diagnostics: list[Diagnostic] = []
    line, line_start = 1, 0

    for m in TOKEN_REGEX.finditer(source):
        kind, text = m.lastgroup, m.group()
        col = m.start() - line_start + 1

        if kind == "NEWLINE":
            line += 1
            line_start = m.end()
        elif kind in ("SKIP", "COMMENT"):
            continue
        elif kind == "NUMBER":
            tokens.append(Token("NUMBER", text, line, col))
        elif kind == "NAME":
            tokens.append(Token(KEYWORDS.get(text, "IDENT"), text, line, col))
        elif kind == "OP":
            tokens.append(Token(OPERATORS[text], text, line, col))
        else:
            prev = diagnostics[-1] if diagnostics else None
            if prev and prev.phase == "lexical" and prev.line == line and prev.col + prev.length == col:
                # Merge a run of adjacent invalid characters into one diagnostic.
                prev.length += 1
                prev.message = f"unexpected characters {source[m.start() - prev.length + 1:m.end()]!r}"
            else:
                hint = LEXICAL_HINTS.get(text)
                diagnostics.append(Diagnostic(
                    "lexical", f"unexpected character {text!r}", line, col,
                    notes=[hint] if hint else [],
                ))

    tokens.append(Token("EOF", "", line, len(source) - line_start + 1))
    return tokens, diagnostics


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


class Node:
    line: int = 0
    col: int = 0

    def label(self) -> str:
        return type(self).__name__

    def children(self) -> list[tuple[str | None, "Node"]]:
        return []

    def to_dict(self, role: str | None = None) -> dict:
        return {
            "type": type(self).__name__,
            "label": self.label(),
            "role": role,
            "line": self.line,
            "col": self.col,
            "children": [child.to_dict(r) for r, child in self.children()],
        }

    def count(self) -> int:
        return 1 + sum(child.count() for _, child in self.children())


@dataclass
class Program(Node):
    statements: list[Node]
    line: int = 1
    col: int = 1

    def children(self):
        return [(None, s) for s in self.statements]


@dataclass
class Block(Node):
    statements: list[Node]
    line: int = 0
    col: int = 0

    def children(self):
        return [(None, s) for s in self.statements]


@dataclass
class Let(Node):
    name: Token
    value: Node
    line: int = 0
    col: int = 0

    def label(self):
        return f"Let {self.name.lexeme}"

    def children(self):
        return [("value", self.value)]


@dataclass
class Assign(Node):
    name: Token
    value: Node
    line: int = 0
    col: int = 0

    def label(self):
        return f"Assign {self.name.lexeme}"

    def children(self):
        return [("value", self.value)]


@dataclass
class Print(Node):
    value: Node
    line: int = 0
    col: int = 0

    def children(self):
        return [("value", self.value)]


@dataclass
class If(Node):
    condition: Node
    then_branch: Node
    else_branch: Node | None
    line: int = 0
    col: int = 0

    def children(self):
        kids = [("cond", self.condition), ("then", self.then_branch)]
        if self.else_branch is not None:
            kids.append(("else", self.else_branch))
        return kids


@dataclass
class While(Node):
    condition: Node
    body: Node
    line: int = 0
    col: int = 0

    def children(self):
        return [("cond", self.condition), ("body", self.body)]


@dataclass
class Binary(Node):
    op: str
    left: Node
    right: Node
    line: int = 0
    col: int = 0

    def label(self):
        return f"BinOp {self.op}"

    def children(self):
        return [("left", self.left), ("right", self.right)]


@dataclass
class Unary(Node):
    op: str
    operand: Node
    line: int = 0
    col: int = 0

    def label(self):
        return f"UnaryOp {self.op}"

    def children(self):
        return [("operand", self.operand)]


@dataclass
class Number(Node):
    value: int
    line: int = 0
    col: int = 0

    def label(self):
        return f"Number {self.value}"


@dataclass
class Bool(Node):
    value: bool
    line: int = 0
    col: int = 0

    def label(self):
        return f"Bool {'true' if self.value else 'false'}"


@dataclass
class Var(Node):
    name: str
    line: int = 0
    col: int = 0

    def label(self):
        return f"Var {self.name}"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
#
#   program    ::= statement* EOF
#   statement  ::= letStmt | assignStmt | ifStmt | whileStmt | printStmt | block
#   letStmt    ::= "let" IDENT "=" expr ";"
#   assignStmt ::= IDENT "=" expr ";"
#   ifStmt     ::= "if" "(" expr ")" statement ( "else" statement )?
#   whileStmt  ::= "while" "(" expr ")" statement
#   printStmt  ::= "print" "(" expr ")" ";"
#   block      ::= "{" statement* "}"
#   expr       ::= unary ( BINOP expr )*        -- precedence climbing
#   unary      ::= "-" unary | "not" expr<3> | primary
#   primary    ::= NUMBER | "true" | "false" | IDENT | "(" expr ")"

# Binary operator precedence table (higher binds tighter). All left-associative.
BINARY_PRECEDENCE = {
    "OR": 1,
    "AND": 2,
    "EQ": 3, "NE": 3,
    "LT": 4, "GT": 4, "LE": 4, "GE": 4,
    "PLUS": 5, "MINUS": 5,
    "STAR": 6, "SLASH": 6, "PERCENT": 6,
}
# 'not' binds looser than comparisons: `not a == b` means `not (a == b)`.
NOT_OPERAND_PRECEDENCE = 3

# Panic-mode recovery stops before these tokens (a new statement can start here).
SYNC_TOKENS = {"LET", "IF", "WHILE", "PRINT", "LBRACE", "RBRACE"}


class ParseError(Exception):
    pass


def describe(tok: Token) -> str:
    return "end of input" if tok.kind == "EOF" else f"'{tok.lexeme}'"


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0
        self.diagnostics: list[Diagnostic] = []

    # -- token helpers ------------------------------------------------------

    @property
    def cur(self) -> Token:
        return self.tokens[self.pos]

    @property
    def prev(self) -> Token:
        return self.tokens[max(0, self.pos - 1)]

    def peek(self, offset: int = 1) -> Token:
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def check(self, *kinds: str) -> bool:
        return self.cur.kind in kinds

    def advance(self) -> Token:
        tok = self.cur
        if tok.kind != "EOF":
            self.pos += 1
        return tok

    def match(self, *kinds: str) -> Token | None:
        return self.advance() if self.check(*kinds) else None

    # -- error helpers ------------------------------------------------------

    def report(self, line: int, col: int, message: str, length: int = 1, notes=()) -> ParseError:
        self.diagnostics.append(Diagnostic("syntax", message, line, col, length, notes=list(notes)))
        return ParseError(message)

    def error_at(self, tok: Token, message: str, notes=()) -> ParseError:
        return self.report(tok.line, tok.col, message, max(1, len(tok.lexeme)), notes)

    def error_after_prev(self, message: str, notes=()) -> ParseError:
        # Point the caret just past the previous token: that is where the
        # missing ';' or ')' belongs, even if the next token is lines away.
        if self.pos == 0:
            return self.error_at(self.cur, message, notes)
        p = self.prev
        return self.report(p.line, p.col + len(p.lexeme), message, 1, notes)

    def expect(self, kind: str, what: str, after_prev: bool = False) -> Token:
        if self.check(kind):
            return self.advance()
        message = f"expected {what}, found {describe(self.cur)}"
        raise self.error_after_prev(message) if after_prev else self.error_at(self.cur, message)

    def synchronize(self) -> None:
        """Panic mode: discard tokens until a likely statement boundary."""
        while not self.check("EOF"):
            if self.cur.kind in SYNC_TOKENS:
                return
            if self.advance().kind == "SEMI":
                return

    # -- grammar ------------------------------------------------------------

    def parse_program(self) -> Program:
        return Program(self.statement_list(closing=None))

    def statement_list(self, closing: str | None) -> list[Node]:
        statements = []
        while not self.check("EOF") and not (closing and self.check(closing)):
            start = self.pos
            try:
                statements.append(self.statement())
            except ParseError:
                self.synchronize()
                if self.pos == start:      # guarantee progress (e.g. a stray '}')
                    self.advance()
        return statements

    def statement(self) -> Node:
        kind = self.cur.kind
        if kind == "LET":
            return self.let_statement()
        if kind == "IF":
            return self.if_statement()
        if kind == "WHILE":
            return self.while_statement()
        if kind == "PRINT":
            return self.print_statement()
        if kind == "LBRACE":
            return self.block()
        if kind == "IDENT":
            return self.assign_statement()
        raise self.error_at(
            self.cur, f"expected a statement, found {describe(self.cur)}",
            ["statements start with 'let', 'if', 'while', 'print', '{' or a variable name"],
        )

    def let_statement(self) -> Let:
        kw = self.advance()
        name = self.expect("IDENT", "variable name after 'let'")
        self.expect("ASSIGN", f"'=' after variable name '{name.lexeme}'", after_prev=True)
        value = self.expression()
        self.expect("SEMI", "';' after let statement", after_prev=True)
        return Let(name, value, kw.line, kw.col)

    def assign_statement(self) -> Assign:
        name = self.advance()
        if not self.check("ASSIGN"):
            raise self.error_at(
                self.cur, f"expected '=' after '{name.lexeme}', found {describe(self.cur)}",
                ["a bare expression is not a statement; did you mean an assignment?"],
            )
        self.advance()
        value = self.expression()
        self.expect("SEMI", "';' after assignment", after_prev=True)
        return Assign(name, value, name.line, name.col)

    def condition(self, keyword: str) -> Node:
        self.expect("LPAREN", f"'(' after '{keyword}'")
        cond = self.expression()
        if self.check("ASSIGN"):
            raise self.error_at(self.cur, f"unexpected '=' in {keyword} condition",
                                ["did you mean '==' for comparison?"])
        self.expect("RPAREN", f"')' after {keyword} condition", after_prev=True)
        return cond

    def if_statement(self) -> If:
        kw = self.advance()
        cond = self.condition("if")
        then_branch = self.statement()
        else_branch = self.statement() if self.match("ELSE") else None
        return If(cond, then_branch, else_branch, kw.line, kw.col)

    def while_statement(self) -> While:
        kw = self.advance()
        cond = self.condition("while")
        body = self.statement()
        return While(cond, body, kw.line, kw.col)

    def print_statement(self) -> Print:
        kw = self.advance()
        self.expect("LPAREN", "'(' after 'print'")
        value = self.expression()
        self.expect("RPAREN", "')' after print argument", after_prev=True)
        self.expect("SEMI", "';' after print statement", after_prev=True)
        return Print(value, kw.line, kw.col)

    def block(self) -> Block:
        lbrace = self.advance()
        statements = self.statement_list(closing="RBRACE")
        if not self.match("RBRACE"):
            # Report but keep the partial block so the AST is still useful.
            self.error_at(lbrace, "unclosed block: expected '}' before end of input",
                          [f"this '{{' at line {lbrace.line}, column {lbrace.col} is never closed"])
        return Block(statements, lbrace.line, lbrace.col)

    # -- expressions: table-driven precedence climbing ------------------------

    def expression(self, min_prec: int = 1) -> Node:
        left = self.unary()
        while True:
            op = self.cur
            prec = BINARY_PRECEDENCE.get(op.kind)
            if prec is None or prec < min_prec:
                return left
            self.advance()
            right = self.expression(prec + 1)   # prec + 1 => left-associative
            left = Binary(op.lexeme, left, right, op.line, op.col)

    def unary(self) -> Node:
        if self.check("MINUS"):
            op = self.advance()
            return Unary("-", self.unary(), op.line, op.col)
        if self.check("NOT"):
            op = self.advance()
            return Unary("not", self.expression(NOT_OPERAND_PRECEDENCE), op.line, op.col)
        return self.primary()

    def primary(self) -> Node:
        tok = self.cur
        if tok.kind == "NUMBER":
            self.advance()
            return Number(int(tok.lexeme), tok.line, tok.col)
        if tok.kind in ("TRUE", "FALSE"):
            self.advance()
            return Bool(tok.kind == "TRUE", tok.line, tok.col)
        if tok.kind == "IDENT":
            self.advance()
            return Var(tok.lexeme, tok.line, tok.col)
        if tok.kind == "LPAREN":
            lparen = self.advance()
            inner = self.expression()
            if not self.match("RPAREN"):
                raise self.error_after_prev(
                    f"expected ')' to close parenthesis, found {describe(self.cur)}",
                    [f"'(' opened at line {lparen.line}, column {lparen.col}"],
                )
            return inner
        raise self.error_at(tok, f"expected expression, found {describe(tok)}")


# ---------------------------------------------------------------------------
# Semantic analysis: block scoping, declare-before-use, no redeclaration
# ---------------------------------------------------------------------------


class SemanticAnalyzer:
    def __init__(self):
        self.scopes: list[dict[str, Token]] = []
        self.diagnostics: list[Diagnostic] = []

    def error(self, line, col, length, message, notes=()):
        self.diagnostics.append(Diagnostic("semantic", message, line, col, length, notes=list(notes)))

    def lookup(self, name: str) -> Token | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def analyze(self, program: Program) -> list[Diagnostic]:
        self.scopes = [{}]
        for stmt in program.statements:
            self.visit(stmt)
        return self.diagnostics

    def visit_scoped(self, node: Node):
        # A non-block body such as `if (c) let x = 1;` still gets its own scope.
        self.scopes.append({})
        self.visit(node)
        self.scopes.pop()

    def visit(self, node: Node):
        if isinstance(node, Block):
            self.scopes.append({})
            for stmt in node.statements:
                self.visit(stmt)
            self.scopes.pop()
        elif isinstance(node, Let):
            self.visit(node.value)            # `let x = x;` uses the outer/undeclared x
            name = node.name
            previous = self.scopes[-1].get(name.lexeme)
            if previous:
                self.error(name.line, name.col, len(name.lexeme),
                           f"variable '{name.lexeme}' is already declared in this scope",
                           [f"previous declaration at line {previous.line}, column {previous.col}",
                            f"use '{name.lexeme} = ...;' to assign a new value"])
            else:
                self.scopes[-1][name.lexeme] = name
        elif isinstance(node, Assign):
            if not self.lookup(node.name.lexeme):
                self.error(node.line, node.col, len(node.name.lexeme),
                           f"assignment to undeclared variable '{node.name.lexeme}'",
                           [f"declare it first with 'let {node.name.lexeme} = ...;'"])
            self.visit(node.value)
        elif isinstance(node, Var):
            if not self.lookup(node.name):
                self.error(node.line, node.col, len(node.name),
                           f"use of undeclared variable '{node.name}'")
        elif isinstance(node, If):
            self.visit(node.condition)
            self.visit_scoped(node.then_branch)
            if node.else_branch is not None:
                self.visit_scoped(node.else_branch)
        elif isinstance(node, While):
            self.visit(node.condition)
            self.visit_scoped(node.body)
        else:
            for _, child in node.children():
                self.visit(child)


# ---------------------------------------------------------------------------
# Interpreter (tree-walking) - lets the review demo actually *run* programs
# ---------------------------------------------------------------------------


class MiniLangRuntimeError(Exception):
    def __init__(self, node: Node, message: str):
        super().__init__(message)
        self.node = node
        self.message = message


class Interpreter:
    MAX_STEPS = 200_000
    MAX_OUTPUT_LINES = 5_000

    def __init__(self):
        self.scopes: list[dict[str, int | bool]] = [{}]
        self.output: list[str] = []
        self.steps = 0

    @staticmethod
    def fmt(value) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def tick(self, node: Node):
        self.steps += 1
        if self.steps > self.MAX_STEPS:
            raise MiniLangRuntimeError(node, f"execution exceeded {self.MAX_STEPS:,} steps (infinite loop?)")

    def run(self, program: Program) -> list[str]:
        for stmt in program.statements:
            self.execute(stmt)
        return self.output

    def find_scope(self, name: str) -> dict:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope
        raise KeyError(name)

    def execute_scoped(self, node: Node):
        self.scopes.append({})
        try:
            self.execute(node)
        finally:
            self.scopes.pop()

    def execute(self, node: Node):
        self.tick(node)
        if isinstance(node, Let):
            self.scopes[-1][node.name.lexeme] = self.evaluate(node.value)
        elif isinstance(node, Assign):
            self.find_scope(node.name.lexeme)[node.name.lexeme] = self.evaluate(node.value)
        elif isinstance(node, Print):
            if len(self.output) >= self.MAX_OUTPUT_LINES:
                raise MiniLangRuntimeError(node, f"output exceeded {self.MAX_OUTPUT_LINES:,} lines")
            self.output.append(self.fmt(self.evaluate(node.value)))
        elif isinstance(node, Block):
            self.scopes.append({})
            try:
                for stmt in node.statements:
                    self.execute(stmt)
            finally:
                self.scopes.pop()
        elif isinstance(node, If):
            if self.truthy(self.evaluate(node.condition)):
                self.execute_scoped(node.then_branch)
            elif node.else_branch is not None:
                self.execute_scoped(node.else_branch)
        elif isinstance(node, While):
            while self.truthy(self.evaluate(node.condition)):
                self.tick(node)
                self.execute_scoped(node.body)

    @staticmethod
    def truthy(value) -> bool:
        return value is True or (not isinstance(value, bool) and value != 0)

    def expect_int(self, node: Node, op: str, *values):
        for v in values:
            if isinstance(v, bool):
                raise MiniLangRuntimeError(node, f"operator '{op}' expects integers, got a boolean")

    def evaluate(self, node: Node):
        if isinstance(node, Number):
            return node.value
        if isinstance(node, Bool):
            return node.value
        if isinstance(node, Var):
            return self.find_scope(node.name)[node.name]
        if isinstance(node, Unary):
            value = self.evaluate(node.operand)
            if node.op == "not":
                return not self.truthy(value)
            self.expect_int(node, "-", value)
            return -value
        if isinstance(node, Binary):
            op = node.op
            if op == "and":
                return self.truthy(self.evaluate(node.left)) and self.truthy(self.evaluate(node.right))
            if op == "or":
                return self.truthy(self.evaluate(node.left)) or self.truthy(self.evaluate(node.right))
            a, b = self.evaluate(node.left), self.evaluate(node.right)
            if op == "==":
                return a == b and isinstance(a, bool) == isinstance(b, bool)
            if op == "!=":
                return not (a == b and isinstance(a, bool) == isinstance(b, bool))
            self.expect_int(node, op, a, b)
            if op == "+": return a + b
            if op == "-": return a - b
            if op == "*": return a * b
            if op in ("/", "%"):
                if b == 0:
                    raise MiniLangRuntimeError(node, "division by zero" if op == "/" else "modulo by zero")
                q = abs(a) // abs(b) * (1 if (a >= 0) == (b >= 0) else -1)  # truncate toward zero, like C
                return q if op == "/" else a - b * q
            if op == "<": return a < b
            if op == ">": return a > b
            if op == "<=": return a <= b
            if op == ">=": return a >= b
        raise MiniLangRuntimeError(node, f"cannot evaluate {node.label()}")


# ---------------------------------------------------------------------------
# Visualisation
# ---------------------------------------------------------------------------


def ast_to_text(node: Node) -> str:
    lines = [node.label()]

    def walk(n: Node, prefix: str):
        kids = n.children()
        for i, (role, child) in enumerate(kids):
            last = i == len(kids) - 1
            tag = f"{role}: " if role else ""
            lines.append(f"{prefix}{'└── ' if last else '├── '}{tag}{child.label()}")
            walk(child, prefix + ("    " if last else "│   "))

    walk(node, "")
    return "\n".join(lines)


DOT_COLORS = {
    "stmt": "#dbeafe", "expr": "#ede9fe", "literal": "#dcfce7", "var": "#fef3c7",
}


def node_category(node: Node) -> str:
    if isinstance(node, (Binary, Unary)):
        return "expr"
    if isinstance(node, (Number, Bool)):
        return "literal"
    if isinstance(node, Var):
        return "var"
    return "stmt"


def ast_to_dot(root: Node) -> str:
    out = [
        "digraph AST {",
        '  graph [rankdir=TB, fontname="Helvetica"];',
        '  node  [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11];',
        '  edge  [fontname="Helvetica", fontsize=9, color="#64748b"];',
    ]
    counter = 0

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    def walk(node: Node) -> str:
        nonlocal counter
        nid = f"n{counter}"
        counter += 1
        out.append(f'  {nid} [label="{esc(node.label())}", fillcolor="{DOT_COLORS[node_category(node)]}"];')
        for role, child in node.children():
            cid = walk(child)
            attr = f' [label="{esc(role)}"]' if role else ""
            out.append(f"  {nid} -> {cid}{attr};")
        return nid

    walk(root)
    out.append("}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def compile_source(source: str, filename: str = "<input>", run: bool = True) -> dict:
    """Run the full pipeline and return a JSON-serialisable result."""
    source = source.replace("\r\n", "\n")
    lines = source.split("\n")
    timings = {}

    t = time.perf_counter()
    tokens, diagnostics = tokenize(source)
    timings["lex"] = time.perf_counter() - t

    t = time.perf_counter()
    parser = Parser(tokens)
    program = parser.parse_program()
    diagnostics += parser.diagnostics
    timings["parse"] = time.perf_counter() - t

    # Semantic checks on a partial (error-recovered) AST would mostly be noise.
    if not diagnostics:
        t = time.perf_counter()
        diagnostics += SemanticAnalyzer().analyze(program)
        timings["semantic"] = time.perf_counter() - t

    output, executed = None, False
    if run and not diagnostics:
        t = time.perf_counter()
        interpreter = Interpreter()
        executed = True
        try:
            interpreter.run(program)
        except MiniLangRuntimeError as e:
            diagnostics.append(Diagnostic("runtime", e.message, e.node.line, e.node.col,
                                          max(1, len(e.node.op) if isinstance(e.node, (Binary, Unary)) else 1)))
        output = interpreter.output
        timings["run"] = time.perf_counter() - t

    diagnostics.sort(key=lambda d: (d.line, d.col))
    errors = [d for d in diagnostics if d.severity == "error"]
    return {
        "filename": filename,
        "success": not errors,
        "tokens": [tok.to_dict() for tok in tokens],
        "ast": program.to_dict(),
        "tree": ast_to_text(program),
        "dot": ast_to_dot(program),
        "diagnostics": [d.to_dict(lines, filename) for d in diagnostics],
        "executed": executed,
        "output": output,
        "stats": {
            "lines": len(lines),
            "tokens": len(tokens),
            "ast_nodes": program.count(),
            "errors": len(errors),
            "timings_ms": {k: round(v * 1000, 3) for k, v in timings.items()},
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MiniLang compiler front-end")
    ap.add_argument("file", help="MiniLang source file (.ml), or - for stdin")
    ap.add_argument("--tokens", action="store_true", help="print the token stream")
    ap.add_argument("--tree", action="store_true", help="print the AST as an indented tree")
    ap.add_argument("--dot", metavar="OUT", help="write the AST as Graphviz DOT to OUT")
    ap.add_argument("--run", action="store_true", help="execute the program if it compiles")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = ap.parse_args(argv)

    if args.file == "-":
        source, name = sys.stdin.read(), "<stdin>"
    else:
        with open(args.file, encoding="utf-8") as f:
            source, name = f.read(), args.file

    result = compile_source(source, name, run=args.run or args.json)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["success"] else 1

    if args.tokens:
        print(f"{'KIND':<10} {'LEXEME':<14} LINE:COL")
        for t in result["tokens"]:
            print(f"{t['kind']:<10} {t['lexeme']!r:<14} {t['line']}:{t['col']}")
        print()
    if args.tree or not (args.tokens or args.dot or args.run):
        print(result["tree"])
        print()
    if args.dot:
        with open(args.dot, "w", encoding="utf-8") as f:
            f.write(result["dot"] + "\n")
        print(f"DOT written to {args.dot}  (render: dot -Tpng {args.dot} -o ast.png)\n")
    if result["output"] is not None and args.run:
        print("--- program output ---")
        print("\n".join(result["output"]) or "(no output)")
        print()

    for d in result["diagnostics"]:
        print(d["rendered"], end="\n\n", file=sys.stderr)
    n = result["stats"]["errors"]
    print(f"{n} error(s)" if n else "compiled successfully", file=sys.stderr)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # box-drawing chars on Windows consoles
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
