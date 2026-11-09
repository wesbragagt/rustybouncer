# Bounded pools

Medians/ranges/CV of three complete repetitions; paired ratios use matching repetition numbers. Shared-host observations are not randomized causal estimates, statistical significance or production SLOs. Quantile medians do not pool raw samples across repetitions. Direct has no proxy CPU/RSS.

| Placement | Workload | Clients | Target | Median TPS | TPS min–max | TPS CV % | p95 ms | p99 ms | Peak backends |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| normalized | churn | 10 | pgbouncer-session | 13,782.0 | 13,249.7–13,844.9 | 2.40 | 0.726 | 1.345 | 10 |
| normalized | churn | 10 | pgbouncer-transaction | 14,611.3 | 14,494.8–14,662.7 | 0.59 | 0.586 | 1.065 | 10 |
| normalized | churn | 10 | rustybouncer-pool-session | 9,743.6 | 9,708.7–9,806.5 | 0.51 | 0.900 | 1.303 | 16 |
| normalized | churn | 10 | rustybouncer-pool-transaction | 8,501.0 | 8,364.8–8,534.7 | 1.06 | 1.023 | 1.267 | 16 |
| normalized | churn | 100 | pgbouncer-session | 13,783.3 | 13,543.9–13,783.6 | 1.01 | 11.239 | 15.620 | 16 |
| normalized | churn | 100 | pgbouncer-transaction | 14,931.4 | 14,161.4–15,018.3 | 3.21 | 13.702 | 19.326 | 16 |
| normalized | churn | 100 | rustybouncer-pool-session | 7,732.8 | 7,730.6–7,770.3 | 0.29 | 13.780 | 16.638 | 16 |
| normalized | churn | 100 | rustybouncer-pool-transaction | 6,803.9 | 6,733.4–6,819.9 | 0.68 | 15.612 | 18.620 | 16 |
| normalized | churn | 1000 | pgbouncer-session | 13,698.9 | 12,956.3–14,056.9 | 4.14 | 111.080 | 121.113 | 16 |
| normalized | churn | 1000 | pgbouncer-transaction | 14,424.8 | 13,584.2–14,593.7 | 3.81 | 112.579 | 136.959 | 16 |
| normalized | churn | 1000 | rustybouncer-pool-session | 2,490.0 | 2,196.0–2,593.2 | 8.49 | 451.941 | 585.869 | 16 |
| normalized | churn | 1000 | rustybouncer-pool-transaction | 2,526.2 | 1,701.6–2,546.1 | 21.35 | 422.950 | 431.031 | 16 |
| normalized | transactions | 10 | pgbouncer-session | 51,865.6 | 51,196.4–54,227.1 | 3.04 | 0.236 | 0.354 | 10 |
| normalized | transactions | 10 | pgbouncer-transaction | 52,462.2 | 52,059.1–53,718.9 | 1.64 | 0.238 | 0.353 | 10 |
| normalized | transactions | 10 | rustybouncer-pool-session | 38,673.8 | 38,152.8–38,937.1 | 1.03 | 0.312 | 0.402 | 16 |
| normalized | transactions | 10 | rustybouncer-pool-transaction | 23,022.8 | 22,567.0–23,487.2 | 2.00 | 0.521 | 0.652 | 16 |
| normalized | transactions | 100 | pgbouncer-session | 53,767.8 | 52,848.5–54,087.1 | 1.20 | 0.354 | 0.511 | 16 |
| normalized | transactions | 100 | pgbouncer-transaction | 49,806.0 | 48,292.9–52,009.9 | 3.74 | 2.352 | 2.973 | 16 |
| normalized | transactions | 100 | rustybouncer-pool-session | 38,624.7 | 38,090.5–38,818.9 | 0.98 | 0.496 | 0.669 | 16 |
| normalized | transactions | 100 | rustybouncer-pool-transaction | 10,600.5 | 10,004.5–10,927.3 | 4.45 | 12.077 | 18.077 | 16 |
| normalized | transactions | 1000 | pgbouncer-session | 48,488.1 | 47,098.7–51,245.1 | 4.31 | 0.454 | 0.787 | 16 |
| normalized | transactions | 1000 | pgbouncer-transaction | 41,794.0 | 41,458.9–43,045.5 | 1.99 | 28.449 | 40.412 | 16 |
| normalized | transactions | 1000 | rustybouncer-pool-session | 36,671.2 | 36,448.3–36,687.8 | 0.37 | 0.517 | 1.067 | 16 |
| normalized | transactions | 1000 | rustybouncer-pool-transaction | 3,200.4 | 3,197.6–3,209.0 | 0.19 | 327.912 | 333.317 | 16 |
| unrestricted | churn | 10 | pgbouncer-session | 13,808.5 | 13,722.1–13,967.6 | 0.90 | 0.719 | 1.224 | 10 |
| unrestricted | churn | 10 | pgbouncer-transaction | 14,297.2 | 14,031.7–14,431.2 | 1.43 | 0.654 | 1.256 | 10 |
| unrestricted | churn | 10 | rustybouncer-pool-session | 13,546.8 | 13,408.0–13,582.5 | 0.68 | 0.770 | 1.140 | 16 |
| unrestricted | churn | 10 | rustybouncer-pool-transaction | 12,392.2 | 12,061.8–12,507.7 | 1.88 | 0.890 | 1.396 | 16 |
| unrestricted | churn | 100 | pgbouncer-session | 13,748.2 | 13,719.0–13,834.8 | 0.44 | 11.783 | 15.040 | 16 |
| unrestricted | churn | 100 | pgbouncer-transaction | 14,565.6 | 13,630.4–14,698.8 | 4.07 | 12.490 | 18.889 | 16 |
| unrestricted | churn | 100 | rustybouncer-pool-session | 12,677.5 | 12,599.9–13,252.6 | 2.78 | 10.164 | 12.866 | 16 |
| unrestricted | churn | 100 | rustybouncer-pool-transaction | 12,330.4 | 12,272.0–12,704.8 | 1.89 | 10.147 | 13.587 | 16 |
| unrestricted | churn | 1000 | pgbouncer-session | 12,593.8 | 12,571.8–12,925.0 | 1.56 | 110.626 | 140.064 | 16 |
| unrestricted | churn | 1000 | pgbouncer-transaction | 13,253.9 | 13,187.6–13,447.1 | 1.01 | 115.436 | 135.304 | 16 |
| unrestricted | churn | 1000 | rustybouncer-pool-session | 4,042.5 | 4,006.0–4,124.5 | 1.50 | 264.654 | 295.931 | 16 |
| unrestricted | churn | 1000 | rustybouncer-pool-transaction | 3,546.3 | 3,545.3–3,938.0 | 6.16 | 319.655 | 367.225 | 16 |
| unrestricted | transactions | 10 | pgbouncer-session | 45,071.3 | 43,917.4–45,327.9 | 1.68 | 0.319 | 0.577 | 10 |
| unrestricted | transactions | 10 | pgbouncer-transaction | 43,684.2 | 41,559.4–44,240.1 | 3.28 | 0.352 | 0.628 | 10 |
| unrestricted | transactions | 10 | rustybouncer-pool-session | 35,333.9 | 34,015.1–36,677.1 | 3.77 | 0.420 | 0.740 | 16 |
| unrestricted | transactions | 10 | rustybouncer-pool-transaction | 23,492.2 | 22,295.6–23,804.2 | 3.43 | 0.675 | 1.213 | 16 |
| unrestricted | transactions | 100 | pgbouncer-session | 45,530.7 | 44,201.9–47,277.9 | 3.38 | 0.524 | 0.882 | 16 |
| unrestricted | transactions | 100 | pgbouncer-transaction | 42,500.9 | 42,477.9–45,277.4 | 3.71 | 3.175 | 4.591 | 16 |
| unrestricted | transactions | 100 | rustybouncer-pool-session | 36,386.1 | 35,029.0–37,270.6 | 3.12 | 0.605 | 1.368 | 16 |
| unrestricted | transactions | 100 | rustybouncer-pool-transaction | 18,102.2 | 17,429.4–20,465.1 | 8.54 | 7.767 | 12.120 | 16 |
| unrestricted | transactions | 1000 | pgbouncer-session | 40,461.5 | 38,378.9–42,149.2 | 4.68 | 0.640 | 1.137 | 16 |
| unrestricted | transactions | 1000 | pgbouncer-transaction | 38,722.8 | 38,116.2–38,768.0 | 0.94 | 31.600 | 37.073 | 16 |
| unrestricted | transactions | 1000 | rustybouncer-pool-session | 32,816.4 | 32,726.5–34,605.9 | 3.18 | 0.832 | 1.801 | 16 |
| unrestricted | transactions | 1000 | rustybouncer-pool-transaction | 4,057.0 | 3,889.4–4,160.8 | 3.39 | 274.735 | 285.268 | 16 |
