// demo.ml - a tour of every MiniLang feature

// integers, arithmetic and precedence
let a = 17;
let b = 5;
print(a + b * 2);           // 27
print((a + b) * 2);         // 44
print(a / b);               // 3   (integer division)
print(a % b);               // 2
print(-a + b);              // -12

// booleans, relational and logical operators (short-circuit)
let big = a > 10;
let even = a % 2 == 0;
print(big and not even);    // true
print(even or b >= 5);      // true

// if / else if / else
if (a < 10) {
    print(1);
} else if (a < 20) {
    print(2);               // this branch runs
} else {
    print(3);
}

// while loop: sum of 1..10
let i = 1;
let sum = 0;
while (i <= 10) {
    sum = sum + i;
    i = i + 1;
}
print(sum);                 // 55

// nested scopes and shadowing
let x = 1;
{
    let x = 100;            // a new x that shadows the outer one
    print(x);               // 100
}
print(x);                   // 1

// Fibonacci numbers below 50
let f0 = 0;
let f1 = 1;
while (f0 < 50) {
    print(f0);
    let next = f0 + f1;     // declared fresh on every iteration
    f0 = f1;
    f1 = next;
}
