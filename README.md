# Lecture 9 Bakery App

A local bakery order tracker built with Python standard library and SQLite.

## Run

Install Python 3.10 or later, then run:

```sh
python app.py
```

Open http://127.0.0.1:8000. The app creates bakery.db and imports the classroom orders automatically. On Windows, you can also double-click Start-Bakery.cmd.

Load each day's messages, review requests, optionally hire helpers, then preview and confirm the end of the day. Valid changes and cancellations apply when messages are loaded. No Instagram account, AI service or payment system is connected.

## Test

Start the app once to initialize bakery.db. In another terminal run:

```sh
python test_app.py
```

Tests use temporary copies, never modify your active database, and cover all seven message days. See TEST-RESULTS.txt for verification results and README.html for rules, data model and implementation details.

Classroom input fixtures are included; runtime databases and local machine paths are excluded.
