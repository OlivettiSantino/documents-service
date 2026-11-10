# Análisis para el oral — `documents-service`

> Informe preparado para el examen oral de **Desarrollo de Software** (UTN FRSR).
> Todo lo que se afirma acá tiene evidencia con la forma `ruta/archivo.ext:línea`.
> Lo que no está en el repo se dice explícitamente: **"no está implementado"**.

---

## 0. Nota metodológica (leer antes que nada)

**Estado del checkout analizado.** El working tree está limpio pero **no está en `main`**: la rama activa es `feat/50-imagen-base-parcheada`, con HEAD en `9b416d7` (`build(docker): instalar zlib1g parcheado y sacar pip/setuptools de la imagen`). `origin/main` está **7 commits más adelante** (`07f8892`, merge del PR #53) y agrega el **re-registro en Consul** (issues #51/#53: `is_registered`, `has_service`, `keep_registered`), que toca `app/main.py` (+22/−19), `app/services/consul.py` (+82) y `tests/test_consul.py` (+200). **Este informe analiza el código que está en disco**, es decir *sin* esa funcionalidad; donde importa, lo aclaro.

**Herramientas que se corrieron de verdad, en esta máquina (Windows 11):**

| Comando | Resultado |
|---|---|
| `uv run pytest -q` | **321 passed, 2 failed en 14,18 s** (total 323, el número que declara `README.md:537`). Los dos que fallan son exactamente `test_a_connection_error_becomes_a_fast_503` y `test_a_connection_error_does_not_show_the_destination_to_the_client` de `tests/test_resilience.py:157` y `:167`, el fallo de Windows que el propio README documenta en `README.md:546` (la conexión rechazada tarda ~2 s y el `EXTRACT_CONNECT_TIMEOUT` de 2 s corta primero: sale `504` en lugar de `503`). **No es un bug del servicio.** |
| `uv export --frozen --no-dev … \| pip-audit==2.10.1 --require-hashes` | **`No known vulnerabilities found`** sobre las 28 dependencias de producción. Es el mismo comando del job `audit` (`.github/workflows/ci.yml:82-83`). |
| `uv tree --no-dev` | **37 paquetes resueltos**; 6 directos, 28 en el export de producción. |
| `uvx ruff check .` | **`All checks passed!`** |
| `docker compose --env-file /dev/null config --quiet` y la variante `-f docker-compose.yml` | **rc=0** las dos (es el job `compose`, `.github/workflows/ci.yml:69-70`; `config` no necesita el daemon). |
| `docker build .` | **No se pudo correr: el daemon de Docker no está levantado** en esta máquina (`failed to connect to … dockerDesktopLinuxEngine`). Por lo mismo **no pude correr Grype**. Así se correría, y está documentado en `README.md:418-443`: `docker compose build documents && grype documents-service:dev --only-fixed --fail-on high`, o la variante sin instalar nada con `anchore/grype:v0.121.2` sobre un `docker save`. |

---

## 1. Resumen ejecutivo

`documents-service` es el microservicio de **persistencia de documentos PDF ya procesados** del sistema `pdf_extractext`: guarda, consulta y borra documentos, deduplica por SHA-256 y **orquesta** la extracción de texto delegándola en `extract-service` (`README.md:3`). Tiene su propia base MongoDB (database per service), su propio repositorio, su propia imagen en GHCR y su propio ciclo de release. Convive con otros dos repos: `extract-service` (extracción) y `pdf_extractext-infra` (Consul + Traefik).

**Fortalezas.** La arquitectura hexagonal es real, no decorativa: la capa de servicios depende solo de `Protocol` (`app/ports.py:13-53`), y eso permite que **las 323 pruebas corran sin MongoDB, sin extract y sin red** (incluidas las de resiliencia). El mapeo de errores a HTTP está centralizado en un único handler (`app/main.py:171-189`) con mensajes fijos y el detalle solo al log, lo que resuelve de una vez "logs sin datos sensibles" y "fallo seguro". El control de admisión de uploads está puesto **antes** de leer el body (`app/api/documents.py:117-129`), que es el único lugar donde sirve. La cadena de suministro está muy por encima de lo habitual en un TP: lockfile con hashes, `pip-audit --require-hashes`, Grype **antes** del push, imagen base y Mongo fijadas por digest, y las GitHub Actions clavadas por SHA. El 12-Factor está cumplido en 9 de 12 factores de forma defendible. Y hay algo poco común: **borraron código**. El circuit breaker, el bulkhead, el retry y el discovery propios se eliminaron en la issue #28 porque duplicaban a Traefik y porque el breaker propio tenía bugs verificados (`README.md:481-486`); ese razonamiento, bien contado, vale más en un oral que tener los tres patrones escritos a mano.

**Debilidades.** No hay **autenticación ni autorización** en ningún endpoint: cualquiera que alcance el gateway puede hacer `DELETE /documents/{id}` (OWASP API1/API5). No hay **TLS** en ningún tramo. No hay **caché** de ningún tipo, ni **rate limit** para `documents` (los middlewares de Traefik existen solo para `extract`: `app/services/consul.py:75-76` y el test que lo fija como invariante, `tests/test_consul.py:421`). **SAGA no se menciona en ningún lado** del repo, ni para decir que no aplica. El **contrato con extract se verifica contra un fixture copiado a mano** (`tests/contracts/extract_response.json`) y nada detecta la deriva si extract cambia su schema. Falta **Dependabot/Renovate**, **SBOM** y **firma de imagen**. Y hay una incoherencia concreta y fácil de señalar: el compose fija Mongo por tag+digest y hay un test que lo exige (`tests/test_compose.py:88`), pero el job de integración de la CI usa `mongo:7.0` pelado (`.github/workflows/ci.yml:42`).

---

## 2. Tabla resumen de cumplimiento

| # | Tema | Estado | Justificación en una línea |
|---|---|---|---|
| 1 | Microservicios: ventajas y desventajas vs. monolito | ✅ | Capacidad de negocio única, base propia, imagen y despliegue propios, contrato HTTP explícito; la granularidad es defendible y las desventajas están asumidas por escrito. |
| 2 | Comunicación entre microservicios | ✅ | HTTP/REST síncrono con contrato documentado, OpenAPI, tests de contrato, discovery por Consul+Traefik, `X-Request-ID` propagado y presupuesto de timeouts explícito; no hay mensajería asíncrona (decisión, no olvido). |
| 3 | Refactorización de una aplicación monolítica | 🟡 | La separación está consumada (repo, base e imagen propios, validador que rechaza `pdf_extract_db`), pero **el proceso** no deja rastro en este repo: `git log` arranca en un `chore: inicializa el repositorio` sin historia importada del monolito. |
| 4 | Proxy inverso | 🟡 | El rol lo cumple Traefik y este repo hace su parte completa (tags de Consul, `EXTRACT_URL=http://traefik`, traducción de `502/504/404/429`), pero la configuración del proxy vive en otro repositorio. |
| 5 | 12 Factor App | ✅ | 9 factores plenos, 3 parciales (IX disposability sin `stop_grace_period`, X paridad con grietas, XII sin procesos de administración). |
| 6 | Réplicas de contenedores | ✅ | `deploy.replicas` por variable, sin puerto publicado, stateless, un registro y un check por réplica, dedupe garantizado por índice único y probado con Mongo real; **pero no hay pruebas de carga en este repo** (a propósito). |
| 7 | Patrones de resiliencia | 🟡 | Timeout, load shedding e idempotencia: ✅. Retry y Circuit Breaker: delegados a Traefik (coherente y documentado), pero nada protege la llamada a Mongo. Rate Limit: ❌ para documents. Cache: ❌. SAGA: ❌ ni implementado ni explicado. |
| 8 | Desarrollo seguro | 🟡 | Contenedor no root, mínimo privilegio, fallo seguro, validación de entradas y manejo de secretos muy sólidos; cadena de suministro excelente (`pip-audit` + Grype + digests + actions por SHA, hoy **0 vulnerabilidades**); pero **sin authn/authz, sin TLS, sin SBOM, sin firma y sin Dependabot**. |

---

## 3. Tema 1 — Microservicios: ventajas y desventajas respecto al monolito

### 3.1 Teoría breve

Un microservicio es un servicio **pequeño, autónomo y desplegable de forma independiente**, organizado alrededor de una **capacidad de negocio**. Las características que la cátedra pide verificar:

- **Capacidad de negocio concreta** y **responsabilidad única**: hace una cosa del dominio.
- **Caja negra**: por fuera solo se ve su interfaz; nadie entra a su base ni a su código.
- **Endpoints como contrato**: la API es el único acoplamiento permitido.
- **Despliegue independiente**: se puede releasear sin coordinar con los demás.
- **Datos propios** (*database per service*): nadie más escribe su base.
- **Stack propio**: elige su lenguaje, framework y dependencias.

La **granularidad** es la decisión difícil: demasiado grande y es un mini-monolito; demasiado chico y aparece el "nano-servicio", donde el costo de red y operación supera el beneficio.

### 3.2 Cómo se ve en el repo

| Característica | Evidencia | Veredicto |
|---|---|---|
| Capacidad de negocio concreta | `README.md:3` ("persistencia de documentos PDF ya procesados") y `README.md:9-11`, que además dice qué **no** hace: no extrae texto, no autentica, no resume. | ✅ |
| Responsabilidad única | `DocumentService` solo valida, deduplica, delega y persiste (`app/services/document_service.py:38-90`). La extracción es un puerto (`app/ports.py:43-53`), nunca código propio. | ✅ |
| Caja negra | Mongo **no publica puerto** y queda solo en la red privada del stack (`docker-compose.yml:26-28`, `docker-compose.yml:127-128`). `documents` tampoco publica puerto: usa `expose`, no `ports` (`docker-compose.yml:54-58`). Desde afuera se entra únicamente por Traefik. | ✅ |
| Endpoints como contrato | Tabla de contrato completa en `README.md:48-56`; OpenAPI autogenerado más el `requestBody` declarado a mano para el upload (`app/api/documents.py:70-91`); un test fija los códigos documentados (`tests/test_api_documents.py:244-247`). | ✅ |
| Despliegue independiente | Imagen propia construida una sola vez por commit y publicada en GHCR (`.github/workflows/ci.yml:85-212`); el compose base corre esa imagen por tag (`docker-compose.yml:53`) y el `build` vive solo en el override de desarrollo (`docker-compose.override.yml:6-14`). El compose **no levanta `extract-service`** a propósito (`docker-compose.yml:1-4`, `README.md:212-213`). | ✅ |
| Datos propios | Base `documents_db` propia (`app/config.py:40`), y un validador que **se niega a arrancar** contra la base del monolito (`app/config.py:17-20` y `:84-93`), probado en `tests/test_config.py:94-97`. Sin motor de migraciones: `create_index` idempotente en el lifespan (`app/db/mongo.py:33-43`). | ✅ |
| Stack propio | Python 3.11 + FastAPI + Motor + httpx, declarado en `pyproject.toml:6-13`, aislado en su propio venv dentro de la imagen (`Dockerfile:17-18`, `Dockerfile:58`). | ✅ |
| Granularidad | Un servicio con 4 endpoints de negocio + 2 de health, ~1.100 líneas en `app/`, una colección, un colaborador externo. No es un CRUD anémico (orquesta, deduplica, hace control de admisión y traduce fallas) ni un mini-monolito. | ✅ defendible |

### 3.3 Ventajas, una por una, en **este** sistema

**Independencia.** Release separado y verificable: `DOCUMENTS_IMAGE_TAG=sha-xxxxxxx docker compose -f docker-compose.yml up -d --wait` levanta una versión exacta sin compilar (`README.md:366-374`), y `docker inspect` del label `org.opencontainers.image.revision` dice qué commit está corriendo. Volver atrás es cambiar un tag. Un cambio en `documents` no obliga a releasear `extract`.

**Equipos autónomos.** Tres repos, tres dueños. El contrato entre repos son tres cosas y nada más: el nombre de la red Docker compartida (`SHARED_NETWORK=mired`, `.env.example:28-31`, mismo nombre en los tres repos), los tres nombres de variables de Consul (`app/config.py:69-71`, con un comentario explícito "no renombrarlos") y el schema de `POST /extract` (`tests/contracts/extract_response.json`). El README incluso cita las issues del repo vecino cuando apoya una decisión (`README.md:492`, extract-service#55).

**Versatilidad tecnológica.** Nada en este repo obliga a que extract sea Python: el acoplamiento es `POST /extract` con cuerpo `application/pdf` y respuesta JSON (`app/services/extractor_client.py:86`, `:95-100`). Extract podría reescribirse en Go sin tocar una línea acá.

**Aislamiento y resiliencia.** Es el punto más trabajado del repo:
- Con **extract caído**, los `GET` siguen funcionando y los `POST` responden `503` con `Retry-After` — probado en `tests/test_resilience.py:83-104`.
- Con **Consul caído**, el servicio arranca igual: el registro corre en una tarea de fondo y ningún fallo se propaga (`app/main.py:120-126`, `app/services/consul.py:158-177`); probado en `tests/test_consul.py:307` y `:364`.
- Con **Mongo caído**, `/health` sigue en `200` (no toca nada) y `/health/ready` da `503` (`app/api/health.py:23-40`); los endpoints de `/documents` dan `503` + `Retry-After` en ~3 s en lugar de colgarse 30 s (`app/db/mongo.py:26-30`, `app/main.py:176-189`), probado en `tests/test_database_unavailable.py:84` y `:127`.
- Un **PDF ya conocido se sirve aunque el gateway esté rechazando extracciones**, porque el dedupe va antes de la llamada (`app/services/document_service.py:51-57`), probado en `tests/test_resilience.py:118`.

**Escalabilidad.** Escala horizontal real por réplicas (`docker-compose.yml:108-117`), no por workers, con la justificación escrita en `app/__main__.py:6-14`. Ver tema 6.

### 3.4 Desventajas, una por una, en **este** sistema

**Dificultad de descomponer.** Quedó una costura visible: el servicio tiene que **saber que existe otra base que no debe tocar** y defenderse por código (`app/config.py:17-20`). Eso es la cicatriz de una descomposición donde el monolito sigue vivo y sigue siendo dueño de `pdf_extract_db`, corriendo sus propias migraciones en cada arranque.

**Complejidad de los sistemas distribuidos.** El repo la paga en cosas que en un monolito no existirían: un cliente HTTP con dos timeouts, un parser de respuestas ajenas con 8 ramas de error (`app/services/extractor_client.py:111-155`), un registro en service discovery con backoff, un middleware de correlación escrito en ASGI puro porque `BaseHTTPMiddleware` rompía el `ContextVar` (`app/middleware.py:1-14`) y una carrera de duplicados entre réplicas que solo el índice único resuelve (`app/services/document_service.py:72-79`).

**Cuándo adoptarlos.** El README lo justifica con números, no con moda (`README.md:15`): bajo la carga de la cátedra (Vegeta, 50 req/s × 30 s) el éxito de extract es 78,24 % y, en el camino real `POST /documents`, el que corta es **este** servicio con 596 `201` y 904 `503` de su propio control de admisión. Es decir: la separación se adoptó para poder medir y proteger el cuello de botella por separado. Es la respuesta correcta a "¿por qué microservicios acá?".

**Consistencia.** Es **eventual y asumida**. El caso concreto está escrito como límite conocido en `README.md:572`: dos subidas simultáneas del mismo PDF pasan las dos el chequeo de checksum, **las dos llaman a extract**, y recién el índice único decide. El resultado final es correcto (un `201`, el resto `200 duplicate` con el mismo `id`) pero se paga una extracción de más. Está probado contra Mongo real, con 20 inserts concurrentes desde dos clientes y con dos `DocumentService` extrayendo a la vez (`tests/integration/test_mongo_repository.py:108` y `:150`).

**Observación (observabilidad).** Parcial. Hay logs estructurados a stdout con `request_id` en cada línea (`app/logging_config.py:50-64`) y el mismo id atraviesa Traefik, documents y extract (`README.md:347-350`). **No hay métricas, ni tracing distribuido, ni alertas**: no está implementado. Curiosidad para el oral: `opentelemetry-api` ya está en el árbol de dependencias, pero entra como transitiva de FastAPI, no porque el servicio instrumente nada.

**Seguridad.** Es la debilidad grande: **cero autenticación y autorización** (ver tema 8), y la superficie interna confía en la red. Mitigado en parte porque nada publica puertos al host y solo `/documents` está ruteado por el gateway.

**Complejidad operacional.** Visible y honesta: hay que crear una red externa a mano (`docker network create mired`), levantar tres stacks en orden, y hay un token de ACL de Consul que si falta hace que Traefik nunca rutee **aunque Docker muestre el contenedor `healthy`** (`README.md:241`). El README dedica ~60 líneas solo a eso (`README.md:237-307`), lo cual es a la vez una buena señal (está documentado) y la medida exacta del costo operativo.

**Latencia.** El presupuesto está escrito y es consistente: cliente 30 s > documents 27 s > Traefik 25 s ≥ extract 23 s (`app/config.py:49-53`). Y hay un salto de red extra asumido a propósito: documents llama a extract **a través de Traefik** (`docker-compose.yml:73`), lo que agrega un hop para ganar balanceo, retry y circuit breaker centralizados.

### 3.5 Grado de cumplimiento

**✅ Cumple.** Cumple las siete características de un microservicio con evidencia directa, la granularidad es defendible, y las desventajas no están ignoradas sino asumidas por escrito (`README.md:567-578`, "Límites conocidos").

### 3.6 Lo que falta o se podría mejorar

- Observabilidad: no hay métricas ni tracing.
- No hay authn/authz (tema 8).
- El repo no tiene mediciones propias que respalden los números del `README.md:15`.

---

## 4. Tema 2 — Comunicación entre microservicios

### 4.1 Teoría breve

**Síncrona** (request/response: HTTP/REST, gRPC): simple de razonar, pero acopla temporalmente — si el otro no está, yo no puedo responder. **Asíncrona** (mensajería, eventos): desacopla en el tiempo y absorbe picos, al costo de consistencia eventual y de infraestructura extra (broker). El **contrato** es la única dependencia legítima, y hay que verificarlo (tests de contrato, idealmente *consumer-driven*). El **descubrimiento** evita direcciones hardcodeadas (registro/catálogo tipo Consul, o DNS). Los **IDs de correlación** son lo que hace posible seguir un pedido que atraviesa N servicios. Y **timeouts y errores** hay que presupuestarlos en cascada, con el de adentro siempre menor que el de afuera.

### 4.2 Cómo se ve en el repo

**Síncrona, HTTP, un solo intento.** Toda la comunicación saliente es un `POST` HTTP por extracción (`app/services/extractor_client.py:88-109`). **No hay mensajería asíncrona**: no está implementado, y es una decisión explícita (el PDF se necesita extraído *para poder responder* el `201`, así que un evento no serviría sin cambiar el contrato a `202 Accepted` + polling).

**Protocolos y formatos.** Hacia afuera, `multipart/form-data` en la entrada y JSON en la salida (`README.md:48-56`). Hacia extract, el PDF viaja como **cuerpo binario** `application/pdf` (`app/services/extractor_client.py:42`, `:95`) y la respuesta es JSON — el camino que usa la cátedra (`README.md:72`). Hacia Consul, JSON sobre la API del agente (`app/services/consul.py:123-136`).

**Contratos y su verificación.** Hay un directorio de tests de contrato dedicado: `tests/contracts/`. El fixture `tests/contracts/extract_response.json` espeja `ExtractResponse` de extract-service, y `tests/contracts/test_extract_contract.py` hace tres cosas bien:
1. Valida **el fixture contra el schema** (`:37-41`), para que no derive.
2. Verifica que las variantes legales se aceptan: `page_count: 0`, `content: ""`, campo desconocido ignorado (`:51-62`).
3. Verifica que **9 variantes inválidas se rechazan** con `ExtractProtocolError`: falta un campo, `content: null`, `page_count` como string / float / bool, negativo, y la respuesta que no es un objeto (`:69-85`).

Eso último es notable: `page_count: True` se rechaza porque en Python `bool` es subclase de `int`, y el parser lo descarta explícitamente (`app/services/extractor_client.py:169`). Es exactamente lo que OWASP API10 ("consumo inseguro de APIs") pide.

**Limitación honesta:** el fixture está **copiado a mano**. No es un contrato *consumer-driven* verificado contra el proveedor (no hay Pact, ni se baja el `openapi.json` de extract en la CI). Si extract cambia su schema y nadie actualiza el fixture, los tests siguen verdes y la ruptura aparece en producción.

**Descubrimiento de servicios.** Dos mitades:
- *Hacia adentro*: documents **se registra** en Consul con su hostname como `ID`, su IP de la red compartida, y un check HTTP a `/health` (`app/services/consul.py:40-67`, `:221-250`). En los tags declara cómo quiere ser publicado por Traefik (`app/services/consul.py:70-84`).
- *Hacia afuera*: documents **ya no descubre nada**. Desde la issue #27/#28 llama siempre a `EXTRACT_URL`, que en el compose es el gateway (`docker-compose.yml:73`), y es Traefik quien lee el catálogo y balancea. El `discovery.py` propio se borró (commit `8635506`), y hay un test que fija el invariante: con Consul habilitado o deshabilitado, el extractor llama siempre a `EXTRACT_URL` (`tests/test_consul.py:493`).

La elección de IP merece una mención: el contenedor está en dos redes, y en vez de confiar en `gethostbyname` se le pregunta al sistema **por qué interfaz saldría un paquete hacia Consul**, con un `connect` UDP que no manda nada (`app/services/consul.py:87-101`). Probado en `tests/test_consul.py:92` y `:78` (el check no puede apuntar a `localhost`, que desde Consul sería el propio contenedor de Consul).

**Propagación de IDs de correlación.** Implementada de punta a punta:
- `RequestIdMiddleware` (`app/middleware.py:47-81`) lee o genera el id, lo pone en un `ContextVar` **en la misma task que corre la app** y lo devuelve en el header de respuesta.
- Está escrito en ASGI puro por una razón concreta documentada en `app/middleware.py:1-14`: con `BaseHTTPMiddleware` el `ContextVar` no se ve de forma confiable desde el endpoint, y el `request_id` saldría como `-` en todos los logs de la capa de servicios.
- El id se **valida**: solo se acepta de afuera si cumple `[A-Za-z0-9-]{1,64}` con `fullmatch` sobre los bytes crudos (`app/middleware.py:29`, `:35-39`). Si no, se genera un `uuid4().hex` y se loguea el reemplazo con a lo sumo 64 caracteres escapados (`app/middleware.py:42-44`, `:61-69`). El comentario explica el ataque que evita: inyección de líneas de log o de headers.
- Viaja a extract en `X-Request-ID` (`app/services/extractor_client.py:96-97`), probado en `tests/test_correlation.py:63` y `:91`.
- 13 tests en `tests/test_correlation.py`, incluyendo que un id terminado en `\n` **no** se acepta (`:136`), lo que justifica el `fullmatch` en lugar de `$`.

**Timeouts y errores entre servicios.** El presupuesto está escrito en tres lugares coherentes (`app/config.py:49-53`, `.env.example:58`, `README.md:492`): **cliente 30 s > documents 27 s > Traefik 25 s ≥ extract 23 s**, con `EXTRACT_CONNECT_TIMEOUT=2.0` para fallar rápido cuando extract no está. Hay un test que verifica el presupuesto como invariante de configuración (`tests/test_config.py:30`). El `httpx.Timeout` se arma en el composition root (`app/main.py:85-92`), con `max_connections` igual a `MAX_CONCURRENT_UPLOADS` porque cada upload admitido hace a lo sumo una llamada.

El mapeo de errores es la pieza más pulida del repo (`app/services/extractor_client.py:111-155`, tabla completa en `README.md:92-105`):

| Respuesta de extract o del gateway | Error de dominio | HTTP al cliente |
|---|---|---|
| `400` / `422` de extract | `UnprocessablePdfError` | `422` — es el archivo del cliente, no una falla de extract |
| `413` | `PayloadTooLargeError` | `413` |
| `429` (`inFlightReq` de Traefik) / `503` | `ExtractUnavailableError` + `Retry-After` si viene | `503` |
| `502` / `404` del gateway | `ExtractUnavailableError` | `503` |
| `504` o timeout de httpx | `ExtractTimeoutError` | `504` |
| Cualquier otro código, JSON inválido o campos faltantes | `ExtractProtocolError` | `502` |

Dos detalles que valen en un oral: el `Retry-After` se parsea **en sus dos formas legales de RFC 9110** (delta-seconds y HTTP-date) con `parse_retry_after` (`app/services/extractor_client.py:48-72`); y el `400` → `422` es un cambio de contrato deliberado y documentado (`README.md:81-86`), con el argumento de que un PDF corrupto no debe abrir el circuit breaker de Traefik, que solo cuenta errores de red y `500`–`502` (`README.md:107`).

### 4.3 Grado de cumplimiento

**✅ Cumple.** Protocolos y formatos explícitos, contrato documentado y testeado con 13 casos, discovery resuelto, correlación propagada **y validada**, y un presupuesto de timeouts coherente en las tres capas. Lo único que baja la nota es que el test de contrato no es *consumer-driven* verificado contra el proveedor.

### 4.4 Lo que falta o se podría mejorar

- El fixture de contrato puede derivar sin que nadie se entere (no se baja el schema de extract en la CI).
- No hay comunicación asíncrona ni outbox; no hace falta para este flujo, pero no está dicho en el README.
- No hay tracing distribuido: el `request_id` permite correlacionar logs a mano, no ver un span tree.

---

## 5. Tema 3 — Refactorización de una aplicación monolítica

### 5.1 Teoría breve

Refactorizar un monolito es extraer capacidades **sin apagar el sistema**. El patrón canónico es **Strangler Fig**: se pone un proxy delante del monolito y se van desviando rutas al servicio nuevo, hasta que el monolito queda "estrangulado". Las piezas:

- **Extracción por capacidad de negocio** (no por capa técnica: no "extraer la capa de datos").
- **Separación de la base de datos**: es la parte difícil. Mientras dos servicios comparten tablas no hay microservicios, hay despliegues separados de un monolito distribuido.
- **Convivencia**: durante la migración, viejo y nuevo coexisten; hace falta decidir quién es dueño de cada dato.

### 5.2 Cómo se ve en el repo

**Lo que está, y es sólido:**

| Pieza | Evidencia |
|---|---|
| Extracción por capacidad de negocio | Dos capacidades, dos servicios: "extraer texto" (extract-service) y "persistir documentos" (este). `README.md:9-11` lo dice en positivo y en negativo. |
| Separación de la base, **defendida por código** | `FORBIDDEN_DB_NAMES = frozenset({"pdf_extract_db"})` (`app/config.py:20`) y el validador que lanza `ValueError` al arrancar (`app/config.py:84-93`). El comentario explica *por qué*: el monolito corre sus propias migraciones en cada arranque y es dueño de un índice único ahí, así que tocar esa base rompería "database per service" en las dos direcciones (`app/config.py:17-19`). Probado: `tests/test_config.py:94-97`. Reforzado en `.env.example:20-21` ("No apuntar nunca a pdf_extract_db: el servicio se niega a arrancar"). |
| Base propia y privada | `documents_db` (`app/config.py:40`), Mongo sin puerto publicado y solo en la red del proyecto (`docker-compose.yml:26-28`, `:127-128`). |
| Sin motor de migraciones (decisión consciente) | `app/db/mongo.py:33-43` + `README.md:564`: "Copiar el motor de migraciones del repo hermano traería un lock distribuido que acá no hace falta — y dos servicios migrando la misma base competirían por él". Es exactamente el argumento de database-per-service. |
| El proxy que haría de Strangler Fig | Traefik, en `pdf_extractext-infra`. Este repo se publica en `/documents` con los tags de Consul (`app/services/consul.py:78-84`). El ruteo por prefijo de path es la mecánica con la que se estrangula un monolito. |
| Estructura espejada del repo hermano | `README.md:563`: "espejan el layering de `extract-service`". |

**Lo que no está:**

- **No hay rastro del monolito en el historial de git de este repo.** `git log --reverse` arranca el 2026-10-07 con `cfa0751 chore: inicializa el repositorio`, seguido de `92d97da feat: esqueleto del servicio, configuracion 12-Factor y /health`. **No hay historia importada** (ni `git filter-repo`, ni subtree, ni commits del monolito). Son 101 commits, todos de este servicio, construido **de cero** sobre un esqueleto especificado por la cátedra (`README.md:563`).
- **La palabra "Strangler" no aparece en ningún archivo del repo.** No está implementado ni documentado como tal.
- **No hay evidencia en este repo de qué código salió del monolito**: no hay un diff, un mapa de módulos viejos → nuevos, ni un commit "port X from pdf_extractext". Lo único que acredita que el monolito existe y sigue vivo es la constante prohibida de `app/config.py:20`.
- **La migración de datos no está** (ni script, ni backfill, ni doble escritura): no está implementado.

**Lo que sí se puede contar como refactorización, y está muy bien documentado**, es una refactorización **interna posterior**: la issue #28, donde se borró el circuit breaker, el bulkhead, el retry y el discovery propios. Esa sí deja rastro quirúrgico en el historial — ocho commits consecutivos, uno por pieza borrada:

```
1b2134c refactor(health): sacar extract_circuit de /health/ready
c3c5bd3 refactor(extractor): borrar el circuit breaker propio
e048b56 refactor(extractor): borrar el bulkhead y el reintento
f74dd4d refactor(exceptions): borrar CircuitOpenError y BulkheadFullError
8635506 refactor(discovery): borrar discovery.py y ExtractEndpointPort
2848fa5 refactor(consul): borrar ConsulAgent.passing_instances
15e0846 refactor(clock): dejar solo now()
57f5093 refactor(config): borrar las variables del breaker, el bulkhead y CONSUL_CACHE_TTL
```

Y con compatibilidad hacia atrás pensada: `extra="ignore"` en `Settings` para que un `.env` viejo con las variables borradas siga arrancando (`app/config.py:26-28`), probado en `tests/test_config.py:49`.

### 5.3 Grado de cumplimiento

**🟡 Cumple parcialmente.** El **resultado** de la descomposición es correcto y está defendido por código (base propia, validador anti-monolito, despliegue e imagen independientes). Lo que falta es el **proceso**: este repo no tiene evidencia de haber sido extraído del monolito — fue escrito de cero — y ni el Strangler Fig ni la migración de datos aparecen en ningún archivo.

### 5.4 Lo que falta o se podría mejorar

- No hay sección en el README que cuente qué se extrajo del monolito, qué quedó en él y qué falta migrar.
- No hay mapa de propiedad de datos entre `pdf_extract_db` (monolito) y `documents_db`.
- Si el monolito sigue guardando documentos, no está dicho si hay doble escritura, backfill o nada.

---

## 6. Tema 4 — Proxy inverso

### 6.1 Teoría breve

Un **proxy inverso** se pone delante de uno o más servidores y recibe el tráfico de los clientes en su nombre: rutea por host/path, **balancea carga**, hace **health checks** para sacar backends no sanos de la rotación, termina TLS y aplica **middlewares** (reescritura de headers, compresión, rate limit, circuit breaker). El cliente solo ve el proxy.

Un **API Gateway** es un proxy inverso **más** preocupaciones de API: autenticación y autorización centralizadas, rate limiting por consumidor, versionado, agregación de respuestas, transformación de payloads, métricas por API. La diferencia no es el software — Traefik puede ser las dos cosas — sino cuántas de esas responsabilidades se le delegan. Dicho en una línea: *el proxy inverso mueve bytes; el gateway gobierna la API*.

### 6.2 Cómo se ve en el repo

**Quién cumple el rol.** **Traefik**, que vive en `pdf_extractext-infra` (`README.md:33`). Este repositorio **no contiene la configuración de Traefik** — eso es importante decirlo en el oral, porque es la parte que no se puede mostrar acá. Lo que sí contiene es **la mitad del contrato que le toca a documents**.

**Cómo se configura el ruteo.** No por archivo, sino por **service discovery**: Traefik corre con el provider `consulcatalog` y `exposedByDefault=false`, así que lee los **tags** que cada servicio declara al registrarse. Los de documents están en `traefik_tags()` (`app/services/consul.py:70-84`):

```python
"traefik.enable=true",
f"{router}.rule=PathPrefix(`/{SERVICE_NAME}`)",
f"{router}.entrypoints=web",
```

Tres tags, tres decisiones: habilitar la publicación (sin `traefik.enable=true` no se publica nada), rutear por prefijo de path `/documents`, y solo el entrypoint de tráfico (el otro es el del dashboard). Están probados en `tests/test_consul.py:412-467`: que los tags publican el servicio, que viajan en el `PUT` al agente, y que el payload del registro los lleva.

**Balanceo de carga.** Lo hace Traefik (round robin) entre las instancias `passing` del catálogo (`README.md:472`). Lo que este repo aporta para que funcione: cada réplica se registra con un `service_id` **único** — su hostname (`app/services/consul.py:236`) — probado en `tests/test_consul.py:229` ("dos réplicas se registran con ids distintos"). Y ninguna réplica publica puerto en el host, para que N contenedores puedan arrancar a la vez (`docker-compose.yml:54-58`).

**Health checks.** Dos niveles, y la distinción está bien pensada:
- El **check de Consul** es HTTP a `http://<IP>:8002/health`, `Interval 10s`, `Timeout 2s`, con `DeregisterCriticalServiceAfter: 1m` (`app/services/consul.py:28-32`, `:58-66`). Es **liveness, no readiness**, y el comentario explica por qué (`app/services/consul.py:8-10`): `/health/ready` depende de Mongo, y un blip de la base nos sacaría del catálogo cuando todavía servimos lecturas correctamente. Probado en `tests/test_consul.py:68` y `:84`.
- El **HEALTHCHECK de Docker** apunta también a `/health`, nunca a `/health/ready` (`Dockerfile:64-69`), con el mismo razonamiento escrito en inglés en el propio Dockerfile: atar el healthcheck del contenedor a la readiness permitiría que un parpadeo de Mongo reiniciara un proceso perfectamente sano.

**Middlewares.** Acá está la decisión más fuerte del tema: **documents no declara ningún middleware en Traefik, a propósito**, y hay un test que lo fija como invariante — `tests/test_consul.py:421`, `test_documents_no_declara_middlewares_en_traefik`. Los middlewares existen solo para extract (`extract-cb` circuit breaker, `extract-retry`, `extract-inflight` inFlightReq), declarados en **los tags de extract**, no acá (`README.md:488-493`). Y documents **traduce** las respuestas que esos middlewares generan (`app/services/extractor_client.py:133-153`): `504` del `responseHeaderTimeout`, `502` cuando no pudo hablar con la réplica, `404` cuando no hay ruta porque extract no está en Consul, `429` del `inFlightReq`.

**Diferencia con un API Gateway.** En este sistema Traefik se usa **como proxy inverso, no como API Gateway completo**:

| Responsabilidad de gateway | ¿Está? |
|---|---|
| Ruteo por path | ✅ `PathPrefix(/documents)` |
| Balanceo + health checks | ✅ vía catálogo de Consul |
| Circuit breaker / retry / límite de concurrencia | ✅ pero **solo para extract** |
| Terminación TLS | ❌ no está implementado (todo HTTP) |
| Autenticación / autorización centralizada | ❌ no está implementado |
| Rate limit por consumidor | ❌ no está implementado para documents |
| Versionado de API, agregación, transformación | ❌ no está implementado |
| Métricas por API | ❌ no está implementado |

Detalle fino que conviene saber: `/health`, `/health/ready` y `/docs` **no están ruteados** por Traefik, y el README argumenta que es correcto (`README.md:166`): con N réplicas, un health a través del balanceador contesta una cualquiera y no dice nada de una instancia en particular. Para mirar una réplica concreta hay recetas con `docker compose exec --index` (`README.md:168-185`). Eso además **reduce la superficie expuesta**: el `/docs` con el esquema OpenAPI no se alcanza desde afuera.

### 6.3 Grado de cumplimiento

**🟡 Cumple parcialmente.** El rol de proxy inverso está cubierto y **la mitad que le toca a este repo está completa y testeada** (tags, registro con IP correcta, check de liveness, traducción de las respuestas del gateway). Es parcial porque la configuración del proxy vive en otro repositorio y porque de las responsabilidades de API Gateway solo están las de resiliencia, y únicamente para extract.

### 6.4 Lo que falta o se podría mejorar

- Sin TLS en el entrypoint.
- Sin rate limit ni autenticación en el gateway para `/documents`.
- Los tags no declaran un health check de Traefik propio ni `sticky sessions` (no hacen falta: el servicio es stateless).

---

## 7. Tema 5 — 12 Factor App (los doce, uno por uno)

| # | Factor | Qué pide | ¿Cumple? | Evidencia | Qué falta |
|---|---|---|---|---|---|
| **I** | **Codebase** | Un repo versionado, muchos despliegues | ✅ | Un repositorio git, 101 commits, ramas por issue y merges por PR (`git log`); un tag `vX.Y.Z` dispara la publicación de la imagen con esa versión (`.github/workflows/ci.yml:6-7`, `:130-134`) | — |
| **II** | **Dependencies** | Declaradas y aisladas explícitamente | ✅ | Declaradas en `pyproject.toml:6-13` (prod) y `:15-20` (dev); fijadas en `uv.lock` **con hashes**; instaladas con `uv sync --frozen --no-dev` en una etapa de build separada (`Dockerfile:17-18`) y copiadas como venv a la etapa final (`Dockerfile:58`). La imagen final **no tiene pip ni setuptools** (`Dockerfile:49-53`): no puede instalar nada en runtime | — |
| **III** | **Config** | En el entorno, nunca en el código | ✅ | `app/config.py` entero; `model_config` **sin `env_file`** a propósito (`app/config.py:28` y el docstring `:3-5`): el proceso nunca depende de un archivo en disco, en desarrollo se usa `uv run --env-file .env`. `.env.example` versionado con el token **vacío** (`.env.example:45`). Dos tests: `tests/test_config.py:9` (defaults usables) y `:18` (todo viene del entorno) | — |
| **IV** | **Backing services** | Recursos adjuntos, intercambiables por config | ✅ | Mongo por `MONGO_URI`, extract por `EXTRACT_URL`, Consul por `CONSUL_HTTP_ADDR` (`app/config.py:39`, `:48`, `:75`). Que son *adjuntos* se demuestra con las **cuatro** formas documentadas de apuntar a extract sin tocar código (`README.md:212-236`): por Traefik, directo en `mired`, en el host, o en otra máquina. Y el rollback de gateway es una variable: `EXTRACT_URL=http://extract:8001 docker compose up -d` (`README.md:338`) | — |
| **V** | **Build, release, run** | Etapas separadas y estrictas | ✅ | Job `image` que **construye una sola vez por commit** y publica en GHCR (`.github/workflows/ci.yml:85-212`), solo si pasaron `tests`, `integration`, `compose` y `audit` (`:90`). El compose base **no tiene `build`**, solo `image:` con tag (`docker-compose.yml:53`); el `build` vive únicamente en el override de desarrollo (`docker-compose.override.yml:6-14`). Release = imagen con tag + `.env` (`README.md:355`). Cuatro tests lo fijan: `tests/test_compose.py:103-119` | — |
| **VI** | **Processes** | Stateless, share-nothing | ✅ | Todo el estado está en Mongo. Lo único en memoria del proceso es el semáforo de admisión (`app/main.py:103-105`), que es **deliberadamente por réplica** y está documentado como tal (`README.md:476`). El único volumen es el de Mongo (`docker-compose.yml:124-125`). Desde la issue #28 no queda ningún estado compartido entre réplicas: se borraron breaker, bulkhead y cache de discovery (`README.md:468`) | — |
| **VII** | **Port binding** | Autocontenido, expone un puerto | ✅ | `HOST`/`PORT` por entorno (`app/config.py:33-34`), uvicorn embebido (`app/__main__.py:27-33`), `EXPOSE 8002` + `ENV PORT=8002` (`Dockerfile:26`, `:62`), `expose` y no `ports` en el compose (`docker-compose.yml:57-58`) | — |
| **VIII** | **Concurrency** | Escalar por procesos | ✅ | `replicas: ${DOCUMENTS_REPLICAS:-1}` (`docker-compose.yml:117`), **un proceso por contenedor sin `workers`**, con la justificación escrita en `app/__main__.py:6-14`. Dos tests: `tests/test_compose.py:69` y `:75` | — |
| **IX** | **Disposability** | Arranque rápido y apagado *graceful* | 🟡 | A favor: el lifespan cierra los tres clientes y **desregistra de Consul** en el `finally` (`app/main.py:143-161`); `DeregisterCriticalServiceAfter: 1m` cubre el `docker kill` (`app/services/consul.py:30-32`); `restart: unless-stopped` (`docker-compose.yml:122`); el registro no bloquea el arranque porque corre en una tarea de fondo (`app/main.py:124-126`). **En contra:** el compose **no define `stop_grace_period`**, así que Docker manda `SIGKILL` a los 10 s por defecto, mientras un `POST /documents` en vuelo puede estar esperando hasta `EXTRACT_TIMEOUT=27 s` a extract | `stop_grace_period` mayor que el peor pedido en vuelo |
| **X** | **Dev/prod parity** | Entornos lo más parecidos posible | 🟡 | A favor: la misma imagen y el mismo `.env.example` sirven para los dos; `uv.lock` idéntico; el override está versionado. **En contra, tres grietas concretas:** (a) el job de integración de la CI usa `mongo:7.0` **sin digest** (`.github/workflows/ci.yml:42`) mientras el compose lo fija por tag+digest (`docker-compose.yml:25`) y hay un test que lo exige (`tests/test_compose.py:88`); (b) desarrollo **compila** su propia imagen (`documents-service:dev`) y producción **baja** la de GHCR: no es el mismo artefacto; (c) correr local con `uv run` implica `LOG_FORMAT=text`, `CONSUL_ENABLED=false` y un Mongo distinto (`README.md:196-210`) | Fijar Mongo por digest en la CI |
| **XI** | **Logs** | Streams de eventos a stdout | ✅ | `configure_logging` manda **solo** a `sys.stdout` y reemplaza los handlers del root para que los de uvicorn pasen por el mismo formatter (`app/logging_config.py:67-80`, con `log_config=None` en `app/__main__.py:31`). `LOG_FORMAT=json` da un objeto JSON por línea (`app/logging_config.py:50-64`) y el compose usa `json` (`docker-compose.yml:63`). Siete tests, incluido `test_logs_go_to_stdout_not_to_a_file` (`tests/test_logging.py:21`) | — |
| **XII** | **Admin processes** | Tareas de administración como procesos one-off | 🟡 | **No hay procesos de administración**, y la decisión está argumentada: `create_index` es idempotente y se llama en el lifespan, así que no hace falta un motor de migraciones (`app/db/mongo.py:33-43`, `README.md:564`). El efecto colateral es que el único "admin task" del servicio corre **dentro** del proceso de la app, no como un one-off reproducible | Nada que arreglar si no aparecen migraciones reales |

**Resultado: 9 ✅ / 3 🟡 / 0 ❌.**

### Grado de cumplimiento

**✅ Cumple.** Nueve factores están cumplidos con evidencia y test; los tres parciales lo son por razones chicas y concretas (una falta de `stop_grace_period`, una imagen sin digest en la CI, y la ausencia de procesos one-off que hoy no hacen falta).

---

## 8. Tema 6 — Réplicas de contenedores

### 8.1 Teoría breve

Escalar **horizontalmente** es agregar instancias, no agrandar la instancia. Para que funcione hace falta: que el servicio sea **stateless** (cualquier réplica puede atender cualquier pedido), que haya un **balanceador** delante, que las réplicas se **registren** y se **chequeen** para poder sacar de la rotación a la que no está sana, y que el **estado compartido** (base de datos) sea el que garantice la consistencia, no la memoria de un proceso.

### 8.2 Cómo se ve en el repo

**Cómo se escala.** Por variable de entorno, sin tocar archivos (`README.md:459-462`):

```bash
DOCUMENTS_REPLICAS=3 docker compose up -d --wait   # sube a 3
DOCUMENTS_REPLICAS=1 docker compose up -d --wait   # vuelve a 1
```

Eso va a `deploy.replicas: ${DOCUMENTS_REPLICAS:-1}` (`docker-compose.yml:117`), documentado en `.env.example:69-74`.

**Cuántas réplicas hay y por qué.** **El default es 1** (`docker-compose.yml:117`, `.env.example:74`), probado en `tests/test_compose.py:69`. El motivo está escrito en `README.md:578`: "Este stack suma memoria. No conviene levantarlo mientras se corren las mediciones de carga sobre `extract-service`". Es decir: **1 por default a propósito**, para no contaminar el benchmark de la cátedra, con N disponible a demanda.

La capacidad escala linealmente y está cuantificada (`README.md:476`): el cupo de admisión es por proceso, así que con N réplicas el sistema admite **N × `MAX_CONCURRENT_UPLOADS`** uploads simultáneos (3 × 4 = 12 con los defaults) y hasta ese número de extracciones en vuelo; la memoria reservada crece igual, **N × `DOCUMENTS_MEMORY`** (3 × 512 MB = 1,5 GB).

**¿Es stateless?** Sí. Todo el estado persistente está en Mongo; lo único en memoria del proceso es el semáforo de admisión (`app/main.py:105`), que es **por réplica por diseño** y está documentado (`README.md:476`: "El cupo de cada réplica no se reparte con las demás: Traefik balancea por pedido, no por carga, así que una réplica puede responder `503` por falta de cupo mientras otra tiene lugar"). Y desde la issue #28 no queda ningún estado que una réplica necesite de otra: se borraron el circuit breaker, el bulkhead y la cache de discovery (`README.md:468`).

**Un proceso por contenedor, no `workers`.** Es la decisión más interesante del tema y está argumentada en `app/__main__.py:6-14` y en `README.md:569`: con `workers`, N procesos quedarían detrás de **un solo** registro en Consul y **un solo** check, y el cupo de uploads (que vive en memoria) se multiplicaría dentro de un límite de memoria pensado para uno. Con réplicas, cada proceso tiene su registro, su check y su límite. Es una respuesta de oral redonda.

**Cómo se balancea.** Traefik, round robin, entre las instancias `passing` del catálogo de Consul (`README.md:472`). Lo que lo habilita desde este repo (tabla en `README.md:470-474`):

| Pieza | Qué garantiza | Evidencia |
|---|---|---|
| Sin puerto publicado | Que N contenedores arranquen a la vez: `expose`, no `ports` | `docker-compose.yml:54-58`; test `tests/test_compose.py:55` |
| Registro único por réplica | Cada una usa su **hostname** como `service_id` y su IP de `mired` | `app/services/consul.py:236`, `:87-101`; test `tests/test_consul.py:229` |
| Tags de Traefik | Publicación y ruteo por `/documents` | `app/services/consul.py:70-84`; test `tests/test_consul.py:412` |
| Índice único `ux_documents_checksum` | El dedupe **entre** réplicas | `app/db/mongo.py:17`, `:40-42`; `app/services/document_service.py:72-79` |

**Cómo se detecta y se saca una réplica no sana.** Tres mecanismos encadenados:
1. El **check de Consul** pega a `/health` cada 10 s con timeout de 2 s (`app/services/consul.py:28-29`). Si deja de responder, pasa a `critical` y Traefik la saca de la rotación.
2. `DeregisterCriticalServiceAfter: 1m` (`app/services/consul.py:32`): si la réplica murió sin apagarse (`docker kill`) y nunca llegó a desregistrarse, Consul la borra del catálogo por su cuenta.
3. En un apagado ordenado, el lifespan **desregistra explícitamente** (`app/main.py:148-154`), y solo desregistra lo que se llegó a registrar.

Más el `HEALTHCHECK` de Docker (`Dockerfile:67-69`), que es lo que `docker compose ps` muestra como `healthy` y lo que `depends_on: condition: service_healthy` espera (`docker-compose.yml:105-107`).

Hay una advertencia operativa valiosa y fácil de olvidar en un oral: **`--wait` espera el healthcheck de Docker, no el de Consul** (`README.md:464`). Una réplica nueva entra a la rotación de Traefik recién cuando su check pasa a `passing`, hasta un intervalo (10 s) después.

**Limitación conocida del checkout analizado:** si Consul **borra** el registro de una réplica viva (por ejemplo tras un reinicio de Consul), esta versión **no se vuelve a registrar**: `register_with_retry` termina en el primer éxito y no vuelve a mirar (`app/services/consul.py:211-218`). Eso es exactamente lo que arreglan los commits que están en `origin/main` y **no** en este checkout (`fe2251e fix(consul): volver a registrarse si Consul borra la réplica`). Vale decirlo así en el oral: el problema se detectó, se arregló en la issue #51 y está mergeado en `main`.

**Resultados de pruebas de carga.** **No hay scripts de carga en este repositorio**, y es deliberado: `README.md:580-582` dice que están en `extract-service/tests/stress/` y apuntan **solo** a extract, porque "medir este servicio con el benchmark de la cátedra daría números no comparables". Los únicos números que aparecen acá son **citados** del informe de extract-service (`README.md:15`): 78,24 % de éxito, P95 4,61 s, ~326 `429` por corrida, y en el camino `POST /documents` de punta a punta, 596 `201` y 904 `503` del control de admisión de este servicio. No hay forma de reproducirlos desde este repo.

Lo que sí hay, y es lo más parecido a una prueba de concurrencia real, son los **tests de integración contra Mongo real** (`tests/integration/test_mongo_repository.py`, 12 tests): 20 inserts concurrentes del mismo checksum desde dos clientes que simulan dos réplicas (`:108`), y dos `DocumentService` extrayendo el mismo PDF a la vez con un *rendezvous* para forzar la carrera (`:130-167`). El resultado verificado es un solo documento.

### 8.3 Grado de cumplimiento

**✅ Cumple.** El escalado horizontal está implementado, parametrizado, documentado con la aritmética de capacidad, y los cuatro invariantes que lo hacen posible están cubiertos por tests. Lo único que falta son mediciones propias, y su ausencia está justificada.

### 8.4 Lo que falta o se podría mejorar

- No hay prueba de carga propia que respalde los números del `README.md:15` ni el escalado de N réplicas.
- El checkout analizado no re-registra una réplica que Consul haya borrado (ya resuelto en `origin/main`).
- El cupo de admisión no se comparte entre réplicas: una puede dar `503` mientras otra tiene lugar (documentado, pero es una ineficiencia real).

---

## 9. Tema 7 — Patrones de resiliencia

### 9.1 Teoría breve

- **SAGA con orquestador**: una transacción de negocio distribuida se parte en pasos locales, cada uno con su **compensación**; un **orquestador** central decide el próximo paso y, si algo falla, dispara las compensaciones en orden inverso. Se usa cuando no hay transacción ACID posible entre servicios.
- **Retry**: reintentar lo que falló. Requiere **idempotencia** y **backoff** (preferentemente con *jitter*), y un tope de intentos; si no, se produce una **tormenta de reintentos** que convierte una degradación en una caída.
- **Circuit Breaker**: tres estados — *closed* (pasa todo, se cuentan fallas), *open* (se rechaza sin intentar, por un tiempo), *half-open* (se deja pasar una prueba; si va bien, cierra). Evita gastar recursos y timeouts contra algo que ya se sabe caído.
- **Rate Limit**: acotar pedidos por unidad de tiempo, por IP/consumidor o global. Es *backpressure* hacia el cliente, y se comunica con `429 Too Many Requests` (o `503`) más `Retry-After`.
- **Cache**: guardar una respuesta para no recalcularla. La parte difícil es la **invalidación**. La **deduplicación** de pedidos en vuelo es su prima: colapsar N pedidos idénticos simultáneos en uno.

### 9.2 La decisión de fondo del repo

Antes de ir patrón por patrón, hay que entender la arquitectura de resiliencia que eligieron, porque es lo que explica la mitad de los "no implementado":

> **documents-service no implementa el circuit breaker: lo aplica Traefik sobre el tráfico hacia extract-service. Lo mismo con el retry y el límite de concurrencia** (`README.md:479`).

Hasta la issue #28 estaban escritos a mano acá. Se borraron por dos razones (`README.md:481-486`): porque duplicaban al gateway, y porque el breaker propio **tenía tres bugs verificados**:

1. contaba como fallas los rechazos de su propio bulkhead;
2. contaba los `400` de PDFs corruptos, así que **5 PDFs rotos abrían el circuito para todos**;
3. un éxito de una llamada vieja cerraba un circuito abierto.

Eso es oro para un oral: no es "no lo hicimos", es "lo hicimos, medimos que estaba mal, y lo movimos a la capa que corresponde". El reparto final está en la tabla de `README.md:488-497`.

### 9.3 SAGA con orquestador

**¿Está implementado?** **No. No está implementado, y además no se menciona en ningún archivo del repo** (grep de "saga"/"SAGA" en `README.md`, `app/` y `tests/`: cero resultados).

**¿Aplica?** Rigurosamente, **no**, y vale la pena saber argumentarlo. El flujo de `POST /documents` tiene dos pasos que tocan recursos distintos (`app/services/document_service.py:54-71`):

1. `extract.extract(pdf)` — llamada remota a otro servicio.
2. `repository.create(...)` — escritura en la base propia.

Para que hiciera falta una SAGA, el paso 1 tendría que dejar un **efecto colateral que haya que compensar** si el paso 2 falla. Y no lo deja: `extract-service` es una función pura desde el punto de vista del estado — recibe bytes, devuelve texto, no guarda nada. Si `create` falla, no hay nada que "des-extraer": el costo es CPU gastada, no estado inconsistente. Y el orden elegido ayuda: **primero el paso remoto, después el local**, así que el único paso que puede fallar después del otro es el que está en la propia base, donde la atomicidad la da Mongo a nivel documento.

**Dónde tendría sentido en este sistema.** Si el flujo creciera a algo como "guardar el documento **y** indexarlo en un buscador **y** notificar a un tercero", ahí sí: tres escrituras en tres dueños distintos, sin transacción posible, y un `delete` del documento como compensación del primer paso. Hoy, con un solo dueño de estado, una SAGA sería complejidad sin beneficio (YAGNI). El sustituto correcto para este caso es lo que efectivamente hicieron: **idempotencia por clave de negocio** (el checksum) más un **índice único** que decide las carreras.

**Grado de cumplimiento: ❌ No cumple** — como implementación. Pero el veredicto honesto es "**no aplica**", y lo que hace falta es **escribirlo** en el README, porque hoy no hay nada que lo explique.

### 9.4 Retry

**¿Está implementado?** En dos lugares, y conviene no confundirlos.

**(a) Hacia extract: NO, deliberadamente.** `ExtractorClient.extract` hace **un solo** `POST` y nada más (`app/services/extractor_client.py:88-109`, docstring `:89-94`). Ni un timeout ni un `503` se reintentan. El argumento (`README.md:499`): reenviar un PDF de varios megabytes a un servicio **ya saturado** duplicaría la carga sobre el cuello de botella que el TP pide no empeorar.

- **Capa donde se reintenta**: Traefik, middleware `extract-retry`, configurado por los tags de **extract**, no de documents (`README.md:490`).
- **Qué reintenta**: solo lo que falló **antes** de salir hacia la réplica (conexión rechazada, o el `dialTimeout` de 2 s contra una réplica caída que Consul todavía no sacó) — y eso vale también para `POST /extract`. **No reintenta nada de lo que ya salió**: ni una respuesta (tampoco el `503`), ni un `502` por una réplica que murió en medio, ni un `504` por `responseHeaderTimeout`.
- **Idempotencia**: no hace falta para ese retry, precisamente porque solo reintenta pedidos que nunca llegaron a ejecutarse.
- **Riesgo de tormenta de reintentos**: **acotado por diseño**. Es el punto más fuerte de esta decisión: con un solo intento en documents y un retry en Traefik que solo cubre dials fallidos, no hay amplificación. Si documents reintentara 3 veces y Traefik otras 2, un pico se multiplicaría por 6 justo cuando el sistema menos lo aguanta.
- **Test**: `tests/test_resilience.py:140`, `test_a_slow_gateway_becomes_a_504_after_extract_timeout_and_is_not_retried`.

**(b) Hacia Consul: SÍ, con backoff.** El registro se reintenta hasta lograrlo (`app/services/consul.py:195-218`):

```python
RETRY_INITIAL_DELAY = 1.0
RETRY_MAX_DELAY = 30.0
...
delay = min(delay * 2, max_delay)
```

- **Capa**: tarea de fondo lanzada en el lifespan (`app/main.py:124-126`), para que **el arranque no la espere**.
- **Backoff**: exponencial, 1 s → 2 s → 4 s … topeado en 30 s. El caso real que cubre está documentado (`app/services/consul.py:34-36`): Consul inicializando el ACL unos segundos después del primer intento, que responde `403 "ACL not found"`.
- **Idempotencia**: el registro de Consul es idempotente por `service_id` (es un `PUT` con el mismo hostname), así que reintentar es seguro.
- **Tests**: `tests/test_consul.py:322` (reintenta tras dos `403` hasta el `200`), `:344` (el backoff se frena en el máximo), `:364` (la app arranca y atiende mientras el registro reintenta).
- **Observación:** **no hay tope de intentos**. Con un token mal configurado, el loop reintenta cada 30 s para siempre, dejando un `WARNING` por intento (`app/services/consul.py:162-166`). Para este TP es aceptable — el servicio sigue atendiendo — pero es ruido de log indefinido, y conviene saber decirlo. Tampoco hay *jitter*: con N réplicas arrancando juntas, todos los reintentos quedan alineados en el mismo instante.

**Grado de cumplimiento: 🟡 Cumple parcialmente.** La ausencia de retry hacia extract es una decisión correcta, argumentada y testeada; el retry hacia Consul está bien hecho con backoff acotado. Es parcial porque el retry hacia extract vive en otro repositorio (no se puede mostrar acá) y porque al de Consul le faltan tope de intentos y jitter.

### 9.5 Circuit Breaker

**¿Está implementado?** **No en este servicio. No está implementado acá, a propósito.** Se borró en la issue #28 (`c3c5bd3 refactor(extractor): borrar el circuit breaker propio`), junto con `CircuitOpenError` (`f74dd4d`), las variables `BREAKER_*` de la configuración (`57f5093`) y el campo `extract_circuit` de `/health/ready` (`1b2134c`). Esto último fue un **cambio de contrato documentado** (`README.md:58`).

**Dónde está.** Traefik, middleware `extract-cb`, declarado por **extract** en sus tags de Consul, con los parámetros `TRAEFIK_CB_EXPRESSION`, `TRAEFIK_CB_FALLBACK` y `TRAEFIK_CB_RECOVERY` (`README.md:489`). **Los valores concretos no están en este repo**: viven en `extract-service`/`pdf_extractext-infra`.

**Qué queda acá.** Solo la **traducción** de lo que el breaker produce. Cuando el circuito está abierto, Traefik responde `503`, y documents lo convierte en `ExtractUnavailableError` con el `Retry-After` que venga (`app/services/extractor_client.py:125-132`). Explícitamente: "documents no lleva la cuenta de estas respuestas: solo las traduce" (`README.md:332`).

Y hay un detalle de diseño que protege al breaker ajeno: el `400`/`422` de extract se traduce a `422`, **no** a `502`, justamente para **no** alimentar un breaker que solo debe contar errores de red y `500`–`502` (`README.md:107`, `app/services/extractor_client.py:116-120`). Es el bug #2 del breaker viejo, resuelto en la traducción.

**Dónde debería estar y no está: hacia Mongo.** Este es el hueco real. No hay breaker, ni fast-fail, ni cache del último estado conocido para la base propia. Con Mongo caído, **cada** pedido paga el `MONGO_TIMEOUT_MS` completo (3 s, `app/config.py:41`) antes de dar su `503`. El atenuante es que el cupo de admisión (4 por réplica) limita cuántos esperan a la vez, y que `/health/ready` ya reporta `database: down` (`app/api/health.py:33-39`) — pero nadie usa ese estado para cortar camino.

**Grado de cumplimiento: 🟡 Cumple parcialmente.** La delegación a Traefik es coherente, está documentada y resolvió bugs reales; el contrato se ajustó en consecuencia. Es parcial porque los parámetros (umbrales, estados, recuperación) no son visibles en este repo y porque la dependencia Mongo no tiene ninguna protección de este tipo.

### 9.6 Rate Limit

**¿Está implementado?** **No para `documents`. No está implementado.** Y está fijado como invariante por un test: `tests/test_consul.py:421`, `test_documents_no_declara_middlewares_en_traefik`. Los tags de documents son tres y ninguno es un `rateLimit` (`app/services/consul.py:78-84`). El `inFlightReq` (`extract-inflight`, `TRAEFIK_INFLIGHT`) existe **solo para extract** (`README.md:491`).

**Qué hay en su lugar: control de admisión (load shedding) + backpressure.** No es lo mismo que un rate limit, y la diferencia importa en un oral: un rate limit cuenta **pedidos por tiempo**; esto acota **trabajo concurrente**.

- **Por IP**: ❌ no está implementado. Nada distingue clientes.
- **Global**: 🟡 parcial, y **por proceso**, no global: un semáforo de `MAX_CONCURRENT_UPLOADS=4` (`app/main.py:105`, `app/config.py:62-65`). Con N réplicas el total es N × 4 (`README.md:476`), y no hay coordinación entre réplicas.
- **Backpressure**: ✅ bien hecho. El cupo se toma **antes de leer un solo byte del body** (`app/api/documents.py:117-129`), con espera acotada a `UPLOAD_SLOT_TIMEOUT=2.0` (`app/api/documents.py:212-219`). El orden de los pasos está pensado (`README.md:506-511`):
  1. `Content-Length` declarado > límite + 64 KiB de margen → `413`, sin leer el body y **sin tomar cupo** (`app/api/documents.py:202-209`).
  2. Sin `multipart/form-data` con boundary → `400` (`app/api/documents.py:119-123`).
  3. Toma cupo; si no se libera en 2 s → `503` + `Retry-After` (`app/api/documents.py:217-219`).
  4. Lee `request.stream()` y corta en `413` apenas se pasa — porque el `Content-Length` **puede mentir o faltar** (`app/api/documents.py:239-259`).
  5. Parsea el multipart en memoria y exige el tamaño exacto (`app/api/documents.py:232-236`).

  Y el cupo **se libera siempre**, en un `finally` que cubre error, `413` o corte del cliente (`app/api/documents.py:135-136`). Hay una clase de tests entera para eso: `tests/test_upload_limits.py:232`, `TestSlotRelease`.
- **Códigos HTTP**: aquí hay un matiz para defender. documents **nunca emite `429`**: su saturación propia es `503` + `Retry-After` (`UploadSlotsFullError` hereda de `RetryableError`, `app/exceptions.py:111-121`), y el `429` que recibe del gateway lo **traduce a `503`** (`app/services/extractor_client.py:125-132`). El argumento está en el docstring de `app/exceptions.py:113-119`: es *load shedding* propio, no una falla upstream. Es coherente, pero un profesor puede señalar que `429` es el código semánticamente correcto para "estás mandando demasiado". **`Retry-After` sí está**, calculado y acotado a un mínimo de 1 s (`app/main.py:196-205`), probado en `tests/test_api_documents.py:124`.

**Grado de cumplimiento: 🟡 Cumple parcialmente.** Hay backpressure real, bien ubicado y con `Retry-After`, pero **no hay rate limit** (ni por IP ni global) y el servicio nunca usa `429`.

### 9.7 Cache

**¿Está implementado?** **No. No hay ninguna caché en el repo.** Grep de `cache`/`Cache` en `app/`: solo aparece en `CONSUL_CACHE_TTL`, y es una variable **borrada** mencionada en un comentario histórico (`README.md:140`). No hay `Cache-Control`, no hay `ETag`, no hay Redis, no hay caché en memoria.

**Qué cumple una función parecida: la deduplicación por checksum.** No es una caché, pero evita trabajo repetido y hay que saber distinguirlas:

- **Qué evita**: una segunda extracción del mismo PDF. El checksum SHA-256 se consulta **antes** de llamar a extract (`app/services/document_service.py:48-57`), así que un PDF repetido nunca llega al extractor.
- **Dónde**: en Mongo, no en memoria — así que **funciona entre réplicas** y sobrevive a reinicios. Esa es la diferencia con una caché de proceso.
- **Invalidación**: no hay, porque **el contenido es inmutable**. El contrato no tiene `PUT`: el puerto del repositorio **omite `update` a propósito** (`app/ports.py:16-18`, citando ISP). Un checksum identifica un contenido para siempre. La única "invalidación" es el `DELETE`.
- **Beneficio medible en resiliencia**: un PDF conocido se sirve **aunque el gateway esté rechazando extracciones** (`app/services/document_service.py:51-53`), probado en `tests/test_resilience.py:118`.

**Deduplicación de pedidos en vuelo: NO está implementada,** y la consecuencia está asumida por escrito (`README.md:572`): dos subidas simultáneas del mismo PDF pasan las dos el chequeo previo, **las dos llaman a extract**, y recién el índice único decide. En el peor caso se pagan tantas extracciones de más como uploads admitidos a la vez (réplicas × 4). El README dice qué haría falta para evitarlo — un lock o una reserva del checksum antes de extraer — y por qué no se hizo (no se justifica para el TP).

**Oportunidad obvia y no tomada:** `GET /documents/{id}` devuelve un recurso **inmutable** y potencialmente de megabytes (`content`). Es el caso de libro para `ETag` + `Cache-Control: public, max-age=…, immutable`, y hoy no hay nada.

**Grado de cumplimiento: ❌ No cumple.** No existe caché de ningún tipo. La deduplicación por checksum es un patrón distinto (idempotencia) y está bien hecha, pero no es lo que el tema pide.

### 9.8 Cómo interactúan los timeouts de las distintas capas

Esta es la parte que mejor se defiende en un oral, porque está **presupuestada**, no improvisada.

**El presupuesto declarado** (`app/config.py:49-53`, `.env.example:58`):

```
cliente 30 s  >  documents 27 s  >  Traefik 25 s  >=  extract 23 s
```

**Por qué en ese orden, y no al revés.** La regla general es "el de adentro menor que el de afuera", pero acá hay un refinamiento deliberado: **documents corta *después* que Traefik** (27 > 25). El motivo está escrito en `app/config.py:50-51`: así el `504` lo da el **gateway**, que sabe qué pasó con la réplica, y no un corte nuestro a ciegas. Es un detalle fino y bien pensado. Hay un test que lo fija como invariante de configuración: `tests/test_config.py:30`, `test_extract_timeout_fits_the_timeout_budget`.

**Tabla completa de timeouts del servicio:**

| Timeout | Valor | Dónde | Para qué |
|---|---|---|---|
| `EXTRACT_TIMEOUT` | 27 s | `app/config.py:52`, armado en `app/main.py:86-88` | Tope total de la llamada a extract |
| `EXTRACT_CONNECT_TIMEOUT` | 2 s | `app/config.py:53` | Con extract caído se falla en 2 s, no en 25 (`README.md:132`) |
| `MONGO_TIMEOUT_MS` | 3000 | `app/config.py:41`, aplicado como `serverSelectionTimeoutMS` en `app/db/mongo.py:26-30` | Que `/health/ready` y los `503` lleguen en ~3 s en vez de los 30 s del default del driver |
| `MONGO_RETRY_AFTER` | 5 s | `app/config.py:43`, usado en `app/main.py:189` | Qué decirle al cliente cuando la base no está |
| `CONSUL_TIMEOUT` | 2 s | `app/config.py:78`, `app/main.py:111` | Que el arranque no espere a un Consul que no responde |
| `UPLOAD_SLOT_TIMEOUT` | 2 s | `app/config.py:67`, `app/api/documents.py:215` | Cuánto espera un upload por cupo antes del `503` |
| Check de Consul | `Interval 10s` / `Timeout 2s` | `app/services/consul.py:28-29` | Detección de réplica no sana |
| `HEALTHCHECK` Docker | `interval 30s` / `timeout 5s` / `retries 3` / `start-period 10s` | `Dockerfile:67` | Liveness del contenedor |

**Las interacciones que importan:**

1. **Fast-fail vs. espera larga.** Un `EXTRACT_CONNECT_TIMEOUT` de 2 s separa "extract no está" (fallo en 2 s, `503`) de "extract está pero tarda" (espera hasta 27 s, `504`). Sin esa separación, una réplica caída costaría 27 s por pedido.
2. **El caso borde donde los timeouts se suman**, documentado en `README.md:492`: un dial fallido **reintentado por Traefik** más una réplica que no responde da el `504` de Traefik a los ~27 s — justo el `EXTRACT_TIMEOUT` de documents, que **puede cortar antes** con su propio `504`. Es decir: el margen de 2 s entre 27 y 25 se consume exactamente en ese escenario. El resultado para el cliente es el mismo (`504`), así que es un empate benigno, pero es honesto que esté escrito.
3. **El cupo de admisión convierte timeouts en backpressure.** Con 4 slots y pedidos que tardan 27 s, el 5° espera 2 s y recibe `503`. Es decir: el timeout largo hacia extract **no se propaga** como latencia a toda la cola; se convierte en rechazos rápidos. Esa es la cadena de resiliencia completa funcionando.
4. **El timeout corto de Mongo protege la readiness.** 3 s en lugar de 30 significa que `/health/ready` puede contestar `503` a tiempo para que un orquestador lo use (`app/db/mongo.py:22-25`).
5. **Incoherencia menor detectada:** el `MONGO_RETRY_AFTER` (5 s) no tiene relación aritmética con nada más del presupuesto; es un valor razonable pero arbitrario. No es un error, pero si preguntan "¿de dónde sale el 5?", la respuesta honesta es "es una estimación, no un cálculo".

**Grado de cumplimiento del tema 7 en conjunto: 🟡 Cumple parcialmente.** Timeout, load shedding e idempotencia están implementados acá y bien testeados; Retry y Circuit Breaker están delegados a Traefik de forma coherente y argumentada; Rate Limit y Cache **no están**; SAGA no aplica pero tampoco está explicado.

### 9.9 Lo que falta o se podría mejorar

- SAGA: no se menciona ni para descartarla.
- Rate limit: ninguno para documents; el servicio nunca emite `429`.
- Cache: ninguna; falta el `ETag`/`Cache-Control` obvio en `GET /documents/{id}`.
- Sin dedupe de pedidos en vuelo: se pagan extracciones duplicadas en la carrera.
- Sin protección (breaker/fast-fail) hacia Mongo.
- Retry a Consul sin tope de intentos ni jitter.

---

## 10. Tema 8 — Desarrollo seguro

## 10.A Principios y buenas prácticas

### 10.A.1 Teoría breve

- **Mínimo privilegio**: cada componente con los permisos mínimos para su función.
- **Defensa en profundidad**: varias capas de control; si una falla, otra atrapa.
- **Fallo seguro** (*fail secure* / *fail closed*): ante el error, el estado por defecto es el seguro.
- **Validación de entradas**: todo lo que entra de afuera es hostil hasta que se valida, y se valida en el **borde**.
- **Manejo de secretos**: fuera del código y del repo, inyectados por entorno, nunca en logs.
- **Contenedores no root**.
- **Superficie expuesta**: publicar lo mínimo.
- **Autenticación y autorización**: quién sos y qué podés hacer.
- **TLS**: confidencialidad e integridad en tránsito.
- **Logs sin datos sensibles**.

### 10.A.2 Cómo se ve en el repo

**Mínimo privilegio.** ✅ Cuatro aplicaciones concretas:
- **Usuario no root** en el contenedor: `groupadd --gid 1001 appgroup`, `useradd --uid 1001 --gid appgroup --no-create-home appuser`, `USER appuser` (`Dockerfile:55-61`). Sin home, UID/GID fijos.
- **Sin gestor de paquetes en la imagen final**: se borran `pip`, `setuptools` y `wheel` de `/usr/local` (`Dockerfile:49-53`). El servicio corre del venv y no instala nada en runtime, así que un atacante con ejecución de código no tiene `pip install`. El comentario lo dice: "En la imagen final `python -c \"import pip\"` falla, a propósito" (`README.md:449`).
- **Token de Consul de alcance acotado**: documents usa `CONSUL_DOCUMENTS_TOKEN`, **no el de management** (`.env.example:40-41`, `README.md:243-247`), con una advertencia operativa de por qué no hay que exportarlo en el shell (`README.md:255`).
- **Permisos de la CI**: `permissions: contents: read` a nivel workflow (`.github/workflows/ci.yml:10-11`), y `packages: write` **solo** en el job `image` (`.github/workflows/ci.yml:93-96`), con `persist-credentials: false` en su checkout (`.github/workflows/ci.yml:111`).

**Defensa en profundidad.** ✅ El patrón se repite en tres lugares:
- *Tamaño del upload*: `Content-Length` declarado → total leído del stream → tamaño exacto del archivo parseado. Tres controles, porque los dos primeros se pueden eludir (`app/api/documents.py:202-259`).
- *Tamaño del contenido*: `MAX_CONTENT_BYTES` validado en el servicio (`app/services/document_service.py:107-123`) **más** el `DocumentTooLarge` de Mongo atrapado en el repositorio como red de seguridad explícita (`app/repositories/document_repository.py:75-84`) — "si el margen no alcanza no puede terminar en un 500". Y el límite se valida al arrancar: `MAX_CONTENT_BYTES` debe ser `> 0` y `< 16 MiB` o el servicio no arranca (`app/config.py:60`, `tests/test_config.py:88`).
- *Duplicados*: chequeo previo en el servicio **más** índice único en Mongo, con el reconocimiento explícito de que el chequeo previo puede perder la carrera y el índice es "la garantía real" (`app/db/mongo.py:36-38`, `app/services/document_service.py:72-79`).

**Fallo seguro.** ✅ Cuatro casos:
- Configuración inválida → **el servicio no arranca**: base del monolito (`app/config.py:84-93`), `MAX_CONTENT_BYTES` fuera de rango (`app/config.py:60`), cualquier campo con constraint violado (`tests/test_config.py:83`).
- Fallo de Consul → **nunca fatal**, el servicio sigue atendiendo (`app/services/consul.py:158-192`, `app/main.py:120-126`).
- Respuesta inutilizable de extract → `ExtractProtocolError` → `502`, en lugar de propagar basura (`app/services/extractor_client.py:157-174`).
- `_parse_object_id` devuelve `None` en vez de lanzar, con un comentario que explica el bug que evita: `ObjectId(None)` **no falla**, acuña un id nuevo al azar, lo que convertiría un pedido inválido en la búsqueda de un documento que nadie tiene (`app/repositories/document_repository.py:19-32`).

**Validación de entradas.** ✅ Muy completa, y toda en el borde:
- **Multipart** parseado a mano sobre el parser de bajo nivel, porque el de Starlette vuelca a disco todo lo que pase de 1 MB **antes** de que corra el endpoint (`app/api/multipart.py:1-12`, `app/api/documents.py:7-10`). Además verifica el **boundary de cierre**, porque `python-multipart` no lo hace en `finalize()` y un body truncado pasaría como un archivo completo y más corto (`app/api/multipart.py:72-75`). 158 líneas de tests (`tests/test_multipart.py`).
- **`X-Request-ID`**: regex `[A-Za-z0-9-]{1,64}` con `fullmatch` sobre bytes crudos (`app/middleware.py:29`, `:37`), con el motivo escrito: "ends up in every log line, in the response and in the call to extract-service, so a long value, spaces, line breaks or control characters would let a client forge log lines or headers" (`app/middleware.py:9-13`). Es prevención de **log injection** y **header injection** explícita.
- **PDF**: magic bytes `%PDF` + extensión `.pdf` + no vacío (`app/services/document_service.py:125-133`).
- **Paginación**: `limit` acotado a `1..100`, `offset >= 0` (`app/api/documents.py:172-173`), probado en `tests/test_api_documents.py:220`.
- **Respuesta de extract** (entrada que viene de otro servicio, igual de hostil): tipos verificados uno por uno, incluido el descarte de `bool` como `int` y el rechazo de `page_count` negativo (`app/services/extractor_client.py:164-174`). 9 variantes inválidas testeadas (`tests/contracts/test_extract_contract.py:69-85`). Esto es OWASP **API10** bien hecho.
- **Inyección en Mongo**: las consultas se arman como diccionarios con `ObjectId` ya validado, nunca con concatenación de strings ni `$where` (`app/repositories/document_repository.py:46`, `:50`, `:91`). No hay superficie de NoSQL injection.

**Manejo de secretos.** ✅ El único secreto es el token de ACL de Consul, y está bien tratado:
- Solo por entorno, con default vacío y comentario "solo por entorno, nunca en el repo" (`app/config.py:76-77`).
- `.env.example` versionado **con el valor vacío** (`.env.example:45`), con instrucciones de dónde sacarlo y la advertencia de no commitearlo (`.env.example:40-44`).
- `.env` en `.gitignore:5` **y** en `.dockerignore:8` — lo segundo importa: no entra al contexto de build. **Verificado: `.env` no está en `git ls-files`.**
- Viaja en el header `X-Consul-Token` (`app/services/consul.py:118-121`) y **no se loguea**: el comentario lo afirma y los logs de error registran `service_id` y el texto del error, nunca las headers (`app/services/consul.py:162-166`).
- Incluso los tests cuidan esto: `tests/test_compose.py` pasa un `--env-file` vacío "para que el `.env` local no cambie el resultado (ni se cuele un token en la salida)" (`tests/test_compose.py:6-7`).

**Contenedores no root.** ✅ `Dockerfile:55-61`, ver arriba.

**Superficie expuesta.** ✅ Minimizada en cuatro niveles:
- Mongo **sin puerto publicado** y **solo en la red privada** del proyecto (`docker-compose.yml:26-28`, `:127-128`).
- documents **sin puerto publicado**: `expose`, no `ports` (`docker-compose.yml:54-58`).
- Solo `/documents` está ruteado por Traefik; `/health`, `/health/ready` y `/docs` **no** (`README.md:86`, `:166`). El esquema OpenAPI no se alcanza desde afuera.
- `exposedByDefault=false` en Traefik: sin el tag `traefik.enable=true` no se publica nada (`app/services/consul.py:72-73`).
- La imagen es *slim*, multi-stage, sin herramientas de build en la etapa final (`Dockerfile:11-21`) y **sin `curl`** (`README.md:178` lo menciona al explicar cómo pegarle a una réplica).

**Autenticación y autorización.** ❌ **No está implementado. Nada.** Ningún endpoint pide credenciales: no hay middleware de auth, no hay `Depends(get_current_user)`, no hay API key, no hay JWT (grep de `auth`/`jwt` en `app/`: cero resultados funcionales). El README lo declara como fuera de alcance (`README.md:11`: "No hace: … autenticar usuarios"). La consecuencia concreta: **cualquiera que alcance el gateway puede hacer `DELETE /documents/{id}`** (`app/api/documents.py:185-195`) y leer el `content` completo de cualquier documento (`app/api/documents.py:151-162`). Mitigación parcial: la superficie está acotada a la red de Docker y al `127.0.0.1:8080` de Traefik.

**TLS.** ❌ **No está implementado.** Todo es HTTP plano: `MONGO_URI=mongodb://…` sin TLS (`app/config.py:39`), `EXTRACT_URL=http://…` (`app/config.py:48`), `CONSUL_HTTP_ADDR=http://consul:8500` (`app/config.py:75`), y el token de ACL **viaja en claro** en un header HTTP. En una red Docker de una sola máquina el riesgo es bajo; en producción no sería aceptable.

**Logs sin datos sensibles.** ✅ Es uno de los puntos más cuidados del repo, y tiene su propia sección en el README (`README.md:111`). La regla es: **mensaje fijo al cliente, detalle solo al log.**
- Cada excepción lleva el `message` fijo de su clase (`app/exceptions.py` entero, `app/main.py:196-205`). Nada que llegue al cliente incluye texto de httpx, pymongo, hosts, IPs ni URLs internas.
- El detalle va a un `warning` con `extra`: `error_type`, `error`, `extract_status`, `extract_detail` (truncado a 200 caracteres, `app/services/extractor_client.py:45`, `:197-205`), `reason`.
- El `503` por conexión rechazada **no muestra el destino**: hay un test dedicado, `test_a_connection_error_does_not_show_the_destination_to_the_client` (`tests/test_resilience.py:167`).
- El error de Mongo se loguea con su tipo porque "the driver's text names hosts and topology, so it only goes to the log" (`app/main.py:176-189`), probado en `tests/test_database_unavailable.py:101`.
- Del `X-Request-ID` rechazado se loguean **a lo sumo 64 caracteres, escapados** con `unicode_escape` (`app/middleware.py:42-44`), probado en `tests/test_correlation.py:141` ("the replacement is logged without the whole value").
- El checksum se loguea **truncado a 12 caracteres** (`app/services/document_service.py:49`).

*Observación menor:* el `str(exc)` de un `ServerSelectionTimeoutError` incluye el host y el puerto de Mongo. Va solo al log, que es lo correcto, pero si el `MONGO_URI` llevara usuario y contraseña, parte de eso podría aparecer en el log. Hoy el URI no tiene credenciales (`docker-compose.yml:64`), así que es un riesgo latente, no actual.

### 10.A.3 Relación con OWASP Top 10 (2021)

| | Categoría | Estado en este repo |
|---|---|---|
| A01 | Broken Access Control | ❌ **Sin authn/authz**: `DELETE` y `GET` de cualquier documento abiertos |
| A02 | Cryptographic Failures | ❌ Sin TLS en ningún tramo; el token de Consul viaja en claro. ✅ El checksum es SHA-256, no MD5 (`app/services/document_service.py:48`) |
| A03 | Injection | ✅ Consultas Mongo como dicts con `ObjectId` validado; **log/header injection** prevenida explícitamente en el `X-Request-ID` (`app/middleware.py:9-13`) |
| A04 | Insecure Design | ✅ Presupuesto de timeouts, load shedding antes de leer el body, límites validados al arrancar, `ports.py` sin `update` por ISP |
| A05 | Security Misconfiguration | ✅ No root, sin pip/setuptools, imágenes por digest, sin puertos publicados, `/docs` no ruteado, sin modo debug |
| A06 | Vulnerable and Outdated Components | ✅ Lo más fuerte del repo: ver 10.B. **Hoy: 0 vulnerabilidades** |
| A07 | Identification and Authentication Failures | ❌ No hay identificación de ningún tipo |
| A08 | Software and Data Integrity Failures | 🟡 Imagen base y Mongo **por digest**, actions **por SHA**, lockfile **con hashes**, Grype antes del push. Falta **SBOM** y **firma de imagen (cosign)** |
| A09 | Security Logging and Monitoring Failures | 🟡 Logs estructurados con `request_id` correlacionable entre servicios. **Sin métricas, sin alertas, sin retención definida** |
| A10 | SSRF | ✅ `EXTRACT_URL` viene de la configuración, **nunca de la entrada del usuario**; no hay ningún endpoint que acepte una URL |

### 10.A.4 Relación con OWASP API Security Top 10 (2023)

| | Categoría | Estado |
|---|---|---|
| API1 | Broken Object Level Authorization (BOLA) | ❌ **El hueco principal.** Cualquiera puede `GET`/`DELETE` cualquier `id`; no hay noción de dueño |
| API2 | Broken Authentication | ❌ No hay autenticación |
| API3 | Broken Object Property Level Authorization | ✅ El listado **omite `content`** a propósito (`app/api/schemas.py:3-5`, `:13-23`), probado en `tests/test_api_documents.py:195` |
| API4 | Unrestricted Resource Consumption | ✅ El punto más fuerte: `MAX_UPLOAD_SIZE`, `MAX_CONTENT_BYTES`, `MAX_CONCURRENT_UPLOADS`, `UPLOAD_SLOT_TIMEOUT`, límites de CPU y RAM en el compose (`docker-compose.yml:118-121`), pool de httpx acotado (`app/main.py:91`) |
| API5 | Broken Function Level Authorization | ❌ `DELETE` no está diferenciado de `GET` por permisos |
| API6 | Unrestricted Access to Sensitive Business Flows | 🟡 El upload (el flujo costoso) está acotado por admisión, y el dedupe evita re-extraer. Sin rate limit por cliente |
| API7 | Server Side Request Forgery | ✅ Ninguna URL viene del usuario |
| API8 | Security Misconfiguration | ✅ Ver A05. Sin CORS permisivo (no hay `CORSMiddleware`: grep en `app/` → cero) |
| API9 | Improper Inventory Management | ✅ OpenAPI versionado por `APP_VERSION` (`app/config.py:31`), `/docs` **no expuesto** por el gateway |
| API10 | Unsafe Consumption of APIs | ✅ Muy bien: la respuesta de extract se valida campo por campo con 9 variantes inválidas testeadas |

### 10.A.5 Grado de cumplimiento (principios)

**🟡 Cumple parcialmente.** Mínimo privilegio, defensa en profundidad, fallo seguro, validación de entradas, manejo de secretos, contenedor no root, superficie expuesta y logs sin datos sensibles están **muy bien** cubiertos, con evidencia y tests. Los dos que faltan son grandes y conocidos: **autenticación/autorización** y **TLS**, ambos declarados fuera de alcance.

---

## 10.B Vulnerabilidades en dependencias

### 10.B.1 Teoría breve

El código de terceros es la mayor parte de cualquier servicio moderno. Lo que hay que exigir: **dependencias fijadas** (lockfile, idealmente con hashes, para evitar que el mismo `install` traiga algo distinto mañana), **escaneo automático** en la CI con una **política de corte** clara, **imágenes fijadas por digest** (un tag es mutable), **acciones de CI fijadas por SHA** (un tag de acción también es mutable y es un vector de supply-chain), **SBOM** (inventario de lo que hay dentro), **firma de imágenes** (procedencia verificable) y **actualización automática** (Dependabot/Renovate) para que lo anterior no se vuelva un museo.

### 10.B.2 Dependencias: directas y transitivas

**Directas de producción: 6** (`pyproject.toml:6-13`):

| Paquete | Restricción declarada | Versión resuelta |
|---|---|---|
| `fastapi` | `>=0.115.0` | 0.142.2 |
| `uvicorn[standard]` | `>=0.32.0` | 0.54.0 |
| `pydantic-settings` | `>=2.6.0` | 2.15.0 |
| `motor` | `>=3.6.0` | 3.7.1 |
| `httpx` | `>=0.28.1` | 0.28.1 |
| `python-multipart` | `>=0.0.12` | 0.0.32 |

**Directas de desarrollo: 3** (`pyproject.toml:15-20`): `pytest>=8.3.0`, `pytest-asyncio>=0.24.0`, `respx>=0.22.0`.

**Totales (medidos ahora con `uv tree` y `uv export`):**
- **37 paquetes resueltos** en total (prod + dev).
- **28 paquetes en el export de producción** → **22 transitivas** sobre las 6 directas.
- El árbol completo (`uv tree --no-dev`): `annotated-doc`, `annotated-types`, `anyio`, `certifi`, `click`, `dnspython`, `h11`, `httpcore`, `httptools`, `idna`, `opentelemetry-api`, `pydantic`, `pydantic-core`, `pymongo`, `python-dotenv`, `pyyaml`, `starlette`, `typing-extensions`, `typing-inspection`, `uvloop`, `watchfiles`, `websockets`.

Son **pocas**, lo cual es una buena señal en sí misma: no hay un ORM, ni un framework de settings pesado, ni librerías de PDF (el servicio no parsea PDFs, solo chequea el magic — `tests/conftest.py:19-20` lo dice explícitamente).

### 10.B.3 ¿Están fijadas?

| Nivel | ¿Fijado? | Evidencia |
|---|---|---|
| **Dependencias Python** | ✅ **Con hashes** | `uv.lock` versionado; `Dockerfile:18` usa `uv sync --frozen --no-dev` (falla si el lock no coincide); el export de la CI lleva `--hash=sha256:…` por paquete (verificado al correrlo ahora); `pip-audit` se corre con `--require-hashes` (`.github/workflows/ci.yml:83`) |
| **Imagen base de Python** | ✅ **Tag + digest** | `ARG PYTHON_IMAGE=python:3.11-slim@sha256:064f0170…` (`Dockerfile:6`), **la misma en las dos etapas** (`Dockerfile:12`, `:21`), con instrucción de "cambiar el tag y el digest juntos" (`Dockerfile:3-5`) |
| **Imagen de `uv`** | 🟡 Tag fijo, sin digest | `ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.7` (`Dockerfile:7`) |
| **Imagen de Mongo (runtime)** | ✅ **Tag + digest** | `mongo:7.0.45@sha256:8bb44427…` (`docker-compose.yml:25`), con el comando para actualizarla en el comentario (`:24`) y **un test que lo exige**: `tests/test_compose.py:88` |
| **Imagen de Mongo (CI)** | ❌ **Sin digest** | `.github/workflows/ci.yml:42`: `image: mongo:7.0`. **Incoherencia con el punto anterior** |
| **GitHub Actions** | ✅ **Todas por SHA** | `actions/checkout@3d3c42e5…`, `astral-sh/setup-uv@68559e99…`, `docker/setup-buildx-action@f87e5991…`, `docker/login-action@8aa5831f…`, `docker/metadata-action@dc802804…`, `docker/build-push-action@c3c9e263…`, `anchore/scan-action@27805bf3…` (`.github/workflows/ci.yml:27`, `:28`, `:112`, `:115`, `:126`, `:140`, `:160`) |
| **Versión de `uv` en la CI** | ✅ Pinned | `UV_VERSION: "0.13.7"` (`.github/workflows/ci.yml:19`), la misma que el Dockerfile |
| **Versión de las herramientas de escaneo** | ✅ Pinned | `pip-audit==2.10.1` (`.github/workflows/ci.yml:83`) y `GRYPE_VERSION: v0.121.2` (`:105`), con el argumento escrito: "el resultado del escaneo no cambia porque salió una versión nueva del escáner" (`:103-104`) |
| **Paquete Debian parcheado** | ✅ Versión exacta | `zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1` (`Dockerfile:42`), con una justificación de reproducibilidad de 10 líneas (`Dockerfile:28-39`) |

### 10.B.4 Escaneos en la CI y su política de corte

**Dos escáneres complementarios**, y el README explica por qué hacen falta los dos (`README.md:384`): "`pip-audit` mira el lock con sus hashes, Grype mira la imagen entera".

**1. `pip-audit` — job `audit`** (`.github/workflows/ci.yml:72-83`):
```yaml
- run: uv export --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.txt
- run: uvx pip-audit==2.10.1 -r requirements.txt --require-hashes --disable-pip --progress-spinner off
```
- Alcance: **solo lo que va a producción** (`--no-dev`).
- **Política de corte: cero tolerancia.** `pip-audit` sale con código ≠ 0 ante **cualquier** vulnerabilidad conocida, sin umbral de severidad ni excepciones. Es la política más estricta posible, y es razonable porque son 28 paquetes bajo control directo.
- `--require-hashes` + `--disable-pip`: no resuelve nada por su cuenta, audita exactamente lo que está fijado.

**2. Grype — paso dentro del job `image`** (`.github/workflows/ci.yml:158-169`):
```yaml
with:
  image: ${{ env.SCAN_TAG }}
  grype-version: ${{ env.GRYPE_VERSION }}
  only-fixed: true
  severity-cutoff: high
  fail-build: true
```
- Alcance: **la imagen entera**, incluidos los paquetes Debian de la base.
- **Política de corte: `high` o `critical`, y solo con fix disponible.** El README dedica dos párrafos a defender ese umbral y **no** "cero vulnerabilidades" (`README.md:388-391`): una vulnerabilidad sin parche no se arregla desde este repo y "una CI que nadie puede poner en verde deja de informar"; y en una base Debian siempre hay `medium`/`low` abiertas, así que cortar ahí "entrenaría al equipo a mergear con el job rojo". Es un argumento de **operabilidad del control**, y es exactamente lo que hay que saber contestar si un profesor pregunta "¿por qué no cero?".
- **El orden es la parte más fina:** build con `load: true` a un tag local que no se publica → Grype → **solo entonces** el push, que no recompila porque sale de la caché de buildx (`.github/workflows/ci.yml:135-181`, `README.md:386`). Resultado: **se publica exactamente lo que se escaneó**. En un PR también se escanea, sin publicar (`PUSH: ${{ github.event_name != 'pull_request' }}`, `.github/workflows/ci.yml:102`).
- El reporte se escribe **fuera del repo** (`${{ runner.temp }}/grype.json`, `:169`) "para que no sea parte del contexto de build", y el paso `Resumen` deja una tabla de hallazgos **por severidad** con `if: always()` (`:182-212`).

**Gating general:** el job `image` tiene `needs: [tests, integration, compose, audit]` (`.github/workflows/ci.yml:90`): **no se publica nada que no haya pasado los tests, la integración con Mongo, la validación del compose y la auditoría**.

**Otros jobs de calidad:** `tests` (unitarios, `:22-32`), `integration` (contra un service container de Mongo, `:34-58`, con el comentario de por qué no usa el compose: depende de la red externa `mired` que no existe en el runner), `compose` (`config --quiet` en las dos variantes, `:60-70`). Más `concurrency` con `cancel-in-progress` (`:14-16`) y `timeout-minutes` en los cinco jobs.

### 10.B.5 Resultado de correr el escaneo **ahora**

**`pip-audit` (corrido en esta máquina, mismo comando que la CI):**

```
No known vulnerabilities found
```

**Grype: no se pudo correr.** El daemon de Docker no está levantado en esta máquina, así que ni `docker build` ni el escaneo son posibles. Así se correría (está documentado en `README.md:418-443`):

```bash
docker compose build documents                  # deja documents-service:dev
grype documents-service:dev --only-fixed --fail-on high
```

O sin instalar nada (la forma que sirve en Windows), pasándole la imagen **como archivo** en lugar de montar `docker.sock`:

```bash
docker save -o /tmp/doc.tar documents-service:dev
MSYS_NO_PATHCONV=1 docker run --rm -v grype-db:/cache -e GRYPE_DB_CACHE_DIR=/cache \
  -v "$(cygpath -w /tmp/doc.tar)":/image.tar:ro -v "$(cygpath -w "$PWD")":/scan:ro -w /scan \
  anchore/grype:v0.121.2 --only-fixed --fail-on high docker-archive:/image.tar
```

Vale destacar el razonamiento de seguridad de esa receta (`README.md:433`): **no se monta `docker.sock`** porque "con ese socket se puede arrancar otro contenedor privilegiado que monte el disco entero, es decir, root en la máquina… Un contenedor que solo tiene que leer no recibe el socket". Es un ejemplo de mínimo privilegio aplicado a la propia herramienta de seguridad.

**Qué dice el repo que pasa hoy** (`README.md:445-450`): el escaneo pasa **sin ninguna excepción**. Los cuatro `high` que tenía la imagen eran todos de la base, ninguno de `uv.lock`, y se resolvieron **en el Dockerfile**, no con un `.grype.yaml`:

| Hallazgo | De dónde salía | Qué se hizo |
|---|---|---|
| `zlib1g` (`CVE-2026-76844`, `CVE-2026-85091`) | paquete Debian de la base | Se instala la versión con el fix, fijada: `Dockerfile:40-43` |
| `jaraco-context` 5.3.0 (`GHSA-58pv-8j8x-9vj2`) y `wheel` 0.45.1 (`GHSA-8rrh-rw8j-w5fx`) | vendorizados en `setuptools/_vendor/` de la base | Se borran `pip`, `setuptools` y `wheel`: `Dockerfile:49-53` |

Y el `.grype.yaml` que existía mientras se ignoraban **ya no existe** en este checkout (lo borró el commit `9b416d7`, verificado con `git diff HEAD main`). El README documenta el formato de una excepción futura, con `reason` y una fecha de revisión (`README.md:452-463`), y aclara que Grype autodetecta el archivo sin pasarle el input `config` (que apagaría esa autodetección).

### 10.B.6 SBOM, firma de imágenes y actualización automática

| Práctica | ¿Está? | Evidencia / Qué falta |
|---|---|---|
| **SBOM** | ❌ **No está implementado.** No se genera ni se publica ningún SBOM (CycloneDX/SPDX). Grype lo construye internamente para escanear pero **no lo emite** como artefacto: el único `output-file` es el JSON de hallazgos (`.github/workflows/ci.yml:167-169`) | Agregar `anchore/sbom-action` y subirlo como artefacto o attestation |
| **Firma de imágenes** | ❌ **No está implementado.** No hay `cosign`, ni `sign`, ni attestations de procedencia (grep de `cosign`/`sigstore`/`attest` en `.github/`: cero). Lo más parecido a procedencia son los labels OCI `source`, `revision` y `created` (`.github/workflows/ci.yml:124-126`, `README.md:363`), que son **informativos, no verificables criptográficamente** | `cosign sign` con la identidad OIDC del workflow |
| **Actualización automática** | ❌ **No está implementado.** **No hay `.github/dependabot.yml` ni `renovate.json`** (verificado con `git ls-files`: `.github/` contiene solo `workflows/ci.yml`). Las actualizaciones son manuales: hay un commit que lo demuestra, `e53410a build(deps): actualizar certifi, dnspython y pymongo por vulnerabilidades` | `.github/dependabot.yml` para `pip`, `github-actions` y `docker` |

Esto es importante en un oral: la CI **detecta** vulnerabilidades muy bien, pero **nada avisa proactivamente** cuando sale una versión nueva. El ciclo se cierra solo si alguien corre la CI; un repo sin pushes durante dos meses no se enteraría de nada. Y hay una ironía útil para contar: todo está tan bien fijado (digests, SHAs, versiones exactas de Debian) que **sin Dependabot ese pinning se vuelve un congelamiento**.

### 10.B.7 Grado de cumplimiento (dependencias)

**🟡 Cumple parcialmente** — y es un "parcialmente" alto. El pinning es ejemplar (hashes, digests, actions por SHA, escáneres con versión fija), hay **dos escáneres complementarios con políticas de corte argumentadas**, el escaneo bloquea el push, el gating entre jobs es correcto, y el resultado medido hoy es **0 vulnerabilidades**. Falta la mitad "proactiva y verificable": **SBOM, firma de imagen y Dependabot/Renovate**, más la incoherencia del `mongo:7.0` sin digest en la CI.

---

## 11. Posibles preguntas del oral (con respuesta lista)

**1. ¿Por qué `documents-service` es un microservicio y no un módulo del monolito?**
Porque cumple las siete características: tiene una capacidad de negocio concreta (persistir documentos procesados), su propia base (`documents_db`, y se niega a arrancar contra la del monolito — `app/config.py:84-93`), su propia imagen en GHCR con su propio ciclo de release, su propio stack y un contrato HTTP que es el único acoplamiento. Y se despliega solo: el compose no levanta `extract-service` a propósito.

**2. ¿Por qué el circuit breaker no está en el código, si es un tema de la materia?**
Porque **estuvo** y lo borramos en la issue #28. Tenía tres bugs verificados: contaba como fallas los rechazos de su propio bulkhead, contaba los `400` de PDFs corruptos (cinco PDFs rotos abrían el circuito para todos) y un éxito de una llamada vieja cerraba un circuito abierto. Además duplicaba al gateway. Hoy el único breaker es el middleware `extract-cb` de Traefik, declarado en los tags de Consul de extract, y documents solo **traduce** sus respuestas: `app/services/extractor_client.py:125-153`.

**3. ¿Por qué `documents` no reintenta la llamada a extract?**
Porque reenviar un PDF de varios megabytes a un servicio **ya saturado** duplicaría la carga sobre el cuello de botella que el TP pide no empeorar. El retry vive en Traefik y solo cubre pedidos que fallaron **antes** de salir hacia la réplica (conexión rechazada o `dialTimeout`), nunca algo que ya se ejecutó. Así no hay tormenta de reintentos: un intento acá por uno allá, sin multiplicación.

**4. El presupuesto de timeouts dice 30 > 27 > 25. ¿Por qué `documents` (27 s) corta *después* que Traefik (25 s), si la regla es que el de adentro sea menor?**
A propósito. Así el `504` lo da el **gateway**, que sabe qué pasó con la réplica, en lugar de un corte nuestro a ciegas. Está escrito en `app/config.py:50-51` y hay un test que lo fija como invariante: `tests/test_config.py:30`. El margen de 2 s se consume exactamente en un caso borde documentado en `README.md:492`: un dial fallido reintentado más una réplica que no responde.

**5. ¿Cómo garantizan que no se guarde el mismo documento dos veces si hay tres réplicas?**
Con el índice único `ux_documents_checksum` en Mongo (`app/db/mongo.py:40-42`), no con el chequeo previo. El `find_by_checksum` del servicio es por proceso y puede perder la carrera; cuando la pierde, el índice rechaza el insert, el repositorio lo traduce a `DuplicateChecksumError` y el servicio re-lee y devuelve `200` con `duplicate: true` y el mismo `id` (`app/services/document_service.py:72-79`). Está probado contra Mongo real con 20 inserts concurrentes desde dos clientes (`tests/integration/test_mongo_repository.py:108`).

**6. ¿Qué pasa si el mismo PDF llega simultáneamente a dos réplicas?**
Las dos pasan el chequeo de checksum y **las dos llaman a extract**: se paga una extracción de más. El resultado final es correcto — un `201`, el resto `200 duplicate` con el mismo `id` — pero el costo está asumido como límite conocido en `README.md:572`. Evitarlo requeriría un lock distribuido o reservar el checksum antes de extraer, y no se justifica para el TP.

**7. ¿Por qué el `HEALTHCHECK` del contenedor y el check de Consul apuntan a `/health` y no a `/health/ready`?**
Porque `/health/ready` depende de MongoDB. Si el healthcheck del contenedor estuviera atado a la readiness, un parpadeo de 3 s de la base **reiniciaría un proceso perfectamente sano**; y si el check de Consul lo estuviera, nos sacaría del catálogo cuando todavía servimos todas las lecturas y rechazamos las escrituras correctamente. Está argumentado en `Dockerfile:64-66` y `app/services/consul.py:8-10`.

**8. ¿Por qué `POST /documents` lee el body a mano en lugar de usar `UploadFile`?**
Porque con `File(...)` FastAPI parsea **todo** el multipart y vuelca a disco lo que pase de 1 MB **antes** de que corra el endpoint: ningún chequeo de tamaño escrito en el endpoint llega a tiempo. Leyéndolo a mano podemos rechazar por `Content-Length` sin leer nada, tomar el cupo de admisión antes del primer byte, y cortar en `413` apenas el total leído supera el límite — porque el `Content-Length` puede mentir o faltar. Está en `app/api/documents.py:7-10` y `:239-259`.

**9. ¿Escalan con `workers` de uvicorn o con réplicas? ¿Por qué?**
Con réplicas (`DOCUMENTS_REPLICAS` → `deploy.replicas`). Con `workers`, N procesos quedarían detrás de **un solo** registro en Consul y **un solo** health check, y el cupo de uploads —que vive en memoria del proceso— se multiplicaría dentro de un límite de memoria dimensionado para uno. Con réplicas, cada proceso tiene su registro, su check y su límite de memoria. Está en `app/__main__.py:6-14`.

**10. ¿Tienen SAGA? Si no, ¿por qué?**
No, y rigurosamente no aplica. El flujo tiene dos pasos: llamar a extract y escribir en nuestra base. Para que hiciera falta una SAGA, el primer paso tendría que dejar un efecto colateral que haya que compensar, y extract no guarda nada: recibe bytes y devuelve texto. Si la escritura falla no hay nada que "des-extraer", solo CPU gastada. Tendría sentido si el flujo creciera a "guardar **y** indexar **y** notificar", con tres dueños de estado distintos. Hoy el sustituto correcto es idempotencia por checksum más índice único.

**11. ¿Qué pasa si Consul está caído cuando arranca el servicio?**
Arranca igual y atiende igual. El registro corre en una **tarea de fondo** (`app/main.py:124-126`) con backoff exponencial de 1 s a 30 s, y ningún fallo se propaga (`app/services/consul.py:158-177`). El caso real que resuelve es Consul inicializando el ACL unos segundos después del primer intento, que responde `403 "ACL not found"`. Lo que sí pasa es que, sin registro, **Traefik nunca rutea a documents aunque Docker lo muestre `healthy`** — y eso está advertido en `README.md:241`.

**12. ¿Cómo se asegura que la imagen que se publica es la que se escaneó?**
El job `image` hace primero un build con `load: true` a un tag local que no se publica (`documents-service:ci`), después corre Grype sobre esa imagen, y **solo al final** hace el push, que no recompila porque sale de la caché de buildx. Todos los tags terminan apuntando al mismo digest. Si Grype encuentra un `high` o `critical` con fix, el job falla y **no se publica nada** (`.github/workflows/ci.yml:135-181`).

**13. ¿Por qué el umbral de Grype es `high` con `only-fixed` y no "cero vulnerabilidades"?**
Por operabilidad del control. Una vulnerabilidad **sin fix publicado** no se puede arreglar desde este repo, así que bloquear por ella dejaría la CI roja sin nada que hacer, y una CI que nadie puede poner en verde deja de informar. Y en una base Debian siempre hay `medium` y `low` abiertas: cortar ahí entrenaría al equipo a mergear con el job rojo. `high`/`critical` **con fix** es accionable: se sube la base o la dependencia que la trae. De hecho así se resolvieron los cuatro `high` que teníamos, en el Dockerfile, y hoy no hay ninguna excepción.

**14. ¿Qué pasa si hago `DELETE /documents/{id}` sin credenciales?**
Se borra. **No hay autenticación ni autorización en ningún endpoint**, y es un hueco reconocido: el README lo declara fuera de alcance (`README.md:11`). En términos de OWASP API Security es API1 (BOLA) y API5 (BFLA). Lo único que lo mitiga hoy es que nada publica puertos al host y que solo `/documents` está ruteado por el gateway. Si tuviera que cerrarlo, lo más barato sería un middleware de API key en Traefik o un `Depends` de verificación en el router, antes de pensar en usuarios.

**15. ¿Qué cachean?**
Nada. **No hay ninguna caché en el repo.** Lo que sí hay es deduplicación por checksum, que no es lo mismo: evita re-extraer un PDF ya conocido, vive en Mongo (así que funciona entre réplicas y sobrevive a reinicios) y no necesita invalidación porque el contenido es inmutable — el contrato no tiene `PUT`, y el puerto del repositorio omite `update` a propósito por ISP (`app/ports.py:16-18`). La oportunidad que no tomamos es un `ETag` + `Cache-Control: immutable` en `GET /documents/{id}`, que devuelve un recurso inmutable de hasta 15 MiB.

**16. Corro `uv run pytest` en Windows y me fallan dos tests. ¿Está roto el servicio?**
No, y está documentado en `README.md:546`. Los dos son de `tests/test_resilience.py` y prueban que una conexión rechazada da un `503` rápido. En Windows un rechazo a un puerto local tarda ~2 s en volver con `WSAECONNREFUSED`, más que el `EXTRACT_CONNECT_TIMEOUT` de 2 s, así que el cliente corta por timeout (`504`) antes de que llegue el rechazo (`503`). En Linux y en la CI el rechazo es inmediato y pasan. Lo verifiqué: 321 pasan, esos 2 fallan, 14 s.

**17. ¿Por qué hay 323 tests y ninguno necesita MongoDB?**
Porque la capa de servicios depende solo de `Protocol` (`app/ports.py`), nunca de Motor ni de httpx, y todos los colaboradores se construyen en un único composition root que acepta reemplazos por keyword (`app/main.py:43-59`). Así los dobles en memoria entran por la puerta de adelante. Los 12 tests que **sí** necesitan Mongo real están aparte, excluidos por default (`pyproject.toml:26`), y cubren lo que un doble no puede probar honestamente: que el índice único rechaza de verdad, que los timestamps vuelven con zona horaria y que un `content` de 17 MiB termina en error de dominio y no en un `500`.

---

## 12. Sugerencias de mejora

### 12.1 Tabla completa, de mayor a menor recomendación

| # | Tema | Problema detectado (con evidencia) | Sugerencia concreta | Esfuerzo | Recom. | Justificación del porcentaje |
|---|---|---|---|---|---|---|
| 1 | Higiene de repo | El working tree está en `feat/50-imagen-base-parcheada` con HEAD en `9b416d7`; `origin/main` está **7 commits adelante** (`07f8892`) y agrega el re-registro en Consul (`app/services/consul.py` +82, `tests/test_consul.py` +200) | `git checkout main && git pull`. Mostrar y defender `main`, no una rama de feature vieja | Bajo | **95 %** | Riesgo real en el oral: se defendería código sin un fix ya mergeado, y una pregunta sobre el re-registro en Consul no tendría respaldo en el código mostrado. Costo: un comando |
| 2 | Dependencias / 12-Factor X | `.github/workflows/ci.yml:42` usa `mongo:7.0` **sin digest**, mientras `docker-compose.yml:25` lo fija por tag+digest y `tests/test_compose.py:88` **exige** que esté fijado. Incoherencia interna | Fijar en la CI el mismo `mongo:7.0.45@sha256:8bb44427…` del compose | Bajo | **90 %** | Es exactamente el tipo de incoherencia que un profesor detecta leyendo dos archivos seguidos, y el repo ya tiene el test que prueba que el equipo sabe que hay que fijarlo. Una línea |
| 3 | Vulnerabilidades en dependencias | **No existe `.github/dependabot.yml` ni `renovate.json`** (`git ls-files` → `.github/` solo tiene `workflows/ci.yml`). Todo el pinning (digests, SHAs, `zlib1g` exacto) se vuelve congelamiento sin nada que avise de versiones nuevas | Agregar `.github/dependabot.yml` para `pip`, `github-actions` y `docker` | Bajo | **85 %** | "Actualización automática" está explícitamente en la lista de temas de la cátedra y hoy es un ❌ liso. Cierra el ciclo detectar→actualizar con ~15 líneas de YAML |
| 4 | Rate Limit | `documents` **no declara ningún middleware** en Traefik (`app/services/consul.py:70-84`) y hay un test que lo fija como invariante (`tests/test_consul.py:421`). El servicio nunca emite `429` | Agregar un tag `rateLimit` al router de documents, con su test | Bajo | **80 %** | Rate Limit es uno de los cinco patrones que la cátedra pide evaluar y hoy es ❌. La mecánica (tags → Traefik) ya está dominada en el repo: son 3 tags más y un test |
| 5 | Resiliencia / documentación | **SAGA no se menciona en ningún archivo del repo** (grep en `README.md`, `app/`, `tests/` → cero) | Agregar al README un párrafo "SAGA: por qué no aplica acá" con el argumento de que extract no deja estado compensable, y dónde sí aplicaría | Bajo | **78 %** | Es un tema del programa que hoy no tiene **nada** escrito. Un párrafo convierte un ❌ en una decisión de diseño defendida. Costo: 10 líneas de README |
| 6 | Desarrollo seguro / supply chain | **No se genera SBOM.** Grype lo construye internamente pero el único `output-file` es el JSON de hallazgos (`.github/workflows/ci.yml:167-169`) | Agregar `anchore/sbom-action` al job `image` y subir el SBOM como artefacto de la corrida | Bajo | **75 %** | "Mencioná SBOM" está en el enunciado del tema 8. El job `image` ya construye y escanea la imagen: agregar el SBOM son ~8 líneas y queda un artefacto mostrable |
| 7 | Cache | **No hay ninguna caché** (grep de `cache` en `app/` → solo una variable borrada). `GET /documents/{id}` devuelve un recurso **inmutable** de hasta 15 MiB sin `ETag` ni `Cache-Control` (`app/api/documents.py:151-162`) | `ETag` (el checksum) + `Cache-Control: public, max-age=…, immutable` y soporte de `If-None-Match` → `304` | Bajo-medio | **72 %** | Cache es otro de los cinco patrones pedidos y hoy es ❌. El caso es de libro (recurso inmutable, sin `PUT`), el checksum ya es el ETag natural, y se defiende solo: "la invalidación es trivial porque el contenido es inmutable" |
| 8 | 12-Factor IX (disposability) | El compose **no define `stop_grace_period`** (`docker-compose.yml:45-122`), así que Docker manda `SIGKILL` a los 10 s por defecto, mientras un `POST /documents` en vuelo puede esperar hasta `EXTRACT_TIMEOUT=27 s` | Agregar `stop_grace_period: 30s` al servicio `documents` | Bajo | **70 %** | Convierte el único 🟡 "operativo" del 12-Factor en ✅ con una línea, y es un apagado *graceful* que hoy no es graceful: se cortan pedidos en vuelo |
| 9 | Comunicación / contratos | El contrato con extract se verifica contra un fixture **copiado a mano** (`tests/contracts/extract_response.json`); nada detecta la deriva si extract cambia su schema (`tests/contracts/test_extract_contract.py:1-11` lo admite) | Un job opcional que baje el `openapi.json` de extract y compare el schema de `ExtractResponse` con el fixture | Medio | **70 %** | "Contratos y su verificación (tests de contrato)" está explícito en el tema 2. Hoy los tests son buenos pero **no son consumer-driven verificados**: una pregunta directa sobre eso hoy se contesta con "confiamos en actualizar el fixture a mano" |
| 10 | Desarrollo seguro / authn-authz | **Ningún endpoint pide credenciales.** `DELETE /documents/{id}` (`app/api/documents.py:185-195`) y `GET` del `content` completo están abiertos. OWASP A01/A07, API1/API2/API5 | Una API key por header, validada en un middleware o un `Depends`, con el valor por entorno; o el middleware `basicAuth`/`forwardAuth` de Traefik vía tags | Medio | **60 %** | Es el hueco más grande, pero el sistema no tiene noción de usuario y el README lo declara fuera de alcance. Para el oral alcanza con **saber explicarlo** como arriba (pregunta 14); implementarlo medio bien podría abrir más preguntas que las que cierra |
| 11 | Circuit Breaker | Nada protege la llamada a Mongo: con la base caída, **cada** pedido paga el `MONGO_TIMEOUT_MS` completo (3 s, `app/config.py:41`) antes de su `503`, y `/health/ready` ya sabe que está caída (`app/api/health.py:33-39`) pero nadie usa ese estado | Un fast-fail simple: cachear el último `ping` fallido por N segundos y responder `503` sin ir a Mongo | Medio | **55 %** | Es un hueco real y se nota al explicar el tema 7, pero el cupo de admisión (4 por réplica) ya acota el daño. Alcanza con poder decir "está acotado por el semáforo, y acá pondría un fast-fail" |
| 12 | Réplicas / pruebas de carga | **No hay ningún script de carga en este repo** (`README.md:580-582`, decisión explícita), pero el README cita números propios de `POST /documents` (596 `201`, 904 `503`, `README.md:15`) que no se pueden reproducir desde acá | Un script mínimo (k6 o Vegeta) que mida `POST /documents` con 1 vs. 3 réplicas, aunque sea solo para respaldar la sección de escalado | Medio | **50 %** | Da evidencia propia para el tema 6 y para los números citados, pero el README ya justifica la ausencia y el riesgo de contaminar el benchmark de extract es real. Es "lindo tener", no "hay que hacerlo" |
| 13 | Dependencias | Solo se escanea la imagen que construye este repo; la de Mongo "es de terceros y no la construye nadie acá" (`README.md:465`) | Agregar un paso de Grype sobre `mongo:7.0.45@sha256:…` en modo informativo (sin `fail-build`) | Bajo | **40 %** | Suma visibilidad y es barato, pero no es accionable: si Mongo tiene un `high`, la única respuesta es subir de versión, que es lo que haría Dependabot (sugerencia #3). Mejor explicarlo que implementarlo |
| 14 | Supply chain | **No hay firma de imagen**: no hay `cosign` ni attestations (grep en `.github/` → cero). Los labels OCI (`.github/workflows/ci.yml:124-126`) son informativos, no verificables | `cosign sign` con la identidad OIDC del workflow + verificación en el despliegue | Medio | **30 %** | Imprescindible en producción real, pero acá el despliegue es `docker compose up` en una máquina del equipo: no hay nadie verificando firmas. Es un "lo haría así si fuera producción" perfecto para el oral |
| 15 | Desarrollo seguro / TLS | Todo es HTTP plano: `MONGO_URI=mongodb://` (`app/config.py:39`), `EXTRACT_URL=http://` (`:48`), `CONSUL_HTTP_ADDR=http://` (`:75`), y el token de ACL **viaja en claro** en un header | TLS en el entrypoint de Traefik, y mTLS entre servicios | Alto | **25 %** | Toda la comunicación vive dentro de una red Docker de una sola máquina, sin puertos publicados. Certificados, rotación y confianza mutua son mucho trabajo para un riesgo que en este despliegue es bajo. Explicarlo, no implementarlo |
| 16 | Observabilidad | **No hay métricas, tracing ni alertas.** Solo logs estructurados con `request_id` (`app/logging_config.py:50-64`). Curiosidad: `opentelemetry-api` ya está en el árbol, pero como transitiva de FastAPI | `prometheus-fastapi-instrumentator` para `/metrics`, u OpenTelemetry con exportador a un collector | Medio-alto | **20 %** | La "observación" es una de las desventajas de microservicios que el tema 1 pide analizar, pero alcanza con **nombrarla como costo asumido**. Montar Prometheus + Grafana para el TP es infraestructura que nadie va a mirar |

### 12.2 Cómo implementar las sugerencias de 70 % o más

---

#### #1 — Actualizar el checkout a `main` (95 %)

No se toca ningún archivo; es solo estado de git.

```bash
cd documents-service
git checkout main
git pull
git log --oneline -3     # debe mostrar 07f8892 Merge pull request #53 …
uv run pytest -q         # re-verificar (en Windows, los 2 de test_resilience siguen fallando: es esperado)
```

Después de eso, la sección 6 de este informe (réplicas) cambia a favor: `app/services/consul.py` pasa a tener `is_registered` / `has_service` y `keep_registered`, que **vuelven a registrar la réplica si Consul borra su entrada**. Vale repasar el nuevo `tests/test_consul.py` antes del oral, porque es un argumento fuerte para la pregunta 11.

---

#### #2 — Fijar Mongo por digest también en la CI (90 %)

**Archivo:** `.github/workflows/ci.yml`

```diff
   integration:
     name: Integración con MongoDB
     runs-on: ubuntu-24.04
     timeout-minutes: 10
-    # Service container y no el docker-compose.yml: el compose depende de la
-    # red externa "mired", que no existe en el runner.
+    # Service container y no el docker-compose.yml: el compose depende de la
+    # red externa "mired", que no existe en el runner.
+    # Misma imagen que el compose, por tag + digest (issue #47): si acá
+    # quedara "mongo:7.0" pelado, la CI podría estar probando contra una
+    # build distinta de la que corre en el compose.
     services:
       mongo:
-        image: mongo:7.0
+        image: mongo:7.0.45@sha256:8bb4442761a63a8dda452744082cdac9644b429807eb977b42324a63cef59ca4
         ports:
           - 27017:27017
```

El digest es exactamente el de `docker-compose.yml:25`. Para mantenerlos sincronizados, lo honesto es dejar el comentario que ya existe en el compose (`docker buildx imagetools inspect mongo:7.0 --format '{{json .Manifest.Digest}}'`) y actualizar los dos juntos. Opcionalmente se puede agregar un test que verifique que coinciden, en la línea de `tests/test_compose.py:88`.

---

#### #3 — Dependabot (85 %)

**Archivo nuevo:** `.github/dependabot.yml`

```yaml
# Actualizaciones automáticas. Todo en este repo está fijado (uv.lock con
# hashes, imágenes por digest, actions por SHA): sin esto, ese pinning se
# vuelve congelamiento y nadie se entera de una versión nueva con fix.
version: 2
updates:
  # Dependencias Python del uv.lock. Complementa al job "audit" (pip-audit):
  # pip-audit avisa de lo vulnerable que YA está instalado; esto propone subir.
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 5
    commit-message:
      prefix: "build(deps)"

  # Las actions están fijadas por SHA: sin Dependabot hay que buscar el SHA
  # nuevo a mano cada vez.
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
    commit-message:
      prefix: "ci(actions)"

  # La imagen base del Dockerfile y el mongo del compose, los dos por digest.
  - package-ecosystem: "docker"
    directories:
      - "/"
    schedule:
      interval: "weekly"
    commit-message:
      prefix: "build(docker)"
```

Dos notas para defenderlo en el oral: (a) `package-ecosystem: "uv"` es el que entiende `uv.lock`; si la versión de Dependabot del día no lo soportara, el fallback es `"pip"` sobre `pyproject.toml`. (b) Cada PR de Dependabot pasa por la CI completa (`tests`, `integration`, `compose`, `audit` y el build+Grype del job `image` en modo PR), así que **ninguna actualización entra sin escanear** — eso es lo que hace que la combinación pinning + Dependabot sea segura y no temeraria.

---

#### #4 — Rate limit para `documents` en Traefik (80 %)

**Archivo:** `app/services/consul.py` — función `traefik_tags()` (hoy `:70-84`)

```python
def traefik_tags() -> tuple[str, ...]:
    """Tags con los que Traefik publica este servicio (issue #27)."""
    router = f"traefik.http.routers.{SERVICE_NAME}"
    rate_limit = f"traefik.http.middlewares.{SERVICE_NAME}-rl.rateLimit"
    return (
        "traefik.enable=true",
        f"{router}.rule=PathPrefix(`/{SERVICE_NAME}`)",
        f"{router}.entrypoints=web",
        # Rate limit por IP de cliente (el default de Traefik es ipStrategy):
        # acota pedidos por tiempo, que es algo distinto del control de
        # admisión de uploads (MAX_CONCURRENT_UPLOADS), que acota trabajo
        # concurrente. Los dos son necesarios: uno frena al cliente abusivo,
        # el otro protege la memoria del proceso.
        f"{rate_limit}.average={RATE_LIMIT_AVERAGE}",
        f"{rate_limit}.burst={RATE_LIMIT_BURST}",
        f"{router}.middlewares={SERVICE_NAME}-rl",
    )
```

Con las constantes arriba del módulo, junto a `CHECK_INTERVAL`:

```python
#: Rate limit por IP que Traefik aplica a /documents. average = pedidos por
#: segundo sostenidos; burst = cuántos se toleran de golpe. Traefik responde
#: 429 cuando se supera, que es el código que documents no emite por su cuenta.
RATE_LIMIT_AVERAGE = 20
RATE_LIMIT_BURST = 40
```

**Tests a tocar:** `tests/test_consul.py:421` (`test_documents_no_declara_middlewares_en_traefik`) **deja de ser válido** y hay que reemplazarlo, no borrarlo:

```python
def test_documents_declara_solo_el_rate_limit_como_middleware():
    """Rate limit sí; circuit breaker, retry e inFlightReq siguen siendo de
    extract (issue #28)."""
    tags = traefik_tags()

    assert any("rateLimit.average" in tag for tag in tags)
    assert f"traefik.http.routers.documents.middlewares=documents-rl" in tags
    assert not any("circuitBreaker" in tag or "retry" in tag or "inFlightReq" in tag
                   for tag in tags)
```

Y actualizar las dos tablas del README que afirman lo contrario: `README.md:285` ("A diferencia de extract, documents **no** declara middlewares") y `README.md:472`.

---

#### #5 — Documentar por qué SAGA no aplica (78 %)

**Archivo:** `README.md`, dentro de la sección `## Resiliencia`, después de la tabla de patrones (hoy `:488-497`).

```markdown
**SAGA: por qué no hay orquestador.** `POST /documents` toca dos recursos —
`extract-service` (remoto) y la base propia (local) — pero **no es una
transacción distribuida**: el paso remoto no deja estado que haya que
compensar. `extract-service` recibe bytes y devuelve texto; no guarda nada.
Si la escritura en Mongo falla después de una extracción exitosa, no hay nada
que "des-extraer": el costo es CPU gastada, no estado inconsistente. Por eso
el orden es deliberado — **primero el paso remoto, después el local** —, así
el único paso que puede fallar al final es el que está en la propia base,
donde la atomicidad la da Mongo a nivel documento.

Haría falta una SAGA si el flujo creciera a "guardar el documento **y**
indexarlo en un buscador **y** notificar a un tercero": tres escrituras en
tres dueños distintos, sin transacción posible, con un `DELETE` del documento
como compensación del primer paso. Hoy, con un solo dueño de estado, un
orquestador sería complejidad sin beneficio (YAGNI). El sustituto correcto
para este caso es lo que efectivamente hay: **idempotencia por clave de
negocio** (el checksum) más el índice único que decide las carreras — ver
[Idempotencia](#resiliencia) y `tests/integration/test_mongo_repository.py`.
```

Conviene además sumar una fila a la tabla de `## Patrones del apunte y dónde están en el código` (`README.md:515-533`):

```markdown
| SAGA con orquestador | **No aplica**: el paso remoto no deja estado compensable. Ver "SAGA: por qué no hay orquestador" | — |
```

---

#### #6 — SBOM en la CI (75 %)

**Archivo:** `.github/workflows/ci.yml`, en el job `image`, **entre** el build local y el escaneo de Grype.

```yaml
      # SBOM de la imagen que se va a escanear y publicar: el inventario de lo
      # que hay dentro (paquetes Debian de la base + el venv). Complementa a
      # los hallazgos de Grype, que dicen qué está mal pero no qué hay.
      # Formato SPDX JSON, que es el que GitHub entiende nativamente.
      - name: SBOM de la imagen
        uses: anchore/sbom-action@<SHA de la release v0.20.x>  # fijar por SHA, como el resto
        with:
          image: ${{ env.SCAN_TAG }}
          format: spdx-json
          artifact-name: documents-service-sbom.spdx.json
          output-file: ${{ runner.temp }}/sbom.spdx.json
```

Tres cosas que importan para defenderlo:
- **Va antes de Grype y sobre `SCAN_TAG`**, es decir sobre la imagen local que todavía no se publicó: el SBOM describe exactamente lo que se escanea y lo que después se empuja (que sale de la misma caché).
- **`output-file` en `runner.temp`**, no en el repo — misma razón que el reporte de Grype (`.github/workflows/ci.yml:168`): no tiene que ser parte del contexto de build.
- **Fijar la action por SHA**, como las otras siete del workflow (`.github/workflows/ci.yml:27`, `:160`, etc.). Es coherencia con el criterio que el repo ya aplica, y es justamente la clase de cosa que Dependabot (sugerencia #3) mantendría.

Opcional, para el paso `Resumen`: agregar al `$GITHUB_STEP_SUMMARY` la cantidad de paquetes del SBOM, con `jq '.packages | length'`.

---

#### #7 — `ETag` + `Cache-Control` en `GET /documents/{id}` (72 %)

**Archivo:** `app/api/documents.py`, endpoint `get_document` (hoy `:151-162`)

```python
#: Un documento es inmutable: el contrato no tiene PUT y el checksum identifica
#: su contenido. Por eso la caché puede ser agresiva y no hace falta
#: invalidación: la única forma de que un id deje de ser válido es el DELETE,
#: y ahí el cliente recibe 404 en el próximo GET condicional.
DOCUMENT_CACHE_CONTROL = "public, max-age=3600, immutable"


@router.get(
    "/{document_id}",
    response_model=DocumentDetail,
    responses={
        304: {"description": "El cliente ya tiene esta versión (If-None-Match)"},
        404: _error("Documento no encontrado"),
        **_DATABASE_DOWN,
    },
    summary="Documento completo",
)
async def get_document(
    document_id: str,
    request: Request,
    response: Response,
    service: DocumentService = Depends(get_document_service),
) -> DocumentDetail | Response:
    document = await service.get(document_id)
    # El checksum ES el ETag: identifica el contenido, es estable y ya está
    # calculado. Fuerte (sin W/), porque es byte a byte del PDF de origen.
    etag = f'"{document.checksum}"'

    if request.headers.get("if-none-match") == etag:
        # 304 sin cuerpo: lo que se ahorra son los hasta 15 MiB de `content`.
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": DOCUMENT_CACHE_CONTROL},
        )

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = DOCUMENT_CACHE_CONTROL
    return DocumentDetail.model_validate(document)
```

**Tests a agregar** en `tests/test_api_documents.py`, clase `TestRead`:

```python
async def test_the_detail_carries_an_etag_and_cache_control(self, make_client):
    async with make_client() as client:
        created = (await client.post("/documents", files=upload())).json()
        response = await client.get(f"/documents/{created['id']}")

    assert response.headers["etag"] == f'"{created["checksum"]}"'
    assert "immutable" in response.headers["cache-control"]

async def test_a_matching_if_none_match_answers_304_without_the_content(self, make_client):
    async with make_client() as client:
        created = (await client.post("/documents", files=upload())).json()
        etag = (await client.get(f"/documents/{created['id']}")).headers["etag"]
        response = await client.get(
            f"/documents/{created['id']}", headers={"If-None-Match": etag}
        )

    assert response.status_code == 304
    assert not response.content
```

**Y el README:** agregar la fila a la tabla de contrato (`README.md:50`) con el `304`, y una fila a la tabla de patrones (`README.md:515-533`): `| Cache (HTTP condicional) | ETag = checksum + Cache-Control immutable en app/api/documents.py | tests/test_api_documents.py |`. Ojo: el `304` es un cambio de contrato, así que corresponde una entrada en la tabla de "Cambio de contrato" del README como las de las issues #28 y #29.

---

#### #8 — `stop_grace_period` (70 %)

**Archivo:** `docker-compose.yml`, servicio `documents` (junto a `restart`, hoy `:122`)

```diff
       resources:
         limits:
           cpus: "${DOCUMENTS_CPUS:-1.0}"
           memory: ${DOCUMENTS_MEMORY:-512M}
+    # 12-Factor IX: apagado graceful de verdad. El default de Docker es
+    # SIGKILL a los 10 s, y un POST /documents en vuelo puede estar esperando
+    # hasta EXTRACT_TIMEOUT (27 s) a extract: con 10 s ese pedido se corta a
+    # la mitad. 30 s le da margen al lifespan para terminar lo que tiene,
+    # desregistrarse de Consul y cerrar los clientes (app/main.py:143-161).
+    stop_grace_period: 30s
     restart: unless-stopped
```

**Test a agregar** en `tests/test_compose.py`, en la línea de los que ya hay:

```python
def test_el_apagado_espera_mas_que_el_timeout_hacia_extract(tmp_path):
    """Un pedido en vuelo puede durar EXTRACT_TIMEOUT (27 s): el SIGKILL no
    puede llegar antes."""
    documents = compose_config(tmp_path)["services"]["documents"]

    # Compose normaliza a nanosegundos.
    assert documents["stop_grace_period"] >= 28_000_000_000
```

Verificar el formato que devuelve `docker compose config --format json` en la máquina antes de fijar la aserción: según la versión puede salir como `"30s"` o como entero en nanosegundos.

---

#### #9 — Detectar la deriva del contrato con extract (70 %)

La idea es que el fixture deje de ser un acto de fe. **Dos variantes, de menor a mayor fidelidad:**

**Variante A (más simple, sin red en el test): un job de CI que compare el schema.**

**Archivo:** `.github/workflows/ci.yml`, job nuevo

```yaml
  contrato:
    name: Contrato con extract-service
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    # continue-on-error: la deriva del contrato ajeno no tiene que bloquear un
    # merge de este repo; tiene que AVISAR. Si extract cambia su schema, el
    # job queda amarillo y hay que actualizar el fixture a conciencia.
    continue-on-error: true
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Bajar el OpenAPI de extract-service
        run: |
          curl -sSfL -o extract-openapi.json \
            https://raw.githubusercontent.com/juandelosrios124/extract-service/main/openapi.json
      - name: Comparar ExtractResponse con el fixture
        run: |
          python - <<'PY'
          import json, sys
          schema = json.load(open("extract-openapi.json"))
          props = schema["components"]["schemas"]["ExtractResponse"]["properties"]
          fixture = json.load(open("tests/contracts/extract_response.json"))
          if set(props) != set(fixture):
              sys.exit(
                  f"Deriva de contrato: extract declara {sorted(props)}, "
                  f"el fixture tiene {sorted(fixture)}"
              )
          print("El fixture coincide con ExtractResponse de extract-service")
          PY
```

Eso requiere que `extract-service` publique su `openapi.json` en el repo (hoy el README de documents muestra cómo **generarlo** desde una réplica, `README.md:182-184`, pero no dice que esté versionado allá). Si no lo está, la alternativa es levantar extract en el job, que es mucho más caro.

**Variante B (la correcta de verdad): un contrato compartido.** Mover el schema a un archivo que **los dos repos** consuman — por ejemplo que extract-service lo publique como artefacto de release y documents lo baje y lo versione, con el job de arriba verificando que la copia local está al día. Eso es *consumer-driven contract testing* sin montar Pact.

**En el README**, además, conviene ser explícito sobre la limitación actual, que hoy solo está en el docstring del test (`tests/contracts/test_extract_contract.py:4-6`):

```markdown
**Deriva del contrato.** `tests/contracts/extract_response.json` es una copia
del `ExtractResponse` de extract-service, mantenida a mano. El job `contrato`
avisa (en amarillo, sin bloquear) si extract cambió su schema y la copia quedó
vieja. Mientras no exista un schema compartido entre los dos repos, esa es la
única red: los tests de `tests/contracts/` validan que **nuestro parser**
acepta el contrato que creemos que extract tiene, no el que extract tiene hoy.
```

---

*Fin del informe.*
