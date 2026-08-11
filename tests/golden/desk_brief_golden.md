# Desk Brief — 2026-08-10

Snapshot 2026-08-10 (opps_2026-08-10.csv), evaluated as of 2026-08-10.

## Headline

- Desk score: 87.7 amount-weighted mean / 95.0 median
- Healthy opps (score >= 80): 80.2% of 368 open
- Open pipeline: $43,165,232
- At-risk dollars (distinct opps with a high-severity violation): $12,081,072
- Violations: 139 high, 100 medium, 65 low
- Insufficient history: none

### Validation

- Source `opps_2026-08-10.csv`: accepted 400/400 rows, rejected 0

## Since last run

No previous run recorded.

## Slipping pipeline

No close-date pushes observed in stored history.

## Fiscal quarters (fiscal year starts month 7)

| Quarter | At-risk $ | At-risk opps | H5 opps |
|---|---|---|---|
| FY2026-Q4 | $1,075,325 | 10 | none |
| FY2027-Q1 | $7,866,041 | 71 | OPP-0020, OPP-0026, OPP-0046, OPP-0078, OPP-0080, OPP-0138, OPP-0163, OPP-0219, OPP-0220, OPP-0223, OPP-0226, OPP-0254, OPP-0260, OPP-0320, OPP-0340, OPP-0342, OPP-0346, OPP-0386 |
| FY2027-Q2 | $3,069,157 | 23 | OPP-0017, OPP-0043, OPP-0134, OPP-0137, OPP-0159, OPP-0160, OPP-0282, OPP-0317, OPP-0318, OPP-0343, OPP-0380 |
| FY2027-Q3 | $70,550 | 3 | OPP-0391 |

## Top 10 exceptions

| # | Opp | Account | Owner | Stage | Amount | Score | Rules | Detail |
|---|---|---|---|---|---|---|---|---|
| 1 | OPP-0044 | Novafinancial Co | Quinn Oakhurst | commit | $839,954 | 65 | H1 H4 | no activity for 15d (> 7d for commit); next step date 2026-07-21 expired |
| 2 | OPP-0193 | Zenithnetworks Group | Kendall Lindqvist | commit | $616,126 | 80 | H2 | close date 2026-07-03 is 38d in the past |
| 3 | OPP-0046 | Vertexenergy Group | Hollis Underhill | prospect | $474,715 | 75 | H5 | forecast commit but stage prospect |
| 4 | OPP-0153 | Pacificfinancial LLC | Zion Calloway | prospect | $417,076 | 65 | H1 H4 | no activity for 83d (> 45d for prospect); next step date 2026-07-19 expired |
| 5 | OPP-0237 | Silverdynamics Inc | Skyler Ivesdale | qualify | $399,808 | 65 | H1 H4 | no activity for 69d (> 30d for qualify); next step date 2026-07-26 expired |
| 6 | OPP-0137 | Vertexsoftware Corp | Logan Ivesdale | qualify | $385,849 | 75 | H5 | forecast commit but stage qualify |
| 7 | OPP-0317 | Blueretail Group | Logan Ivesdale | propose | $368,677 | 55 | H4 H5 | next step empty; forecast commit without a valid next step |
| 8 | OPP-0133 | Goldenfoods Group | Kendall Lindqvist | prospect | $284,740 | 55 | H1 H4 H6 | no activity for 48d (> 45d for prospect); next step has no date; 48d in prospect (norm 30d) |
| 9 | OPP-0199 | Harborsystems LLC | Morgan Calloway | develop | $267,875 | 35 | H1 H2 H4 H6 | no activity for 24d (> 21d for develop); close date 2026-07-08 is 33d in the past; next step empty; 53d in develop (norm 45d) |
| 10 | OPP-0057 | Harborlabs Corp | Skyler Ivesdale | propose | $244,820 | 80 | H2 | close date 2026-08-05 is 5d in the past |

## Owners

| Owner | Open | Mean | Median | Violations | Pipeline | Coverage | Flags |
|---|---|---|---|---|---|---|---|
| Avery Calloway | 7 | 96.4 | 100.0 | 3 | $974,716 | 1.02 | low_coverage |
| Avery Ristori | 7 | 79.3 | 80.0 | 7 | $304,706 | 0.87 | low_coverage |
| Blair Denholm | 7 | 96.4 | 100.0 | 3 | $1,001,323 | 0.94 | low_coverage |
| Blair Hollowell | 7 | 93.6 | 90.0 | 5 | $652,409 | 2.04 | low_coverage |
| Blair Oakhurst | 6 | 79.2 | 85.0 | 8 | $928,671 | 0.79 | low_coverage |
| Casey Grantley | 7 | 82.1 | 80.0 | 7 | $1,003,680 | 0.67 | low_coverage |
| Corin Farrow | 7 | 76.4 | 75.0 | 7 | $525,426 | 1.55 | low_coverage |
| Corin Kirkwood | 6 | 78.3 | 80.0 | 8 | $216,385 | 0.42 | low_coverage |
| Darby Ashford | 4 | 95.0 | 95.0 | 2 | $411,065 | 0.87 | small_n, low_coverage |
| Darby Bramwell | 7 | 80.0 | 80.0 | 7 | $259,134 | 0.37 | low_coverage |
| Darby Kirkwood | 4 | 93.8 | 92.5 | 3 | $350,411 | 0.73 | small_n, low_coverage |
| Ellis Lindqvist | 6 | 98.3 | 100.0 | 2 | $894,408 | 0.63 | low_coverage |
| Finley Underhill | 6 | 95.0 | 97.5 | 4 | $926,281 | 0.81 | low_coverage |
| Frankie Denholm | 6 | 80.0 | 80.0 | 6 | $238,472 | 0.36 | low_coverage |
| Greer Ristori | 7 | 64.3 | 80.0 | 15 | $259,817 | 0.41 | low_coverage |
| Greer Westerley | 6 | 95.0 | 95.0 | 3 | $287,629 | 0.60 | low_coverage |
| Harper Bramwell | 7 | 96.4 | 100.0 | 3 | $418,899 | 0.62 | low_coverage |
| Harper Grantley | 7 | 98.6 | 100.0 | 1 | $520,181 | 1.13 | low_coverage |
| Harper Ivesdale | 7 | 93.6 | 90.0 | 5 | $1,086,440 | 0.88 | low_coverage |
| Hollis Marchetti | 6 | 95.0 | 95.0 | 4 | $747,588 | 0.71 | low_coverage |
| Hollis Southgate | 7 | 100.0 | 100.0 | 0 | $348,567 | 0.74 | low_coverage |
| Hollis Underhill | 5 | 77.0 | 75.0 | 5 | $1,026,601 | 0.75 | low_coverage |
| Jordan Vantrease | 6 | 98.3 | 100.0 | 2 | $502,341 | 0.39 | low_coverage |
| Kendall Grantley | 7 | 90.0 | 100.0 | 5 | $419,688 | 1.13 | low_coverage |
| Kendall Lindqvist | 7 | 61.4 | 55.0 | 17 | $1,547,927 | 1.07 | low_coverage |
| Logan Denholm | 7 | 89.3 | 90.0 | 8 | $1,224,251 | 1.13 | low_coverage |
| Logan Ivesdale | 7 | 73.6 | 75.0 | 10 | $1,514,511 | 1.46 | low_coverage |
| Marlow Bramwell | 6 | 76.7 | 75.0 | 6 | $277,378 | 0.60 | low_coverage |
| Morgan Calloway | 7 | 72.1 | 65.0 | 12 | $1,640,432 | 1.40 | low_coverage |
| Noor Underhill | 6 | 98.3 | 100.0 | 1 | $1,160,545 | 0.94 | low_coverage |
| Parker Thistlewood | 7 | 95.7 | 95.0 | 4 | $639,290 | 1.68 | low_coverage |
| Quinn Lindqvist | 6 | 95.0 | 97.5 | 3 | $271,970 | 0.39 | low_coverage |
| Quinn Oakhurst | 5 | 79.0 | 80.0 | 6 | $1,619,303 | 1.09 | low_coverage |
| Quinn Ristori | 6 | 81.7 | 77.5 | 6 | $483,595 | 1.03 | low_coverage |
| Reese Denholm | 5 | 99.0 | 100.0 | 1 | $1,934,450 | 1.30 | low_coverage |
| Reese Northcote | 6 | 95.0 | 97.5 | 3 | $1,134,784 | 1.07 | low_coverage |
| Riley Grantley | 4 | 92.5 | 92.5 | 4 | $447,632 | 0.79 | small_n, low_coverage |
| Riley Hollowell | 6 | 100.0 | 100.0 | 0 | $579,993 | 0.62 | low_coverage |
| Riley Ristori | 6 | 70.8 | 72.5 | 10 | $237,282 | 0.48 | low_coverage |
| Riley Southgate | 7 | 96.4 | 100.0 | 3 | $449,870 | 1.02 | low_coverage |
| Rowan Hollowell | 6 | 97.5 | 100.0 | 2 | $282,086 | 0.71 | low_coverage |
| Rowan Westerley | 6 | 82.5 | 82.5 | 6 | $284,921 | 0.54 | low_coverage |
| Sage Bramwell | 6 | 85.8 | 90.0 | 7 | $301,458 | 0.77 | low_coverage |
| Sage Eastvale | 6 | 93.3 | 92.5 | 5 | $641,007 | 0.68 | low_coverage |
| Sage Jasperson | 6 | 95.8 | 97.5 | 3 | $249,003 | 0.37 | low_coverage |
| Sage Oakhurst | 7 | 82.9 | 90.0 | 7 | $370,885 | 0.95 | low_coverage |
| Sage Vantrease | 6 | 95.0 | 97.5 | 4 | $229,458 | 0.36 | low_coverage |
| Skyler Grantley | 6 | 98.3 | 100.0 | 2 | $277,339 | 0.45 | low_coverage |
| Skyler Ivesdale | 5 | 71.0 | 65.0 | 9 | $1,043,428 | 0.70 | low_coverage |
| Tatum Ivesdale | 6 | 73.3 | 82.5 | 10 | $2,036,470 | 1.48 | low_coverage |
| Tatum Lindqvist | 6 | 74.2 | 72.5 | 9 | $276,057 | 0.69 | low_coverage |
| Tatum Underhill | 5 | 99.0 | 100.0 | 1 | $1,114,017 | 0.76 | low_coverage |
| Vesper Calloway | 6 | 99.2 | 100.0 | 1 | $1,023,585 | 0.91 | low_coverage |
| Vesper Westerley | 6 | 98.3 | 100.0 | 1 | $264,890 | 0.41 | low_coverage |
| Wren Ivesdale | 5 | 91.0 | 90.0 | 6 | $105,862 | 0.28 | low_coverage |
| Wren Yardley | 6 | 95.8 | 97.5 | 3 | $1,139,067 | 0.85 | low_coverage |
| Zion Calloway | 7 | 70.7 | 65.0 | 12 | $1,391,052 | 1.41 | low_coverage |
| Zion Northcote | 7 | 95.7 | 100.0 | 2 | $262,964 | 0.54 | low_coverage |
| Zion Ristori | 6 | 99.2 | 100.0 | 1 | $938,628 | 0.67 | low_coverage |
| Zion Vantrease | 4 | 91.2 | 90.0 | 4 | $1,465,002 | 1.10 | small_n, low_coverage |

## Forecast integrity (H5)

- OPP-0046 — Hollis Underhill, stage prospect, $474,715: forecast commit but stage prospect
- OPP-0137 — Logan Ivesdale, stage qualify, $385,849: forecast commit but stage qualify
- OPP-0317 — Logan Ivesdale, stage propose, $368,677: forecast commit without a valid next step
- OPP-0342 — Quinn Ristori, stage qualify, $205,327: forecast commit but stage qualify
- OPP-0380 — Corin Farrow, stage qualify, $163,703: forecast commit but stage qualify
- OPP-0017 — Logan Ivesdale, stage develop, $160,393: forecast commit but stage develop
- OPP-0346 — Hollis Underhill, stage qualify, $140,349: forecast commit but stage qualify
- OPP-0318 — Sage Oakhurst, stage commit, $101,617: forecast commit without a valid next step
- OPP-0219 — Casey Grantley, stage qualify, $95,487: forecast commit but stage qualify
- OPP-0343 — Marlow Bramwell, stage qualify, $87,282: forecast commit but stage qualify
- OPP-0220 — Avery Ristori, stage propose, $85,475: forecast commit without a valid next step
- OPP-0226 — Hollis Underhill, stage prospect, $79,734: forecast commit but stage prospect
- OPP-0159 — Casey Grantley, stage prospect, $78,236: forecast commit but stage prospect
- OPP-0282 — Quinn Ristori, stage develop, $76,657: forecast commit but stage develop
- OPP-0386 — Frankie Denholm, stage prospect, $74,870: forecast commit but stage prospect
- OPP-0340 — Avery Ristori, stage develop, $73,766: forecast commit but stage develop
- OPP-0078 — Sage Oakhurst, stage develop, $60,483: forecast commit but stage develop
- OPP-0043 — Marlow Bramwell, stage qualify, $59,721: forecast commit but stage qualify
- OPP-0163 — Marlow Bramwell, stage prospect, $57,175: forecast commit but stage prospect
- OPP-0080 — Corin Farrow, stage develop, $54,286: forecast commit but stage develop
- OPP-0020 — Corin Farrow, stage develop, $48,103: forecast commit but stage develop
- OPP-0260 — Corin Farrow, stage prospect, $47,610: forecast commit but stage prospect
- OPP-0138 — Sage Oakhurst, stage prospect, $36,041: forecast commit but stage prospect
- OPP-0026 — Frankie Denholm, stage qualify, $31,572: forecast commit but stage qualify
- OPP-0223 — Marlow Bramwell, stage qualify, $29,580: forecast commit but stage qualify
- OPP-0320 — Corin Farrow, stage prospect, $21,244: forecast commit but stage prospect
- OPP-0391 — Sage Bramwell, stage prospect, $19,702: forecast commit but stage prospect
- OPP-0254 — Darby Bramwell, stage propose, $19,019: forecast commit without a valid next step
- OPP-0134 — Darby Bramwell, stage commit, $15,248: forecast commit without a valid next step
- OPP-0160 — Avery Ristori, stage develop, $12,682: forecast commit but stage develop
