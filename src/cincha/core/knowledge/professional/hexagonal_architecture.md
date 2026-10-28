# Arquitectura Hexagonal (Ports & Adapters) aplicada a Lambdas

> Referencia para architecture_agent y development_agent.

## Idea central
El dominio (lógica de negocio) no depende de la infraestructura. Se comunica con el
exterior a través de **puertos** (interfaces). Los **adapters** implementan esos puertos.

```
        [Driving adapters]          [Dominio]            [Driven adapters]
   HTTP / SQS / DynamoStream  →  Puerto de entrada  →  Service  →  Puerto de salida  →  Gateway (KMS/Dynamo/APC/HTTP)
```

## Regla crítica
- El **core/dominio NUNCA importa de adapters**. Los puertos son interfaces; los adapters
  las implementan. La dependencia apunta hacia adentro.

## Aplicado a una Lambda Go de este proyecto
1. **Puerto de entrada**: el handler recibe el evento (HTTP/SQS/Stream) y lo traduce a un
   comando de dominio.
2. **Dominio (service/)**: la lógica pura. Depende de interfaces (`ISenderGateway`,
   `IKmsGateway`, etc.), no de implementaciones.
3. **Puertos de salida (gateways)**: interfaces hacia DynamoDB, KMS, APC, HTTP.
4. **Adapters**: implementaciones concretas de esos gateways (usan el SDK / módulo core compartido).
5. **Composition root**: `InitializeXxx` — ensambla dominio + adapters (DI manual).

## Orden correcto para agregar funcionalidad
1. Define el puerto (interface en el dominio).
2. Implementa el adapter (gateway concreto).
3. Úsalo en el Service inyectándolo por interface.
4. Ensambla en `InitializeXxx`.
5. Expón vía handler/trigger si aplica.

## Beneficios en este contexto
- Testeable: se mockean los gateways (Mockery) sin tocar AWS.
- Intercambiable: cambiar de DynamoDB o de proveedor de HSM no toca el dominio.
- PCI: el manejo de llaves queda aislado detrás de un puerto (`IKmsGateway`).