// scope.ml - syntactically fine, but breaks the scoping rules
let total = 0;
{
    let inner = 5;
    total = total + inner;
}
print(inner);        // inner is out of scope here
let total = 1;       // redeclaration in the same scope
count = 3;           // never declared
