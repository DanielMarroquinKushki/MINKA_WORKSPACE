# Checklist de Revisión de Código — dev-agent

> Usado por pr_review_agent. Clasifica cada hallazgo: BLOQUEANTE / SUGERENCIA / NIT.

## Seguridad y PCI-DSS (BLOQUEANTE si falla)
- [ ] Sin secretos/llaves/tokens hardcodeados (`0123456789ABCDEF`, `BEGIN PRIVATE KEY`).
- [ ] Sin PAN/PIN/CVV en logs o en claro.
- [ ] Sin `InsecureSkipVerify: true`.
- [ ] Llaves manejadas vía KMS/Secrets Manager/HSM, nunca en el repo.

## Correctitud (BLOQUEANTE si falla)
- [ ] Sin `panic()` en código productivo.
- [ ] Todos los errores se retornan y manejan (no `_` silencioso injustificado).
- [ ] Errores envueltos con contexto (`%w`).
- [ ] Idempotencia respetada en handlers SQS/Stream.

## Arquitectura (SUGERENCIA/BLOQUEANTE según impacto)
- [ ] El dominio depende de interfaces, no de implementaciones.
- [ ] Dependencias externas detrás de un Gateway.
- [ ] Wiring en `InitializeXxx`, no disperso.
- [ ] Si una interface tiene varias definiciones, ¿se sincronizaron TODAS?
- [ ] Interfaces nuevas pequeñas (ISP) — no replicar interfaces de ~27 métodos.

## Compatibilidad
- [ ] ¿El cambio usa una API del módulo core compatible con la versión del repo?
      (leer el go.mod de cada repo — cuidado con version drift.)

## Testing
- [ ] Hay tests para el cambio (path feliz + path de error).
- [ ] Mocks actualizados (Mockery) si cambió una interface.

## Convenciones
- [ ] Naming y estructura consistentes con el archivo/paquete existente.
- [ ] Sin código duplicado reutilizable.
- [ ] TODOs nuevos registrados en tech_debt.md.

## Veredicto final
Resumen: N bloqueantes, M sugerencias, K nits. Recomendación al dev (aprobar / cambios requeridos).