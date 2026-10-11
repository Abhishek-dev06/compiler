from pathlib import Path
import base64
import io
import zipfile

import pytest
import requests

from app import create_app, find_java_public_class


def encoded_judge_result(result):
    """Create the text shape returned by Judge0 with base64_encoded=true."""
    return {
        key: base64.b64encode(value.encode("utf-8")).decode("ascii")
        if key in {"stdout", "stderr", "compile_output", "message"} and isinstance(value, str)
        else value
        for key, value in result.items()
    }


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "history.db")})
    app.execute_judge = None  # Document that execution is patched below, not local.
    with app.test_client() as client:
        yield client, app


def fake_result(source_code, language, stdin):
    return {"stdout": f"received: {stdin}\n", "status": {"id": 3, "description": "Accepted"}, "time": "0.012", "memory": 8400}


def test_execution_is_stored_and_restored(client, monkeypatch):
    client, app = client
    # execute_judge is a closure, so patch the requests boundary with a complete
    # Judge0 submission/poll sequence.
    calls = []
    class Response:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body
    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if method == "POST": return Response({"token": "abc"})
        return Response(encoded_judge_result({"stdout": "Hello, World!\n", "status": {"id": 3, "description": "Accepted"}, "time": "0.01", "memory": 8500}))
    monkeypatch.setattr("app.requests.request", fake_request)
    result = client.post("/api/execute", json={"source_code": 'print(input())', "language": "python", "stdin": "Ada\n"})
    assert result.status_code == 200
    run = result.get_json()
    assert run["status"] == "Accepted"
    assert run["stdout"] == "Hello, World!\n"
    assert calls[0][0] == "POST" and calls[1][0] == "GET"
    history = client.get("/api/history").get_json()
    assert len(history) == 1 and "source_code" not in history[0]
    restored = client.get(f"/api/history/{run['id']}").get_json()
    assert restored["source_code"] == 'print(input())'
    assert restored["stdin"] == "Ada\n"
    assert client.delete(f"/api/history/{run['id']}").status_code == 200
    assert client.get("/api/history").get_json() == []


def test_api_failure_is_stored_without_fabricated_output(client, monkeypatch):
    client, _ = client
    # Force the requests layer to fail in the app's real async protocol.
    monkeypatch.setattr(
        "app.requests.request",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("offline")),
    )
    response = client.post("/api/execute", json={"source_code": "print(1)", "language": "python", "stdin": ""})
    assert response.status_code == 200
    run = response.get_json()
    assert run["status"] == "API Error"
    assert run["stdout"] is None
    assert "Judge0" in run["message"]


def test_invalid_language_is_rejected(client):
    client, _ = client
    response = client.post("/api/execute", json={"source_code": "x", "language": "ruby", "stdin": ""})
    assert response.status_code == 400


def test_history_survives_a_new_backend_instance(tmp_path: Path, monkeypatch):
    database = str(tmp_path / "persistent-history.db")

    class Response:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    monkeypatch.setattr(
        "app.requests.request",
        lambda method, url, **kwargs: Response({"token": "restart"}) if method == "POST" else Response(encoded_judge_result({"stdout": "persisted\n", "status": {"id": 3, "description": "Accepted"}})),
    )
    first_app = create_app({"TESTING": True, "DATABASE": database})
    with first_app.test_client() as first_client:
        response = first_client.post("/api/execute", json={"source_code": "print('persisted')", "language": "python", "stdin": ""})
        assert response.status_code == 200
    restarted_app = create_app({"TESTING": True, "DATABASE": database})
    with restarted_app.test_client() as restarted_client:
        history = restarted_client.get("/api/history").get_json()
    assert len(history) == 1
    assert history[0]["status"] == "Accepted"


def read_multifile_archive(value):
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(value))) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def test_java_main_uses_the_existing_single_file_judge0_flow(client, monkeypatch):
    client, _ = client
    source = """public class Main {
    public static void main(String[] args) { System.out.println("Java works"); }
}
"""
    calls = []

    class Response:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    def judge0(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if method == "POST":
            return Response({"token": "java-main"})
        return Response(encoded_judge_result({"stdout": "Java works\n", "status": {"id": 3, "description": "Accepted"}, "time": "0.041", "memory": 11200}))

    monkeypatch.setattr("app.requests.request", judge0)
    run = client.post("/api/execute", json={"source_code": source, "language": "java", "stdin": ""}).get_json()
    post = next(call for call in calls if call[0] == "POST")
    assert not any("/languages/89" in call[1] for call in calls)
    assert post[2]["language_id"] == 62
    assert base64.b64decode(post[2]["source_code"]).decode("utf-8") == source
    assert run["status"] == "Accepted"
    assert run["stdout"] == "Java works\n"


@pytest.mark.parametrize("class_name", ["Stack", "ArrayStack"])
def test_public_generic_java_class_uses_its_real_filename(client, monkeypatch, class_name):
    client, _ = client
    source = f"""public class {class_name}<T> {{
    public static void main(String[] args) {{ System.out.println("{class_name} works"); }}
}}
"""
    calls = []

    class Response:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    def judge0(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if method == "GET" and url.endswith("/languages/89"):
            return Response({"id": 89, "name": "Multi-file program"})
        if method == "POST":
            return Response({"token": f"{class_name}-token"})
        return Response(encoded_judge_result({"stdout": f"{class_name} works\n", "status": {"id": 3, "description": "Accepted"}, "time": "0.02", "memory": 9000}))

    monkeypatch.setattr("app.requests.request", judge0)
    run = client.post("/api/execute", json={"source_code": source, "language": "java", "stdin": ""}).get_json()
    post = next(call for call in calls if call[0] == "POST")
    assert any(call[0] == "GET" and call[1].endswith("/languages/89") for call in calls)
    assert post[2]["language_id"] == 89
    assert "source_code" not in post[2]
    files = read_multifile_archive(post[2]["additional_files"])
    assert files[f"{class_name}.java"] == source
    assert f"javac -encoding UTF-8 -d . {class_name}.java" in files["compile"]
    assert f"exec java {class_name}" in files["run"]
    assert run["status"] == "Accepted"
    assert run["stdout"] == f"{class_name} works\n"


def test_java_detector_ignores_comments_strings_and_nested_classes():
    source = '''// public class Fake<T> {}
class Outer {
    String text = "public class Wrong<T> {}";
    public class Nested<T> {}
}
public final class RealStack < T > {}
'''
    detected = find_java_public_class(source)
    assert detected is not None
    assert detected.name == "RealStack"
    assert detected.package_name is None
    assert find_java_public_class('String source = "public class Fake<T>";') is None
    annotated = find_java_public_class("public @Deprecated final class Annotated<T> {}")
    assert annotated is not None and annotated.name == "Annotated"


def test_java_multifile_preserves_input_output_errors_and_history(client, monkeypatch):
    client, _ = client
    source = """package demo;
public class Stack<T> {
    public static void main(String[] args) { System.out.println("got input"); }
}
"""
    calls = []

    class Response:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    def judge0(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if method == "GET" and url.endswith("/languages/89"):
            return Response({"id": 89, "name": "Multi-file program"})
        if method == "POST":
            return Response({"token": "stack-input"})
        return Response(encoded_judge_result({"stdout": "got input\n", "status": {"id": 3, "description": "Accepted"}, "time": "0.031", "memory": 10240}))

    monkeypatch.setattr("app.requests.request", judge0)
    run = client.post("/api/execute", json={"source_code": source, "language": "java", "stdin": "Ada\n"}).get_json()
    post = next(call for call in calls if call[0] == "POST")
    files = read_multifile_archive(post[2]["additional_files"])
    assert files["demo/Stack.java"] == source
    assert base64.b64decode(post[2]["stdin"]).decode("utf-8") == "Ada\n"
    assert run["stdout"] == "got input\n"
    assert run["execution_time"] == "0.031" and run["memory"] == 10240
    restored = client.get(f"/api/history/{run['id']}").get_json()
    assert restored["source_code"] == source and restored["stdin"] == "Ada\n"


@pytest.mark.parametrize(
    ("result", "field", "value"),
    [
        ({"compile_output": "Stack.java:1: error: broken", "status": {"id": 6, "description": "Compilation Error"}}, "compile_output", "Stack.java:1: error: broken"),
        ({"stderr": "Exception in thread \"main\" java.lang.RuntimeException", "status": {"id": 11, "description": "Runtime Error (NZEC)"}}, "stderr", "Exception in thread \"main\" java.lang.RuntimeException"),
    ],
)
def test_java_multifile_error_results_are_reported(client, monkeypatch, result, field, value):
    client, _ = client
    source = "public class Stack<T> { public static void main(String[] args) {} }"

    class Response:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    def judge0(method, url, **kwargs):
        if method == "GET" and url.endswith("/languages/89"):
            return Response({"id": 89, "name": "Multi-file program"})
        return Response({"token": "java-error"}) if method == "POST" else Response(encoded_judge_result(result))

    monkeypatch.setattr("app.requests.request", judge0)
    run = client.post("/api/execute", json={"source_code": source, "language": "java", "stdin": ""}).get_json()
    assert run["status"] == result["status"]["description"]
    assert run[field] == value


def test_java_multifile_unavailable_is_recorded_honestly(client, monkeypatch):
    client, _ = client

    class Response:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    monkeypatch.setattr("app.requests.request", lambda method, url, **kwargs: Response({"id": 89, "name": "Java (OpenJDK)"}))
    source = "public class Stack<T> { public static void main(String[] args) {} }"
    run = client.post("/api/execute", json={"source_code": source, "language": "java", "stdin": ""}).get_json()
    assert run["status"] == "Executor Configuration Error"
    assert "Multi-file program" in run["message"]
    assert run["source_code"] == source


def test_restore_then_edit_submits_the_edited_current_source_once(client, monkeypatch):
    client, _ = client
    sources = [
        "public class Stack<T> { public static void main(String[] args) { System.out.println(\"old\"); } }",
        "public class Stack<T> { public static void main(String[] args) { System.out.println(\"edited\"); } }",
    ]
    submitted_archives = []

    class Response:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    def judge0(method, url, **kwargs):
        if method == "GET" and url.endswith("/languages/89"):
            return Response({"id": 89, "name": "Multi-file program"})
        if method == "POST":
            submitted_archives.append(kwargs["json"]["additional_files"])
            return Response({"token": f"token-{len(submitted_archives)}"})
        return Response(encoded_judge_result({"stdout": "ok\n", "status": {"id": 3, "description": "Accepted"}}))

    monkeypatch.setattr("app.requests.request", judge0)
    original = client.post("/api/execute", json={"source_code": sources[0], "language": "java", "stdin": ""}).get_json()
    restored = client.get(f"/api/history/{original['id']}").get_json()
    assert restored["source_code"] == sources[0]
    client.post("/api/execute", json={"source_code": sources[1], "language": "java", "stdin": ""})
    assert len(submitted_archives) == 2
    assert read_multifile_archive(submitted_archives[1])["Stack.java"] == sources[1]
    script = (Path(__file__).parents[1] / "static" / "app.js").read_text(encoding="utf-8")
    assert script.count("source_code: editor.getValue()") == 1


@pytest.mark.parametrize(
    ("language", "source", "stdin", "judge_result", "expected_status", "expected_field"),
    [
        ("c", '#include <stdio.h>\nint main(void){puts("OK");}', "", {"stdout": "OK\n", "status": {"id": 3, "description": "Accepted"}, "time": "0.01", "memory": 8700}, "Accepted", "stdout"),
        ("cpp", '#include <iostream>\nint main(){std::cout << "OK";}', "", {"stdout": "OK", "status": {"id": 3, "description": "Accepted"}, "time": "0.02", "memory": 9100}, "Accepted", "stdout"),
        ("python", "print(", "", {"compile_output": "SyntaxError: '(' was never closed", "status": {"id": 6, "description": "Compilation Error"}}, "Compilation Error", "compile_output"),
        ("python", "1 / 0", "", {"stderr": "ZeroDivisionError: division by zero", "status": {"id": 11, "description": "Runtime Error (NZEC)"}}, "Runtime Error (NZEC)", "stderr"),
    ],
)
def test_judge0_results_are_kept_as_returned(client, monkeypatch, language, source, stdin, judge_result, expected_status, expected_field):
    client, _ = client
    class Response:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body
    def judge0(method, url, **kwargs):
        return Response({"token": "test-token"}) if method == "POST" else Response(encoded_judge_result(judge_result))
    monkeypatch.setattr("app.requests.request", judge0)
    run = client.post("/api/execute", json={"source_code": source, "language": language, "stdin": stdin}).get_json()
    assert run["status"] == expected_status
    assert run[expected_field] == judge_result[expected_field]
