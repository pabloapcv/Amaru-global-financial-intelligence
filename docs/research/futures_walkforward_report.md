# Futures Walk-Forward Validation Report

*Generated: 2026-11-06 | Data mode: synthetic*

## Overview

This artifact demonstrates walk-forward forecast evaluation on futures data.
For each contract, we test multiple forecasting methods against naive baselines
and report whether any beats the benchmark out of sample.

## Data Summary

| Contract | Observations | Last Price | Last Date |
|----------|--------------|------------|-----------|
| FUT.GC | 2089 | $1858.27 | 2026-11-05 |
| FUT.CL | 2089 | $68.45 | 2026-11-05 |
| FUT.ZC | 2089 | $390.65 | 2026-11-05 |

## Walk-Forward Results

Each contract is forecasted 3 months ahead using rolling-origin evaluation.

| Contract | Best Method | Beats Naive? | No-Change MAE | Seasonal MAE | Drift MAE |
|----------|-------------|--------------|---------------|--------------|-----------|
| FUT.GC | no_change | No | 6.532% | 24.658% | 6.775% |
| FUT.CL | no_change | No | 14.932% | 20.828% | 15.523% |
| FUT.ZC | no_change | No | 9.108% | 12.773% | 9.48% |

## Correlation Structure

Based on 96 monthly return observations:

| Pair | Correlation |
|------|-------------|
| FUT.GC vs FUT.CL | -0.071 |
| FUT.GC vs FUT.ZC | -0.179 |
| FUT.CL vs FUT.ZC | 0.082 |

## Interpretation

- **Beats naive = No**: The naive no-change forecast is hard to beat statistically.
  This is a common result in commodity futures and is reported honestly.
- **Correlation**: Positive correlation indicates shared macro sensitivity (e.g., USD, risk appetite).

## Data Quality Notes

- Data mode: **synthetic**
- Continuous contracts use synthetic roll (not official exchange methodology)
- Free delayed data has no SLA; gaps may occur around rolls
- For production trading, implement a licensed exchange adapter

## How to Regenerate

```bash
# With synthetic data (no network):
python demos/research/futures_walkforward.py

# With live Stooq data:
python demos/research/futures_walkforward.py --live
```