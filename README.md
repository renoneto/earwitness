# EarWitness

EarWitness finds the ads and brand mentions in podcasts and quotes them to the second. For each ad it shows the passage the host read, the show, the episode and the time in the episode, so you can check every result yourself.

With EarWitness you can:

- search the words hosts say in ads, by brand, show or date;
- compare how brands are pitched across shows;
- set an alert for a brand or a word and see new ads when they are heard;
- ask a question about the ads in plain English and get an answer that links every claim to its passage.

The hosted app is at https://app.getearwitness.com/.

This repository holds the product source only. It has no podcast data. See [What is left out](#what-is-left-out).

## How Ask uses Meta's Muse Spark

Ask answers questions such as "How do hosts pitch VPNs in this collection?" from the episodes in the collection.

1. The question goes to Meta's Muse Spark model (`muse-spark-1.3`, Meta Model API) together with a small set of search tools.
2. The model searches the collection through those tools. The server returns matching transcript passages and labels each one.
3. A final call asks the model for a short answer in which every paragraph cites the passages it relies on.
4. Our code then checks the answer before anyone sees it (`check_answer` in `earwitness/app/ask_contract.py`):
   - every quote must appear word for word in a cited passage;
   - every show and brand the answer names is checked against the cited passages;
   - any claim that fails a check is removed, and the reason is recorded.

The answer shows each claim with a link to the passage behind it.

The code is in:

- `earwitness/app/meta_client.py`: the API client, the key loader and the spending caps;
- `earwitness/app/ask.py`: the steps of one answer;
- `earwitness/app/ask_tools.py`: the search tools the model may call;
- `earwitness/app/ask_contract.py`: the answer checks.

Ask runs only in the hosted app, and only when the workspace owner turns it on. The API key is read from a file named by `EARWITNESS_META_KEY_FILE`, never from the code. Each answer has a fixed spending cap. The local app below makes no model calls.

## Run it locally

Tested on macOS (Apple Silicon) with Python 3.14.7 and Node 26.

The local app uses only Python's standard library and SQLite, so it needs no installs. It serves on 127.0.0.1 only.

1. Create a workspace from the small synthetic test collection in this repository:

   ```sh
   python3 scripts/app_workspace.py seed --workspace .earwitness --source-root tests/fixtures/export-contract
   ```

2. Start the app:

   ```sh
   python3 scripts/app_server.py --workspace .earwitness --port 8771
   ```

3. Open http://127.0.0.1:8771/app/ for the app, or http://127.0.0.1:8771/ for the public pages.

The test collection has four short made-up episodes, so most screens show only a few results.

### Tests

To run the frontend tests:

```sh
node --test tests/*.test.cjs
```

The Python tests need the pinned packages:

```sh
python3 -m venv .venv-hosted
. .venv-hosted/bin/activate
pip install -r requirements-hosted.txt
python -m unittest discover -s tests -t .
```

Exact versions are in the `requirements-*.lock` files. The pipeline that downloads and transcribes episodes uses `requirements.txt`; cutting audio clips also needs `ffmpeg`.

## What is left out

| Left out | Why |
|---|---|
| Transcripts, audio, episode lists, and the brand and show lists built from them | They come from podcasts we do not own, and they are large. |
| Podcast cover art, brand logos, and screenshots of real ads | They belong to the shows and brands. The public pages run without them, so those images are missing locally. |
| Tests and test files that quote real podcast passages | Same reason as the transcripts. The remaining tests use made-up text, some of it written in the style of a real ad and naming real brands. |
| Hosting, deployment and operations files | They describe our servers, not the product. |
| Internal planning documents | They are working notes, not product source. |

Some code comments mention internal design notes that are not in this repository, and a few quote a short phrase from a real ad as an example.

## License

Copyright 2026 Reno Labs. Licensed under the Apache License, Version 2.0; see [LICENSE](LICENSE).

The license covers the code in this repository. It does not cover podcast content, show art, brand logos or brand names, which belong to their owners. The repository leaves these out, apart from brand names and a few short ad phrases quoted as examples in comments and on the home page. Fonts in `app/fonts/` are IBM Plex, under the SIL Open Font License (`app/fonts/OFL.txt`).
