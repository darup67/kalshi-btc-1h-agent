"""The call model (copied from ~/kalshi-btc-agent/model.py), shared by study.py and agent.py so the live
agent runs exactly what was calibrated. For the 1-hour above/below ladder: P(BRTI 60s average at the hour > K).

Direction is not forecast: every direction test in market-lab (Chronos-2,
Chronos-Bolt, AutoGluon) came back at coin-flip. What Chronos-2 does forecast
with skill is volatility (beat time-of-day+EWMA on all five futures). So a call
is: gap already on the board, divided by the volatility still to come.

  P(YES) = Phi( gap / sqrt(sigma_1m^2 * r_eff + sigma_proxy^2) )

gap        proxy spot minus strike, $
r_eff      minutes left, minus 2/3: settlement averages the last 60s of BRTI,
           and a 1-minute average of a random walk carries 1/3 min of variance
sigma_1m   $ per sqrt(minute), from the Chronos-2 15m range forecast
sigma_proxy measured error of the proxy against Kalshi's settlement values
"""
import math

RANGE_TO_SIGMA = math.sqrt(8 / math.pi)  # E[range of BM over T] = 1.596 * sigma * sqrt(T)


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def sigma_1m_from_range(range_15m):
    return range_15m / RANGE_TO_SIGMA / math.sqrt(15)


def p_yes(gap, minutes_left, sigma_1m, sigma_proxy):
    r_eff = max(minutes_left - 2 / 3, 1 / 3)
    sd = math.sqrt(sigma_1m ** 2 * r_eff + sigma_proxy ** 2)
    return phi(gap / sd), sd


def kalshi_fee(price):
    """Kalshi taker fee per contract, $: ceil(0.07 * P * (1-P) * 100) cents."""
    return math.ceil(0.07 * price * (1 - price) * 100 - 1e-9) / 100
