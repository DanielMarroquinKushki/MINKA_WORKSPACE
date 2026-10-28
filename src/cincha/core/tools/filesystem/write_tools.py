"""
write_tools — escritura de bajo nivel en el filesystem.

IMPORTANTE (regla de seguridad del proyecto):
Estas funciones NO deben ser llamadas directamente por un agente para escribir
en los repos Go. La escritura pasa SIEMPRE por orchestrator.approval_gate, que
muestra el diff y pide confirmación. Estas son las primitivas que el gate usa
una vez aprobado el cambio.
"""

from pathlib import Path


def write_file(path: str, content: str) -> str:
    """
    Escribe `content` en `path`, creando directorios padre si faltan.
    Sobreescribe si el archivo existe.

    Devuelve un mensaje de resultado. Pensada para uso POST-aprobación.
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: escrito {path} ({len(content)} chars)"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR escribiendo {path}: {exc}"


def apply_diff(path: str, old_content: str, new_content: str) -> str:
    """
    Reemplazo seguro: solo escribe si el contenido actual coincide con
    `old_content` (evita pisar cambios inesperados / condiciones de carrera).
    """
    p = Path(path)
    if not p.is_file():
        return f"ERROR: no existe {path}"

    current = p.read_text(encoding="utf-8", errors="replace")
    if current != old_content:
        return (
            "ERROR: el contenido actual no coincide con old_content. "
            "El archivo cambió desde la última lectura — reintenta la lectura."
        )
    return write_file(path, new_content)
