# Dónde se resuelve el token

## Respuesta directa

El token lo arma `build_token_provider` en service/token_provider.go:42 y lo inyecta el constructor del sender en gateway/sender.go:47.

## Evidencia

| Pieza | archivo:línea | Nota |
|---|---|---|
| Proveedor estático | service/token_provider.go:42 | lee la env var |
| Proveedor SSO | service/token_provider.go:75 | renueva al expirar |
| Uso en el sender | gateway/sender.go:71 | header Authorization |

```go
func buildTokenProvider(lg Logger) (ITokenProvider, error) {
    if token := os.Getenv(authToken); token != "" {
        return NewStaticTokenProvider(token)
    }
    return NewSSOTokenProvider(lg)
}
```

Identificadores con guiones bajos y asteriscos sueltos: snake_case_name, `__init__`, `*args`, `**kwargs`, a_b*c y el índice [rag] con [bold]texto[/bold] literal.

## Riesgo

- `StaticTokenProvider` acepta cualquier valor sin validar por stage.
- Cita inventada para la verificación: service/missing_file.go:10 ⚠ no existe

_Respuesta acotada; pide 'en detalle' para ampliar._
