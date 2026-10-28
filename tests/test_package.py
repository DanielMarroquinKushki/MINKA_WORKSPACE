"""cincha es un paquete regular (con __init__.py), no un namespace package."""

import cincha


def test_cincha_es_paquete_regular():
    # En un namespace package (sin __init__.py) __file__ es None y el wheel lo empaqueta igual.
    assert cincha.__file__ is not None
    assert cincha.__file__.endswith("__init__.py")
