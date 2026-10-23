# Polymarket US — бот на лаге цены (paper)

Рынки `BTC/ETH Up or Down` 5m / 15m / 1h / 1d на Polymarket US рассчитываются по CF Benchmarks BRTI:
среднее 60 секундных цен в последнюю минуту до начала и до конца окна.

`pm/lag.py` строит прокси BRTI (медиана Coinbase, Kraken, Bitstamp), считает справедливую вероятность Up
с учётом волатильности и минутного усреднения и ищет моменты, когда стакан Polymarket ещё не догнал движение цены.
Сделки виртуальные: исполнение по следующему снимку стакана, комиссия тейкера θ·p·(1−p) из поля `feeCoefficient` рынка.

- Запуск локально или на VPS: `pip install websockets && python pm/lag.py --minutes 600`
- GitHub Actions: workflow `pm-paper` каждые 6 часов, результаты и `REPORT.md` в ветке `pm-results`
- Сводка: `python pm/report.py`

Живой режим (ордера через API Polymarket US, ключи Ed25519) — только после того, как paper покажет плюс после комиссий.
