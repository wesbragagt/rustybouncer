# Bounded pools

Medians/ranges/CV of three complete repetitions; paired ratios use matching repetition numbers. Shared-host observations are not randomized causal estimates, statistical significance or production SLOs. Quantile medians do not pool raw samples across repetitions. Direct has no proxy CPU/RSS.

| Placement | Workload | Clients | Target | Median TPS | TPS min–max | TPS CV % | p95 ms | p99 ms | Peak backends |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| normalized | churn | 10 | pgbouncer-session | 13,909.1 | 13,844.0–13,943.8 | 0.36 | 0.668 | 1.174 | 10 |
| normalized | churn | 10 | pgbouncer-transaction | 14,354.2 | 14,013.1–14,518.5 | 1.80 | 0.617 | 1.140 | 10 |
| normalized | churn | 10 | rustybouncer-pool-session | 9,837.9 | 9,561.5–9,925.3 | 1.94 | 0.871 | 1.237 | 16 |
| normalized | churn | 10 | rustybouncer-pool-transaction | 8,457.6 | 8,418.5–8,460.0 | 0.28 | 1.021 | 1.218 | 16 |
| normalized | churn | 100 | pgbouncer-session | 13,727.5 | 13,705.7–13,929.7 | 0.90 | 11.280 | 13.904 | 16 |
| normalized | churn | 100 | pgbouncer-transaction | 14,987.1 | 14,864.4–15,027.5 | 0.57 | 13.078 | 17.326 | 16 |
| normalized | churn | 100 | rustybouncer-pool-session | 9,225.9 | 9,207.9–9,264.9 | 0.32 | 11.509 | 13.448 | 16 |
| normalized | churn | 100 | rustybouncer-pool-transaction | 7,798.3 | 7,784.7–7,818.8 | 0.22 | 13.793 | 15.563 | 16 |
| normalized | churn | 1000 | pgbouncer-session | 12,840.2 | 11,216.3–12,945.6 | 7.86 | 103.121 | 116.126 | 16 |
| normalized | churn | 1000 | pgbouncer-transaction | 13,276.8 | 13,166.7–13,322.9 | 0.61 | 112.195 | 129.610 | 16 |
| normalized | churn | 1000 | rustybouncer-pool-session | 5,544.8 | 5,508.4–8,797.5 | 28.54 | 188.485 | 193.964 | 16 |
| normalized | churn | 1000 | rustybouncer-pool-transaction | 7,213.1 | 4,369.6–7,548.9 | 27.39 | 158.086 | 165.456 | 16 |
| normalized | transactions | 10 | pgbouncer-session | 43,790.1 | 33,159.5–43,821.5 | 15.27 | 0.351 | 0.640 | 10 |
| normalized | transactions | 10 | pgbouncer-transaction | 42,428.9 | 32,963.6–46,059.6 | 16.70 | 0.333 | 0.802 | 10 |
| normalized | transactions | 10 | rustybouncer-pool-session | 31,454.5 | 22,190.1–33,065.0 | 20.31 | 0.372 | 1.538 | 16 |
| normalized | transactions | 10 | rustybouncer-pool-transaction | 21,856.1 | 12,620.3–21,996.9 | 28.54 | 0.570 | 0.752 | 16 |
| normalized | transactions | 100 | pgbouncer-session | 35,542.5 | 34,838.0–42,056.4 | 10.62 | 0.830 | 3.517 | 16 |
| normalized | transactions | 100 | pgbouncer-transaction | 44,487.4 | 43,602.6–45,859.3 | 2.55 | 3.002 | 4.414 | 16 |
| normalized | transactions | 100 | rustybouncer-pool-session | 35,651.3 | 35,559.5–35,958.2 | 0.58 | 0.559 | 0.797 | 16 |
| normalized | transactions | 100 | rustybouncer-pool-transaction | 13,586.6 | 10,934.8–14,225.7 | 13.51 | 11.580 | 12.717 | 16 |
| normalized | transactions | 1000 | pgbouncer-session | 32,922.5 | 32,038.1–33,977.7 | 2.94 | 1.374 | 3.446 | 16 |
| normalized | transactions | 1000 | pgbouncer-transaction | 26,913.8 | 26,119.3–26,988.8 | 1.81 | 43.179 | 48.211 | 16 |
| normalized | transactions | 1000 | rustybouncer-pool-session | 21,512.4 | 21,251.6–21,699.4 | 1.05 | 3.513 | 4.153 | 16 |
| normalized | transactions | 1000 | rustybouncer-pool-transaction | 9,677.1 | 8,914.9–13,316.1 | 22.11 | 112.431 | 118.071 | 16 |
| unrestricted | churn | 10 | pgbouncer-session | 13,760.8 | 13,658.0–13,906.3 | 0.91 | 0.738 | 1.298 | 10 |
| unrestricted | churn | 10 | pgbouncer-transaction | 14,399.3 | 14,015.2–14,795.3 | 2.71 | 0.621 | 1.218 | 10 |
| unrestricted | churn | 10 | rustybouncer-pool-session | 12,733.1 | 11,538.6–13,209.3 | 6.89 | 0.851 | 1.390 | 16 |
| unrestricted | churn | 10 | rustybouncer-pool-transaction | 12,223.6 | 11,397.8–12,561.3 | 4.96 | 0.904 | 1.397 | 16 |
| unrestricted | churn | 100 | pgbouncer-session | 13,899.5 | 13,701.2–13,931.5 | 0.90 | 12.189 | 17.328 | 16 |
| unrestricted | churn | 100 | pgbouncer-transaction | 15,143.9 | 14,696.6–15,192.9 | 1.82 | 13.740 | 18.171 | 16 |
| unrestricted | churn | 100 | rustybouncer-pool-session | 13,582.2 | 13,408.2–13,702.6 | 1.09 | 9.387 | 12.077 | 16 |
| unrestricted | churn | 100 | rustybouncer-pool-transaction | 13,102.6 | 12,846.7–13,455.1 | 2.33 | 9.364 | 12.434 | 16 |
| unrestricted | churn | 1000 | pgbouncer-session | 12,841.4 | 12,728.6–12,939.2 | 0.82 | 122.247 | 141.626 | 16 |
| unrestricted | churn | 1000 | pgbouncer-transaction | 13,443.5 | 13,106.1–13,549.4 | 1.73 | 125.342 | 141.578 | 16 |
| unrestricted | churn | 1000 | rustybouncer-pool-session | 12,671.1 | 12,559.0–12,754.5 | 0.78 | 91.876 | 103.779 | 16 |
| unrestricted | churn | 1000 | rustybouncer-pool-transaction | 12,134.9 | 12,054.4–12,513.3 | 2.00 | 98.776 | 118.084 | 16 |
| unrestricted | transactions | 10 | pgbouncer-session | 45,264.4 | 41,570.9–45,864.7 | 5.26 | 0.315 | 0.498 | 10 |
| unrestricted | transactions | 10 | pgbouncer-transaction | 45,328.7 | 44,596.7–45,351.8 | 0.95 | 0.310 | 0.482 | 10 |
| unrestricted | transactions | 10 | rustybouncer-pool-session | 37,333.2 | 34,430.9–37,691.4 | 4.90 | 0.382 | 0.690 | 16 |
| unrestricted | transactions | 10 | rustybouncer-pool-transaction | 24,390.4 | 23,094.3–25,629.0 | 5.20 | 0.643 | 1.031 | 16 |
| unrestricted | transactions | 100 | pgbouncer-session | 46,180.1 | 42,563.3–47,470.4 | 5.60 | 0.510 | 0.976 | 16 |
| unrestricted | transactions | 100 | pgbouncer-transaction | 44,890.7 | 44,734.4–44,954.7 | 0.25 | 2.874 | 3.898 | 16 |
| unrestricted | transactions | 100 | rustybouncer-pool-session | 36,409.4 | 36,106.3–36,879.1 | 1.07 | 0.635 | 1.397 | 16 |
| unrestricted | transactions | 100 | rustybouncer-pool-transaction | 24,012.7 | 22,332.9–24,677.1 | 5.10 | 5.325 | 6.835 | 16 |
| unrestricted | transactions | 1000 | pgbouncer-session | 42,665.3 | 42,577.3–43,197.0 | 0.78 | 0.563 | 0.979 | 16 |
| unrestricted | transactions | 1000 | pgbouncer-transaction | 39,034.3 | 38,238.8–39,552.5 | 1.70 | 31.264 | 39.724 | 16 |
| unrestricted | transactions | 1000 | rustybouncer-pool-session | 34,164.6 | 34,094.8–34,483.2 | 0.60 | 0.735 | 1.716 | 16 |
| unrestricted | transactions | 1000 | rustybouncer-pool-transaction | 21,624.0 | 21,154.6–22,227.0 | 2.48 | 54.825 | 88.895 | 16 |
