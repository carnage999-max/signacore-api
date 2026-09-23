# SignaCore coding style

This describes the conventions already present in the two SignaCore repositories, inferred from
the existing code rather than imposed on it. Where a rule is enforced by tooling, the tool is
named; everything else is convention that reviewers should hold to.

SignaCore is two repositories that ship together:

- `signacore-api` — Django + Django REST Framework. The system of record for documents, fields,
  signing, billing and audit.
- `signa-core` — Next.js App Router admin console. It holds no document state of its own; it
  reads and writes through the Django API.

The signer portal is served by Django as a template with plain JavaScript, not by Next.js.

---

## Python

### Tooling

Enforced by `pre-commit` (`isort`, `black`, `ruff`) — run `pre-commit run --all-files` before
committing.

- Line length **120**, target **py312**.
- `isort` with the `black` profile. First-party packages are `apps`, `services`, `signacore_api`,
  `tasks`, `tests`, `utils`.
- `ruff` lint rules `E4`, `E7`, `E9`, `F`.
- Migrations, `media/`, `storage/`, `staticfiles/` and caches are excluded from all three.

### Module layout

| Package | Holds |
| --- | --- |
| `apps/<app>/` | Django apps: models, serializers, views, urls, migrations |
| `services/` | Domain logic with no Django request context — PDF parsing, rendering, external API clients |
| `utils/` | Small shared helpers: encryption, storage, throttling, tokens |
| `tasks/` | Celery tasks |
| `tests/` | One module per area, plus shared fixture builders |

Anything that opens a PDF, talks to Stripe, or renders a document belongs in `services/`, not in
a view. Views handle authorization, serialization and audit logging, then delegate.

### Typing

- Start modules with `from __future__ import annotations`.
- Annotate every function signature, including return types.
- Use modern syntax: `str | Path`, `list[DetectedField]`, `dict[str, Any]`, `int | None`.
- Use `@dataclass` for value objects that cross a boundary (`DetectedField`, `ImportReport`,
  `AnchorMatch`). Reach for a dataclass before a bare tuple or dict when a function returns more
  than one related value.

### Naming

Full words, no abbreviations. `signing_request`, not `sr`. `detected_fields`, not `fields2`.
`page_index`, not `i`, when the number carries meaning.

- Private module helpers are prefixed with `_`.
- Booleans read as assertions: `is_widget_hidden`, `has_active_behavior`, `is_comb`.
- Constants are module-level and uppercase, grouped near the top with a comment naming their
  source when they come from a specification:

  ```python
  # PDF 32000-1 tables 227-228: field flags.
  FLAG_READ_ONLY = 1 << 0
  FLAG_REQUIRED = 1 << 1
  ```

### Comments and docstrings

Write no comment that restates the code. A docstring earns its place when it records *why*
something is done a particular way — a specification rule, a library defect, a security
boundary, an ordering constraint:

```python
def has_active_behavior(document: fitz.Document, widget: fitz.Widget) -> bool:
    """Detect actions or scripts on the widget or anywhere up its inherited field chain.

    PyMuPDF's ``script*`` properties read only the widget's own dictionary, so a validation
    script declared on a parent field dictionary - which is how kid widgets normally carry
    one - would otherwise go unnoticed.
    """
```

Do not document what a reader can see. Do not reference the ticket, the author, or the change
that introduced the code; that belongs in the commit message.

### Django models

- Choice sets are nested `TextChoices` classes named `<Thing>Enum`, with explicit values:

  ```python
  class FieldTypeEnum(models.TextChoices):
      SIGNATURE = "SIGNATURE", "Signature"
      TEXT = "TEXT", "Text"
  ```

- Every enum has a test in `tests/test_models.py` asserting its exact members, so a change to a
  stored value is deliberate.
- Personal and customer data uses the encrypted field types from `utils.encryption`
  (`EncryptedTextField`, `EncryptedEmailField`). Document files use `encrypted_file_storage`.
- Primary keys are `UUIDField(primary_key=True, default=uuid.uuid4, editable=False)`.

### Migrations

Generated with `makemigrations`, never hand-written, with one exception: a data migration is
written by hand and **must** carry a module docstring explaining what it rewrites and why, must
be reversible where the transform allows, and must skip rows it cannot safely convert rather
than guessing. See `apps/documents/migrations/0017_convert_legacy_acroform_field_origin.py`.

### Views and serializers

- Views are DRF `APIView` subclasses with explicit
  `authentication_classes = []`, `permission_classes = [HasValidSignacoreSecret]` and
  `serializer_class`.
- Scope every query to the caller's organization. Use the existing helpers
  (`get_scoped_document`, `get_request_actor_and_organization`) rather than filtering by hand.
- Return explicit `status.HTTP_*` constants.
- Serializers declare `fields` as an explicit tuple. Never `__all__`.
- Record admin actions with `log_admin_event`, and keep the metadata non-sensitive.

### Security

These are settled decisions, not preferences:

- Never execute content that arrives in an uploaded file — no PDF JavaScript, no actions.
- Treat everything read out of an uploaded document as untrusted text, including field names,
  option values and labels.
- Never persist raw scripts, prefilled values or external URLs from an uploaded document.
- Do not weaken encrypted-at-rest storage or organization scoping to make a feature simpler.
- Prefer failing closed. If a guarantee cannot be proven, withhold the artefact rather than
  distributing it — but preserve the user's work while doing so.

### Tests

- Django `TestCase`, one module per area, named `tests/test_<area>.py`.
- Settings are pinned per class with a stacked `@override_settings(...)`.
- API behaviour is tested through `APIClient` against real URLs, not by calling views directly.
- Fixtures are built by repository-owned builders (`tests/pdf_builders.py`), never read from a
  developer's machine. Each builder has a docstring naming the case it exercises.
- Test names are full sentences describing the behaviour:
  `test_hidden_field_is_ignored_and_its_value_never_reaches_a_signer`.
- Assert the user-visible outcome, not the implementation. A test that would still pass with the
  feature removed is not a test.

Run the suite with the documented command in `CLAUDE.md`; it uses Postgres via
`docker compose`, which is the environment CI matches.

---

## TypeScript and React

### Tooling

- `eslint-config-next` (core-web-vitals + typescript). No Prettier and no `.editorconfig`:
  match the surrounding file.
- TypeScript `strict: true`, path alias `@/*`.
- Tests run under `vitest` (`pnpm test`).

### Read the Next.js docs first

`AGENTS.md` at the repo root is explicit, and it is not boilerplate: this is **Next.js 16**, and
its APIs, conventions and file structure differ from older versions. Read the relevant guide in
`node_modules/next/dist/docs/` before writing routing, caching or server-component code.

### Components

- Named exports for components (`export function SignacoreAdminConsole`). `export default` only
  where the framework requires it — `page.tsx`, `layout.tsx`, route handlers.
- Server pages declare `export const runtime = "nodejs"` and
  `export const dynamic = "force-dynamic"` where they read a session.
- Styling is CSS Modules next to the component (`SignacoreAdminConsole.module.css`), referenced
  as `styles.thing`. No inline style objects except for computed geometry.
- Icons come from `lucide-react`.

### API boundary

The Django API speaks `snake_case`; the console works in `camelCase`. Keep the two apart:

- Declare a response interface that mirrors the API exactly, in `snake_case`
  (`AdminDocumentFieldResponse`).
- Declare the local editing shape in `camelCase` (`EditableFieldDraft`).
- Convert in one place, with a pair of named mappers (`toEditableFieldDraft`,
  `fromEditableFieldDraft`). Do not spread an API response straight into component state.

Optional API fields are typed optional (`max_length?: number | null`) so that adding a field on
the server does not break the build before the console uses it.

### Session handling

Admin routes guard with `getAdminSession()` and redirect through the shared constant
(`COMPANY_LOGIN_PATH`), never a literal path repeated per page. Authorization is enforced by
Django; hiding a control in the console is presentation, never protection.

### Tests

`vitest` covers pure functions — mappers, geometry, presentation helpers — in a `.test.ts`
beside the module. There is no component-rendering test harness; interface behaviour is checked
in a browser instead. If you add logic worth testing, extract it into a pure function so it can
be.

---

## Commits and branches

- Work on a feature branch cut from `main` (`signacore-api`) or `staging` (`signa-core`).
  `staging` is the frontend's integration branch and is ahead of `main`.
- Subject line in the imperative, prefixed `feat:`, `fix:`, `docs:` or `refactor:`.
- The body explains the problem before the change: what was wrong, what a user experienced, then
  what was done about it. Wrap at roughly 80 columns.
- Do not describe the diff. Describe the behaviour.
- Commit only after formatting and the full suite pass.
