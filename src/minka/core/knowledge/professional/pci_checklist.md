# Checklist PCI-DSS para revisión de código

Se carga solo cuando el proyecto declara `COMPLIANCE = ["pci"]`. Complementa la checklist
general; cada punto se verifica con `archivo:línea`, nunca por suposición.

## Datos de tarjeta
- El PAN nunca se registra en logs ni en mensajes de error; si debe mostrarse, va enmascarado
  (primeros 6 / últimos 4 como máximo).
- Datos sensibles de autenticación (CVV, PIN, pista completa) no se persisten después de la
  autorización, ni siquiera cifrados.
- Los campos con datos de tarjeta viajan y se guardan cifrados; la llave no vive junto al dato.

## Llaves y secretos
- Ninguna llave privada, token ni credencial embebida en el repositorio ni en archivos de
  configuración versionados.
- Los valores por defecto "de ejemplo" no pueden servir como credencial válida en producción.
- Rotación: el código no asume una llave única e inmutable (lee identificador o versión).

## Transporte y validación
- TLS obligatorio y con verificación de certificado habilitada.
- Toda entrada externa se valida antes de usarse en consultas, rutas o comandos.
- Los errores hacia el cliente no exponen trazas, consultas ni nombres internos.

## Trazabilidad
- Las operaciones sobre datos de tarjeta dejan registro de quién, cuándo y qué, sin el dato.
- Cambios en reglas de riesgo o de enmascarado se documentan en el ADR del equipo.
