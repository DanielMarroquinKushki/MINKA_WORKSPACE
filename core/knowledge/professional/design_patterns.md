# Patrones de Diseño — GoF + patrones de los repos del proyecto

> Referencia conceptual. Los ejemplos con archivo:línea reales viven en
> `knowledge/extracted/patterns.md` (extraído del código).

## Patrones presentes en el codebase

### Factory / Constructor
- Funciones `NewXxx()` e `InitializeXxx()` construyen servicios y ensamblan dependencias.
- `InitializeXxx` es además el *composition root* (DI manual).

### Strategy
- Distintas estrategias de traducción/mapeo (ej. traducción de PIN por red de pago).
- Se selecciona la estrategia según el tipo de transacción/red.

### Command (+ Undo)
- Encapsula una operación como objeto; permite componer y, en algunos casos, deshacer
  (relevante para Reverso/Anulación). Ver `service/shared/command/`.

### Gateway (Ports & Adapters)
- Toda dependencia externa (DynamoDB, KMS, APC, HTTP, SNS, SQS) detrás de una interface
  Gateway. El dominio no conoce el SDK.

### Chain of Responsibility (Middleware)
- La cadena `logging → rollbar → parse → schema → dominio → error handling` es un
  chain of responsibility aplicado al procesamiento del evento de la lambda.

### Dependency Injection
- Inyección manual por constructor/`Initialize`. Sin framework de DI.

## Anti-patrones a evitar (detectados en el proyecto)
- **Interface gorda**: una interface de ~27 métodos viola ISP. No replicar.
- **Panic en producción**: usar manejo de errores, no panic.
- **Acceso directo al SDK** saltándose el Gateway/módulo core.

## Al crear una nueva lambda
Combinar: Bootstrap (core) + Middleware chain + Factory (`Initialize`) + Gateways +
Strategy si hay variantes por red. Ver `professional/lambda_patterns.md`.