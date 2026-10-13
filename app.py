"""CodeRun DSA Practice Platform; code always runs remotely through Judge0."""
from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import secrets
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
LANGUAGES = {
    "python": {"name": "Python 3", "judge0_id": 71, "monaco": "python"},
    "c": {"name": "C (GCC)", "judge0_id": 50, "monaco": "c"},
    "cpp": {"name": "C++ (G++)", "judge0_id": 54, "monaco": "cpp"},
    "java": {"name": "Java", "judge0_id": 62, "monaco": "java"},
}
TOPICS = ("Arrays", "Strings", "Hashing", "Two Pointers", "Sliding Window", "Binary Search", "Sorting", "Linked Lists", "Stacks", "Queues", "Recursion", "Backtracking", "Trees", "BST", "Heaps", "Graphs", "Greedy", "Dynamic Programming", "Tries", "Bit Manipulation", "Math")
STATUSES, PRIORITIES, DIFFICULTIES = ("Unsolved", "Attempted", "Solved"), ("High", "Medium", "Low"), ("Easy", "Medium", "Hard")
JUDGE0_TEXT_FIELDS = ("stdout", "stderr", "compile_output", "message")
JAVA_CLASS_MODIFIERS = {"abstract", "final", "sealed", "strictfp"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# The old hardcoded fallback key. It is public, so it is treated as "not set".
INSECURE_SECRET_KEYS = {"", "local-development-change-this-secret", "change-me", "changeme"}

class ExecutorConfigurationError(RuntimeError):
    pass

@dataclass(frozen=True)
class JavaPublicClass:
    name: str
    package_name: str | None

def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip(): return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def running_in_production() -> bool:
    """Render sets RENDER=true automatically; APP_ENV=production works on any other host."""
    return env_flag("RENDER") or os.environ.get("APP_ENV", "").strip().lower() == "production"

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def optional_positive_int(value: str | None, setting_name: str) -> int | None:
    if value is None or not value.strip(): return None
    try: parsed = int(value)
    except ValueError as exc: raise ValueError(f"{setting_name} must be a positive integer or empty.") from exc
    if parsed <= 0: raise ValueError(f"{setting_name} must be a positive integer or empty.")
    return parsed

def java_tokens(source: str) -> list[str]:
    """Small lexer that deliberately skips Java comments and literal text."""
    tokens, index, length = [], 0, len(source)
    while index < length:
        char = source[index]
        if char.isspace(): index += 1
        elif source.startswith("//", index):
            newline = source.find("\n", index + 2); index = length if newline == -1 else newline + 1
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2); index = length if end == -1 else end + 2
        elif source.startswith('\"\"\"', index):
            end = source.find('\"\"\"', index + 3); index = length if end == -1 else end + 3
        elif char in {'\"', "'"}:
            quote = char; index += 1
            while index < length:
                if source[index] == "\\": index += 2
                elif source[index] == quote: index += 1; break
                else: index += 1
        elif char == "_" or char == "$" or char.isalpha():
            end = index + 1
            while end < length and (source[end] in "_$" or source[end].isalnum()): end += 1
            tokens.append(source[index:end]); index = end
        else: tokens.append(char); index += 1
    return tokens

def find_java_public_class(source: str) -> JavaPublicClass | None:
    tokens, package_name, depth, index = java_tokens(source), None, 0, 0
    while index < len(tokens):
        token = tokens[index]
        if token == "{": depth += 1
        elif token == "}": depth = max(0, depth - 1)
        elif depth == 0 and token == "package":
            parts, cursor, expecting = [], index + 1, True
            while cursor < len(tokens) and tokens[cursor] != ";":
                item = tokens[cursor]
                if expecting:
                    if not item or not (item[0].isalpha() or item[0] in "_$"): break
                    parts.append(item)
                elif item != ".": break
                expecting = not expecting; cursor += 1
            if parts and not expecting and cursor < len(tokens) and tokens[cursor] == ";": package_name = ".".join(parts)
        elif depth == 0 and token == "public":
            cursor = index + 1
            while cursor < len(tokens):
                if tokens[cursor] in JAVA_CLASS_MODIFIERS: cursor += 1
                elif tokens[cursor:cursor + 3] == ["non", "-", "sealed"]: cursor += 3
                elif tokens[cursor] == "@":
                    cursor += 2
                    while cursor + 1 < len(tokens) and tokens[cursor] == ".": cursor += 2
                    if cursor < len(tokens) and tokens[cursor] == "(":
                        nested = 1; cursor += 1
                        while cursor < len(tokens) and nested:
                            nested += (tokens[cursor] == "(") - (tokens[cursor] == ")"); cursor += 1
                else: break
            if cursor + 1 < len(tokens) and tokens[cursor] == "class":
                name = tokens[cursor + 1]
                if name and (name[0].isalpha() or name[0] in "_$"): return JavaPublicClass(name, package_name)
        index += 1
    return None

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL UNIQUE COLLATE NOCASE,email TEXT NOT NULL UNIQUE COLLATE NOCASE,password_hash TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS problems(id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT NOT NULL UNIQUE,title TEXT NOT NULL,statement TEXT NOT NULL,constraints_text TEXT NOT NULL DEFAULT '',examples_json TEXT NOT NULL DEFAULT '[]',difficulty TEXT NOT NULL CHECK(difficulty IN ('Easy','Medium','Hard')),starter_codes_json TEXT NOT NULL DEFAULT '{}',test_cases_json TEXT NOT NULL DEFAULT '[]',hints_json TEXT NOT NULL DEFAULT '[]',explanation TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topics(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS problem_topics(problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,PRIMARY KEY(problem_id,topic_id));
CREATE TABLE IF NOT EXISTS user_problem_progress(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,status TEXT NOT NULL DEFAULT 'Unsolved' CHECK(status IN ('Unsolved','Attempted','Solved')),priority TEXT NOT NULL DEFAULT 'Medium' CHECK(priority IN ('High','Medium','Low')),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(user_id,problem_id));
CREATE TABLE IF NOT EXISTS saved_solutions(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,title TEXT NOT NULL,source_code TEXT NOT NULL,language TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',time_complexity TEXT NOT NULL DEFAULT '',space_complexity TEXT NOT NULL DEFAULT '',complexity_explanation TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bookmarks(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,created_at TEXT NOT NULL,PRIMARY KEY(user_id,problem_id));
CREATE TABLE IF NOT EXISTS notes(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,body TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(user_id,problem_id));
CREATE INDEX IF NOT EXISTS idx_problem_topics_topic ON problem_topics(topic_id,problem_id);
CREATE INDEX IF NOT EXISTS idx_progress_user_status ON user_problem_progress(user_id,status);
CREATE INDEX IF NOT EXISTS idx_solutions_user_problem ON saved_solutions(user_id,problem_id,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_bookmarks_user ON bookmarks(user_id,created_at DESC);
"""

def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    production = running_in_production()
    app.config.from_mapping(DATABASE=os.environ.get("DATABASE_PATH", str(BASE_DIR / "coderun.db")),SECRET_KEY=os.environ.get("SECRET_KEY", "").strip(),SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Lax",SESSION_COOKIE_SECURE=env_flag("SESSION_COOKIE_SECURE", production),PERMANENT_SESSION_LIFETIME=timedelta(days=14),PREFERRED_URL_SCHEME="https" if production else "http",MAX_CONTENT_LENGTH=int(os.environ.get("MAX_REQUEST_BYTES", "250000")),CSRF_ENABLED=True,JUDGE0_API_URL=os.environ.get("JUDGE0_API_URL", "https://ce.judge0.com").rstrip("/"),JUDGE0_JAVA_MULTIFILE_LANGUAGE_ID=optional_positive_int(os.environ.get("JUDGE0_JAVA_MULTIFILE_LANGUAGE_ID", "89"), "JUDGE0_JAVA_MULTIFILE_LANGUAGE_ID"),JUDGE0_AUTH_TOKEN=os.environ.get("JUDGE0_AUTH_TOKEN") or os.environ.get("JUDGE0_API_KEY", ""),JUDGE0_AUTH_USER=os.environ.get("JUDGE0_AUTH_USER", ""),JUDGE0_REQUEST_TIMEOUT=float(os.environ.get("JUDGE0_REQUEST_TIMEOUT", "15")),JUDGE0_POLL_INTERVAL=float(os.environ.get("JUDGE0_POLL_INTERVAL", "0.75")),JUDGE0_MAX_WAIT=float(os.environ.get("JUDGE0_MAX_WAIT", "25")),HISTORY_LIMIT=int(os.environ.get("HISTORY_LIMIT", "100")))
    if test_config: app.config.update(test_config)
    if app.config["SECRET_KEY"] in INSECURE_SECRET_KEYS:
        # Never fall back to a known key in production: anyone could forge a signed session cookie.
        if production and not app.config.get("TESTING"):
            raise RuntimeError("SECRET_KEY is not set. Add a long random SECRET_KEY environment variable (for example the output of `python -c \"import secrets; print(secrets.token_hex(32))\"`) before starting CodeRun in production.")
        # Local development only: a random per-process key. Sessions reset whenever the server restarts.
        app.config["SECRET_KEY"] = secrets.token_hex(32)
        app.logger.warning("SECRET_KEY is not set; using a temporary random key. Sessions will reset on restart.")
    if env_flag("TRUST_PROXY", production):
        # Render terminates HTTPS at its proxy; honour X-Forwarded-For/Proto so Flask sees the real client and scheme.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # type: ignore[method-assign]
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    @app.teardown_appcontext
    def close_db(_: BaseException | None = None) -> None:
        db = g.pop("db", None)
        if db is not None: db.close()
    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"]); g.db.row_factory = sqlite3.Row; g.db.execute("PRAGMA foreign_keys=ON")
        return g.db
    def column_exists(table: str, column: str) -> bool:
        return any(row["name"] == column for row in get_db().execute(f"PRAGMA table_info({table})"))
    def init_db() -> None:
        get_db().executescript(SCHEMA)
        get_db().executescript("""CREATE TABLE IF NOT EXISTS executions(id INTEGER PRIMARY KEY AUTOINCREMENT,source_code TEXT NOT NULL,language TEXT NOT NULL,stdin TEXT NOT NULL DEFAULT '',stdout TEXT,stderr TEXT,compile_output TEXT,message TEXT,status TEXT NOT NULL,status_id INTEGER,execution_time TEXT,memory INTEGER,created_at TEXT NOT NULL,user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,problem_id INTEGER REFERENCES problems(id) ON DELETE SET NULL);CREATE INDEX IF NOT EXISTS idx_executions_created_at ON executions(created_at DESC);""")
        # Additive upgrade: old histories remain in place, now unassigned rather than exposed.
        if not column_exists("executions", "user_id"): get_db().execute("ALTER TABLE executions ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
        if not column_exists("executions", "problem_id"): get_db().execute("ALTER TABLE executions ADD COLUMN problem_id INTEGER REFERENCES problems(id) ON DELETE SET NULL")
        get_db().execute("CREATE INDEX IF NOT EXISTS idx_executions_user_created ON executions(user_id,id DESC)")
        seed_problems(); get_db().commit()
    def seed_problems() -> None:
        now = utc_now()
        for topic in TOPICS: get_db().execute("INSERT OR IGNORE INTO topics(name) VALUES (?)", (topic,))
        samples = [
          ("two-sum","Two Sum","Given integers on the first line and a target on the second line, print the zero-based indexes of two numbers that add to the target.","Use zero-based indexes. Assume exactly one answer.","Easy",["Arrays","Hashing"],[{"input":"2 7 11 15\n9\n","output":"0 1\n","explanation":"2 + 7 = 9."}],["A hash map can remember values already seen.","Check target - current before storing current."],"A one-pass hash map has O(n) expected time and O(n) auxiliary space.",{"python":"nums = list(map(int, input().split()))\ntarget = int(input())\n# print the two indexes\n","c":"#include <stdio.h>\nint main(void) { return 0; }\n","cpp":"#include <bits/stdc++.h>\nusing namespace std;\nint main() { }\n","java":"public class Main {\n  public static void main(String[] args) {\n    // read nums and target\n  }\n}\n"},[{"input":"2 7 11 15\n9\n","expected_output":"0 1\n"}]),
          ("valid-parentheses","Valid Parentheses","Given a string containing parentheses, brackets and braces, print true when the sequence is balanced; otherwise print false.","Input contains only (), [] and {} characters.","Easy",["Stacks","Strings"],[{"input":"([{}])\n","output":"true\n","explanation":"Every opener is matched in reverse order."}],["A stack stores unmatched opening brackets."],"Push openers and pop matching closers: O(n) time and O(n) space.",{"python":"s = input().strip()\n# print True or False\n","c":"#include <stdio.h>\nint main(void) { return 0; }\n","cpp":"#include <bits/stdc++.h>\nusing namespace std;\nint main() { string s; cin >> s; }\n","java":"public class Main {\n  public static void main(String[] args) { }\n}\n"},[{"input":"([{}])\n","expected_output":"true\n"}]),
          ("binary-search","Binary Search","Given a sorted list of integers and a target, print its index or -1 when absent.","Input list is sorted in nondecreasing order.","Easy",["Binary Search","Arrays"],[{"input":"1 3 5 7 9\n7\n","output":"3\n","explanation":"7 appears at index 3."}],["Discard half the range after every comparison."],"Binary search is O(log n) time with O(1) auxiliary space.",{"python":"nums = list(map(int, input().split()))\ntarget = int(input())\n# print the index\n","c":"#include <stdio.h>\nint main(void) { return 0; }\n","cpp":"#include <bits/stdc++.h>\nusing namespace std;\nint main() {}\n","java":"public class Main { public static void main(String[] args) {} }\n"},[{"input":"1 3 5 7 9\n7\n","expected_output":"3\n"}]),
          ("climbing-stairs","Climbing Stairs","Given n steps, print the number of distinct ways to climb one or two steps at a time.","1 ≤ n ≤ 45.","Easy",["Dynamic Programming","Math"],[{"input":"4\n","output":"5\n","explanation":"The answer follows the Fibonacci recurrence."}],["Each state depends only on the previous two states."],"Keep two previous counts for O(n) time and O(1) auxiliary space.",{"python":"n = int(input())\n# print number of ways\n","c":"#include <stdio.h>\nint main(void) { return 0; }\n","cpp":"#include <bits/stdc++.h>\nusing namespace std;\nint main() { int n; cin >> n; }\n","java":"public class Main { public static void main(String[] args) {} }\n"},[{"input":"4\n","expected_output":"5\n"}])]
        for slug,title,statement,constraints,difficulty,tags,examples,hints,explanation,starter,tests in samples:
            get_db().execute("""INSERT OR IGNORE INTO problems(slug,title,statement,constraints_text,examples_json,difficulty,starter_codes_json,test_cases_json,hints_json,explanation,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(slug,title,statement,constraints,json.dumps(examples),difficulty,json.dumps(starter),json.dumps(tests),json.dumps(hints),explanation,now,now))
            problem_id = get_db().execute("SELECT id FROM problems WHERE slug=?",(slug,)).fetchone()["id"]
            for tag in tags: get_db().execute("INSERT OR IGNORE INTO problem_topics(problem_id,topic_id) SELECT ?,id FROM topics WHERE name=?",(problem_id,tag))

    def get_user() -> sqlite3.Row | None:
        if "user" not in g:
            user_id = session.get("user_id"); g.user = get_db().execute("SELECT id,username,email,created_at FROM users WHERE id=?",(user_id,)).fetchone() if isinstance(user_id,int) else None
        return g.user
    def csrf_token() -> str:
        if "csrf_token" not in session: session["csrf_token"] = secrets.token_urlsafe(32)
        return str(session["csrf_token"])
    @app.context_processor
    def template_context() -> dict[str, Any]: return {"current_user":get_user(),"csrf_token":csrf_token(),"topics":TOPICS}
    @app.before_request
    def csrf_protect() -> Any:
        if request.method not in {"POST","PUT","PATCH","DELETE"} or not app.config["CSRF_ENABLED"]: return None
        submitted = request.headers.get("X-CSRFToken") or request.form.get("csrf_token"); expected = session.get("csrf_token")
        if not expected or not submitted or not secrets.compare_digest(str(expected),str(submitted)):
            return (jsonify(error="Invalid or missing CSRF token.",code="csrf_failed"),400) if request.path.startswith("/api/") else abort(400,"Invalid or missing CSRF token.")
        return None
    @app.after_request
    def no_store_dynamic_responses(response: Any) -> Any:
        # Pages embed the session's CSRF token and API responses are per-user, so neither may be cached or shared.
        if request.endpoint != "static" and response.mimetype in {"text/html","application/json"}: response.headers.setdefault("Cache-Control","no-store")
        return response
    def login_required(view: Callable[...,Any]) -> Callable[...,Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if get_user() is None: return (jsonify(error="Authentication required."),401) if request.path.startswith("/api/") else redirect(url_for("login",next=request.full_path))
            return view(*args,**kwargs)
        return wrapped
    def home_endpoint() -> str:
        # The DSA dashboard is optional; fall back to the compiler when its template is not deployed.
        return "dashboard" if "dashboard.html" in app.jinja_env.list_templates() else "compiler"
    def safe_next(default_endpoint: str) -> str:
        destination = request.args.get("next","")
        if destination.startswith("/") and not destination.startswith("//") and "\\" not in destination and "\n" not in destination and "\r" not in destination: return destination
        return url_for(default_endpoint)
    def request_json() -> dict[str,Any] | tuple[Any,int]:
        payload=request.get_json(silent=True)
        return payload if isinstance(payload,dict) else (jsonify(error="Send a JSON request body."),400)
    def bounded_text(value: Any,label: str,maximum: int,allow_empty: bool=True) -> str:
        if not isinstance(value,str) or (not allow_empty and not value.strip()): raise ValueError(f"{label} is required.")
        if len(value)>maximum: raise ValueError(f"{label} exceeds the {maximum:,}-character limit.")
        return value
    def judge_headers() -> dict[str,str]:
        headers={"Content-Type":"application/json"}
        if app.config["JUDGE0_AUTH_TOKEN"]: headers["X-Auth-Token"]=app.config["JUDGE0_AUTH_TOKEN"]
        if app.config["JUDGE0_AUTH_USER"]: headers["X-Auth-User"]=app.config["JUDGE0_AUTH_USER"]
        return headers
    def encode_judge_text(text: str) -> str: return base64.b64encode(text.encode()).decode()
    def decode_judge_result(result: dict[str,Any]) -> dict[str,Any]:
        decoded=dict(result)
        for field in JUDGE0_TEXT_FIELDS:
            if decoded.get(field) is not None:
                try: decoded[field]=base64.b64decode(decoded[field],validate=True).decode("utf-8")
                except (binascii.Error,UnicodeDecodeError,TypeError) as exc: raise RuntimeError(f"Judge0 returned invalid base64 data for {field}.") from exc
        return decoded
    def call_judge(method: str,path: str,**kwargs: Any) -> dict[str,Any]:
        try:
            response=requests.request(method,f"{app.config['JUDGE0_API_URL']}{path}",headers=judge_headers(),timeout=app.config["JUDGE0_REQUEST_TIMEOUT"],**kwargs); response.raise_for_status(); payload=response.json()
        except requests.Timeout as exc: raise RuntimeError("Judge0 request timed out. Check the Judge0 service and try again.") from exc
        except requests.RequestException as exc: raise RuntimeError(f"Judge0 API request failed: {exc}") from exc
        except ValueError as exc: raise RuntimeError("Judge0 returned invalid JSON.") from exc
        if not isinstance(payload,dict): raise RuntimeError("Judge0 returned an unexpected response.")
        return payload
    java_multifile_available: bool | None = None
    def java_multifile_language() -> int:
        nonlocal java_multifile_available
        configured=app.config["JUDGE0_JAVA_MULTIFILE_LANGUAGE_ID"]
        if configured is None: raise ExecutorConfigurationError("This Judge0 host is configured for Java Main.java runs only; multi-file Java support is disabled.")
        if java_multifile_available is None:
            try: info=call_judge("GET",f"/languages/{configured}")
            except RuntimeError as exc: raise ExecutorConfigurationError("Could not verify Judge0 multi-file Java support on the configured service.") from exc
            java_multifile_available=info.get("id")==configured and "multi-file" in str(info.get("name","")).casefold()
        if not java_multifile_available: raise ExecutorConfigurationError("The configured Judge0 host does not expose the required Multi-file program runner. Run public class Main or enable a compatible multi-file Judge0 runner.")
        return configured
    def build_java_multifile_archive(source: str, public_class: JavaPublicClass) -> str:
        package_path=public_class.package_name.replace(".","/") if public_class.package_name else ""; source_path=f"{package_path+'/' if package_path else ''}{public_class.name}.java"; class_name=f"{public_class.package_name+'.' if public_class.package_name else ''}{public_class.name}"
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(source_path,source)
            for path,contents in (("compile",f"#!/bin/bash\nset -euo pipefail\njavac -encoding UTF-8 -d . {source_path}\n"),("run",f"#!/bin/bash\nset -euo pipefail\nexec java {class_name}\n")):
                entry=zipfile.ZipInfo(path); entry.external_attr=0o100755<<16; bundle.writestr(entry,contents)
        return base64.b64encode(archive.getvalue()).decode()
    def execute_judge(source: str,language: str,stdin: str) -> dict[str,Any]:
        submission={"source_code":encode_judge_text(source),"language_id":LANGUAGES[language]["judge0_id"],"stdin":encode_judge_text(stdin)}
        public_class=find_java_public_class(source) if language=="java" else None
        if public_class and (public_class.name!="Main" or public_class.package_name is not None): submission={"language_id":java_multifile_language(),"stdin":encode_judge_text(stdin),"additional_files":build_java_multifile_archive(source,public_class)}
        token=call_judge("POST","/submissions?base64_encoded=true&wait=false",json=submission).get("token")
        if not isinstance(token,str) or not token: raise RuntimeError("Judge0 did not return a submission token.")
        deadline=time.monotonic()+app.config["JUDGE0_MAX_WAIT"]
        while time.monotonic()<deadline:
            result=call_judge("GET",f"/submissions/{token}?base64_encoded=true&fields=stdout,stderr,compile_output,message,status,time,memory")
            if isinstance(result.get("status"),dict) and isinstance(result["status"].get("id"),int) and result["status"]["id"]>2: return decode_judge_result(result)
            time.sleep(app.config["JUDGE0_POLL_INTERVAL"])
        raise RuntimeError("Judge0 did not complete this submission before the application wait limit.")
    def serialize_execution(row: sqlite3.Row,include_source: bool=True) -> dict[str,Any]:
        value=dict(row)
        if not include_source:
            for key in ("source_code","stdin","stdout","stderr","compile_output","message"): value.pop(key,None)
        value["language_name"]=LANGUAGES.get(value["language"],{}).get("name",value["language"]); return value
    def record_execution(source: str,language: str,stdin: str,result: dict[str,Any],user_id: int,problem_id: int|None) -> int:
        status_obj=result.get("status") if isinstance(result.get("status"),dict) else {}; status=str(status_obj.get("description") or result.get("status") or "Unknown")
        cursor=get_db().execute("""INSERT INTO executions(source_code,language,stdin,stdout,stderr,compile_output,message,status,status_id,execution_time,memory,created_at,user_id,problem_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(source,language,stdin,result.get("stdout"),result.get("stderr"),result.get("compile_output"),result.get("message"),status,status_obj.get("id"),result.get("time"),result.get("memory"),utc_now(),user_id,problem_id)); get_db().commit(); return int(cursor.lastrowid)
    def problem_topics(problem_id: int) -> list[str]: return [row["name"] for row in get_db().execute("SELECT t.name FROM topics t JOIN problem_topics pt ON pt.topic_id=t.id WHERE pt.problem_id=? ORDER BY t.name",(problem_id,))]
    def serialize_problem(row: sqlite3.Row,user_id: int|None=None,detail: bool=False) -> dict[str,Any]:
        value=dict(row); value["topics"]=problem_topics(row["id"])
        for key in ("examples_json","starter_codes_json","test_cases_json","hints_json"): value[key.removesuffix("_json")]=json.loads(value.pop(key))
        if user_id is not None:
            progress=get_db().execute("SELECT status,priority FROM user_problem_progress WHERE user_id=? AND problem_id=?",(user_id,row["id"])).fetchone(); value["status"]=progress["status"] if progress else "Unsolved"; value["priority"]=progress["priority"] if progress else "Medium"; value["bookmarked"]=get_db().execute("SELECT 1 FROM bookmarks WHERE user_id=? AND problem_id=?",(user_id,row["id"])).fetchone() is not None; note=get_db().execute("SELECT body FROM notes WHERE user_id=? AND problem_id=?",(user_id,row["id"])).fetchone(); value["note"]=note["body"] if note else ""
        if not detail:
            for key in ("statement","constraints_text","examples","starter_codes","test_cases","hints","explanation","note"): value.pop(key,None)
        return value
    def fetch_problem(identifier: str|int) -> sqlite3.Row|None:
        return get_db().execute(f"SELECT * FROM problems WHERE {'id' if isinstance(identifier,int) else 'slug'}=?",(identifier,)).fetchone()
    def owned_solution(solution_id: int) -> sqlite3.Row|None:
        user=get_user(); return get_db().execute("SELECT * FROM saved_solutions WHERE id=? AND user_id=?",(solution_id,user["id"])).fetchone() if user else None
    def serialize_solution(row: sqlite3.Row) -> dict[str,Any]:
        value=dict(row); value["problem_title"]=get_db().execute("SELECT title FROM problems WHERE id=?",(row["problem_id"],)).fetchone()["title"]; return value

    @app.get("/")
    def index() -> Any: return redirect(url_for(home_endpoint() if get_user() else "compiler"))
    @app.route("/register",methods=["GET","POST"])
    def register() -> Any:
        if get_user(): return redirect(url_for(home_endpoint()))
        error=None
        if request.method=="POST":
            username=request.form.get("username","").strip(); email=request.form.get("email","").strip().lower(); password=request.form.get("password","")
            if not USERNAME_RE.fullmatch(username): error="Username must be 3–32 letters, numbers, or underscores."
            elif not EMAIL_RE.fullmatch(email): error="Enter a valid email address."
            elif len(password)<8: error="Password must be at least 8 characters."
            elif get_db().execute("SELECT 1 FROM users WHERE username=? OR email=?",(username,email)).fetchone(): error="That username or email is already registered."
            if error is None:
                now=utc_now(); cursor=get_db().execute("INSERT INTO users(username,email,password_hash,created_at,updated_at) VALUES(?,?,?,?,?)",(username,email,generate_password_hash(password),now,now)); get_db().commit(); session.clear(); session.permanent=True; session["user_id"]=int(cursor.lastrowid); csrf_token(); return redirect(url_for(home_endpoint()))
        return render_template("register.html",error=error)
    @app.route("/login",methods=["GET","POST"])
    def login() -> Any:
        if get_user(): return redirect(safe_next(home_endpoint()))
        error=None
        if request.method=="POST":
            identity=request.form.get("identity","").strip(); password=request.form.get("password",""); user=get_db().execute("SELECT * FROM users WHERE username=? OR email=?",(identity,identity.lower())).fetchone()
            if user is None or not check_password_hash(user["password_hash"],password): error="Incorrect username/email or password."
            else:
                session.clear(); session.permanent=True; session["user_id"]=user["id"]; csrf_token(); return redirect(safe_next(home_endpoint()))
        return render_template("login.html",error=error)
    @app.post("/logout")
    @login_required
    def logout() -> Any: session.clear(); return redirect(url_for("compiler"))
    @app.get("/compiler")
    def compiler() -> str: return render_template("index.html",languages=LANGUAGES,problem=None)
    @app.get("/dashboard")
    @login_required
    def dashboard() -> str:
        user=get_user(); rows=get_db().execute("SELECT p.id,p.difficulty,COALESCE(upp.status,'Unsolved') status FROM problems p LEFT JOIN user_problem_progress upp ON upp.problem_id=p.id AND upp.user_id=?",(user["id"],)).fetchall(); total=len(rows); solved=sum(row["status"]=="Solved" for row in rows); attempted=sum(row["status"]=="Attempted" for row in rows); difficulty={key:{"total":0,"solved":0} for key in DIFFICULTIES}
        for row in rows: difficulty[row["difficulty"]]["total"]+=1; difficulty[row["difficulty"]]["solved"]+=row["status"]=="Solved"
        topic_rows=get_db().execute("""SELECT t.name,COUNT(DISTINCT p.id) total,SUM(CASE WHEN upp.status='Solved' THEN 1 ELSE 0 END) solved FROM topics t LEFT JOIN problem_topics pt ON pt.topic_id=t.id LEFT JOIN problems p ON p.id=pt.problem_id LEFT JOIN user_problem_progress upp ON upp.problem_id=p.id AND upp.user_id=? GROUP BY t.id ORDER BY t.name""",(user["id"],)).fetchall(); recent=get_db().execute("SELECT e.id,e.status,e.language,e.created_at,p.title problem_title FROM executions e LEFT JOIN problems p ON p.id=e.problem_id WHERE e.user_id=? ORDER BY e.id DESC LIMIT 6",(user["id"],)).fetchall(); return render_template("dashboard.html",metrics={"total":total,"solved":solved,"attempted":attempted},difficulty=difficulty,topic_rows=topic_rows,recent=recent)
    @app.get("/problems")
    @login_required
    def problems() -> str:
        user=get_user(); query=request.args.get("q","").strip(); topic=request.args.get("topic","").strip(); difficulty=request.args.get("difficulty","").strip(); sort=request.args.get("sort","title"); page=max(1,request.args.get("page",1,type=int)); where,params=["1=1"],[user["id"]]
        if query: where.append("(p.title LIKE ? OR p.statement LIKE ?)"); params.extend([f"%{query}%",f"%{query}%"])
        if difficulty in DIFFICULTIES: where.append("p.difficulty=?"); params.append(difficulty)
        if topic in TOPICS: where.append("EXISTS(SELECT 1 FROM problem_topics fpt JOIN topics ft ON ft.id=fpt.topic_id WHERE fpt.problem_id=p.id AND ft.name=?)"); params.append(topic)
        order={"title":"p.title COLLATE NOCASE","difficulty":"CASE p.difficulty WHEN 'Easy' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END","status":"COALESCE(upp.status,'Unsolved')"}.get(sort,"p.title COLLATE NOCASE"); predicate=" AND ".join(where); count=get_db().execute(f"SELECT COUNT(*) count FROM problems p LEFT JOIN user_problem_progress upp ON upp.problem_id=p.id AND upp.user_id=? WHERE {predicate}",params).fetchone()["count"]; per_page=10; rows=get_db().execute(f"SELECT p.* FROM problems p LEFT JOIN user_problem_progress upp ON upp.problem_id=p.id AND upp.user_id=? WHERE {predicate} ORDER BY {order} LIMIT ? OFFSET ?",[*params,per_page,(page-1)*per_page]).fetchall(); return render_template("problems.html",problems=[serialize_problem(row,user["id"]) for row in rows],filters={"q":query,"topic":topic,"difficulty":difficulty,"sort":sort,"page":page,"pages":max(1,(count+per_page-1)//per_page)})
    @app.get("/problems/<slug>")
    @login_required
    def problem_detail(slug: str) -> str:
        problem=fetch_problem(slug)
        if problem is None: abort(404)
        return render_template("problem_detail.html",languages=LANGUAGES,problem=serialize_problem(problem,get_user()["id"],True))
    @app.get("/saved-solutions")
    @login_required
    def saved_solutions() -> str: return render_template("saved_solutions.html",solutions=[serialize_solution(row) for row in get_db().execute("SELECT * FROM saved_solutions WHERE user_id=? ORDER BY updated_at DESC",(get_user()["id"],))])
    @app.get("/bookmarks")
    @login_required
    def bookmarks_page() -> str: return render_template("bookmarks.html",problems=[serialize_problem(row,get_user()["id"]) for row in get_db().execute("SELECT p.* FROM problems p JOIN bookmarks b ON b.problem_id=p.id WHERE b.user_id=? ORDER BY b.created_at DESC",(get_user()["id"],))])
    @app.get("/progress")
    @login_required
    def progress_page() -> Any: return redirect(url_for(home_endpoint()))
    @app.get("/history")
    @login_required
    def history_page() -> str: return render_template("history.html")
    @app.get("/profile")
    @login_required
    def profile() -> str: return render_template("profile.html")
    @app.get("/api/csrf")
    def csrf_endpoint() -> Any:
        # Same-origin JavaScript uses this to recover from an expired session. Cross-origin pages cannot read the response.
        return jsonify(csrf_token=csrf_token())
    @app.get("/api/languages")
    def languages() -> Any: return jsonify(LANGUAGES)
    @app.get("/api/me")
    def me() -> Any:
        user=get_user(); return jsonify(authenticated=user is not None,user=dict(user) if user else None,guest_policy="Guests can run code but runs are not saved. Sign in to use history and DSA practice data.")
    @app.post("/api/execute")
    def execute() -> Any:
        payload=request_json()
        if isinstance(payload,tuple): return payload
        try: source=bounded_text(payload.get("source_code"),"Code",100_000,False); stdin=bounded_text(payload.get("stdin",""),"Custom input",50_000)
        except ValueError as exc: return jsonify(error=str(exc)),400
        language=payload.get("language")
        if not isinstance(language,str) or language not in LANGUAGES: return jsonify(error="Choose a supported language."),400
        problem_id=payload.get("problem_id")
        if problem_id is not None and (not isinstance(problem_id,int) or fetch_problem(problem_id) is None): return jsonify(error="Unknown problem."),400
        try: result=execute_judge(source,language,stdin)
        except ExecutorConfigurationError as exc: result={"status":{"description":"Executor Configuration Error"},"message":str(exc)}
        except RuntimeError as exc: result={"status":{"description":"API Error"},"message":str(exc)}
        user=get_user(); status=result.get("status") if isinstance(result.get("status"),dict) else {}
        if user is None: return jsonify(id=None,source_code=source,language=language,stdin=stdin,stdout=result.get("stdout"),stderr=result.get("stderr"),compile_output=result.get("compile_output"),message=result.get("message"),status=status.get("description","Unknown"),status_id=status.get("id"),execution_time=result.get("time"),memory=result.get("memory"),history_saved=False)
        execution_id=record_execution(source,language,stdin,result,user["id"],problem_id); row=get_db().execute("SELECT * FROM executions WHERE id=? AND user_id=?",(execution_id,user["id"])).fetchone(); response=serialize_execution(row); response["history_saved"]=True; return jsonify(response)
    @app.get("/api/history")
    @login_required
    def history() -> Any: return jsonify([serialize_execution(row,False) for row in get_db().execute("SELECT * FROM executions WHERE user_id=? ORDER BY id DESC LIMIT ?",(get_user()["id"],app.config["HISTORY_LIMIT"]))])
    @app.get("/api/history/<int:execution_id>")
    @login_required
    def history_detail(execution_id: int) -> Any:
        row=get_db().execute("SELECT * FROM executions WHERE id=? AND user_id=?",(execution_id,get_user()["id"])).fetchone()
        if row is None: abort(404)
        return jsonify(serialize_execution(row))
    @app.delete("/api/history/<int:execution_id>")
    @login_required
    def delete_history(execution_id: int) -> Any:
        cursor=get_db().execute("DELETE FROM executions WHERE id=? AND user_id=?",(execution_id,get_user()["id"])); get_db().commit()
        if cursor.rowcount==0: abort(404)
        return jsonify(deleted=True,id=execution_id)
    @app.get("/api/problems")
    @login_required
    def api_problems() -> Any: return jsonify([serialize_problem(row,get_user()["id"]) for row in get_db().execute("SELECT * FROM problems ORDER BY title")])
    @app.get("/api/problems/<slug>")
    @login_required
    def api_problem_detail(slug: str) -> Any:
        problem=fetch_problem(slug)
        if problem is None: abort(404)
        return jsonify(serialize_problem(problem,get_user()["id"],True))
    @app.post("/api/problems/<int:problem_id>/progress")
    @login_required
    def update_progress(problem_id: int) -> Any:
        if fetch_problem(problem_id) is None: abort(404)
        payload=request_json()
        if isinstance(payload,tuple): return payload
        status,priority=payload.get("status","Unsolved"),payload.get("priority","Medium")
        if status not in STATUSES or priority not in PRIORITIES: return jsonify(error="Invalid status or priority."),400
        now=utc_now(); get_db().execute("INSERT INTO user_problem_progress(user_id,problem_id,status,priority,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,problem_id) DO UPDATE SET status=excluded.status,priority=excluded.priority,updated_at=excluded.updated_at",(get_user()["id"],problem_id,status,priority,now,now)); get_db().commit(); return jsonify(status=status,priority=priority)
    @app.post("/api/problems/<int:problem_id>/bookmark")
    @login_required
    def add_bookmark(problem_id: int) -> Any:
        if fetch_problem(problem_id) is None: abort(404)
        get_db().execute("INSERT OR IGNORE INTO bookmarks(user_id,problem_id,created_at) VALUES(?,?,?)",(get_user()["id"],problem_id,utc_now())); get_db().commit(); return jsonify(bookmarked=True)
    @app.delete("/api/problems/<int:problem_id>/bookmark")
    @login_required
    def remove_bookmark(problem_id: int) -> Any: get_db().execute("DELETE FROM bookmarks WHERE user_id=? AND problem_id=?",(get_user()["id"],problem_id)); get_db().commit(); return jsonify(bookmarked=False)
    @app.get("/api/problems/<int:problem_id>/note")
    @login_required
    def get_note(problem_id: int) -> Any:
        row=get_db().execute("SELECT body,updated_at FROM notes WHERE user_id=? AND problem_id=?",(get_user()["id"],problem_id)).fetchone(); return jsonify(dict(row) if row else {"body":"","updated_at":None})
    @app.put("/api/problems/<int:problem_id>/note")
    @login_required
    def save_note(problem_id: int) -> Any:
        if fetch_problem(problem_id) is None: abort(404)
        payload=request_json()
        if isinstance(payload,tuple): return payload
        try: body=bounded_text(payload.get("body",""),"Note",10_000)
        except ValueError as exc: return jsonify(error=str(exc)),400
        now=utc_now(); get_db().execute("INSERT INTO notes(user_id,problem_id,body,created_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,problem_id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at",(get_user()["id"],problem_id,body,now,now)); get_db().commit(); return jsonify(body=body,updated_at=now)
    @app.get("/api/problems/<int:problem_id>/solutions")
    @login_required
    def list_solutions(problem_id: int) -> Any: return jsonify([serialize_solution(row) for row in get_db().execute("SELECT * FROM saved_solutions WHERE user_id=? AND problem_id=? ORDER BY updated_at DESC",(get_user()["id"],problem_id))])
    @app.post("/api/problems/<int:problem_id>/solutions")
    @login_required
    def create_solution(problem_id: int) -> Any:
        if fetch_problem(problem_id) is None: abort(404)
        payload=request_json()
        if isinstance(payload,tuple): return payload
        try: title=bounded_text(str(payload.get("title","Untitled solution")).strip(),"Solution title",120,False); source=bounded_text(payload.get("source_code"),"Code",100_000,False); notes_text=bounded_text(payload.get("notes",""),"Solution notes",10_000)
        except ValueError as exc: return jsonify(error=str(exc)),400
        language=payload.get("language")
        if language not in LANGUAGES: return jsonify(error="Choose a supported language."),400
        now=utc_now(); cursor=get_db().execute("INSERT INTO saved_solutions(user_id,problem_id,title,source_code,language,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(get_user()["id"],problem_id,title,source,language,notes_text,now,now)); get_db().commit(); return jsonify(serialize_solution(owned_solution(int(cursor.lastrowid)))),201
    @app.get("/api/solutions/<int:solution_id>")
    @login_required
    def get_solution(solution_id: int) -> Any:
        row=owned_solution(solution_id)
        if row is None: abort(404)
        return jsonify(serialize_solution(row))
    @app.patch("/api/solutions/<int:solution_id>")
    @login_required
    def edit_solution(solution_id: int) -> Any:
        row=owned_solution(solution_id)
        if row is None: abort(404)
        payload=request_json()
        if isinstance(payload,tuple): return payload
        try: title=bounded_text(payload.get("title",row["title"]),"Solution title",120,False); source=bounded_text(payload.get("source_code",row["source_code"]),"Code",100_000,False); notes_text=bounded_text(payload.get("notes",row["notes"]),"Solution notes",10_000)
        except ValueError as exc: return jsonify(error=str(exc)),400
        language=payload.get("language",row["language"])
        if language not in LANGUAGES: return jsonify(error="Choose a supported language."),400
        get_db().execute("UPDATE saved_solutions SET title=?,source_code=?,language=?,notes=?,updated_at=? WHERE id=? AND user_id=?",(title,source,language,notes_text,utc_now(),solution_id,get_user()["id"])); get_db().commit(); return jsonify(serialize_solution(owned_solution(solution_id)))
    @app.delete("/api/solutions/<int:solution_id>")
    @login_required
    def delete_solution(solution_id: int) -> Any:
        cursor=get_db().execute("DELETE FROM saved_solutions WHERE id=? AND user_id=?",(solution_id,get_user()["id"])); get_db().commit()
        if cursor.rowcount==0: abort(404)
        return jsonify(deleted=True,id=solution_id)
    @app.put("/api/solutions/<int:solution_id>/complexity")
    @login_required
    def update_complexity(solution_id: int) -> Any:
        if owned_solution(solution_id) is None: abort(404)
        payload=request_json()
        if isinstance(payload,tuple): return payload
        try: time_complexity=bounded_text(payload.get("time_complexity",""),"Time complexity",200); space_complexity=bounded_text(payload.get("space_complexity",""),"Space complexity",200); explanation=bounded_text(payload.get("explanation",""),"Complexity explanation",4_000)
        except ValueError as exc: return jsonify(error=str(exc)),400
        get_db().execute("UPDATE saved_solutions SET time_complexity=?,space_complexity=?,complexity_explanation=?,updated_at=? WHERE id=? AND user_id=?",(time_complexity,space_complexity,explanation,utc_now(),solution_id,get_user()["id"])); get_db().commit(); return jsonify(serialize_solution(owned_solution(solution_id)))
    @app.post("/api/dry-run")
    @login_required
    def dry_run() -> Any:
        payload=request_json()
        if isinstance(payload,tuple): return payload
        try: source=bounded_text(payload.get("source_code"),"Code",100_000,False); bounded_text(payload.get("stdin",""),"Custom input",50_000)
        except ValueError as exc: return jsonify(error=str(exc)),400
        language=payload.get("language")
        if language not in LANGUAGES: return jsonify(error="Choose a supported language."),400
        steps=[{"step":step,"line":line_no,"text":line.strip(),"variables":None,"condition":None,"collections":None} for step,(line_no,line) in enumerate(((i,text) for i,text in enumerate(source.splitlines(),1) if text.strip()),1)]
        return jsonify(mode="manual",language=language,steps=steps,limitation="Manual trace mode: CodeRun does not execute or instrument arbitrary submissions on Flask, so variable values and branch results are intentionally not guessed. Use the controls to record your own observations.")
    with app.app_context(): init_db()
    return app

app=create_app()
if __name__=="__main__": app.run(host="127.0.0.1",port=int(os.environ.get("PORT","5000")),debug=os.environ.get("FLASK_DEBUG","").lower() in {"1","true","yes"})
