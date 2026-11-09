# Final performance comparison

Every repetition retained. Three shared-host runs give descriptive ranges, CV and paired repetition ratios; no outlier removal, causal isolation, statistical significance or production SLO is established. Before/after batches are separated in time. Instrumented observations never stand in for normal timing. Broader canonical release criteria remain open.

Runtime source: `14f077e`; complete harness checkpoint: `e4e9968`.

Original `rustybouncer-session` means passthrough, and `rustybouncer-routed` means unshared routing. Only `rustybouncer-pool-*` enables shared pooling.

All 30 before +30 final original lookup arms, 30 equal-core arms, 144 before +144 final pool arms, 180 additional workload arms and 96 shared-pool mix arms are retained. All normal measured transactions report zero query errors. The separate 24 fault experiments retain expected outage errors and require zero unexpected errors.

## Original lookup before scheduling

| Clients | Target | Median Q/s | Q/s min–max | CV % | p50 ms | p95 ms | p99 ms | Peak backends |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | direct | 168,480 | 147,323–172,459 | 8.30 | 0.049 | 0.109 | 0.199 | 10 |
| 10 | pgbouncer-session | 130,400 | 113,008–132,368 | 8.51 | 0.068 | 0.121 | 0.217 | 10 |
| 10 | pgbouncer-transaction | 132,038 | 125,344–132,118 | 2.99 | 0.068 | 0.114 | 0.195 | 10 |
| 10 | rustybouncer-session | 127,221 | 24,584–128,305 | 63.80 | 0.069 | 0.130 | 0.234 | 10 |
| 10 | rustybouncer-routed | 106,739 | 54,908–110,084 | 34.15 | 0.086 | 0.144 | 0.262 | 10 |
| 100 | direct | 272,486 | 248,773–273,751 | 5.31 | 0.239 | 0.997 | 2.392 | 100 |
| 100 | pgbouncer-session | 123,215 | 108,260–130,356 | 9.35 | 0.745 | 1.260 | 1.864 | 100 |
| 100 | pgbouncer-transaction | 125,824 | 116,973–129,221 | 5.10 | 0.731 | 1.301 | 1.939 | 100 |
| 100 | rustybouncer-session | 138,279 | 120,446–149,366 | 10.73 | 0.636 | 1.449 | 2.327 | 100 |
| 100 | rustybouncer-routed | 133,547 | 116,871–134,215 | 7.66 | 0.657 | 1.473 | 2.300 | 100 |


## Original lookup after scheduling

| Clients | Target | Median Q/s | Q/s min–max | CV % | p50 ms | p95 ms | p99 ms | Peak backends |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | direct | 163,990 | 154,520–226,158 | 21.43 | 0.050 | 0.114 | 0.203 | 10 |
| 10 | pgbouncer-session | 134,587 | 120,631–156,898 | 13.32 | 0.067 | 0.113 | 0.188 | 10 |
| 10 | pgbouncer-transaction | 124,426 | 123,306–128,365 | 2.12 | 0.070 | 0.128 | 0.224 | 10 |
| 10 | rustybouncer-session | 115,749 | 114,180–138,382 | 11.03 | 0.075 | 0.152 | 0.273 | 10 |
| 10 | rustybouncer-routed | 104,552 | 96,193–136,728 | 19.03 | 0.087 | 0.150 | 0.275 | 10 |
| 100 | direct | 334,233 | 313,527–334,989 | 3.72 | 0.203 | 0.804 | 1.723 | 100 |
| 100 | pgbouncer-session | 155,008 | 152,731–155,783 | 1.03 | 0.621 | 0.788 | 1.107 | 100 |
| 100 | pgbouncer-transaction | 148,795 | 138,201–152,011 | 4.94 | 0.620 | 0.979 | 1.364 | 100 |
| 100 | rustybouncer-session | 182,268 | 178,057–185,334 | 2.01 | 0.519 | 0.963 | 1.519 | 100 |
| 100 | rustybouncer-routed | 171,531 | 164,624–174,132 | 2.89 | 0.523 | 1.103 | 1.607 | 100 |


## Original lookup with one proxy CPU

| Clients | Target | Median Q/s | Q/s min–max | CV % | p50 ms | p95 ms | p99 ms | Peak backends |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | direct | 202,304 | 201,826–207,442 | 1.53 | 0.043 | 0.082 | 0.135 | 10 |
| 10 | pgbouncer-session | 84,595 | 70,965–105,257 | 19.86 | 0.059 | 0.099 | 3.000 | 10 |
| 10 | pgbouncer-transaction | 75,867 | 71,112–103,620 | 21.02 | 0.061 | 0.100 | 3.015 | 10 |
| 10 | rustybouncer-session | 76,214 | 67,734–101,252 | 21.32 | 0.064 | 0.100 | 3.021 | 10 |
| 10 | rustybouncer-routed | 63,627 | 39,325–71,515 | 28.85 | 0.085 | 0.118 | 3.124 | 10 |
| 100 | direct | 304,315 | 295,300–311,001 | 2.60 | 0.220 | 0.892 | 1.939 | 100 |
| 100 | pgbouncer-session | 101,322 | 81,834–104,976 | 12.95 | 0.620 | 3.748 | 4.917 | 100 |
| 100 | pgbouncer-transaction | 105,011 | 103,798–106,054 | 1.08 | 0.608 | 3.757 | 4.328 | 100 |
| 100 | rustybouncer-session | 94,388 | 87,447–95,253 | 4.63 | 0.672 | 3.818 | 6.830 | 100 |
| 100 | rustybouncer-routed | 60,953 | 58,475–61,908 | 2.93 | 1.053 | 4.456 | 6.874 | 100 |


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


# Additional workloads

Medians/ranges/CV of three complete repetitions; paired ratios use matching repetition numbers. Shared-host observations are not randomized causal estimates, statistical significance or production SLOs. Quantile medians do not pool raw samples across repetitions. Direct has no proxy CPU/RSS.

| Placement | Workload | Clients | Target | Median TPS | TPS min–max | TPS CV % | p95 ms | p99 ms | Peak backends |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| normalized | cte | 10 | direct | 157,297.4 | 151,599.3–168,919.0 | 5.54 | 0.103 | 0.161 | 10 |
| normalized | cte | 10 | pgbouncer-session | 136,392.8 | 97,108.3–136,414.0 | 18.40 | 0.104 | 0.165 | 10 |
| normalized | cte | 10 | pgbouncer-transaction | 98,481.3 | 94,758.3–137,049.6 | 21.27 | 0.143 | 0.860 | 10 |
| normalized | cte | 10 | rustybouncer-routed | 29,890.8 | 29,632.7–54,522.3 | 37.61 | 0.457 | 3.344 | 10 |
| normalized | cte | 10 | rustybouncer-session | 95,802.5 | 92,721.2–126,428.3 | 17.75 | 0.138 | 1.069 | 10 |
| normalized | cte | 100 | direct | 238,995.1 | 235,409.4–240,240.3 | 1.05 | 1.011 | 2.525 | 100 |
| normalized | cte | 100 | pgbouncer-session | 131,268.5 | 126,502.7–135,058.6 | 3.27 | 1.066 | 1.859 | 100 |
| normalized | cte | 100 | pgbouncer-transaction | 133,065.1 | 93,633.3–133,652.9 | 19.10 | 1.144 | 1.702 | 100 |
| normalized | cte | 100 | rustybouncer-routed | 51,642.9 | 50,269.0–51,836.6 | 1.67 | 2.644 | 3.112 | 100 |
| normalized | cte | 100 | rustybouncer-session | 130,418.4 | 130,316.6–132,093.8 | 0.76 | 1.026 | 1.527 | 100 |
| normalized | large | 10 | direct | 10,889.7 | 10,252.9–11,520.1 | 5.82 | 1.494 | 1.974 | 10 |
| normalized | large | 10 | pgbouncer-session | 3,326.1 | 2,278.3–3,443.3 | 21.27 | 3.624 | 4.529 | 10 |
| normalized | large | 10 | pgbouncer-transaction | 3,461.7 | 2,258.3–3,498.0 | 22.96 | 3.339 | 4.331 | 10 |
| normalized | large | 10 | rustybouncer-routed | 3,843.6 | 3,705.9–6,021.6 | 28.72 | 4.997 | 5.684 | 10 |
| normalized | large | 10 | rustybouncer-session | 4,923.2 | 4,832.5–6,727.8 | 19.46 | 4.587 | 5.153 | 10 |
| normalized | large | 100 | direct | 14,448.8 | 14,264.6–14,476.5 | 0.80 | 21.170 | 46.507 | 100 |
| normalized | large | 100 | pgbouncer-session | 3,154.1 | 3,142.0–3,218.4 | 1.30 | 35.532 | 39.352 | 100 |
| normalized | large | 100 | pgbouncer-transaction | 3,118.0 | 2,969.8–3,152.8 | 3.16 | 35.568 | 43.704 | 100 |
| normalized | large | 100 | rustybouncer-routed | 5,397.5 | 5,181.5–5,483.7 | 2.91 | 23.867 | 27.993 | 100 |
| normalized | large | 100 | rustybouncer-session | 5,984.9 | 5,633.4–6,004.0 | 3.55 | 18.615 | 21.340 | 100 |
| normalized | mixed | 10 | direct | 4,897.5 | 4,784.9–4,950.6 | 1.73 | 20.118 | 20.156 | 10 |
| normalized | mixed | 10 | pgbouncer-session | 4,736.9 | 4,535.4–4,784.1 | 2.82 | 20.136 | 20.191 | 10 |
| normalized | mixed | 10 | pgbouncer-transaction | 4,865.6 | 4,786.4–4,873.2 | 0.99 | 20.135 | 20.188 | 10 |
| normalized | mixed | 10 | rustybouncer-routed | 4,722.4 | 4,670.6–4,847.3 | 1.91 | 20.170 | 20.479 | 10 |
| normalized | mixed | 10 | rustybouncer-session | 4,818.2 | 4,492.8–4,934.5 | 4.82 | 20.133 | 20.190 | 10 |
| normalized | mixed | 100 | direct | 48,695.1 | 48,557.8–49,129.1 | 0.61 | 20.106 | 20.125 | 100 |
| normalized | mixed | 100 | pgbouncer-session | 48,192.7 | 46,895.4–48,436.6 | 1.73 | 20.124 | 20.170 | 100 |
| normalized | mixed | 100 | pgbouncer-transaction | 47,875.8 | 47,474.1–47,906.9 | 0.51 | 20.131 | 20.175 | 100 |
| normalized | mixed | 100 | rustybouncer-routed | 46,446.9 | 45,898.0–47,911.1 | 2.23 | 20.155 | 20.300 | 100 |
| normalized | mixed | 100 | rustybouncer-session | 47,922.0 | 47,145.7–48,378.5 | 1.30 | 20.131 | 20.185 | 100 |
| unrestricted | cte | 10 | direct | 155,917.5 | 151,012.8–156,548.2 | 1.96 | 0.104 | 0.161 | 10 |
| unrestricted | cte | 10 | pgbouncer-session | 126,871.7 | 122,420.6–129,139.0 | 2.71 | 0.117 | 0.191 | 10 |
| unrestricted | cte | 10 | pgbouncer-transaction | 128,812.2 | 127,452.0–139,032.8 | 4.80 | 0.112 | 0.172 | 10 |
| unrestricted | cte | 10 | rustybouncer-routed | 78,918.7 | 73,081.5–81,712.8 | 5.65 | 0.205 | 0.287 | 10 |
| unrestricted | cte | 10 | rustybouncer-session | 127,996.1 | 121,136.0–135,671.4 | 5.67 | 0.118 | 0.188 | 10 |
| unrestricted | cte | 100 | direct | 247,916.5 | 237,988.0–253,114.5 | 3.12 | 0.961 | 2.431 | 100 |
| unrestricted | cte | 100 | pgbouncer-session | 124,049.8 | 121,818.8–128,574.9 | 2.76 | 1.202 | 1.931 | 100 |
| unrestricted | cte | 100 | pgbouncer-transaction | 124,985.9 | 124,288.6–128,157.8 | 1.64 | 1.233 | 1.771 | 100 |
| unrestricted | cte | 100 | rustybouncer-routed | 98,643.9 | 96,544.1–106,401.2 | 5.16 | 1.837 | 2.580 | 100 |
| unrestricted | cte | 100 | rustybouncer-session | 141,209.4 | 139,877.1–149,132.6 | 3.49 | 1.349 | 2.063 | 100 |
| unrestricted | large | 10 | direct | 11,287.4 | 11,203.9–11,877.2 | 3.20 | 1.431 | 1.857 | 10 |
| unrestricted | large | 10 | pgbouncer-session | 3,538.3 | 2,987.9–3,620.1 | 10.17 | 3.235 | 3.758 | 10 |
| unrestricted | large | 10 | pgbouncer-transaction | 3,585.9 | 3,324.5–3,603.9 | 4.46 | 3.175 | 3.797 | 10 |
| unrestricted | large | 10 | rustybouncer-routed | 8,633.7 | 8,237.7–9,010.3 | 4.48 | 1.779 | 2.438 | 10 |
| unrestricted | large | 10 | rustybouncer-session | 9,326.1 | 9,291.6–9,526.6 | 1.35 | 1.652 | 2.273 | 10 |
| unrestricted | large | 100 | direct | 15,139.3 | 14,653.2–15,358.6 | 2.40 | 18.461 | 42.673 | 100 |
| unrestricted | large | 100 | pgbouncer-session | 3,223.5 | 2,771.8–3,294.5 | 9.16 | 34.627 | 42.620 | 100 |
| unrestricted | large | 100 | pgbouncer-transaction | 3,115.3 | 2,769.1–3,183.3 | 7.35 | 35.588 | 39.600 | 100 |
| unrestricted | large | 100 | rustybouncer-routed | 9,704.0 | 9,567.3–9,989.5 | 2.21 | 22.800 | 34.315 | 100 |
| unrestricted | large | 100 | rustybouncer-session | 11,001.6 | 10,450.3–11,269.7 | 3.83 | 20.037 | 31.617 | 100 |
| unrestricted | mixed | 10 | direct | 4,893.6 | 4,780.0–4,923.8 | 1.56 | 20.118 | 20.154 | 10 |
| unrestricted | mixed | 10 | pgbouncer-session | 4,880.1 | 4,844.2–4,982.0 | 1.46 | 20.133 | 20.171 | 10 |
| unrestricted | mixed | 10 | pgbouncer-transaction | 4,896.6 | 4,831.4–4,899.6 | 0.79 | 20.134 | 20.171 | 10 |
| unrestricted | mixed | 10 | rustybouncer-routed | 4,828.1 | 4,822.3–4,840.4 | 0.19 | 20.140 | 20.181 | 10 |
| unrestricted | mixed | 10 | rustybouncer-session | 4,871.4 | 4,761.8–4,897.3 | 1.49 | 20.133 | 20.171 | 10 |
| unrestricted | mixed | 100 | direct | 48,734.0 | 48,635.6–49,006.4 | 0.39 | 20.108 | 20.130 | 100 |
| unrestricted | mixed | 100 | pgbouncer-session | 48,134.6 | 47,717.1–48,869.9 | 1.21 | 20.120 | 20.144 | 100 |
| unrestricted | mixed | 100 | pgbouncer-transaction | 48,377.9 | 48,018.1–48,752.8 | 0.76 | 20.118 | 20.139 | 100 |
| unrestricted | mixed | 100 | rustybouncer-routed | 48,341.1 | 48,269.4–48,408.8 | 0.14 | 20.130 | 20.158 | 100 |
| unrestricted | mixed | 100 | rustybouncer-session | 48,411.3 | 48,249.5–48,807.1 | 0.59 | 20.120 | 20.146 | 100 |


## Shared-pool duration mix (before)

Shared-pool short/long mix, before candidate. Full logging and per-client completion; all repetitions retained.

| Placement | Workload | Clients | Target | Median TPS | TPS min–max | TPS CV % | p95 ms | p99 ms | Peak backends |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| normalized | mixed | 10 | pgbouncer-session | 4,728.2 | 4,531.1–4,781.1 | 2.82 | 20.152 | 20.276 | 10 |
| normalized | mixed | 10 | pgbouncer-transaction | 4,739.6 | 4,712.8–4,741.5 | 0.34 | 20.151 | 20.308 | 10 |
| normalized | mixed | 10 | rustybouncer-pool-session | 4,776.2 | 4,767.9–4,778.1 | 0.11 | 20.142 | 20.203 | 16 |
| normalized | mixed | 10 | rustybouncer-pool-transaction | 4,711.1 | 4,596.5–4,758.0 | 1.77 | 20.177 | 20.377 | 16 |
| normalized | mixed | 100 | pgbouncer-session | 7,537.8 | 7,407.9–7,800.0 | 2.63 | 20.167 | 20.519 | 16 |
| normalized | mixed | 100 | pgbouncer-transaction | 7,611.8 | 7,549.4–7,656.0 | 0.70 | 31.232 | 37.943 | 16 |
| normalized | mixed | 100 | rustybouncer-pool-session | 7,657.8 | 7,636.5–7,791.6 | 1.09 | 20.133 | 20.181 | 16 |
| normalized | mixed | 100 | rustybouncer-pool-transaction | 6,227.8 | 5,550.0–7,057.8 | 12.03 | 33.981 | 41.017 | 16 |
| unrestricted | mixed | 10 | pgbouncer-session | 4,833.1 | 4,822.2–4,891.3 | 0.77 | 20.136 | 20.184 | 10 |
| unrestricted | mixed | 10 | pgbouncer-transaction | 4,846.0 | 4,774.2–4,948.0 | 1.80 | 20.133 | 20.171 | 10 |
| unrestricted | mixed | 10 | rustybouncer-pool-session | 4,769.9 | 4,718.9–4,818.6 | 1.05 | 20.143 | 20.203 | 16 |
| unrestricted | mixed | 10 | rustybouncer-pool-transaction | 4,892.3 | 4,833.9–4,970.9 | 1.40 | 20.163 | 20.218 | 16 |
| unrestricted | mixed | 100 | pgbouncer-session | 7,680.8 | 7,659.0–7,738.9 | 0.54 | 20.131 | 20.173 | 16 |
| unrestricted | mixed | 100 | pgbouncer-transaction | 7,715.5 | 7,644.8–7,901.0 | 1.71 | 31.022 | 39.298 | 16 |
| unrestricted | mixed | 100 | rustybouncer-pool-session | 7,682.0 | 7,596.7–7,712.8 | 0.78 | 20.137 | 20.181 | 16 |
| unrestricted | mixed | 100 | rustybouncer-pool-transaction | 7,464.1 | 7,412.2–7,490.2 | 0.53 | 31.353 | 37.201 | 16 |


## Shared-pool duration mix (after)

Shared-pool short/long mix, after candidate. Full logging and per-client completion; all repetitions retained.

| Placement | Workload | Clients | Target | Median TPS | TPS min–max | TPS CV % | p95 ms | p99 ms | Peak backends |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| normalized | mixed | 10 | pgbouncer-session | 4,732.4 | 4,526.7–4,758.1 | 2.71 | 20.143 | 20.281 | 10 |
| normalized | mixed | 10 | pgbouncer-transaction | 4,730.8 | 4,727.9–4,818.3 | 1.08 | 20.135 | 20.187 | 10 |
| normalized | mixed | 10 | rustybouncer-pool-session | 4,712.9 | 4,544.7–4,745.7 | 2.31 | 20.148 | 20.274 | 16 |
| normalized | mixed | 10 | rustybouncer-pool-transaction | 4,764.2 | 4,651.3–4,808.6 | 1.71 | 20.162 | 20.261 | 16 |
| normalized | mixed | 100 | pgbouncer-session | 7,552.7 | 7,465.6–7,886.7 | 2.91 | 20.164 | 20.565 | 16 |
| normalized | mixed | 100 | pgbouncer-transaction | 7,572.8 | 7,523.0–7,582.4 | 0.42 | 31.317 | 38.395 | 16 |
| normalized | mixed | 100 | rustybouncer-pool-session | 7,604.6 | 7,514.8–7,716.9 | 1.33 | 20.139 | 20.486 | 16 |
| normalized | mixed | 100 | rustybouncer-pool-transaction | 7,581.4 | 7,398.4–7,668.4 | 1.83 | 31.486 | 37.844 | 16 |
| unrestricted | mixed | 10 | pgbouncer-session | 4,792.3 | 4,755.9–4,807.7 | 0.56 | 20.133 | 20.169 | 10 |
| unrestricted | mixed | 10 | pgbouncer-transaction | 4,818.9 | 4,806.4–4,844.9 | 0.41 | 20.130 | 20.168 | 10 |
| unrestricted | mixed | 10 | rustybouncer-pool-session | 4,817.5 | 4,801.7–4,830.4 | 0.30 | 20.140 | 20.186 | 16 |
| unrestricted | mixed | 10 | rustybouncer-pool-transaction | 4,776.9 | 4,705.6–4,913.5 | 2.20 | 20.170 | 20.220 | 16 |
| unrestricted | mixed | 100 | pgbouncer-session | 7,749.9 | 7,623.7–7,897.1 | 1.76 | 20.128 | 20.164 | 16 |
| unrestricted | mixed | 100 | pgbouncer-transaction | 7,722.6 | 7,716.5–7,766.2 | 0.35 | 33.821 | 39.412 | 16 |
| unrestricted | mixed | 100 | rustybouncer-pool-session | 7,766.1 | 7,590.2–7,790.6 | 1.42 | 20.136 | 20.175 | 16 |
| unrestricted | mixed | 100 | rustybouncer-pool-transaction | 7,808.3 | 7,665.6–7,820.2 | 1.11 | 30.960 | 37.474 | 16 |
