// primes.ml - print every prime below 30 (nested loops + logic)
let n = 2;
while (n < 30) {
    let d = 2;
    let prime = true;
    while (d * d <= n and prime) {
        if (n % d == 0) {
            prime = false;
        }
        d = d + 1;
    }
    if (prime) {
        print(n);
    }
    n = n + 1;
}
