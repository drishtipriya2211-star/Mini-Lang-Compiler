// factorial.ml - iterative factorial using a while loop
let n = 5;
let result = 1;
let i = 1;

while (i <= n) {
    result = result * i;
    i = i + 1;
}

print(result);   // 120
