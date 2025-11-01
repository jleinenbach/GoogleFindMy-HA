# BOOKMARKS.md
> Central references for AGENTS.md / Quality & Security requirements (Python 3.12, Home Assistant 2025.10, Secure Coding, Software Supply Chain).
> Updated: 2025-10-19

## 1) Python 3.12 — Language, Style, Typing, Safety
- PEP 8 – Style Guide: https://peps.python.org/pep-0008/
- PEP 257 – Docstring Conventions: https://peps.python.org/pep-0257/
- PEP 695 – Type Parameter Syntax (Generics): https://peps.python.org/pep-0695/
- What’s New in Python 3.12: https://docs.python.org/3/whatsnew/3.12.html
- Exceptions & `raise … from …` (tutorial): https://docs.python.org/3/tutorial/errors.html
- `asyncio.TaskGroup` (structured concurrency): https://docs.python.org/3/library/asyncio-task.html#taskgroups
- `subprocess` – security considerations / avoid `shell=True`: https://docs.python.org/3/library/subprocess.html#security-considerations
- Shell escaping via `shlex.quote`: https://docs.python.org/3/library/shlex.html#shlex.quote
- `pickle` – security limitations (avoid for untrusted data): https://docs.python.org/3/library/pickle.html#security-limitations
- Safe literal parsing via `ast.literal_eval`: https://docs.python.org/3/library/ast.html#ast.literal_eval
- `tarfile` – extraction & path traversal note: https://docs.python.org/3/library/tarfile.html#tarfile.TarFile.extractall
- `zipfile` – untrusted archives & traversal note: https://docs.python.org/3/library/zipfile.html#zipfile-objects

## 2) Home Assistant (Developer Docs, 2024–2025)
- Fetching data (DataUpdateCoordinator): https://developers.home-assistant.io/docs/integration_fetching_data/
- Inject web session (`async_get_clientsession`/httpx): https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/inject-websession/
- Test connection before configure (Config Flow): https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/test-before-configure/
- Blocking operations (keep event loop clean): https://developers.home-assistant.io/docs/asyncio_blocking_operations
- Appropriate polling intervals: https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/appropriate-polling/
- Entity unavailable on errors: https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/entity-unavailable/
- Integration setup failures & reauth: https://developers.home-assistant.io/docs/integration_setup_failures/
- Integration file structure (coordinator.py, entity.py, …): https://developers.home-assistant.io/docs/creating_integration_file_structure/
- Diagnostics (redact sensitive data): https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/
- Integration diagnostics (`async_redact_data`): https://developers.home-assistant.io/docs/core/integration_diagnostics/
- Repairs platform (issue registry & flows): https://developers.home-assistant.io/docs/core/platform/repairs/
- Repairs (user docs): https://www.home-assistant.io/integrations/repairs/
- Secrets (`!secret`): https://www.home-assistant.io/docs/configuration/secrets/

## 3) Secure Development — Standards & Guidance
- NIST SP 800-218 — Secure Software Development Framework (SSDF): https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-218.pdf
- BSI TR-02102-1 — Cryptographic mechanisms & key lengths: https://www.bsi.bund.de/SharedDocs/Downloads/DE/BSI/Publikationen/TechnischeRichtlinien/TR02102/BSI-TR-02102-1.pdf

### OWASP Cheat Sheet Series (selected)
- Injection Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Injection_Prevention_Cheat_Sheet.html
- OS Command Injection Defense: https://cheatsheetseries.owasp.org/cheatsheets/OS_Command_Injection_Defense_Cheat_Sheet.html
- Deserialization Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Deserialization_Cheat_Sheet.html
- Logging Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
- Application Logging Vocabulary: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Vocabulary_Cheat_Sheet.html
- OWASP Top 10 ↔ Cheat Sheets index: https://cheatsheetseries.owasp.org/IndexTopTen.html

## 4) Software Supply Chain & Reproducibility
- pip — Secure installs (`--require-hashes`, `--only-binary`): https://pip.pypa.io/en/stable/topics/secure-installs/
- pip-tools — `pip-compile`: https://pip-tools.readthedocs.io/en/latest/cli/pip-compile/
- CycloneDX Python SBOM Tool: https://cyclonedx-bom-tool.readthedocs.io/
- Dependency-Track (SBOM/SCA): https://docs.dependencytrack.org/

## 5) Repo & Documentation Hygiene (GitHub/Markdown)
- GitHub — Community health files overview: https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories
- Default community health files (`.github` repo): https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file
- Organization-wide health files (GitHub changelog): https://github.blog/changelog/2019-02-21-organization-wide-community-health-files/
- Relative links in Markdown (GitHub blog): https://github.blog/news-insights/product-news/relative-links-in-markup-files/
- CommonMark spec (current) — link reference definitions: https://spec.commonmark.org/current/
