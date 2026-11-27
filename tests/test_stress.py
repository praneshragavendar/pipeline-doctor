"""Stress tests: large logs and many logs must stay fast and correct."""

import time

import pytest

from pipelinedoctor.agents import Doctor


@pytest.fixture(scope="module")
def doctor():
    return Doctor()


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_large_log_with_error_at_the_end(tmp_path, doctor):
    # 100,000 lines of noise, real error on the last line.
    log = tmp_path / "huge.log"
    noise = "".join(f"[INFO] step {i} compiled module_{i % 50} ok\n" for i in range(100_000))
    log.write_text(noise + "fatal: java.io.IOException: No space left on device\n", encoding="utf-8")

    start = time.perf_counter()
    diagnosis = doctor.diagnose(str(log), use_llm=False)
    elapsed = time.perf_counter() - start

    assert diagnosis.rule_id == "DISK_SPACE"
    assert elapsed < 20, f"took {elapsed:.1f}s"


def test_many_logs_in_a_row(tmp_path, doctor):
    paths = []
    for i in range(200):
        p = tmp_path / f"build_{i}.log"
        p.write_text(f"step {i}\nModuleNotFoundError: No module named 'pkg{i}'\n", encoding="utf-8")
        paths.append(p)
    results = [doctor.diagnose(str(p), use_llm=False) for p in paths]
    assert all(r.rule_id == "PYTHON_MODULE_MISSING" for r in results)


def test_binary_garbage_does_not_crash(tmp_path, doctor):
    log = tmp_path / "garbage.log"
    log.write_bytes(bytes(range(256)) * 50)
    diagnosis = doctor.diagnose(str(log), use_llm=False)
    assert diagnosis.status in {"diagnosed", "unknown"}
