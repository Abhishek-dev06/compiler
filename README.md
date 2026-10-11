# CodeRun — Online Compiler with Execution History and Analysis

CodeRun is a dark, responsive Flask web application that uses **Judge0 CE** to compile and run Python, C, C++, and Java. Every execution—successful, compiler/runtime failure, or Judge0 API failure—is saved in a persistent SQLite database. Programs are never executed by Flask.

## Features

- Locally bundled Monaco code editor (no browser-side CDN dependency), language selector, custom standard input, terminal output, and execution metrics.
- Python 3 (Judge0 ID 71), C GCC (50), C++ G++ (54), and Java. Java `public class Main` uses ID 62; other top-level public Java classes use Judge0's verified multi-file runner.
- Asynchronous Judge0 submit-and-poll protocol using Judge0's Base64 mode, preserving UTF-8 code (including Java generics) across the API boundary, plus remote timeout and API failure handling.
- SQLite-backed history that survives refreshes and backend restarts.
- Restore source/input/output from a historical run and delete individual records.
- Server-side environment configuration; browser code receives no Judge0 credentials.

## Setup

Requirements: Python 3.10+ and access to a Judge0 CE-compatible API.

```powershell
cd C:\Users\User\OneDrive\Documents\ChatGPT\cybersicurity\CodeRun
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

Open http://127.0.0.1:5000.

The default `.env.example` points at the public Judge0 CE instance. For a reliable production deployment, set `JUDGE0_API_URL` to a Judge0 CE deployment you control and, if it requires authentication, set `JUDGE0_AUTH_TOKEN` and/or `JUDGE0_AUTH_USER`. (`JUDGE0_API_KEY` is accepted as a token alias.) These values are read only on the backend. The default database is `coderun.db` next to `app.py`; set `DATABASE_PATH` for another persistent location.

## Run tests

```powershell
pytest -q
```

The test suite simulates Judge0's real `POST /submissions` followed by `GET /submissions/{token}` protocol. It verifies persistence, restore/delete behavior, validation, and API-failure recording without executing a program locally or requiring external network access.

## Judge0 behavior

CodeRun submits with `base64_encoded=true` and `wait=false`, then polls while Judge0 reports `In Queue` or `Processing` (status IDs 1–2). CodeRun Base64-encodes UTF-8 source/input on submission and decodes Judge0 text results before displaying and storing them; it does not rewrite source code. Judge0's returned status, standard output, compiler output, runtime error text, time, and memory are stored as returned. The application adds no synthetic compiler output or measurements. Its own maximum wait limits only how long the HTTP request remains open; a non-finished remote job is stored as an `API Error` with a diagnostic message.

## Java class filenames

Judge0 language ID 62 is a single-file `Main.java`/`Main` runner. CodeRun keeps that efficient path for `public class Main`. For another top-level public class, CodeRun lexes the Java source without reading comments or string literals, preserves the exact source in a Base64 ZIP as `<ClassName>.java`, and submits it to Judge0's Multi-file program runner (ID 89 by default). The archive includes only the unmodified source plus deterministic `compile` and `run` scripts.

Before the first such run, CodeRun calls `GET /languages/89` on the configured Judge0 host and proceeds only when it confirms the `Multi-file program` language. If the host does not support it—or set `JUDGE0_JAVA_MULTIFILE_LANGUAGE_ID=` to disable it—the execution is persisted with `Executor Configuration Error` rather than being incorrectly submitted as `Main.java`. A top-level public Java class still needs a `main` method to execute.
