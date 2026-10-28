# Go Best Practices — dev-agent

> Guía de referencia para los agentes al leer, escribir y revisar código Go
> de los servicios del proyecto activo.

## Reglas críticas de este proyecto
- **NUNCA usar `panic()` en código productivo** (solo en mocks/tests). Un panic en una
  lambda de pagos tumba la transacción sin manejo controlado.
- **SIEMPRE usar la abstracción del módulo core compartido (`CORE_MODULE_NAME`)** (ej. `ILambdaGateway`) en lugar del
  SDK crudo de AWS. Facilita testing y consistencia.
- **Si una interface tiene múltiples definiciones** en el codebase — si modificas una,
  sincroniza todas. (Ver `knowledge/extracted/interfaces.md` para las ubicaciones exactas.)
- **Llaves RSA / secretos NUNCA en el repo** — usar AWS Secrets Manager / KMS.
- **Ningún token de ejemplo (`"0123456789ABCDEF"`) debe quedar** en producción.
- **Nunca loggear PAN/PIN/datos de tarjeta sin enmascarar** (PCI-DSS).

## Manejo de errores Go
- Siempre retornar `error` como último valor de retorno.
- Usar el catálogo de errores del módulo core compartido (ver `interfaces.md` / `extracted/`).
- Nunca silenciar errores con `_` salvo justificación explícita y documentada.
- Envolver errores con contexto (`fmt.Errorf("...: %w", err)`) para trazabilidad.

## Interfaces y Dependency Injection
- Depender de interfaces (puertos), nunca de implementaciones concretas.
- El *composition root* es la función `InitializeXxx` — ahí va el wiring de dependencias.
- **ISP (Interface Segregation):** interfaces pequeñas. Una interface de
  ~27 métodos es un anti-ejemplo, NO replicar ese tamaño en interfaces nuevas.

## Patrones obligatorios en nuevas lambdas
1. Bootstrap vía el módulo core compartido.
2. Cadena de middleware: `logging → rollbar → parse → schema → dominio → error handling`.
3. Dependency injection manual en `InitializeXxx`.
4. Gateway para toda dependencia externa (DynamoDB, APC, KMS, HTTP, SNS, SQS).

## Concurrencia
- Preferir canales y `context.Context` para cancelación/timeout.
- Cuidado con goroutines en lambdas: el runtime congela el proceso tras el return del handler.
- No compartir estado mutable entre invocaciones sin sincronización.

## Testing
- Testify + Mockery (mocks generados en `mocks/`).
- Un test por rama de decisión relevante; cubrir el path de error, no solo el feliz.
- Los mocks pueden usar `panic()` — el código productivo no.

## Versionado del core
- ⚠️ Las versiones se leen siempre del go.mod real con read_file — nunca confíes en
  versiones hardcodeadas en este documento.
- Puede haber **version drift**: distintos servicios fijan versiones distintas del core. Al compartir
  código entre servicios, leer el go.mod de cada repo y verificar compatibilidad de
  la API del core en todos. (Ver `extracted/tech_debt.md`.)

---
*Las secciones que citan archivo:línea concretos viven en `knowledge/extracted/`,
generadas desde el código real.*