"""
MiniLang - a small imperative language with a hand-written compiler front-end.

Pipeline:
    source text
      -> Lexer      (regex-based, tracks line/column)        -> tokens
      -> Parser     (recursive descent + table-driven         -> AST
                     precedence climbing, panic-mode recovery)
      -> Semantic   (scoped symbol table, declare-before-use, -> symbols + diagnostics
                     no redeclaration, static type checks)
      -> Visualise  (indented text tree, Graphviz DOT)
      -> CodeGen    (AST -> stack-machine bytecode)           -> bytecode
      -> VM         (stack-based virtual machine)             -> program output
         (or the tree-walking Interpreter, with --engine ast)

Usage (CLI):
    python minilang.py samples/factorial.ml --tokens --tree --run
    python minilang.py samples/demo.ml --symbols --bytecode --run
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
# Semantic analysis: scoped symbol table, declare-before-use, type checking
# ---------------------------------------------------------------------------

INT, BOOL, UNKNOWN = "int", "bool", "?"
ARITHMETIC_OPS = {"+", "-", "*", "/", "%"}
RELATIONAL_OPS = {"<", ">", "<=", ">="}
EQUALITY_OPS = {"==", "!="}
LOGICAL_OPS = {"and", "or"}


@dataclass
class Scope:
    id: int
    kind: str                 # global | block | if-then | else | while-body
    line: int
    parent: int | None
    depth: int
    symbols: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "global" if self.kind == "global" else f"{self.kind} @ line {self.line}"

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name, "line": self.line,
                "parent": self.parent, "depth": self.depth, "symbol_count": len(self.symbols)}


@dataclass
class Symbol:
    name: str
    type: str
    slot: int                 # storage slot assigned for the VM
    scope: Scope
    line: int
    col: int
    uses: int = 0
    assignments: int = 0
    shadows: Symbol | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name, "type": self.type, "slot": self.slot,
            "scope_id": self.scope.id, "scope": self.scope.name, "depth": self.scope.depth,
            "line": self.line, "col": self.col, "uses": self.uses, "assignments": self.assignments,
            "shadows": f"{self.shadows.scope.name} (line {self.shadows.line})" if self.shadows else None,
        }


class SemanticAnalyzer:
    """Walks the AST with a stack of scopes.

    Produces: diagnostics, a report of every scope/symbol, and `resolution`,
    a map from each Let/Assign/Var node to the Symbol it refers to (used by
    the code generator to emit slot numbers instead of names).
    """

    def __init__(self):
        self.scopes: list[Scope] = []        # every scope ever opened, for the report
        self.stack: list[Scope] = []         # currently open scopes
        self.symbols: list[Symbol] = []
        self.resolution: dict[int, Symbol] = {}
        self.diagnostics: list[Diagnostic] = []

    # -- helpers --------------------------------------------------------------

    def error(self, line, col, length, message, notes=(), severity="error"):
        self.diagnostics.append(Diagnostic("semantic", message, line, col, length,
                                           severity=severity, notes=list(notes)))

    def push(self, kind: str, line: int):
        scope = Scope(len(self.scopes), kind, line,
                      self.stack[-1].id if self.stack else None, len(self.stack))
        self.scopes.append(scope)
        self.stack.append(scope)

    def pop(self):
        self.stack.pop()

    def lookup(self, name: str) -> Symbol | None:
        for scope in reversed(self.stack):
            if name in scope.symbols:
                return scope.symbols[name]
        return None

    def report(self) -> dict:
        return {"scopes": [s.to_dict() for s in self.scopes],
                "symbols": [s.to_dict() for s in self.symbols]}

    # -- statements -------------------------------------------------------------

    def analyze(self, program: Program) -> list[Diagnostic]:
        self.push("global", 1)
        for stmt in program.statements:
            self.visit(stmt)
        self.pop()
        for sym in self.symbols:
            if sym.uses == 0:
                self.error(sym.line, sym.col, len(sym.name),
                           f"variable '{sym.name}' is declared but never used", severity="warning")
        return self.diagnostics

    def visit_body(self, node: Node, kind: str):
        # Every if/else/while body gets its own scope, even `if (c) let x = 1;`.
        if isinstance(node, Block):
            self.visit_block(node, kind)
        else:
            self.push(kind, node.line)
            self.visit(node)
            self.pop()

    def visit_block(self, node: Block, kind: str = "block"):
        self.push(kind, node.line)
        for stmt in node.statements:
            self.visit(stmt)
        self.pop()

    def visit(self, node: Node):
        if isinstance(node, Block):
            self.visit_block(node)
        elif isinstance(node, Let):
            value_type = self.type_of(node.value)     # `let x = x;` sees the outer/undeclared x
            name = node.name
            previous = self.stack[-1].symbols.get(name.lexeme)
            if previous:
                self.error(name.line, name.col, len(name.lexeme),
                           f"variable '{name.lexeme}' is already declared in this scope",
                           [f"previous declaration at line {previous.line}, column {previous.col}",
                            f"use '{name.lexeme} = ...;' to assign a new value"])
                return
            sym = Symbol(name.lexeme, value_type, len(self.symbols), self.stack[-1],
                         name.line, name.col, shadows=self.lookup(name.lexeme))
            self.stack[-1].symbols[name.lexeme] = sym
            self.symbols.append(sym)
            self.resolution[id(node)] = sym
        elif isinstance(node, Assign):
            name = node.name.lexeme
            sym = self.lookup(name)
            value_type = self.type_of(node.value)
            if not sym:
                self.error(node.line, node.col, len(name),
                           f"assignment to undeclared variable '{name}'",
                           [f"declare it first with 'let {name} = ...;'"])
                return
            sym.assignments += 1
            self.resolution[id(node)] = sym
            if UNKNOWN not in (sym.type, value_type) and sym.type != value_type:
                self.error(node.line, node.col, len(name),
                           f"cannot assign a {value_type} value to '{name}', which has type {sym.type}",
                           [f"'{name}' was declared at line {sym.line} with a {sym.type} value"])
        elif isinstance(node, Print):
            self.type_of(node.value)
        elif isinstance(node, If):
            self.type_of(node.condition)
            self.visit_body(node.then_branch, "if-then")
            if node.else_branch is not None:
                self.visit_body(node.else_branch, "else")
        elif isinstance(node, While):
            self.type_of(node.condition)
            self.visit_body(node.body, "while-body")

    # -- expressions: resolve names and infer a static type ---------------------

    def type_of(self, node: Node) -> str:
        if isinstance(node, Number):
            return INT
        if isinstance(node, Bool):
            return BOOL
        if isinstance(node, Var):
            sym = self.lookup(node.name)
            if not sym:
                self.error(node.line, node.col, len(node.name), f"use of undeclared variable '{node.name}'")
                return UNKNOWN
            sym.uses += 1
            self.resolution[id(node)] = sym
            return sym.type
        if isinstance(node, Unary):
            operand = self.type_of(node.operand)
            if node.op == "not":
                return BOOL
            if operand == BOOL:
                self.error(node.line, node.col, 1, "unary '-' expects an integer, got bool")
            return INT
        if isinstance(node, Binary):
            left, right = self.type_of(node.left), self.type_of(node.right)
            op = node.op
            if op in LOGICAL_OPS:
                return BOOL
            if op in EQUALITY_OPS:
                if UNKNOWN not in (left, right) and left != right:
                    self.error(node.line, node.col, len(op), f"cannot compare {left} with {right} using '{op}'")
                return BOOL
            if BOOL in (left, right):
                self.error(node.line, node.col, len(op), f"operator '{op}' expects integers, got bool",
                           ["booleans only work with 'and', 'or', 'not', '==' and '!='"])
            return BOOL if op in RELATIONAL_OPS else INT
        return UNKNOWN


# ---------------------------------------------------------------------------
# Runtime helpers shared by the tree-walking interpreter and the stack VM
# ---------------------------------------------------------------------------


class OperationError(Exception):
    pass


class MiniLangRuntimeError(Exception):
    def __init__(self, line: int, col: int, length: int, message: str):
        super().__init__(message)
        self.line, self.col, self.length, self.message = line, col, length, message

    @classmethod
    def at(cls, node: Node, message: str) -> "MiniLangRuntimeError":
        length = len(node.op) if isinstance(node, (Binary, Unary)) else 1
        return cls(node.line, node.col, length, message)


def truthy(value) -> bool:
    return value is True or (not isinstance(value, bool) and value != 0)


def format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _same(a, b) -> bool:
    return a == b and isinstance(a, bool) == isinstance(b, bool)


def apply_unary(op: str, value):
    if op == "not":
        return not truthy(value)
    if isinstance(value, bool):
        raise OperationError("unary '-' expects an integer, got a boolean")
    return -value


def apply_binary(op: str, a, b):
    """Every non-short-circuit binary operator."""
    if op == "==":
        return _same(a, b)
    if op == "!=":
        return not _same(a, b)
    if isinstance(a, bool) or isinstance(b, bool):
        raise OperationError(f"operator '{op}' expects integers, got a boolean")
    if op == "+": return a + b
    if op == "-": return a - b
    if op == "*": return a * b
    if op in ("/", "%"):
        if b == 0:
            raise OperationError("division by zero" if op == "/" else "modulo by zero")
        q = abs(a) // abs(b) * (1 if (a >= 0) == (b >= 0) else -1)   # truncate toward zero, like C
        return q if op == "/" else a - b * q
    if op == "<": return a < b
    if op == ">": return a > b
    if op == "<=": return a <= b
    if op == ">=": return a >= b
    raise OperationError(f"unknown operator '{op}'")


MAX_OUTPUT_LINES = 5_000


# ---------------------------------------------------------------------------
# Engine 1: tree-walking interpreter
# ---------------------------------------------------------------------------


class Interpreter:
    MAX_STEPS = 200_000

    def __init__(self):
        self.scopes: list[dict[str, int | bool]] = [{}]
        self.output: list[str] = []
        self.steps = 0

    def tick(self, node: Node):
        self.steps += 1
        if self.steps > self.MAX_STEPS:
            raise MiniLangRuntimeError.at(node, f"execution exceeded {self.MAX_STEPS:,} steps (infinite loop?)")

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
            if len(self.output) >= MAX_OUTPUT_LINES:
                raise MiniLangRuntimeError.at(node, f"output exceeded {MAX_OUTPUT_LINES:,} lines")
            self.output.append(format_value(self.evaluate(node.value)))
        elif isinstance(node, Block):
            self.execute_scoped_block(node)
        elif isinstance(node, If):
            if truthy(self.evaluate(node.condition)):
                self.execute_scoped(node.then_branch)
            elif node.else_branch is not None:
                self.execute_scoped(node.else_branch)
        elif isinstance(node, While):
            while truthy(self.evaluate(node.condition)):
                self.tick(node)
                self.execute_scoped(node.body)

    def execute_scoped_block(self, node: Block):
        self.scopes.append({})
        try:
            for stmt in node.statements:
                self.execute(stmt)
        finally:
            self.scopes.pop()

    def evaluate(self, node: Node):
        if isinstance(node, (Number, Bool)):
            return node.value
        if isinstance(node, Var):
            return self.find_scope(node.name)[node.name]
        try:
            if isinstance(node, Unary):
                return apply_unary(node.op, self.evaluate(node.operand))
            if isinstance(node, Binary):
                if node.op == "and":
                    return truthy(self.evaluate(node.left)) and truthy(self.evaluate(node.right))
                if node.op == "or":
                    return truthy(self.evaluate(node.left)) or truthy(self.evaluate(node.right))
                return apply_binary(node.op, self.evaluate(node.left), self.evaluate(node.right))
        except OperationError as e:
            raise MiniLangRuntimeError.at(node, str(e)) from None
        raise MiniLangRuntimeError.at(node, f"cannot evaluate {node.label()}")


# ---------------------------------------------------------------------------
# Engine 2: bytecode compiler + stack-based virtual machine
# ---------------------------------------------------------------------------
#
#   CONST v       push constant v                LOAD s     push slot s
#   STORE s       pop into slot s                PRINT      pop and print
#   ADD SUB MUL DIV MOD LT GT LE GE EQ NE        pop b, pop a, push (a op b)
#   NEG NOT       pop a, push (op a)             TO_BOOL    replace top with its truthiness
#   JUMP t        pc = t                         HALT       stop
#   JUMP_IF_FALSE t / JUMP_IF_TRUE t             pop; jump if falsy / truthy

BINARY_OPCODES = {
    "+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
    "<": "LT", ">": "GT", "<=": "LE", ">=": "GE", "==": "EQ", "!=": "NE",
}
OPCODE_OPERATORS = {v: k for k, v in BINARY_OPCODES.items()}
JUMP_OPCODES = {"JUMP", "JUMP_IF_FALSE", "JUMP_IF_TRUE"}


@dataclass
class Instruction:
    op: str
    arg: object = None
    line: int = 0
    col: int = 0
    length: int = 1
    comment: str = ""

    def to_dict(self, addr: int) -> dict:
        if self.arg is None:
            arg = ""
        elif self.op in JUMP_OPCODES:
            arg = f"-> {self.arg}"
        elif self.op == "CONST":
            arg = format_value(self.arg)
        else:
            arg = str(self.arg)
        return {"addr": addr, "op": self.op, "arg": arg, "comment": self.comment,
                "line": self.line, "col": self.col}


class CodeGenerator:
    """Compiles a semantically valid AST to stack-machine bytecode.

    Names were already resolved by the SemanticAnalyzer, so every variable
    access becomes a numbered slot: shadowed variables simply get different slots.
    """

    def __init__(self, resolution: dict[int, Symbol]):
        self.resolution = resolution
        self.code: list[Instruction] = []

    def emit(self, op: str, arg=None, node: Node | None = None, comment: str = "") -> int:
        length = len(node.op) if isinstance(node, (Binary, Unary)) else 1
        self.code.append(Instruction(op, arg, node.line if node else 0, node.col if node else 0, length, comment))
        return len(self.code) - 1

    def patch(self, at: int):
        """Point the jump at `at` to the next instruction to be emitted."""
        self.code[at].arg = len(self.code)

    def generate(self, program: Program) -> list[Instruction]:
        for stmt in program.statements:
            self.statement(stmt)
        self.emit("HALT")
        return self.code

    def statement(self, node: Node):
        if isinstance(node, (Let, Assign)):
            self.expression(node.value)
            sym = self.resolution[id(node)]
            self.emit("STORE", sym.slot, node, f"{'let ' if isinstance(node, Let) else ''}{sym.name}")
        elif isinstance(node, Print):
            self.expression(node.value)
            self.emit("PRINT", node=node)
        elif isinstance(node, Block):
            for stmt in node.statements:
                self.statement(stmt)
        elif isinstance(node, If):
            self.expression(node.condition)
            to_else = self.emit("JUMP_IF_FALSE", node=node, comment="if: skip then-branch")
            self.statement(node.then_branch)
            if node.else_branch is None:
                self.patch(to_else)
            else:
                to_end = self.emit("JUMP", node=node, comment="if: skip else-branch")
                self.patch(to_else)
                self.statement(node.else_branch)
                self.patch(to_end)
        elif isinstance(node, While):
            start = len(self.code)
            self.expression(node.condition)
            to_exit = self.emit("JUMP_IF_FALSE", node=node, comment="while: exit loop")
            self.statement(node.body)
            self.emit("JUMP", start, node, "while: back to condition")
            self.patch(to_exit)

    def expression(self, node: Node):
        if isinstance(node, (Number, Bool)):
            self.emit("CONST", node.value, node)
        elif isinstance(node, Var):
            sym = self.resolution[id(node)]
            self.emit("LOAD", sym.slot, node, sym.name)
        elif isinstance(node, Unary):
            self.expression(node.operand)
            self.emit("NOT" if node.op == "not" else "NEG", node=node)
        elif isinstance(node, Binary) and node.op in LOGICAL_OPS:
            # Short-circuit: `a and b` skips b when a is falsy; `a or b` when a is truthy.
            self.expression(node.left)
            jump = self.emit("JUMP_IF_FALSE" if node.op == "and" else "JUMP_IF_TRUE", node=node,
                             comment=f"{node.op}: short-circuit")
            self.expression(node.right)
            self.emit("TO_BOOL", node=node)
            to_end = self.emit("JUMP", node=node)
            self.patch(jump)
            self.emit("CONST", node.op == "or", node)
            self.patch(to_end)
        elif isinstance(node, Binary):
            self.expression(node.left)
            self.expression(node.right)
            self.emit(BINARY_OPCODES[node.op], node=node)


class VirtualMachine:
    MAX_INSTRUCTIONS = 2_000_000

    def __init__(self, code: list[Instruction], slot_count: int):
        self.code = code
        self.slots: list = [None] * slot_count
        self.stack: list = []
        self.output: list[str] = []
        self.executed = 0
        self.max_stack = 0

    def run(self) -> list[str]:
        code, stack, slots = self.code, self.stack, self.slots
        pc = 0
        while True:
            ins = code[pc]
            pc += 1
            self.executed += 1
            if self.executed > self.MAX_INSTRUCTIONS:
                raise MiniLangRuntimeError(ins.line, ins.col, ins.length,
                                           f"execution exceeded {self.MAX_INSTRUCTIONS:,} instructions (infinite loop?)")
            op = ins.op
            try:
                if op == "CONST":
                    stack.append(ins.arg)
                elif op == "LOAD":
                    stack.append(slots[ins.arg])
                elif op == "STORE":
                    slots[ins.arg] = stack.pop()
                elif op == "PRINT":
                    if len(self.output) >= MAX_OUTPUT_LINES:
                        raise OperationError(f"output exceeded {MAX_OUTPUT_LINES:,} lines")
                    self.output.append(format_value(stack.pop()))
                elif op == "JUMP":
                    pc = ins.arg
                elif op == "JUMP_IF_FALSE":
                    if not truthy(stack.pop()):
                        pc = ins.arg
                elif op == "JUMP_IF_TRUE":
                    if truthy(stack.pop()):
                        pc = ins.arg
                elif op == "TO_BOOL":
                    stack[-1] = truthy(stack[-1])
                elif op == "NEG":
                    stack[-1] = apply_unary("-", stack[-1])
                elif op == "NOT":
                    stack[-1] = apply_unary("not", stack[-1])
                elif op == "HALT":
                    return self.output
                else:
                    b = stack.pop()
                    stack[-1] = apply_binary(OPCODE_OPERATORS[op], stack[-1], b)
            except OperationError as e:
                raise MiniLangRuntimeError(ins.line, ins.col, ins.length, str(e)) from None
            if len(stack) > self.max_stack:
                self.max_stack = len(stack)


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


ENGINES = ("vm", "ast")


def compile_source(source: str, filename: str = "<input>", run: bool = True, engine: str = "vm") -> dict:
    """Run the full pipeline and return a JSON-serialisable result.

    engine: "vm"  - compile to bytecode and run on the stack VM (default)
            "ast" - run the tree-walking interpreter directly on the AST
    """
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}")
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

    # Semantic checks on a partial (error-recovered) AST would mostly be noise,
    # so they only run once the AST was built without lexical/syntax errors.
    analyzer = None
    if not diagnostics:
        t = time.perf_counter()
        analyzer = SemanticAnalyzer()
        diagnostics += analyzer.analyze(program)
        timings["semantic"] = time.perf_counter() - t

    has_errors = any(d.severity == "error" for d in diagnostics)
    bytecode = None
    if analyzer and not has_errors:
        t = time.perf_counter()
        bytecode = CodeGenerator(analyzer.resolution).generate(program)
        timings["codegen"] = time.perf_counter() - t

    output, executed, vm_stats = None, False, None
    if run and bytecode is not None:
        t = time.perf_counter()
        executed = True
        runner = VirtualMachine(bytecode, len(analyzer.symbols)) if engine == "vm" else Interpreter()
        try:
            runner.run() if engine == "vm" else runner.run(program)
        except MiniLangRuntimeError as e:
            diagnostics.append(Diagnostic("runtime", e.message, e.line, e.col, e.length))
        output = runner.output
        if engine == "vm":
            vm_stats = {"instructions_executed": runner.executed, "max_stack_depth": runner.max_stack,
                        "slots": len(runner.slots)}
        timings["run"] = time.perf_counter() - t

    diagnostics.sort(key=lambda d: (d.line, d.col))
    errors = [d for d in diagnostics if d.severity == "error"]
    warnings = [d for d in diagnostics if d.severity == "warning"]
    return {
        "filename": filename,
        "success": not errors,
        "tokens": [tok.to_dict() for tok in tokens],
        "ast": program.to_dict(),
        "tree": ast_to_text(program),
        "dot": ast_to_dot(program),
        "symbols": analyzer.report() if analyzer else None,
        "bytecode": [ins.to_dict(i) for i, ins in enumerate(bytecode)] if bytecode is not None else None,
        "diagnostics": [d.to_dict(lines, filename) for d in diagnostics],
        "engine": engine,
        "executed": executed,
        "output": output,
        "vm": vm_stats,
        "stats": {
            "lines": len(lines),
            "tokens": len(tokens),
            "ast_nodes": program.count(),
            "symbols": len(analyzer.symbols) if analyzer else 0,
            "instructions": len(bytecode) if bytecode is not None else 0,
            "errors": len(errors),
            "warnings": len(warnings),
            "timings_ms": {k: round(v * 1000, 3) for k, v in timings.items()},
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MiniLang compiler front-end")
    ap.add_argument("file", help="MiniLang source file (.ml), or - for stdin")
    ap.add_argument("--tokens", action="store_true", help="print the token stream")
    ap.add_argument("--tree", action="store_true", help="print the AST as an indented tree")
    ap.add_argument("--dot", metavar="OUT", help="write the AST as Graphviz DOT to OUT")
    ap.add_argument("--symbols", action="store_true", help="print the symbol table")
    ap.add_argument("--bytecode", action="store_true", help="print the compiled stack-VM bytecode")
    ap.add_argument("--run", action="store_true", help="execute the program if it compiles")
    ap.add_argument("--engine", choices=ENGINES, default="vm",
                    help="execution engine: stack VM (default) or AST tree-walker")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = ap.parse_args(argv)

    if args.file == "-":
        source, name = sys.stdin.read(), "<stdin>"
    else:
        with open(args.file, encoding="utf-8") as f:
            source, name = f.read(), args.file

    result = compile_source(source, name, run=args.run or args.json, engine=args.engine)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["success"] else 1

    if args.tokens:
        print(f"{'KIND':<10} {'LEXEME':<14} LINE:COL")
        for t in result["tokens"]:
            print(f"{t['kind']:<10} {t['lexeme']!r:<14} {t['line']}:{t['col']}")
        print()
    if args.tree or not (args.tokens or args.dot or args.run or args.symbols or args.bytecode):
        print(result["tree"])
        print()
    if args.symbols and result["symbols"]:
        print(f"{'NAME':<12} {'TYPE':<5} {'SLOT':<5} {'SCOPE':<24} {'DECL':<8} USES  ASSIGNS")
        for s in result["symbols"]["symbols"]:
            scope = "  " * s["depth"] + s["scope"]
            print(f"{s['name']:<12} {s['type']:<5} {s['slot']:<5} {scope:<24} "
                  f"{str(s['line']) + ':' + str(s['col']):<8} {s['uses']:<5} {s['assignments']}")
        print()
    if args.bytecode and result["bytecode"]:
        for ins in result["bytecode"]:
            comment = f"; {ins['comment']}" if ins["comment"] else ""
            print(f"{ins['addr']:>4}  {ins['op']:<14} {ins['arg']:<8} {comment}")
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
    n, w = result["stats"]["errors"], result["stats"]["warnings"]
    summary = f"{n} error(s)" if n else "compiled successfully"
    print(summary + (f", {w} warning(s)" if w else ""), file=sys.stderr)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # box-drawing chars on Windows consoles
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
