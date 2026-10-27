// precedence.ml - operator precedence and associativity
let a = 2 + 3 * 4;          // * binds tighter than +   -> 14
let b = (2 + 3) * 4;        // parentheses override     -> 20
let c = 10 - 4 - 3;         // left-associative         -> 3
let d = 17 % 5 * 2;         // same level, left to right -> 4
let e = -a + b;             // unary minus binds tightest -> 6

// or < and < not < comparisons < + - < * / %
let ok = a < b and not c == 0 or false;

print(a);
print(b);
print(c);
print(d);
print(e);
print(ok);
