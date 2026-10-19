# Herramientas: Make, hooks, guardas y por qué

**Qué vas a aprender.** Antes de escribir una sola línea de la aplicación, AdmitLens preparó el repositorio: un `Makefile` en la raíz y otro en `level-1/backend`, tres hooks de git, un motor de guardas propio en `tools/guards`, gitleaks fijado por hash, un `make env` que genera las contraseñas locales, PostgreSQL 16 en Docker Compose y dos workflows de CI. Esta nota recorre cada pieza con el código real: qué problema resuelve, qué alternativa se descartó y qué decisión del proyecto la fija. Al terminar deberías poder explicar cada archivo de la raíz línea por línea y llevarte una lista de buenas prácticas para tu próximo repositorio mid+.

**Para quién.** Un backend junior que sabe Python y git básico, y que nunca configuró un repo público con guardas contra fugas.

**Cómo leerla.** La sección 1 es el mapa. De la 2 a la 8 hay una sección por pieza, siempre con los mismos cinco bloques:

| Bloque | Qué responde |
| --- | --- |
| **Qué es** | La idea en dos o tres frases |
| **El problema que resuelve** | Qué se rompe si no existe |
| **En AdmitLens** | El archivo, el símbolo y la decisión que lo fija |
| **Herramienta** | Con qué se implementa |
| **Para muchos usuarios** | Qué cambia cuando el equipo o el sistema crecen |

> **Sobre las referencias.** Las decisiones del proyecto se citan por su identificador (por ejemplo, ADR-ADLN-0007 o PLAN-CA-0005 §6.5). Esos documentos viven en las notas privadas del proyecto; aquí se cuenta lo que deciden, para que la nota se entienda sola. El protocolo de trabajo completo está en [00 — Protocolo de desarrollo](../00-protocolo-de-desarrollo.md).

## Contenido

1. [El mapa](#1-el-mapa)
2. [Make, la única puerta](#2-make-la-única-puerta)
3. [Los hooks de git](#3-los-hooks-de-git)
4. [El motor de detección: tools/guards](#4-el-motor-de-detección-toolsguards)
5. [gitleaks, fijado por sha256](#5-gitleaks-fijado-por-sha256)
6. [make env: los secretos locales](#6-make-env-los-secretos-locales)
7. [PostgreSQL 16 en compose](#7-postgresql-16-en-compose)
8. [CI: los dos workflows](#8-ci-los-dos-workflows)
9. [Cómo encaja todo](#9-cómo-encaja-todo)
10. [Buenas prácticas para un repo mid+](#10-buenas-prácticas-para-un-repo-mid)
11. [Anti-patrones y patrones](#11-anti-patrones-y-patrones)
12. [Preguntas de repaso](#12-preguntas-de-repaso)
13. [Para practicar](#13-para-practicar)

---

## 1. El mapa

```mermaid
%%{init: {"theme": "default", "themeVariables": {"fontSize": "14px"}, "flowchart": {"useMaxWidth": true, "nodeSpacing": 25, "rankSpacing": 35, "padding": 10}}}%%
flowchart TB
    DEV["Tú"] -->|"make setup · make doctor"| ROOT["Makefile de la raíz<br/>contrato KR"]
    DEV -->|"git commit · git push"| HOOKS[".githooks/<br/>pre-commit · commit-msg · pre-push"]
    HOOKS -->|"make hook-*"| ROOT
    ROOT -->|"python -m guards MODO"| G["tools/guards<br/>rutas · contenido · identidad<br/>mensajes · lint"]
    G -->|"gitleaks dir · stdin"| GL["gitleaks 8.30.1<br/>fijado por sha256"]
    ROOT -->|"pre-push"| LC["level-checks.sh<br/>make check por nivel"]
    LC --> L1["level-1/backend/Makefile<br/>contrato KM"]
    DEV -->|"make env · make db-up"| L1
    L1 --> ENV[".env<br/>modo 600, nunca versionado"]
    L1 --> DB[("PostgreSQL 16<br/>127.0.0.1 · UTC")]
    CI["GitHub Actions<br/>guards.yml · level-1-ci.yml"] -->|"solo make"| ROOT
    CI -->|"solo make"| L1
```

| Pieza | Problema que resuelve | Dónde | Quién lo decide |
| --- | --- | --- | --- |
| `Makefile` de la raíz | Que tú y CI corran lo mismo, sin secretos en variables | [`Makefile`](../../Makefile) | ADR-ADLN-0007 §E7.4 |
| `Makefile` de nivel | Un solo vocabulario de verbos por nivel | [`level-1/backend/Makefile`](../../level-1/backend/Makefile) | PLAN-CA-0005 §6.3 (contrato KM), ADR-ADLN-0010 §2.5 |
| Hooks | Prevenir **antes** de que el commit exista o salga | [`.githooks/`](../../.githooks/pre-commit) | ADR-ADLN-0007 §E7.4 y §E7.9 |
| `tools/guards` | Lo que gitleaks no ve: rutas, identidad, mensajes, merges, lint | [`tools/guards/guards/`](../../tools/guards/guards/cli.py) | PLAN-CA-0005 §6.1 |
| gitleaks fijado | Formatos de secreto conocidos, con un binario que no cambia solo | [`tools/guards/bin/`](../../tools/guards/bin/install-gitleaks.sh), [`.gitleaks.toml`](../../.gitleaks.toml) | ADR-ADLN-0007 §2.4 |
| `make env` | Contraseñas locales generadas, nunca escritas a mano ni versionadas | [`level-1/backend/ops/make-env.sh`](../../level-1/backend/ops/make-env.sh) | ADR-ADLN-0010 §2.5 |
| Compose de Postgres | El mismo motor que los niveles 2 a 4, solo en loopback y en UTC | [`level-1/backend/docker-compose.yml`](../../level-1/backend/docker-compose.yml) | ADR-ADLN-0010 §2.1 y §2.2 |
| CI | Detectar lo que se escapó y repetir las comprobaciones en limpio | [`.github/workflows/`](../../.github/workflows/guards.yml) | ADR-ADLN-0007 §2.4, PLAN-CA-0005 §6.3 (KX) |

**Tres reglas atraviesan todo:**

1. **Make es la única puerta.** Nada se ejecuta «a mano», ni en tu máquina ni en CI.
2. **Una guarda detecta; nunca redacta.** Informa regla, archivo y línea, jamás el valor.
3. **Ante la duda, falla cerrado.** Si falta una herramienta o algo no cuadra, el código de salida es 2 y la operación se bloquea.

---

## 2. Make, la única puerta

### Qué es

GNU Make lee un `Makefile` con **objetivos** (`setup:`), sus **dependencias** (`setup: hooks`) y una **receta**: las líneas con tabulador que ejecuta el shell. Un objetivo `.PHONY` no es un archivo, es un verbo: `make setup` significa «ejecuta el verbo setup».

### El problema que resuelve

- **Deriva entre tu máquina y CI.** Si CI ejecuta `uv run pytest -q` y tú `pytest`, un día uno pasa y el otro no, y nadie sabe por qué. Con Make, el workflow solo dice `run: make guards-test`: lo que corre CI es exactamente lo que corres tú.
- **Secretos expandidos por Make.** Si una receta usa `$(POSTGRES_PASSWORD)`, Make sustituye el valor **antes** de llamar al shell. `make -n` (que imprime las recetas sin ejecutarlas) lo imprime en la terminal o en el log de CI.

### En AdmitLens: el `Makefile` de la raíz (contrato KR)

Los objetivos. Los públicos son los que lista `make help`; los `hook-*` no aparecen ahí, porque no llevan comentario `##`:

| Objetivo | Qué hace | Estado |
| --- | --- | --- |
| `help` | Lista los objetivos con su descripción (`## …`) | real |
| `setup` | Depende de `hooks`; instala gitleaks fijado, sincroniza el entorno de `tools/guards` y escribe la configuración local de git | real |
| `hooks` | `git config --local core.hooksPath .githooks` y `chmod +x` de los tres hooks | real |
| `doctor` | Comprueba que tu clon esté preparado ([§4](#make-doctor)) | real |
| `secrets-scan` | Árbol de trabajo más toda la historia ([§4](#make-secrets-scan)) | real |
| `guards-test` | La suite de `tools/guards` (679 tests, unos 116 s) | real |
| `lint` | Lint de Makefiles, `.gitignore`, workflows, `.gitattributes` y `.gitleaks.toml` | real |
| `parity` | Contrato congelado (ADR-ADLN-0007 §E7.7); sale con rc 2: «implemented in L2-P1» | reservado |
| `datagen` | Construcción de los datos semilla; rc 2: «implemented in L1-P5» | reservado |
| `hook-pre-commit`, `hook-commit-msg`, `hook-pre-push` | Internos: los llaman los hooks | real |

Las líneas que sostienen el diseño, en [`Makefile`](../../Makefile):

```make
SHELL := /bin/sh

# Simple (:=) assignments win over the environment, so an inherited GUARDS_DIR or
# GITLEAKS cannot redirect the hooks; only an explicit command-line override can.
GUARDS_DIR := tools/guards
GITLEAKS := .tools/bin/gitleaks

GUARDS = PYTHONPATH="$(GUARDS_DIR)" GUARDS_GITLEAKS="$(GITLEAKS)" uv run --quiet --locked --project "$(GUARDS_DIR)" python -m guards

.DEFAULT_GOAL := help
```

Línea por línea:

- **`SHELL := /bin/sh`**: las recetas corren en POSIX `sh`, igual en macOS y en Ubuntu. Nada de bash.
- **`:=`, `=` y `?=`**: `:=` (simple) expande el valor una sola vez, al leer la línea; `=` (recursiva) lo expande cada vez que se usa. Las dos tienen algo en común que aquí importa: en Make, una asignación del archivo **gana a una variable de entorno con el mismo nombre**. Si alguien exporta `GITLEAKS=/tmp/falso`, los hooks siguen usando `.tools/bin/gitleaks`; solo un `make GITLEAKS=…` explícito en la línea de comandos lo cambia (lo prueba `test_tool_paths_ignore_the_environment_but_honour_the_command_line` en [`test_make_no_leak.py`](../../tools/guards/tests/test_make_no_leak.py)). `?=` haría lo contrario: solo asigna si la variable no existe, así que el entorno ganaría.
- **`GUARDS`**: la forma única de llamar al motor. `uv run --locked` se niega a correr si `uv.lock` no coincide con `pyproject.toml`: la herramienta de seguridad corre siempre con las versiones bloqueadas.
- **`.DEFAULT_GOAL := help`**: `make` sin argumentos no hace nada peligroso; enseña la ayuda.

**`help` se documenta solo.** Cada objetivo público lleva un comentario `## descripción` en su línea, y la receta es un `awk` que lee el propio Makefile (`$(MAKEFILE_LIST)`) e imprime esas descripciones. Declarar un objetivo y documentarlo es la misma línea, así que nunca se desincronizan.

**`secrets-scan` combina dos códigos de salida:**

```make
secrets-scan: ## Full scan: work tree plus every commit on every ref (merges, messages, tags, identities)
	@tree_rc=0; hist_rc=0; \
	$(GUARDS) tree || tree_rc=$$?; \
	$(GUARDS) history || hist_rc=$$?; \
	if [ "$$tree_rc" -ge 2 ] || [ "$$hist_rc" -ge 2 ]; then exit 2; fi; \
	if [ "$$tree_rc" -ne 0 ] || [ "$$hist_rc" -ne 0 ]; then exit 1; fi
```

- Corre **los dos** escaneos aunque el primero falle: un solo run te da todos los hallazgos.
- Un 2 (error o herramienta ausente) gana a un 1 (hallazgo): un error nunca se disfraza de «solo hallazgos».
- `$$?` es el `$?` del shell. El `$$` le dice a Make «esto no es tuyo, pásalo tal cual».

**Los objetivos reservados fallan, no mienten.** `parity` y `datagen` existen para que el contrato quede congelado, pero salen con rc 2 y el nombre del plan que los implementa. Un objetivo vacío con rc 0 haría creer a CI que algo se comprobó.

### En AdmitLens: el `Makefile` de `level-1/backend` (contrato KM)

| Verbo | Qué hace hoy | Estado |
| --- | --- | --- |
| `install` | `uv sync --locked`: crea `.venv` desde `uv.lock` | real |
| `lint` | `ruff check .` y `ruff format --check .` | real |
| `typecheck` | `mypy app tests`; **informativo**, fuera de `check` y de CI | real |
| `test-unit` | `pytest -m "not docker"`: sin base de datos | real |
| `test` | Depende de `db-check`; `pytest --cov=app` | real |
| `check` | `lint` + `test-unit`: la compuerta de calidad del `pre-push`, sin Docker | real |
| `env` | `sh ops/make-env.sh .env.example .env` ([§6](#6-make-env-los-secretos-locales)) | real |
| `db-up`, `db-stop`, `db-down`, `db-check` | Ciclo de vida de la base ([§7](#7-postgresql-16-en-compose)) | real |
| `openapi`, `openapi-check`, `seed`, `dev` | rc 2: «implemented in L1-P4» | reservado |
| `migrate` | rc 2: «implemented in L1-P2» | reservado |

Dos detalles para leer línea por línea:

```make
check: lint test-unit ## Quality gate run by pre-push: lint + test-unit, no Docker
```

`check` no tiene receta, solo dependencias: Make corre `lint`, después `test-unit`, y falla en cuanto uno falla. Tampoco puede fallar por falta de código: con [`app/__init__.py`](../../level-1/backend/app/__init__.py) vacío, hoy `test-unit` recoge 49 tests de `tests/tooling` (`-m "not docker"`) y deja fuera el único test con el marker `docker`, que necesita la base.

```make
ifneq ($(filter true 1,$(CI)),)
db-check:
	@echo "db-check: skipped in CI (service container)"
else
db-check: ## Check the database is up via Docker ...
```

Es un condicional de Make, evaluado al **leer** el archivo. GitHub pone `CI=true`; en CI no hay compose, y la base llegará como contenedor de servicio en el siguiente plan (ADR-ADLN-0010 §2.4). `$(filter true 1,$(CI))` deja `true` o `1` y vacía cualquier otro valor, así que `CI=false` sigue siendo local (`test_db_check_runs_when_ci_is_false` en [`test_make_targets.py`](../../level-1/backend/tests/tooling/test_make_targets.py)).

### Los secretos nunca son variables de Make

La cabecera de los dos Makefiles lo dice: «This file never includes .env and never expands a secret as a Make variable». Así se cumple:

| Regla | Cómo | Quién la vigila |
| --- | --- | --- |
| Nunca `include .env` | Make no lee `.env`; lo leen Docker Compose o la app | Regla de lint `make-include-env` |
| Nunca `$(PASSWORD)` | Las recetas leen `$$POSTGRES_PASSWORD` del shell, **dentro del contenedor** (`db-up`) | Regla `make-secret-var`: un nombre con `PASSWORD`, `SECRET`, `TOKEN`, `KEY`, `DSN` o `URL` dentro de `$(…)` es hallazgo. `URL` entró con el hotfix `[Debugging - 2]`: antes, `$(DATABASE_URL)` se le escapaba. Busca por subcadena (`MONKEY` también cae) a propósito: falla cerrado, y un falso positivo se arregla renombrando la variable. La segunda red es el test de `make -n` con canarios (dos filas más abajo) |
| Toda línea de receta empieza por `@` | Make no imprime la receta antes de ejecutarla | `test_every_recipe_line_starts_with_at` |
| `make -n` no muestra valores | Con un valor canario en el entorno, el dry run de cada objetivo no lo contiene | `test_dry_run_of_every_target_prints_no_canary` (raíz), `test_make_n_shows_no_values` (nivel) |
| Lo que llega de un hook viaja por el entorno | `MSG_FILE`, `REMOTE` y `PUSH_URL` se leen como `$$VAR`; Make nunca los expande | Cabecera del [`Makefile`](../../Makefile) |

**Portable a Make 3.81.** macOS trae GNU Make 3.81 (de 2006) y Ubuntu trae 4.x. Los dos Makefiles evitan `.ONESHELL`, `.SHELLFLAGS`, `.RECIPEPREFIX`, `$(file …)`, `!=`, `::=` y los objetivos agrupados `&:`. La regla `make-gnu-construct` los rechaza, y `make doctor` avisa si Make es anterior a 3.81.

### Herramienta

GNU Make (3.81 o posterior), `awk` para la ayuda y [uv](https://docs.astral.sh/uv/) para ejecutar Python con el lockfile.

### Para muchos usuarios

- **El Makefile es la documentación ejecutable** del repo: quien llega lee `make help`, no un wiki desactualizado.
- **En un monorepo**, cada carpeta autónoma tiene su Makefile con los mismos verbos, y la raíz solo tiene objetivos de todo el repo. La raíz **nunca escribe una ruta de nivel** (ADR-ADLN-0007 §E7.4 y §E7.7). El único que recorre niveles es [`tools/guards/bin/level-checks.sh`](../../tools/guards/bin/level-checks.sh), y los descubre con un patrón (`level-N/backend`) sobre los archivos que cambió el push.
- **Por qué Make** (ADR-ADLN-0007 §E7.4): es universal, y el responsable del proyecto quería practicarlo. **Alternativa descartada:** exigir `gmake` 4.x, que añade un paso para quien reutilice el repo como semilla (PLAN-CA-0005 §6.5).

> **Anti-patrón:** `include .env` y `psql "$(DATABASE_URL)"` en una receta.
>
> **Patrón:** el Makefile no conoce valores; la receta los lee del shell (`$$VAR`) o dentro del contenedor, y un test con un canario prueba que `make -n` no los muestra.

---

## 3. Los hooks de git

### Qué es

Un hook es un programa que git ejecuta en un momento fijo. Si sale con un código distinto de 0, git **aborta** la operación. AdmitLens usa tres:

| Hook | Cuándo lo llama git | Qué recibe |
| --- | --- | --- |
| `pre-commit` | Antes de crear el commit | Nada; lo que importa es el índice |
| `commit-msg` | Con el mensaje ya escrito | `$1`: la ruta del archivo del mensaje |
| `pre-push` | Antes de enviar refs al remoto | `$1` el nombre del remoto, `$2` su URL y, por stdin, una línea por ref |

### El problema que resuelve

En un repo público con push directo a `main`, **CI detecta pero no previene**: cuando el job avisa, el commit ya es público, y un secreto publicado no se despublica; se rota (ADR-ADLN-0007 §2.4). La única capa propia que **previene** es la local. Y como `.git/hooks/` no se versiona, los hooks viven en `.githooks/` y `make hooks` le dice a git que los use (`core.hooksPath`).

### En AdmitLens

Los tres hooks son **delgados**: cinco líneas de `sh` que delegan en Make. Este es [`.githooks/pre-commit`](../../.githooks/pre-commit) completo:

```sh
#!/bin/sh
# Thin hook: all logic lives in `make hook-pre-commit` (tools/guards). Fails closed without make.
command -v make >/dev/null 2>&1 || { echo "pre-commit: make not found; commit blocked" >&2; exit 2; }
top=$(git rev-parse --show-toplevel) || exit 2
MAKEFILES= MAKEFLAGS= MFLAGS= GNUMAKEFLAGS= exec make -s -C "$top" -f Makefile hook-pre-commit
```

| Línea | Qué hace | Por qué |
| --- | --- | --- |
| 3 | Si no hay `make`, rc 2 | **Falla cerrado:** sin la herramienta, el commit no pasa |
| 4 | Busca la raíz del repo | El hook puede correr desde un subdirectorio |
| 5, `MAKEFILES=` | Vacía la lista de Makefiles extra que GNU Make lee del entorno | Un `MAKEFILES` heredado podría inyectar objetivos |
| 5, `MAKEFLAGS= MFLAGS= GNUMAKEFLAGS=` | Vacía las banderas heredadas | Un `-n` heredado haría que el hook «pasara» sin ejecutar nada |
| 5, `-f Makefile` | Fija el archivo | Sin `-f`, GNU Make preferiría un `GNUmakefile` o un `makefile` si existieran |
| 5, `exec` | El proceso de `make` reemplaza al del hook | El código de salida de Make es el del hook |

Los otros dos solo cambian lo que pasan por el entorno:

- [`commit-msg`](../../.githooks/commit-msg) convierte `$1` en una ruta absoluta (`MSG_FILE="$dir/$(basename -- "$1")"`), porque `make -C` cambia de directorio. La ruta viaja por el entorno, nunca como argumento de Make.
- [`pre-push`](../../.githooks/pre-push) pasa `REMOTE="$1"` y `PUSH_URL="$2"`.

Qué comprueba cada uno:

| Hook | Objetivo | Modo de las guardas | Comprueba |
| --- | --- | --- | --- |
| `pre-commit` | `hook-pre-commit` | `staged` | El **índice** (lo que entrará en el commit, no tu árbol de trabajo): rutas prohibidas, rutas que el `.gitignore` excluye, contenido, archivos de más de 1 MiB y gitleaks sobre los blobs del índice |
| `commit-msg` | `hook-commit-msg` | `message` | El formato de ADR-ADLN-0012 §2.1, los trailers y URLs que prohíbe su §2.2, rutas locales, emails y marcadores privados; gitleaks por stdin |
| `pre-push` | `hook-pre-push` | `prepush` y después `level-checks.sh` | Solo `refs/heads/*` (los tags se rechazan aquí: el proyecto no usa tags, ADR-ADLN-0013 §2.2), nunca borrar `main`, cada commit nuevo con gitleaks y con las reglas propias y, al final, `make check` en cada `level-N/backend` que cambió |

El objetivo `hook-pre-push`, línea por línea:

```make
hook-pre-push:
	@if [ -z "$$REMOTE" ]; then echo "hook-pre-push: REMOTE is required" >&2; exit 2; fi; \
	umask 077; refs=$$(mktemp) || exit 2; trap 'rm -f "$$refs"' EXIT INT TERM; \
	cat >"$$refs" || exit 2; \
	PUSH_URL="$$PUSH_URL" $(GUARDS) prepush "$$REMOTE" <"$$refs" || exit $$?; \
	LEVEL_MAKE="$(LEVEL_MAKE)" sh "$(GUARDS_DIR)/bin/level-checks.sh" "$$REMOTE" "$$refs"
```

- La entrada que git manda por stdin se necesita **dos veces**, para las guardas y para `level-checks.sh`. Por eso se guarda en un temporal privado (`umask 077`) que `trap` borra al salir.
- Primero las guardas de fugas y, **solo si pasan**, la compuerta de calidad (`|| exit $$?`).
- `level-checks.sh` es una compuerta de calidad, no una guarda: si un nivel no define `check` (lo sondea con `make -n check`), avisa y sigue.

Lo que hace `prepush` con cada ref (`mode_prepush` en [`cli.py`](../../tools/guards/guards/cli.py)):

1. una ref remota que no es `refs/heads/*` da `push-ref-not-allowed`;
2. borrar `refs/heads/main` da `push-delete-main`;
3. si el remoto tiene un commit que tu clon no conoce, rc 2: «run git fetch first»;
4. ante una rama nueva, pregunta por el `main` real con `ls-remote` **a la URL del push** (`$2`), nunca a la ref local de seguimiento, que cualquiera puede mover con `git update-ref`;
5. todos los commits del rango pasan por `range_checks`: identidades, mensajes con formato y cada versión de cada archivo.

### Los límites, dichos en voz alta

| Hueco | Quién lo cubre |
| --- | --- |
| `git commit --no-verify` salta los hooks | En la máquina del responsable, una configuración local de permisos de las herramientas de desarrollo niega `--no-verify` y los cambios de `core.hooksPath`; no se versiona, así que no viaja con un clon. Para todos: push protection y CI |
| Un clon nuevo no tiene `core.hooksPath`: vive en `.git/config`, que no viaja | `make setup` en cada clon; `make doctor` da FAIL si falta |
| Un commit hecho en la web de GitHub no pasa por tus hooks | CI (`guards.yml`) y push protection |

### Herramienta

POSIX `sh`, git (`core.hooksPath`) y GNU Make.

### Para muchos usuarios

- Los hooks son una cortesía con quien colabora (le avisan antes de que se equivoque), **no** un control: el control está en el servidor. En un equipo, la PR obligatoria con checks requeridos convierte la detección de CI en prevención. AdmitLens no la usa porque una sola persona integra, con push directo a `main` (ADR-ADLN-0007 §2.4).
- **Alternativa descartada:** el framework `pre-commit`. Añade un DSL, descarga repos de terceros (cadena de suministro dentro de la herramienta de seguridad) y esconde el mecanismo que se quería aprender (PLAN-CA-0005 §6.5).

> **Anti-patrón:** un hook de 200 líneas de bash, sin tests, que hace `exit 0` si no encuentra su herramienta.
>
> **Patrón:** un hook de cinco líneas que falla cerrado y delega en un motor con tests.

---

## 4. El motor de detección: tools/guards

### Qué es

Un paquete Python 3.12 en [`tools/guards/`](../../tools/guards/pyproject.toml) con **una sola dependencia de runtime** (PyYAML, para leer los workflows) y su propia suite de tests. Se ejecuta como `python -m guards MODO`, y su interfaz es el contrato KG de PLAN-CA-0005 §6.3.

### El problema que resuelve

gitleaks conoce unos 200 formatos de proveedor, pero los canarios del plan midieron lo que **no** ve: una DSN con contraseña, una contraseña débil, una forma de número de seguridad social, una ruta local, el email del autor de un commit, el contenido que solo existe en un merge y el texto de los mensajes (PLAN-CA-0005 §6.5, «Solo gitleaks»). Las guardas cubren eso, y gitleaks sigue haciendo lo suyo.

### En AdmitLens

**Dos motores, una responsabilidad cada uno** (PLAN-CA-0005 §6.1):

| Motor | Detecta |
| --- | --- |
| **gitleaks** con [`.gitleaks.toml`](../../.gitleaks.toml) | Contenido con forma de secreto: las reglas por defecto más las reglas `admitlens-*` ([§5](#5-gitleaks-fijado-por-sha256)) |
| **`tools/guards`** | Estructura, identidad y metadatos: rutas, emails, autor y committer, mensajes, cabeceras CSV con forma de datos de pacientes, y lint de Makefiles, `.gitignore`, workflows, `.gitattributes` y del propio `.gitleaks.toml` |

Los modos, según `USAGE` en [`cli.py`](../../tools/guards/guards/cli.py):

| Modo | Qué escanea | Quién lo usa |
| --- | --- | --- |
| `staged` | El índice | `pre-commit` |
| `message <archivo>` | Un mensaje de commit o de tag | `commit-msg` |
| `prepush <remoto>` | Los commits que va a enviar el push (stdin de git) | `pre-push` |
| `range <remoto> <desde> <hasta>` | Un rango, a mano | Tú |
| `tree` | Los archivos versionados **y** los no ignorados aunque aún no estén versionados | `make secrets-scan` |
| `history` | Todos los commits de todas las refs, con merges, mensajes, tags e identidades | `make secrets-scan` |
| `identity <rango>` | Autor, committer y tagger | Tú |
| `lint` | Makefiles, `.gitignore`, workflows, `.gitattributes` y `.gitleaks.toml` | `make lint` |
| `doctor` | La preparación de tu clon | `make doctor` |

Las familias de reglas, una por archivo en [`tools/guards/guards/rules/`](../../tools/guards/guards/rules/paths.py):

| Archivo | Reglas | Qué atrapan |
| --- | --- | --- |
| [`paths.py`](../../tools/guards/guards/rules/paths.py) | `path-env`, `path-data`, `path-key`, `path-local-artifact`, `path-dump`, `path-report`, `path-gitleaksignore`, `gitleaks-config-shadow`, `path-too-large`, `path-ignored` | Un `git add -f` de lo que el `.gitignore` excluye: secretos, datos, claves, dumps, informes de escáneres y la configuración local de las herramientas de desarrollo. La única excepción por archivo es el nombre exacto `.env.example`; nunca hay excepción por directorio |
| [`content.py`](../../tools/guards/guards/rules/content.py) | `content-email`, `content-phi-header`, `content-private-marker`, `content-dsn-password`, `content-secret-assignment`, `content-secret-assignment-py` y una regla para las URLs de sesión que prohíbe ADR-ADLN-0012 | Emails fuera de la allowlist (`.test`, `example.com`, `users.noreply.github.com` y la dirección noreply de GitHub), cabeceras CSV con nombre y fecha de nacimiento o un identificador clínico, marcadores privados, y secretos con la misma regex que gitleaks pero sin su allowlist defectuosa ([§5](#el-hueco-que-se-encontró-midiendo)) |
| [`identity.py`](../../tools/guards/guards/rules/identity.py) | `identity-email`, `identity-name` y, desde el hotfix `[Debugging - 2]`, `identity-owner-name` | Autor, committer o tagger sin email noreply, o con un nombre real declarado como privado. `identity-owner-name` exige que un commit con el email noreply del dueño del repo lleve como nombre el `owner_name` declarado en `.guards.toml`, que es versionado (ver la [sección 2.2 del protocolo](../00-protocolo-de-desarrollo.md#22-prerrequisitos-de-la-cuenta-ui-web)) |
| [`messages.py`](../../tools/guards/guards/rules/messages.py) | `msg-format`, `msg-local-path`, `msg-email`, `msg-private-marker`, tres reglas para ciertos trailers de coautoría generados por herramientas, los trailers de sesión y las URLs de sesión y, desde el hotfix `[Debugging - 2]`, `msg-skip-ci` | El formato de ADR-ADLN-0012 §2.1 y lo que prohíbe su §2.2. `msg-skip-ci` rechaza `[skip ci]` y sus variantes, que saltarían CI. Exime **solo** la línea exacta del trailer `Signed-off-by` de Dependabot (`DEPENDABOT_SIGNOFF`) |
| [`lint.py`](../../tools/guards/guards/rules/lint.py) | `make-*`, `gitignore-negation`, `wf-*` (9 reglas), `gitattributes-*` y `gitleaks-config-*` (7 reglas) | Que nadie debilite las piezas que conectan el resto: un workflow sin `permissions:`, una acción sin SHA fijado, un `run:` que no es `make`, una negación nueva en `.gitignore`, una regla de gitleaks borrada |

Los marcadores privados (nombre real, email personal, rutas privadas) viven en `.guards.local.toml`, que git ignora: las reglas versionadas nunca los contienen. CI no tiene ese archivo, así que esas comprobaciones son solo locales, por diseño ([`markers.py`](../../tools/guards/guards/markers.py)).

### Cómo informan sin imprimir valores

Este es el corazón del diseño: **una guarda que imprime lo que encontró es una fuga más**.

```python
@dataclass(frozen=True, order=True)
class Finding:
    """One detection. There is deliberately no field for the matched value."""

    rule: str
    path: str
    line: int
    commit: str | None = None
```

[`Finding`](../../tools/guards/guards/findings.py) **no tiene dónde guardar el valor**, así que es imposible imprimirlo por descuido. La salida es una línea por hallazgo, `regla⇥ruta⇥línea[⇥commit]`, más un resumen en stderr con los nombres de las reglas. Además:

| Mecanismo | Dónde |
| --- | --- |
| gitleaks corre con `--redact`; su informe JSON va a un temporal `0600` que se borra en `finally`; solo se leen `RuleID`, `File` y `StartLine`; su stdout y su stderr se capturan y nunca se repiten | `Gitleaks._run` en [`gitleaks.py`](../../tools/guards/guards/gitleaks.py) |
| Los mensajes se informan con pseudo-rutas (`:message`, `:author`), nunca con la ruta real del archivo | `mode_message` en [`cli.py`](../../tools/guards/guards/cli.py), [`identity.py`](../../tools/guards/guards/rules/identity.py) |
| Un error (`GuardError`) nombra rutas o herramientas, nunca contenido; un error de PyYAML se convierte en una línea, porque su texto cita la línea del archivo | [`findings.py`](../../tools/guards/guards/findings.py), [`lint.py`](../../tools/guards/guards/rules/lint.py) |
| `Markers.__repr__` solo muestra cuántos marcadores hay | [`markers.py`](../../tools/guards/guards/markers.py) |
| `doctor` imprime `ok`, `warn` o `FAIL` y el nombre de la comprobación, nunca el email ni el nombre | [`doctor.py`](../../tools/guards/guards/doctor.py) |
| Una excepción inesperada sale como `internal error (TipoDeError); failing closed`, sin el mensaje | `main` en [`cli.py`](../../tools/guards/guards/cli.py) |

**Regex para detectar, nunca para redactar.** Si una regex de detección falla, da un falso negativo, pero la salida sigue sin el valor, porque la guarda nunca lo imprime. Si falla una regex de **redacción**, imprime el secreto completo, y encima la salida *parece* redactada. Por eso el proyecto no filtra salidas con `sed`: elige comandos que no seleccionan el valor.

**Los tests prueban la ausencia sin escribir secretos.** En [`tests/support/fakes.py`](../../tools/guards/tests/support/fakes.py):

- `make_fake` arma cada secreto falso en tiempo de ejecución, a partir de fragmentos cortos y con una semilla fija: en el repo no hay ningún literal con forma de secreto;
- `Secret` no muestra su valor en `repr` ni en `str`;
- `assert_absent` falla nombrando la regla, una huella y el canal, nunca el valor. `assert valor not in salida` está prohibido por convención, porque pytest imprimiría el valor en el diff de la aserción.

[`test_no_value_output.py`](../../tools/guards/tests/test_no_value_output.py) comprueba que ningún modo (`tree`, `staged`, `message`, `identity`, los errores de TOML y de YAML) deja escapar el canario.

### make secrets-scan

Corre `guards tree` y `guards history`. El modo `history` (`mode_history`):

- recorre `rev-list --all`: **todos** los commits de todas las refs, merges incluidos;
- escanea cada versión de cada archivo **una sola vez**, atribuida al commit más antiguo que la introdujo;
- pasa los mensajes de commits y de tags anotados por las reglas de mensaje y por `gitleaks stdin`, **sin exigir el formato**, porque Dependabot y los merges de la web escriben el suyo;
- usa la configuración de gitleaks de `HEAD` **unida** a la de `refs/remotes/origin/main` cuando esa ref existe. Sirve en PRs, en otras ramas y en local. En un push a `main` en CI, `origin/main` es el propio `HEAD` y la unión no añade nada: allí, contra un debilitamiento de la configuración, protegen el lint que la congela (`gitleaks-config-*`) y la unión que hace el `pre-push` con el `main` del remoto;
- si el repo es **superficial** (`git rev-parse --is-shallow-repository` responde `true`), da rc 2, en CI y en local. Es así desde el hotfix `[Debugging - 2]`; antes, un checkout sin `fetch-depth: 0` escaneaba un solo commit **sin avisar**, porque en un push a `main` `actions/checkout` crea `origin/main` igualmente (con el refspec `+<SHA>:refs/remotes/origin/main`);
- con `CI=true`, si falta `origin/main` (pasa en PRs y en ramas que no son `main`), da rc 2 en vez de degradarse en silencio.

<details>
<summary>Profundiza: por qué las guardas no usan el modo <code>git</code> de gitleaks</summary>

El plan original decía «gitleaks con `--log-opts=-m`», porque sin `-m` gitleaks no ve los merges. La revisión de código del primer commit midió algo peor: el modo `git` de gitleaks lee `git log -p`, y `.gitattributes` (`-diff`, `binary`), los colores o un `git -c` pueden cegarlo. El código final (docstring de [`gitleaks.py`](../../tools/guards/guards/gitleaks.py)) nunca usa ese modo: las guardas enumeran los blobs ellas mismas (`diff-tree -m`) y los pasan a `gitleaks dir`, y los mensajes a `gitleaks stdin`. Por eso hay reglas de lint que limitan `.gitattributes` (`gitattributes-diff`, `gitattributes-macro`) y `make doctor` exige que `.git/info/attributes` esté vacío.

</details>

### make doctor

Responde a «¿este clon está preparado para que las guardas corran?». Salida real del 2026-10-18:

```text
ok    git config core.hooksPath
ok    git config user.useConfigOnly
ok    git config push.default
ok    git config push.followTags
ok    git config status.showUntrackedFiles
ok    git config core.autocrlf
ok    hook pre-commit
ok    hook commit-msg
ok    hook pre-push
ok    gitleaks 8.30.1 at the configured path
ok    no .gitleaksignore
ok    no .git/info/attributes
ok    user.email is a GitHub noreply address
ok    user.name is not a private real name
warn  actionlint installed (optional)
warn  shellcheck installed (optional)
ok    GNU Make >= 3.81
doctor: all required checks passed
```

| Comprobación | Si falla | Por qué existe |
| --- | --- | --- |
| `core.hooksPath` y los tres hooks presentes y ejecutables | FAIL | Sin ellos, la capa local no existe |
| `user.useConfigOnly=true` | FAIL | Git no inventa una identidad con tu usuario y el nombre de tu máquina |
| `push.default=simple`, `push.followTags=false` | FAIL | Un `git push` no envía más ramas ni tags de los que querías |
| `status.showUntrackedFiles=all`, `core.autocrlf=false` | warn | Higiene: ver todo lo nuevo y no reescribir finales de línea |
| gitleaks con el sha256 fijado | FAIL | Un binario cambiado o falso no pasa ([§5](#5-gitleaks-fijado-por-sha256)) |
| Ningún `.gitleaksignore` | FAIL | Sería una supresión que nadie revisa |
| `.git/info/attributes` vacío | FAIL | Es local, nadie lo revisa y gana a todo `.gitattributes` |
| Email noreply; nombre que no es un nombre real declarado | FAIL | Identidad pública sin datos personales |
| `.guards.local.toml` ausente o sin nombres reales | warn | Se puede trabajar anónimo, detrás de un alias |
| `actionlint`, `shellcheck` | warn | Opcionales |

`doctor` sale con rc 1 si alguna comprobación da FAIL; los `warn` no fallan. Si trabajas con varios worktrees, debe pasar en **cada** uno (ADR-ADLN-0007 §E7.9).

### Falla cerrado: cuándo da rc 2

- gitleaks no está, no es la versión fijada o su hash no coincide;
- existe un `.gitleaksignore`;
- `.gitleaks.toml` falta del índice o del commit escaneado (borrar el archivo no apaga las reglas propias), salvo en el arranque de un repo sin commits;
- el remoto tiene commits que tu clon no conoce;
- el remoto empuja a una URL distinta de la de lectura y no llegó `PUSH_URL`;
- el nombre del remoto es inválido: como argumento, a las guardas se les pasa el **nombre**, nunca la URL. La URL del push llega aparte, por la variable `PUSH_URL` (desde el arreglo de la `pushurl`), y solo se usa como argumento de `ls-remote`; nunca se imprime, porque puede llevar un token;
- el repo es superficial (desde el hotfix `[Debugging - 2]`);
- en CI, falta `origin/main`;
- cualquier excepción no prevista.

### Herramienta

Python 3.12 (de la biblioteca estándar: `dataclasses`, `re`, `tomllib`, `subprocess`, `tempfile`), PyYAML, pytest y uv con lockfile.

### Para muchos usuarios

- En una organización, parte de esto lo hacen los *custom patterns* del secret scanning de GitHub y las políticas centrales. En una cuenta personal no existen, y por eso las reglas propias viven en el repo.
- **El presupuesto de la suite.** 679 tests tardan unos 116 segundos. Se aceptó un presupuesto de 120 s antes que añadir `pytest-xdist`: cada dependencia de la herramienta de seguridad es cadena de suministro.
- **Alternativas descartadas** (PLAN-CA-0005 §6.5): solo un escáner propio (reinventa un motor de historial maduro); detect-secrets como motor (su baseline guarda SHA1 reversibles); allowlists por directorio y supresiones en línea (`gitleaks:allow`), que son puntos ciegos y atajos hacia `--no-verify`.

> **Anti-patrón:** `grep -rn 'AKIA' .` para «ver si hay claves». Imprime la línea, con la clave.
>
> **Patrón:** `grep -rl` (archivos) o `grep -c` (conteos), o una guarda cuyo hallazgo no tiene campo de valor.

---

## 5. gitleaks, fijado por sha256

### Qué es

[gitleaks](https://github.com/gitleaks/gitleaks) es un escáner de secretos escrito en Go, con unas 200 reglas para formatos conocidos (claves de nube, tokens, JWT). AdmitLens fija **la versión exacta (8.30.1)** y **el hash del binario**.

### El problema que resuelve

Una herramienta de seguridad que cambia sola es un riesgo doble: un `brew upgrade` puede cambiar qué detecta sin que nadie lo note, y un binario descargado sin verificar puede ser cualquier cosa. Si el binario no es exactamente el esperado, no corre nada.

### En AdmitLens: install-gitleaks.sh

[`gitleaks.sha256`](../../tools/guards/bin/gitleaks.sha256) guarda **dos hashes por plataforma**, en formato `sha256sum`: el del tarball de la release (copiado del `checksums.txt` oficial) y el del binario `gitleaks` que va dentro. [`install-gitleaks.sh`](../../tools/guards/bin/install-gitleaks.sh), paso a paso:

| Paso | Código | Por qué |
| --- | --- | --- |
| 1 | `case "${os}-${arch}"` elige `darwin_arm64` o `linux_x64`; otra plataforma da rc 2 | Solo hay pines para las plataformas que se usan: el Mac y el runner de CI |
| 2 | `pin_of` exige **exactamente una** línea por archivo | Un archivo de pines manipulado con dos líneas no elige por ti |
| 3 | Si el binario instalado ya tiene el hash, termina | Idempotente: `make setup` se puede repetir |
| 4 | `curl -fsSL` del tarball a un `mktemp -d` | `-f` hace fallar un 404 en vez de guardar una página de error |
| 5 | Verifica el hash del **tarball antes de extraerlo** | No se descomprime nada sin verificar |
| 6 | Extrae solo `gitleaks` y verifica el hash del **binario** | Segundo cerrojo |
| 7 | `gitleaks version` debe decir `8.30.1` | Tercera comprobación, la más débil |
| 8 | Copia a `.tools/bin/gitleaks` (ignorado por git), `chmod 0755` y **verifica otra vez** | La copia no se corrompió |

Y en cada escaneo, `binary_ok` ([`gitleaks.py`](../../tools/guards/guards/gitleaks.py)) compara el hash del binario que va a ejecutar con el pin de su plataforma. Un binario que miente sobre su versión, o que alguien cambió después de instalarlo, da rc 2.

### Las reglas propias de .gitleaks.toml

`useDefault = true`, en la sección `extend`, mantiene las reglas por defecto. Las propias llevan el prefijo `admitlens-`:

| Regla | Qué detecta |
| --- | --- |
| `admitlens-dsn-password` | `esquema://usuario:contraseña@` para Postgres, Redis, AMQP, MongoDB y MySQL |
| `admitlens-secret-assignment` | Un nombre que contiene `PASSWORD`, `PASSWD`, `SECRET`, `TOKEN`, `APIKEY`, `API_KEY`, `PRIVATE_KEY` o `DSN`, con un valor literal, **solo** en `.env*`, YAML, TOML, INI, CFG y compose |
| `admitlens-secret-assignment-py` | En `*.py`, solo un string literal asignado a un nombre sensible; leer `os.environ[...]` no cae |
| `admitlens-ssn` | La forma 3-2-4 de un número de seguridad social en rangos válidos, con las exclusiones por alternancia (RE2 no tiene lookahead) |
| `admitlens-local-path` | Rutas de la carpeta personal en macOS, Linux y Windows, nombres de proyecto derivados de ellas y temporales del sistema |
| `admitlens-vault-marker` | El marcador de la bóveda de notas privada |
| `admitlens-path-env`, `-path-data`, `-path-key`, `-path-db` | Reglas solo de ruta, para la historia |

- **Supresiones solo por valor:** `__GENERATE__`, `__REQUIRED__`, `<…>`, `${…}` y `$VAR`. No hay allowlists por ruta.
- **La configuración se lee del índice o del commit** a un temporal, nunca del árbol de trabajo: debilitarla en tu árbol no baja la detección.
- **El lint congela la configuración:** `gitleaks-config-rule-missing`, `-rule-changed`, `-allowlist`, `-extend`, `-key`, `-rule-id` e `-invalid`. Se puede **añadir** detección; quitarla exige cambiar también el lint, con revisión.

#### El hueco que se encontró midiendo

Con `useDefault = true`, gitleaks hereda una allowlist global que no se puede quitar y que tiene dos defectos: no ancla `gitleaks.toml` en la ruta (un archivo `a-gitleaks.toml` con una DSN da 0 hallazgos) y su regex `(?i)^true|false|null$` no está agrupada, así que descarta secretos que empiecen por `true`, contengan `false` o terminen en `null`. Se cerraron los dos sin renunciar a las reglas por defecto:

- el primero, con la regla de ruta `gitleaks-config-shadow`;
- el segundo, con `content-dsn-password` y `content-secret-assignment*` de [`content.py`](../../tools/guards/guards/rules/content.py), que usan **la misma regex** que el toml (un test lo comprueba) pero sin esa allowlist.

**Alternativa descartada:** `useDefault = false` y copiar las 200 reglas, lo que obliga a mantenerlas a mano y a perder sus actualizaciones (decisión D-0001 de PLAN-CA-0005).

### Herramienta

gitleaks 8.30.1, `sha256sum` o `shasum -a 256`, `curl` y `tar`.

### Para muchos usuarios

- Es el mismo principio que fijar las acciones de CI por un SHA de 40 caracteres ([§8](#8-ci-los-dos-workflows)) y la imagen de Postgres por digest ([§7](#7-postgresql-16-en-compose)): **fija todo lo que se mueve**, y que cada actualización sea un cambio revisado.
- En un equipo grande se usan registros internos de artefactos y atestaciones de procedencia; la idea es la misma.

> **Anti-patrón:** `brew install gitleaks` en el README y `gitleaks git` en CI, con la versión que toque ese día.
>
> **Patrón:** versión exacta, hashes del tarball y del binario versionados, comprobación en cada ejecución y rc 2 si algo no cuadra.

---

## 6. make env: los secretos locales

### Qué es

`make env` crea `level-1/backend/.env` a partir de [`.env.example`](../../level-1/backend/.env.example) y genera las contraseñas. Lo implementa [`ops/make-env.sh`](../../level-1/backend/ops/make-env.sh), en POSIX `sh`.

### El problema que resuelve

- Una contraseña escrita a mano acaba siendo `postgres` o `admin`, repetida en todas las máquinas y, tarde o temprano, en un commit.
- Un `.env` a medio escribir (el disco se llenó, pulsaste Ctrl-C) deja la base con una credencial rota.
- Un script que «regenera» el `.env` encima de uno existente rompe la base local, porque el volumen de Postgres guarda la contraseña de su **primer** arranque.

### En AdmitLens

`.env.example` (contrato KE) solo tiene nombres y valores seguros:

```dotenv
POSTGRES_USER=admitlens
POSTGRES_DB=admitlens
POSTGRES_PASSWORD=__GENERATE__
ADMITLENS_DB_PORT=5432
ADMITLENS_DATABASE_URL=postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${ADMITLENS_DB_PORT}/${POSTGRES_DB}
ADMITLENS_TEST_DATABASE_URL=postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${ADMITLENS_DB_PORT}/admitlens_test
```

`make-env.sh`, en el orden en que corre:

| Paso | Código | Por qué |
| --- | --- | --- |
| 1 | `set -eu`, `LC_ALL=C`, `umask 077` | Falla ante cualquier error o variable sin definir; `[A-Z]` significa bytes y no letras de otro alfabeto; todo lo que se cree nace privado |
| 2 | Si `.env` existe (archivo **o enlace simbólico**), rc 1 sin tocarlo | No cambia sus bytes ni su `mtime`, y te recuerda que el volumen guarda la contraseña del primer arranque |
| 3 | `command -v openssl` o rc 2 | Falla cerrado |
| 4 | `mktemp "${target}.tmp.XXXXXX"` y `chmod 600` | Se escribe aparte, en privado |
| 5 | Por cada línea: valida `NAME=VALUE`, el nombre en `[A-Z0-9_]` y que no esté repetido | Un `.env.example` roto no produce un `.env` roto |
| 6 | `__GENERATE__` se convierte en `$(openssl rand -hex 24)`, y comprueba que sean 48 caracteres hexadecimales | 24 bytes aleatorios son 192 bits, y el hexadecimal no necesita escaparse dentro de una URL |
| 7 | `${NAME}` se sustituye por el valor de `NAME`, definido **antes**; `eval` solo ve nombres validados, nunca valores | Las URLs con la contraseña se componen dentro del shell |
| 8 | Rechaza un `__GENERATE` sobrante o un `\r` (un ejemplo con finales CRLF) | Una credencial con `\r` falla de una forma muy difícil de diagnosticar |
| 9 | `printf '%s=%s\n'` | `printf` es un *builtin*: el valor nunca es argumento de un programa externo y no aparece en `ps` |
| 10 | `ln "${tmp}" "${target}"` | `ln` **no sobrescribe**: si `.env` apareció mientras tanto, falla. Un `mv` lo habría pisado |
| 11 | Imprime `wrote .env (mode 600) with: NOMBRES` y `generated: NOMBRES` | Solo nombres |

Los tests de [`test_make_env.py`](../../level-1/backend/tests/tooling/test_make_env.py) lo demuestran uno por uno: `test_writes_mode_600_even_with_umask_000`, `test_prints_names_only`, `test_generates_48_hex_distinct_between_runs`, `test_refuses_to_overwrite_and_keeps_bytes_and_mtime`, `test_refuses_a_dangling_symlink`, `test_invalid_example_never_leaves_a_partial_env` y `test_without_openssl_fails_closed`.

**Por qué `openssl` en `sh` y no Python.** ADR-ADLN-0010 §2.5 pide `openssl rand -hex 24` bajo `umask 077`. El borrador del plan propuso Python con el argumento de que el valor quedaría en `argv`, pero era falso: el valor se captura en el shell y se escribe con un builtin. Se volvió a lo que dice el ADR (PLAN-CA-0005 §6.5).

### Herramienta

POSIX `sh`, `openssl`, `mktemp` y `ln`.

### Para muchos usuarios

- En la nube, los secretos los guarda la plataforma (variables cifradas o un gestor de secretos) y en el servidor no hay ningún `.env`. En el nivel 4 serán las variables de la plataforma de despliegue.
- En el CI del nivel 1 **no habrá contraseña**: el contenedor de servicio, que llega en el plan siguiente (L1-P2), usará `trust` y solo será accesible dentro del runner (ADR-ADLN-0010 §2.4).

> **Anti-patrón:** `cp .env.example .env` y editar a mano, o un `.env.example` con `POSTGRES_PASSWORD=postgres`.
>
> **Patrón:** el ejemplo dice `__GENERATE__`, un script lo rellena con 192 bits aleatorios, escribe en un temporal privado, enlaza sin sobrescribir e imprime solo nombres.

---

## 7. PostgreSQL 16 en compose

### Qué es

Docker Compose levanta **solo la base de datos** del nivel 1. La app corre en tu máquina con `uvicorn` (ADR-ADLN-0010 §2.1); contenerizarla es trabajo del nivel 2.

### El problema que resuelve

Probar en SQLite y desplegar en Postgres esconde las diferencias de dialecto (fechas, JSON, enums) hasta el peor momento. ADR-ADLN-0010 pone Postgres 16 desde el nivel 1, con un estándar único de fechas: `timestamptz` en UTC.

### En AdmitLens: docker-compose.yml

En [`level-1/backend/docker-compose.yml`](../../level-1/backend/docker-compose.yml):

| Línea | Qué hace | Por qué |
| --- | --- | --- |
| `name: admitlens-l1` | Nombre del proyecto | Los contenedores y volúmenes no chocan con los de otro nivel (ADR-ADLN-0007 §2.2) |
| `image: postgres:16.15-bookworm@sha256:0ea6…` | Tag **y** digest del índice multiarquitectura | El tag dice qué versión es; el digest del índice fija, para cada arquitectura, una imagen concreta. Un Mac (arm64) y Linux (amd64) bajan imágenes distintas, con bytes distintos, pero cada uno siempre la misma. Tag y digest se actualizan juntos |
| `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?…run make env}` | Contraseña obligatoria | Sin `.env`, compose no arranca: nunca hay una base sin contraseña ni con una por defecto |
| `TZ: UTC` y `-c timezone=UTC` en `command` | Sistema y servidor en UTC | Toda sesión empieza en UTC (ADR-ADLN-0010 §2.2) |
| `-c log_error_verbosity=terse` | Quita `DETAIL`, `HINT` y `CONTEXT` del log del servidor | Esos campos pueden repetir valores de las filas. Las líneas `STATEMENT` se siguen registrando: `terse` no las quita |
| `"127.0.0.1:${ADMITLENS_DB_PORT:-5432}:5432"` | Publica el puerto solo en loopback | Con `5432:5432`, Docker lo publica en todas las interfaces: tu base quedaría abierta en la wifi de la cafetería |
| `pgdata:/var/lib/postgresql/data` | Volumen con nombre | Los datos sobreviven a `db-stop` |
| `./ops/postgres:/opt/admitlens/postgres:ro` | Monta el SQL en solo lectura | No es el directorio de inicialización de la imagen: nada corre solo, y `db-up` lo aplica cada vez |
| `pg_isready -q -h 127.0.0.1 …` en `healthcheck` | Sano solo si responde **por TCP** | En el primer arranque, la imagen levanta un servidor temporal solo por socket; por TCP, ese no cuenta como sano |
| `restart: "no"` | No se reinicia solo | Una base local que no arranca debe verse, no esconderse en un bucle |

La cabecera del archivo avisa: `docker compose config` sin `-q` imprime el archivo renderizado **con la contraseña**. El test `test_config_q_passes_with_password_and_prints_nothing` de [`test_compose.py`](../../level-1/backend/tests/tooling/test_compose.py) lo vigila.

[`ops/postgres/ensure-test-db.sql`](../../level-1/backend/ops/postgres/ensure-test-db.sql):

- `CREATE DATABASE` no puede ir dentro de una transacción ni de un bloque `DO`. Por eso se construye con un `SELECT` que devuelve la sentencia **solo si la base no existe**, y el `\gexec` de `psql` la ejecuta. Si ya existe, no hay filas y no pasa nada: es idempotente;
- `TEMPLATE template0 ENCODING 'UTF8'` da una base limpia con la misma codificación;
- `ALTER DATABASE admitlens_test SET timezone TO 'UTC'`, y lo mismo con `:"DBNAME"` (la base de desarrollo): así `SHOW timezone` devuelve exactamente `UTC`.

### Los objetivos de la base

| Objetivo | Qué hace | El detalle que importa |
| --- | --- | --- |
| `db-up` | Comprueba que existan el SQL y `.env`; `up -d --wait --wait-timeout 90 db`; aplica el SQL **dentro del contenedor** y prueba la contraseña | La prueba entra por la dirección de servicio `db`, con autenticación `scram`, porque loopback y el socket usan `trust` en la imagen oficial. Así, un `.env` nuevo sobre un volumen viejo no parece sano (`test_db_up_rejects_a_env_regenerated_over_an_existing_volume`) |
| `db-stop` | `docker compose stop db` | Conserva el volumen |
| `db-down` | `docker compose down -v` | **Borra el volumen.** Exige una terminal (`[ ! -t 0 ]` da rc 2) y que escribas `admitlens-l1`. En CI o en un script nunca borra nada (`test_db_down_aborts_without_a_tty`) |
| `db-check` | `pg_isready` dentro del contenedor | Falla rápido con «run make db-up», sin traceback ni DSN; se salta con `CI=true` |

Fíjate en `db-up`: la contraseña se lee como `$$POSTGRES_PASSWORD` **dentro** de un `sh -c` en el contenedor. Make nunca la ve.

### Herramienta

Docker Compose v2, la imagen oficial `postgres:16`, `psql` y `pg_isready`.

### Para muchos usuarios

- En el nivel 4 la base es un servicio gestionado, con pooler y backups. El compose desaparece del despliegue, pero el estándar de UTC y de tipos es el mismo desde la primera migración (ADR-ADLN-0010 §2.2).
- **Alternativas descartadas** (ADR-ADLN-0010 §4): SQLite (Alt A), que deja dos dialectos; SQLite para los tests unitarios y Postgres en CI (Alt G); testcontainers (Alt H), que añade una dependencia y el control de Docker desde Python, y que se puede reconsiderar en el nivel 3.

> **Anti-patrón:** `ports: ["5432:5432"]`, `POSTGRES_PASSWORD: postgres` y `docker compose down -v` en un script de limpieza.
>
> **Patrón:** loopback, contraseña obligatoria y generada, imagen por digest y el borrado detrás de una confirmación escrita en una terminal.

---

## 8. CI: los dos workflows

### Qué es

GitHub Actions ejecuta los workflows de `.github/workflows/` en máquinas de GitHub. AdmitLens tiene dos: [`guards.yml`](../../.github/workflows/guards.yml), para todo el repo, y [`level-1-ci.yml`](../../.github/workflows/level-1-ci.yml), para el nivel 1.

### El problema que resuelve

Los hooks se pueden saltar y no existen en un clon sin `make setup`. CI repite las comprobaciones **en una máquina limpia** y sobre **toda** la historia. En un repo público detecta, pero no previene.

### En AdmitLens: guards.yml

| Línea | Qué hace | Por qué |
| --- | --- | --- |
| `on: push, pull_request, workflow_dispatch` y `schedule` (`cron: "17 6 * * 1"`) | Cada push y cada PR, a mano y cada lunes | El escaneo semanal se entera de los cambios en las reglas por defecto aunque nadie haga push |
| **Sin `paths:`** | Corre siempre | Cualquier archivo puede traer un secreto, y un check filtrado se queda en «Expected» (PLAN-CA-0005 §6.3, KX) |
| `permissions: {}` y `contents: read` en el job | El token del workflow solo puede leer | Si una acción se compromete, no puede escribir en el repo |
| `concurrency` con `cancel-in-progress` solo en PR | Un run en curso por rama: si llega otro, el que estaba **pendiente** se cancela; en una PR también se cancela el que está en curso | La cobertura no se pierde: cada run escanea toda la historia, así que el último cubre lo que traían los cancelados |
| `runs-on: ubuntu-24.04` | Fija la versión de Ubuntu | `ubuntu-latest` cambia de versión de Ubuntu sin aviso, y la regla `wf-runner-latest` lo rechaza. La etiqueta no congela la imagen: GitHub la sigue actualizando cada semana (paquetes y herramientas) |
| `actions/checkout@3d3c42e5…  # v7.0.1` | Acción fijada por un SHA de 40 caracteres | Un tag (`v7`) se puede mover; un SHA no. La regla `wf-uses-unpinned` lo exige |
| `fetch-depth: 0` | Toda la historia | Sin él, el clon es **superficial**: desde el hotfix `[Debugging - 2]`, `history` da rc 2; antes escaneaba un solo commit sin avisar. En un push a `main`, `origin/main` existe igual; en PRs y en otras ramas faltaría, y con `CI=true` eso también da rc 2 |
| `persist-credentials: false` | El token no se guarda en ningún sitio | Desde checkout v6, las credenciales persistidas van a un archivo aparte, no a `.git/config`; con `false` no se guardan, y los pasos siguientes no las heredan |
| `run: make setup`, `run: make secrets-scan` | Solo `make` | La regla `wf-run-not-make` rechaza cualquier otro `run:` |
| `if: ${{ !cancelled() }}` y `make guards-test` | Corre aunque `secrets-scan` haya fallado | Un test rojo nunca tapa una fuga, ni al revés: siempre ves las dos cosas |

### En AdmitLens: level-1-ci.yml

| Job | Comando | Notas |
| --- | --- | --- |
| `static` | `make -C level-1/backend lint` | `typecheck` no entra en CI |
| `unit` | `make -C level-1/backend test-unit` | Sin base; el job de integración con un contenedor de servicio llega en el siguiente plan (ADR-ADLN-0010 §2.4) |
| `secrets` | `make setup` y `make secrets-scan`, con `fetch-depth: 0` | ADR-ADLN-0007 §2.4: cada workflow de nivel escanea fugas |

Diferencias con `guards.yml`: este **sí** filtra por `paths` (`level-1/**` y su propio archivo), porque solo tiene sentido cuando cambia el nivel, y solo el job `secrets` trae toda la historia.

**Lo que las reglas `wf-*` prohíben en cualquier workflow:** `pull_request_target` y `workflow_run` (corren con los permisos del repo sobre código ajeno), `${{ github.event.* }}` dentro de `run:` (inyección de shell desde, por ejemplo, el título de una PR), un workflow sin `permissions:`, acciones sin SHA y etiquetas de runner flotantes o calculadas. Y `::add-mask::` **no es un control** (PLAN-CA-0005 §6.4): solo reemplaza el valor por asteriscos en el log, y únicamente cuando aparece tal cual; el proceso lo sigue teniendo y puede sacarlo transformado o por otro canal.

**Dependabot** ([`.github/dependabot.yml`](../../.github/dependabot.yml)) propone cada mes las actualizaciones de las acciones, de `tools/guards` y de `level-1/backend`, con 7 días de `cooldown` y agrupadas. En el nivel 1 solo agrupa las versiones *minor* y *patch*: las mayores llegan en PRs separadas. Ojo con las **alertas** de vulnerabilidades: cubren los `uv.lock`, pero no las acciones fijadas por SHA, porque solo cubren las acciones referenciadas con versión semántica; las acciones solo reciben estas PRs mensuales. Sus PRs pasan por el mismo CI; la historia de la primera está en la [sección 2.7 del protocolo](../00-protocolo-de-desarrollo.md#27-qué-pasa-con-dependabot).

### Herramienta

GitHub Actions, `actions/checkout`, `astral-sh/setup-uv` (con uv 0.11.2 fijado) y Make.

### Para muchos usuarios

- Con PRs obligatorias, `guards` pasaría a ser un check requerido y CI dejaría de ser solo detección. Aquí no lo es, a propósito: bloquearía el push directo a `main` (PLAN-CA-0005 §6.3 y §6.5).
- Cada nivel nuevo trae su `level-N-ci.yml`, y las comparaciones de paridad entre niveles vivirán solo ahí (ADR-ADLN-0007 §E7.7).

> **Anti-patrón:** `uses: actions/checkout@v4`, `runs-on: ubuntu-latest`, los `permissions` por defecto y `run: pip install -r requirements.txt && pytest`.
>
> **Patrón:** acciones por SHA, runner fijado, `permissions: {}` con lectura por job y `run: make <objetivo>`, vigilado por un lint con tests.

---

## 9. Cómo encaja todo

Un commit con un secreto, desde tu teclado hasta GitHub:

```mermaid
%%{init: {"theme": "default", "themeVariables": {"fontSize": "14px"}, "sequence": {"useMaxWidth": true, "wrap": true}}}%%
sequenceDiagram
    autonumber
    participant T as Tú
    participant H as Hooks
    participant M as Make raíz
    participant G as tools/guards
    participant GH as GitHub
    participant CI as CI guards
    T->>H: git commit
    H->>M: make hook-pre-commit
    M->>G: guards staged
    G-->>T: regla, archivo, línea (rc 1). El commit no existe
    T->>H: git commit (ya corregido)
    H->>M: make hook-commit-msg
    M->>G: guards message
    G-->>H: rc 0
    T->>H: git push
    H->>M: make hook-pre-push
    M->>G: guards prepush + level-checks
    G-->>H: rc 0
    H->>GH: envía las refs
    GH->>GH: push protection y rulesets
    GH->>CI: guards.yml y level-1-ci.yml
    CI-->>GH: make secrets-scan y tests en verde
```

Por qué existe cada pieza, y qué se descartó:

| Pieza | Existe porque | Alternativa descartada | Fuente |
| --- | --- | --- | --- |
| Make como única puerta | Tú y CI ejecutan lo mismo; ningún secreto pasa por Make | Exigir `gmake` 4.x | ADR-ADLN-0007 §E7.4; PLAN-CA-0005 §6.5 |
| Hooks delgados | Prevenir antes del push, con lógica que tiene tests | El framework `pre-commit` | PLAN-CA-0005 §6.5 |
| `tools/guards` | gitleaks no ve rutas, identidad, merges ni mensajes | Solo gitleaks; detect-secrets | PLAN-CA-0005 §6.5 |
| gitleaks fijado | La detección no cambia sin revisión | `useDefault = false` con 200 reglas copiadas | D-0001 de PLAN-CA-0005 |
| `make env` | Contraseñas únicas, privadas y nunca escritas a mano | `make env` en Python | ADR-ADLN-0010 §2.5; PLAN-CA-0005 §6.5 |
| Postgres en compose | Un solo dialecto en los cuatro niveles | SQLite; testcontainers | ADR-ADLN-0010 §4, Alt A y Alt H |
| `guards` sin `paths` en CI | Se escanea todo, siempre | `guards` como check requerido | PLAN-CA-0005 §6.3 y §6.5 |
| Repo público con todo esto desde el primer commit | Lo publicado no se despublica | Repo privado hasta pulirlo | ADR-ADLN-0007 §4, Alt G |

---

## 10. Buenas prácticas para un repo mid+

Lo que recomiendo llevar a tu próxima aplicación, en el orden en que se hace:

1. **Antes del primer commit:** `.gitignore` estricto con una sola negación (`!.env.example`), `.gitattributes` con `eol=lf`, `.editorconfig`, licencia y `SECURITY.md`.
2. **Identidad del repo:** email noreply y `user.useConfigOnly=true` en `--local`; nada de la identidad global.
3. **Un Makefile en la raíz** con `help`, `setup`, `doctor`, `secrets-scan` y `guards-test`, y uno por carpeta autónoma con los mismos verbos.
4. **Ningún secreto en Make:** ni `include .env` ni `$(SECRET)`; recetas con `@`; un test de `make -n` con un canario.
5. **Hooks versionados en `.githooks/`**, delgados, que fallen cerrados y deleguen en un motor con tests.
6. **Un motor de guardas cuyo hallazgo no tenga campo de valor:** regla, archivo, línea.
7. **El escáner de secretos fijado por versión y por hash**, verificado en cada ejecución.
8. **La configuración del escáner, leída del índice o del commit** y congelada por un lint: se puede añadir detección, no quitarla en silencio.
9. **Fakes armados en tiempo de ejecución** y aserciones que nunca imprimen el valor.
10. **Un `make env` que genera, no copia:** `umask 077`, temporal más `ln`, nunca sobrescribe e imprime nombres.
11. **El motor de base de datos real desde el nivel uno**, en loopback, con contraseña obligatoria, imagen por digest y UTC en tres sitios: sistema, servidor y base.
12. **Lo destructivo, detrás de una confirmación escrita en una terminal**; sin terminal, aborta.
13. **CI que solo llama a Make**, con acciones por SHA, runner fijado, `permissions: {}` y `persist-credentials: false`.
14. **Un workflow de guardas sin filtro de rutas** y con `fetch-depth: 0`.
15. **Dependabot con `cooldown` y grupos**; las versiones mayores, en PRs aparte y revisadas.
16. **Un `doctor` por clon y por worktree**, que imprime estados y nunca valores.
17. **Los objetivos aún no implementados fallan con rc 2** y dicen quién los implementa.
18. **Cada decisión con su alternativa descartada por escrito** (un ADR o un plan), para no volver a discutirla.

---

## 11. Anti-patrones y patrones

| Anti-patrón | Patrón |
| --- | --- |
| «CI corre gitleaks, el repo está protegido» | Prevenir en local (hooks) y en el servidor (push protection, rulesets), detectar en CI y rotar si algo se publica |
| Redactar la salida de un comando con `sed` | No seleccionar el valor: nombres (`cut -d= -f1`), conteos (`grep -c`) o huellas (`shasum -a 256 \| cut -c1-10`) |
| Un hook u objetivo de Make que hace `exit 0` cuando falta su herramienta | rc 2, un mensaje claro y la operación bloqueada |
| Una allowlist por directorio (`tests/`) para que los tests «no molesten» | Fakes armados por fragmentos en tiempo de ejecución, y allowlists solo por valor de placeholder |
| Borrar el volumen de la base desde un script de limpieza | `db-stop` conserva; `db-down` pide escribir el nombre del proyecto en una terminal |

---

## 12. Preguntas de repaso

<details>
<summary>1. (Trampa) «Cambio <code>GITLEAKS := .tools/bin/gitleaks</code> por <code>GITLEAKS ?= .tools/bin/gitleaks</code>, que es más flexible». ¿Qué se rompe?</summary>

`?=` solo asigna si la variable no existe: un `GITLEAKS` exportado en el entorno ganaría y podría apuntar los hooks a otro binario. Con `:=` (o `=`), la asignación del archivo gana al entorno, y solo un override explícito en la línea de comandos la cambia. [§2](#2-make-la-única-puerta)

</details>

<details>
<summary>2. (Trampa) «Pongo <code>psql "$(DATABASE_URL)"</code> en la receta, con <code>@</code> delante, así que no se imprime». ¿Qué falla?</summary>

`make -n` imprime la receta **expandida**, con el valor, aunque lleve `@`. La receta debe leer `$$DATABASE_URL` del shell. Desde el hotfix `[Debugging - 2]`, la regla `make-secret-var` la rechaza, porque su patrón incluye `URL` (antes, `$(DATABASE_URL)` se le escapaba). La segunda red es el test de `make -n` con canarios. [§2](#los-secretos-nunca-son-variables-de-make)

</details>

<details>
<summary>3. ¿Para qué vacía el hook <code>MAKEFLAGS</code> antes de llamar a Make?</summary>

Si el hook corre dentro de otro `make` lanzado con `-n`, la bandera se hereda: Make no ejecutaría nada, saldría con 0 y el commit pasaría sin escanear. [§3](#3-los-hooks-de-git)

</details>

<details>
<summary>4. ¿Por qué <code>pre-commit</code> escanea el índice y no el árbol de trabajo?</summary>

Lo que entra en el commit es el índice. Puedes tener el archivo limpio en disco y, en el índice, una versión con el secreto ya añadida con `git add`. [§3](#3-los-hooks-de-git)

</details>

<details>
<summary>5. ¿Cómo informa una guarda de una DSN con contraseña sin imprimirla?</summary>

`Finding` no tiene campo de valor: solo regla, ruta, línea y commit. gitleaks corre con `--redact` y de su informe solo se leen `RuleID`, `File` y `StartLine`. [§4](#cómo-informan-sin-imprimir-valores)

</details>

<details>
<summary>6. (Trampa) «Si mi regex de redacción falla, como mucho se ve un asterisco de más». ¿Qué falla?</summary>

Una redacción que no casa imprime el secreto completo, y la salida parece redactada. Una regex de detección que falla da un falso negativo, pero la salida sigue sin el valor. [§4](#cómo-informan-sin-imprimir-valores)

</details>

<details>
<summary>7. ¿Por qué <code>gitleaks.sha256</code> tiene dos líneas por plataforma?</summary>

Una es el hash del tarball, que se verifica antes de extraerlo; la otra, el del binario, que se verifica después de extraerlo, después de copiarlo y en cada escaneo. [§5](#en-admitlens-install-gitleakssh)

</details>

<details>
<summary>8. ¿Por qué <code>make env</code> termina con <code>ln</code> y no con <code>mv</code>?</summary>

`ln` falla si el destino ya existe; `mv` lo pisaría. Así, si `.env` apareció mientras se escribía, no se sobrescribe. [§6](#6-make-env-los-secretos-locales)

</details>

<details>
<summary>9. Generas un <code>.env</code> nuevo y <code>make db-up</code> falla al probar la contraseña. ¿Por qué, y por qué la prueba entra por <code>db</code> y no por <code>127.0.0.1</code>?</summary>

El volumen guarda la contraseña de su primer arranque. Por loopback y por el socket, la imagen usa `trust` y cualquier contraseña «funciona»; por la dirección de servicio usa `scram` y la contraseña se comprueba de verdad. [§7](#los-objetivos-de-la-base)

</details>

<details>
<summary>10. ¿Por qué <code>guards.yml</code> no tiene <code>paths:</code> y <code>level-1-ci.yml</code> sí?</summary>

Las guardas deben correr ante cualquier cambio, y un check filtrado se queda en «Expected». El CI de nivel solo tiene sentido cuando cambia ese nivel. [§8](#8-ci-los-dos-workflows)

</details>

<details>
<summary>11. (Trampa) «<code>guards.yml</code> hace un checkout normal: total, <code>secrets-scan</code> lee el repo». ¿Qué falla?</summary>

Sin `fetch-depth: 0`, `actions/checkout` hace un clon superficial de un solo commit. En un push a `main` crea `origin/main` igualmente, así que antes del hotfix `[Debugging - 2]` el escaneo veía un solo commit **sin avisar**. Desde el hotfix, `history` da rc 2 en un repo superficial, en CI y en local. En PRs y ramas, además, faltaría `origin/main`, que con `CI=true` también da rc 2. [§4](#make-secrets-scan) y [§8](#en-admitlens-guardsyml)

</details>

<details>
<summary>12. ¿Por qué <code>make guards-test</code> lleva <code>if: ${{ !cancelled() }}</code>?</summary>

Para que corra aunque `secrets-scan` falle: un fallo no puede esconder al otro. [§8](#en-admitlens-guardsyml)

</details>

---

## 13. Para practicar

Hazlo todo **en un repo de prueba dentro de un directorio temporal**, nunca en AdmitLens:

1. Escribe un Makefile con un objetivo que use `$(PASSWORD)` y otro con `$$PASSWORD`. Exporta un valor falso y compara la salida de `make -n` de los dos.
2. Crea un hook `pre-commit` de cinco líneas que llame a `make check`, y comprueba que, sin `make` en el `PATH`, el commit se bloquea con rc 2.
3. Ejecuta `make doctor` en AdmitLens y explica cada línea con la tabla de [make doctor](#make-doctor).
4. Lee `mode_prepush` en [`cli.py`](../../tools/guards/guards/cli.py) y dibuja qué pasa con el push de un tag, con el de una rama nueva y con el borrado de `main`.
5. Lee [`make-env.sh`](../../level-1/backend/ops/make-env.sh) y responde: ¿qué pasa si `.env.example` define dos veces el mismo nombre? ¿Y si una URL usa `${NOMBRE}` antes de definirlo?
6. Con la base arriba (`make db-up`), conéctate y ejecuta `SHOW timezone;` en las dos bases. Explica las tres capas que hacen que salga `UTC`.
7. Explica a otra persona, sin mirar la nota, por qué en este repo CI detecta pero no previene.
