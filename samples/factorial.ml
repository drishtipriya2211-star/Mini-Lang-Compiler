// factorial.ml - while loop, conditionals and logical operators
let n = 5;
let result = 1;
let i = 1;

while (i <= n and result > 0) {
    result = result * i;
    i = i + 1;
}

if (result == 120 or n < 0) {
    print(result);      // 120
} else {
    print(0);
}
