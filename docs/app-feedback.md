# App feedback intake

A self-hosted app instance (any project) sends the wishes of its users to AIWerkstatt with an instance token, without a GitHub
issue, and reads the state of each wish back.

## Token

```
python3 service_token.py create <name> --profile app --project <project-id> --instance <instance-id>
```

The token is bound to one project and one instance (lowercase letters, digits, dashes). A token without both is never valid. It may only
call the three routes below; every other route answers 401 or 403. Every call lands in the service-token audit log.

## Routes (app token)

| Route | Purpose |
|---|---|
| `POST /api/app-feedback/<project>/wishes` | Submit a wish. `201 {id, status:"submitted"}`; the same wish again answers `200` with the same id. |
| `GET /api/app-feedback/<project>/wishes` | The state of this instance's wishes (newest first, at most 100). |
| `GET /api/app-feedback/<project>/wishes/<id>` | One wish; another instance's id answers 404. |

`<project>` must equal the project of the token (403 otherwise).

Body of a submission:

```json
{"text": "up to 2000 characters",
 "context": {"page": "...", "version": "1.2.3", "device": "phone|tablet|desktop", "width": 390, "lang": "en"},
 "image": "data:image/png;base64,..."}
```

The image is optional (png, jpeg or webp, at most 1.5 MB decoded, `AIWERKSTATT_APP_FEEDBACK_IMAGE_MAX`); the leading bytes must match the type.

State: `submitted`, `in_progress` (the linked task has started), `done` (with `version`), `declined` (with a short `answer`, at most 500
characters).

## Limits

- Network: only the socket address counts (never `X-Forwarded-For`) and it must be inside `AIWERKSTATT_APP_FEEDBACK_NETS` (comma separated;
  default `100.64.0.0/10,fd7a:115c:a1e0::/48,172.16.0.0/12,127.0.0.0/8,::1/128`). A bad list means nothing is allowed.
- Rate: `AIWERKSTATT_APP_FEEDBACK_RATE` submissions per hour per token (default 10; 0 or junk falls back to 10). Refused-network requests do
  not count.
- Size: request body at most about 2 MB (413), text 2000 characters.

## Processing

`AIWERKSTATT_APP_FEEDBACK_MODES="project=auto,other=collect"` sets the mode per project; everything else is `collect`.

- `collect`: the wish only waits in the review list.
- `auto`: a clean wish becomes exactly one task in the project. The task is restricted (review gate on) and the wish text is fenced between
  `UNTRUSTED_WISH` markers with a note that it is third-party data, never instructions.
- In both modes a deterministic screen holds a wish for review instead of creating a task: known prompt-injection phrases, role markers,
  shell one-liners, requests to send data to a URL, secret assignments, threats, or text longer than 1200 characters. Its state stays
  `submitted`.

## Admin routes

For a signed-in admin or a coordinator token only (an app token gets 403):

- `GET /api/app-feedback/<project>/pending`: wishes without a task, with text, context and hold reason.
- `GET /api/app-feedback/<project>/wishes/<id>/image`: the attached image.
- `POST /api/app-feedback/<project>/wishes/<id>/release`: create the task now (409 when it already has one).
- `POST /api/app-feedback/<project>/wishes/<id>/resolve` with `{"status":"done","version":"x.y.z"}` or
  `{"status":"declined","answer":"..."}`. `done` needs a version, `declined` needs an answer; links, addresses, mail addresses and paths are
  stripped from answers.

## Storage

Table `app_wishes` in the engine database; images under `<data>/app-feedback/` (mode 0600). Tests: `backend/tests/test_app_feedback.py`.
