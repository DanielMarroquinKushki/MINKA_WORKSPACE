"""
test_runner.py — Corre tests Go después de un cambio del agente.
Implementación funcional, lista para cablearse al flujo de escritura en el
Nivel 5 (Ola 2), cuando EVALUATOR_ENABLED=True y RUN_TESTS_AFTER_CHANGE=True.
"""

import subprocess
import time
from dataclasses import dataclass


@dataclass
class TestResult:
    passed: bool
    repo: str
    packages_tested: int
    packages_failed: int
    duration_seconds: float
    output: str
    failed_tests: list[str]
    error_message: str = ""


def run_tests(repo_path: str, repo_name: str, timeout: int = 60) -> TestResult:
    """
    Corre go test ./... en el repo afectado.

    Args:
        repo_path: ruta absoluta al repo Go
        repo_name: nombre del repo (pos, hsm, core)
        timeout: segundos máximo de ejecución

    Returns:
        TestResult con resultado completo
    """
    start = time.time()

    try:
        result = subprocess.run(
            ["go", "test", "./...", "-v", "-timeout", f"{timeout}s"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )

        duration = time.time() - start
        output = result.stdout + result.stderr

        # Parsea resultados
        passed = result.returncode == 0
        packages_tested = output.count("\nok ")
        packages_failed = output.count("\nFAIL\t")

        # Extrae tests fallidos
        failed_tests = []
        for line in output.split("\n"):
            if line.startswith("--- FAIL:"):
                test_name = line.replace("--- FAIL:", "").strip().split(" ")[0]
                failed_tests.append(test_name)

        return TestResult(
            passed=passed,
            repo=repo_name,
            packages_tested=packages_tested,
            packages_failed=packages_failed,
            duration_seconds=round(duration, 2),
            output=output[-3000:],  # últimas 3000 chars
            failed_tests=failed_tests,
        )

    except subprocess.TimeoutExpired:
        return TestResult(
            passed=False,
            repo=repo_name,
            packages_tested=0,
            packages_failed=0,
            duration_seconds=timeout,
            output="",
            failed_tests=[],
            error_message=f"Timeout después de {timeout}s",
        )
    except FileNotFoundError:
        return TestResult(
            passed=False,
            repo=repo_name,
            packages_tested=0,
            packages_failed=0,
            duration_seconds=0,
            output="",
            failed_tests=[],
            error_message="go no encontrado en PATH",
        )
    except Exception as e:
        return TestResult(
            passed=False,
            repo=repo_name,
            packages_tested=0,
            packages_failed=0,
            duration_seconds=0,
            output="",
            failed_tests=[],
            error_message=str(e),
        )


def detect_repo_from_path(file_path: str, config) -> tuple[str, str]:
    """
    Detecta qué repo Go fue modificado basándose en la ruta del archivo.
    Retorna (repo_name, repo_path) o ("", "") si no detecta.
    """
    for repo_name, repo_path in config.REPOS.items():
        if not repo_path:  # config.py global trae REPOS vacíos; "".startswith("") es True
            continue
        if file_path.startswith(repo_path):
            return repo_name, repo_path
    return "", ""


def format_test_result(result: TestResult) -> str:
    """Formatea el resultado para mostrarlo en la terminal con Rich."""
    if result.error_message:
        return f"❌ Error al correr tests: {result.error_message}"

    status = "✅ TESTS PASARON" if result.passed else "❌ TESTS FALLARON"
    summary = (
        f"{status} — {result.repo}\n"
        f"Paquetes: {result.packages_tested} OK, "
        f"{result.packages_failed} fallidos | "
        f"Tiempo: {result.duration_seconds}s"
    )

    if result.failed_tests:
        summary += "\nTests fallidos:\n"
        for test in result.failed_tests:
            summary += f"  - {test}\n"

    return summary
