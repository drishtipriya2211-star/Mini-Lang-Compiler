// elseif.ml - 'else if' chains are just nested if statements
let score = 72;

if (score >= 90) {
    print(1);
} else if (score >= 75) {
    print(2);
} else if (score >= 60) {
    print(3);       // this branch runs
} else {
    print(4);
}
