# Third-party notices

Project source is licensed under MIT. Dependencies are installed separately by uv and retain their own licenses; this repository does not vendor their source or binaries. This inventory records the local review environment, including development/audit tools. Exact resolved artifacts are pinned in uv.lock.

The local source/header review found no additional license file or attribution header imposed on the project source. The repository owner must retain distribution rights for their own code and any content they later add. No private media is distributed.

Psycopg and its binary implementation use LGPL-3.0-only; Certifi uses MPL-2.0. These dependencies are not relicensed as MIT. Redistributing a bundled image or binary requires retaining the applicable notices and meeting corresponding-source/relinking obligations where applicable. See the upstream [Psycopg license](https://github.com/psycopg/psycopg/blob/master/LICENSE.txt), [GNU LGPL terms](https://www.gnu.org/licenses/lgpl-3.0.html) and [Certifi license](https://github.com/certifi/python-certifi/blob/master/LICENSE). The source-only project uses their public interfaces without modifying their code.

qrcode metadata includes a broad classifier, but the installed LICENSE text explicitly grants BSD-style redistribution and identifies its predecessor code under MIT. Its QR Code trademark notice remains an upstream attribution. See the [upstream license](https://github.com/lincolnloop/python-qrcode/blob/main/LICENSE). This is not a proprietary-code dependency.

| Distribution | Reviewed version | Declared license |
| --- | --- | --- |
| aiofiles | 25.1.0 | Apache Software License |
| aiogram | 3.31.0 | MIT |
| aiohappyeyeballs | 2.7.1 | Python Software Foundation License |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT |
| aiosignal | 1.4.0 | Apache Software License |
| alembic | 1.20.1 | MIT |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.16.0 | MIT |
| attrs | 26.1.0 | MIT |
| boolean.py | 5.0 | BSD-2-Clause |
| CacheControl | 0.14.4 | Apache-2.0 |
| certifi | 2026.10.23 | Mozilla Public License 2.0 (MPL 2.0) |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.1 | BSD-3-Clause |
| colorama | 0.4.6 | BSD License |
| cyclonedx-python-lib | 11.12.0 | Apache Software License |
| defusedxml | 0.7.1 | Python Software Foundation License |
| fastapi | 0.162.2 | MIT |
| filelock | 4.2.10 | MIT |
| frozenlist | 1.8.0 | Apache-2.0 |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT License |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD License |
| idna | 3.20 | BSD-3-Clause |
| iniconfig | 2.3.1 | MIT |
| license-expression | 30.4.4 | Apache-2.0 |
| magic-filter | 1.0.12 | MIT |
| Mako | 1.4.3 | MIT |
| markdown-it-py | 4.3.0 | MIT License |
| MarkupSafe | 3.0.4 | BSD-3-Clause |
| mdurl | 0.1.2 | MIT License |
| msgpack | 1.2.4 | Apache-2.0 |
| multidict | 7.2.1 | Apache-2.0 |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| packageurl-python | 0.17.6 | MIT License |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pip-requirements-parser | 32.0.1 | MIT |
| pip_api | 0.0.35 | Apache Software License |
| pip_audit | 2.10.1 | Apache Software License |
| platformdirs | 4.14.5 | MIT |
| pluggy | 1.7.0 | MIT License |
| propcache | 0.5.4 | Apache-2.0 |
| psycopg | 3.3.8 | LGPL-3.0-only |
| psycopg-binary | 3.3.8 | LGPL-3.0-only |
| py-serializable | 2.2.0 | Apache Software License |
| pydantic | 2.13.5 | MIT |
| pydantic-settings | 2.16.2 | MIT |
| pydantic_core | 2.46.5 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| pyparsing | 3.3.3 | MIT |
| pypng | 0.20220715.0 | MIT License |
| pytest | 9.2.0 | MIT |
| pytest-asyncio | 1.4.1.post1 | Apache-2.0 |
| pytest-socket | 0.8.1 | MIT |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| qrcode | 8.2 | BSD-3-Clause; predecessor MIT (LICENSE inspected) |
| requests | 2.34.2 | Apache Software License |
| rich | 15.0.0 | MIT License |
| ruff | 0.17.4 | MIT |
| sortedcontainers | 2.4.0 | Apache Software License |
| SQLAlchemy | 2.0.57 | MIT |
| starlette | 1.10.0 | BSD-3-Clause |
| tomli | 2.5.0 | MIT |
| tomli_w | 1.2.0 | MIT License |
| typing-inspection | 0.4.5 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| tzdata | 2026.6 | Apache-2.0 |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| yarl | 1.25.1 | Apache-2.0 |

All listed dependencies provide redistribution grants under their respective open-source licenses. No UNKNOWN license appeared in the installed-metadata inventory. Python/uv/container images and the PostgreSQL server are independent distributions with their own notices; PostgreSQL uses the [PostgreSQL License](https://www.postgresql.org/about/licence/). A source release does not transfer ownership of dependencies or media.

To refresh the environment inventory: `uv run pip-licenses --format=markdown --with-urls`. Inspect the actual upstream license text where metadata is ambiguous. Never include absolute installation paths in a public report.
