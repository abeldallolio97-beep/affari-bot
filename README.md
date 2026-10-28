# affari-bot

Bot per trovare affari su Subito.it (Torino) da rivendere. Comandi da Telegram:

- `/scan` scansione completa (~1000 annunci per ricerca) con classifica degli affari
- `/scan elettronica` solo una ricerca
- `/test` prova la lettura di Subito, senza AI
- `/help`

Le ricerche stanno in `links.txt`. I criteri (margine minimo 100 €, affidabilità 50%, ≥3 riferimenti di prezzo)
si cambiano con variabili nel workflow. Le chiavi stanno solo nei Secrets di GitHub:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`.
Il bot non compra e non scrive ai venditori: avvisa soltanto.
