# Corn Supply-Demand Research Report

*Generated: 2026-11-06 | Data mode: synthetic*

## Executive Summary

This research artifact demonstrates Amaru's verified pipeline by answering:
**What does the USDA corn stocks-to-use ratio imply for price?**

**Finding:** The stocks-to-use model has signal (R² = 0.647, p < 0.05).
For the 2026/2027 projection (stocks-to-use = 17.83%), the model implies a price of $219.07/MT (80% band: $184.4–$260.25).

Current price ($160.33) is **below 80% band (cheap vs fundamentals)**.

## Methodology

1. **Data**: USDA PSD corn balance sheets (world excluding China for stocks-to-use, per USDA convention)
2. **Model**: log(real price) regressed on stocks-to-use (%) across market years
3. **Validation**: Walk-forward test — each year fitted only on prior data
4. **Standard errors**: HAC (Newey-West) for autocorrelation robustness

## Model Results

| Metric | Value |
|--------|-------|
| Years in sample | 36 (1990–2025) |
| R² | 0.647 |
| p-value | 0.0 |
| Slope | -4.08% price per 1pp stocks-to-use |

## Walk-Forward Validation

| Metric | Value |
|--------|-------|
| Out-of-sample tests | 21 |
| Mean Absolute Error | 17.1% |
| Naive baseline MAE | 14.65% |
| Beats naive? | No |
| Right direction | 17/20 (85.0%) |

## Current Volatility Regime

- Annualized volatility: 37.17%
- Percentile vs history: 43.5th
- Regime: **normal**

## World Balance (recent years)

| Year | Production (MMT) | Consumption (MMT) | Stocks-to-Use (%) |
|------|------------------|-------------------|-------------------|
| 2019/20 | 831.6 | 762.4 | 18.8% |
| 2020/21 | 852.5 | 797.3 | 14.2% |
| 2021/22 | 852.9 | 774.2 | 11.6% |
| 2022/23 | 878.4 | 793.3 | 17.2% |
| 2023/24 | 876.7 | 776.6 | 22.6% |
| 2024/25 | 860.1 | 797.8 | 22.9% |
| 2025/26 | 862.2 | 749.5 | 19.5% |
| 2026/27 | 911.3 | 817.9 | 17.8% |

## Limitations

- **Not a trading signal**: Association does not imply causation; prices respond to many factors
- **Model instability**: The relationship can shift with biofuel policy, trade disruptions
- **Data lag**: USDA projections are revised monthly; stocks-to-use estimates are uncertain
- **Data mode**: This report uses synthetic fixture data

---
*Research and decision support only. Not investment advice.*

## How to Regenerate

```bash
# With synthetic data (no API keys):
python demos/research/corn_supply_demand.py

# With live data:
python demos/research/corn_supply_demand.py --live
```