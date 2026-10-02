# Kudos System Specification
 link to github https://github.com/zoruy/Kudos_App.git

## 1. Purpose and Scope

The Kudos System lets authenticated employees publicly recognise colleagues for positive contributions. An employee selects a colleague, writes a short appreciation message, and submits it. Visible kudos appear in a recent-activity feed on the main dashboard. Administrators can hide or permanently delete inappropriate entries.

This specification covers the new Kudos feature only. Existing application authentication, employee-directory data, dashboard shell, and administrator role mechanism are assumed to be available.

## 2. Roles and Permissions

| Role | Permissions |
| --- | --- |
| Authenticated employee | View visible kudos, create kudos for another active employee, and view their own submitted kudos. |
| Administrator | All employee permissions plus view hidden kudos, hide/restore kudos, and permanently delete kudos. |
| Unauthenticated visitor | No access to the kudos form, dashboard feed, or Kudos API. |

Employees cannot give kudos to themselves. Only active employees are valid recipients. A sender may not edit a submitted kudos in the initial release; they should contact an administrator if it must be removed.

## 3. Functional Requirements

### 3.1 User Stories

1. As an authenticated employee, I can choose an active colleague from the employee list so that I can recognise them.
2. As an authenticated employee, I can write and submit a short appreciation message (1–500 characters) for a selected colleague.
3. As an authenticated employee, I can see recently submitted, visible kudos on the dashboard.
4. As an administrator, I can view all kudos, including hidden entries, and hide or restore an inappropriate kudos with a moderation reason.
5. As an administrator, I can permanently delete a kudos when retention policy requires it.

### 3.2 Create Kudos

- The form displays a searchable, accessible list of active colleagues, showing at minimum display name and a distinguishing attribute such as team or email.
- The current user and inactive users are excluded from the selectable list.
- The message field is required, has a maximum of 500 Unicode characters after trimming leading and trailing whitespace, and displays a live remaining-character count.
- The user submits by selecting a recipient, entering a valid message, and activating **Send kudos**.
- On success, the system stores the kudos, marks it visible, shows a confirmation, clears the form, and makes the entry eligible for the public feed immediately.
- While a request is in progress, repeat submission is disabled. The server also enforces duplicate protection: an identical sender, recipient, and normalised message may be created only once within five minutes.
- The system must reject malformed, empty, over-length, self-directed, or unauthorised requests even if client-side validation is bypassed.

### 3.3 Dashboard Feed

- The dashboard displays the newest visible kudos first.
- Each card shows sender display name, recipient display name, message, and a human-friendly timestamp with an accessible exact timestamp.
- Hidden and permanently deleted kudos are never displayed to ordinary employees or returned by the public feed endpoint.
- The initial page shows 20 entries. Users can load additional entries through cursor-based pagination.
- Empty state: display a clear message when there are no visible kudos.
- The feed refreshes after a successful in-session submission. Real-time cross-user refresh is out of scope for the initial release.

### 3.4 Moderation

- Administrators have a moderation view with filters for visible, hidden, and all non-deleted kudos.
- An administrator can hide a visible kudos. Hiding removes it from the public feed immediately, retains it for audit/review, and records the acting administrator, time, and a required moderation reason (1–500 characters).
- An administrator can restore a hidden kudos. Restoration returns it to the public feed and records the restoring administrator and time; the prior moderation reason is retained in the audit record.
- An administrator can permanently delete a kudos after a confirmation step. It is no longer visible in any feed or moderation list. The deletion action is recorded in an audit log.
- Moderation actions must be idempotent where practical: hiding an already hidden record or restoring an already visible record returns its current state without creating an inconsistent result.

### 3.5 Validation, Errors, and Edge Cases

- Client and server validate recipient, message, role, and request format. Server validation is authoritative.
- Messages are rendered as plain text; HTML is not interpreted. The system normalises whitespace for duplicate detection while retaining the submitted display text after safe trimming.
- Rate limit creation to 10 kudos per sender per hour. Excess requests receive a clear retry-later response.
- If a selected colleague becomes inactive before submission, reject the request and ask the user to choose another colleague.
- A duplicate submission caused by retry or double-click returns the existing kudos identifier and a success-equivalent response rather than creating another record.
- Validation errors identify the affected field without exposing internal implementation details. Unexpected failures show a generic error and preserve the entered form values.

### 3.6 Responsive and Accessible Experience

- The form, feed, and moderation view work from 320 px-wide mobile screens through desktop layouts.
- All form controls have programmatic labels, visible focus states, keyboard operation, and screen-reader announcements for submission and validation outcomes.
- Colour alone is never used to convey validation, visibility, or moderation status.

## 4. Technical Design

### 4.1 Architecture and Assumptions

- Integrate with the existing web application using its established frontend framework, backend framework, session/token authentication, and employee directory. Do not create a parallel identity store.
- Use a REST-style JSON API under `/api` and an application database that supports transactions and indexes (for example PostgreSQL).
- Treat employee identity and roles as server-derived from authentication; do not accept sender identity or administrator status from the browser.
- Store timestamps in UTC and format them in the client using the viewer's locale/time zone.

### 4.2 Data Model

#### `users` (existing)

Existing directory/identity table or service. Required fields used by Kudos: `id` (UUID or bigint primary key), `display_name`, `email`, `team` (optional), `is_active`, and role/permission information.

#### `kudos`

| Field | Type | Rules / purpose |
| --- | --- | --- |
| `id` | UUID or bigint | Primary key. |
| `sender_id` | FK to `users.id` | Required; creator. |
| `recipient_id` | FK to `users.id` | Required; recognised colleague; must differ from sender. |
| `message` | varchar(500) | Required, trimmed plain text. |
| `message_normalized` | varchar(500) | Required; normalised form used for duplicate detection. |
| `is_visible` | boolean | Required; defaults to `true`; controls inclusion in public feed. |
| `moderated_by` | FK to `users.id`, nullable | Administrator responsible for the latest visibility action. |
| `moderated_at` | timestamptz, nullable | Time of latest visibility action. |
| `reason_for_moderation` | varchar(500), nullable | Required when hiding; retained for review. |
| `created_at` | timestamptz | Required; defaults to current time. |
| `deleted_at` | timestamptz, nullable | Soft-delete marker used to prevent public display and support auditable deletion. |
| `deleted_by` | FK to `users.id`, nullable | Administrator who deleted the record. |

Constraints and indexes:

- Check constraint: `sender_id <> recipient_id`.
- Check constraint: visible records cannot be soft-deleted.
- Index public-feed query on `(is_visible, deleted_at, created_at DESC, id DESC)`.
- Index sender rate-limit query on `(sender_id, created_at DESC)`.
- Index moderation queries on `(is_visible, deleted_at, created_at DESC)`.
- Prevent five-minute duplicates in application logic within a transaction, because the time window is dynamic. Optionally add a short-lived idempotency-key store if the platform already provides one.

#### `kudos_moderation_audit`

| Field | Type | Purpose |
| --- | --- | --- |
| `id` | UUID or bigint | Primary key. |
| `kudos_id` | FK to `kudos.id` | Moderated kudos. |
| `action` | enum/text | `hidden`, `restored`, or `deleted`. |
| `actor_id` | FK to `users.id` | Acting administrator. |
| `reason` | varchar(500), nullable | Required for `hidden`; optional otherwise. |
| `created_at` | timestamptz | Action time. |

Audit records are append-only. Deleting Kudos must preserve the audit record and follow the organisation's data-retention policy for the original message.

### 4.3 API Design

All endpoints require authentication and use JSON. Standard error body:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Please correct the highlighted fields.",
    "fields": { "message": "Message must be 500 characters or fewer." }
  }
}
```

| Method and path | Access | Request | Success response | Notes |
| --- | --- | --- | --- | --- |
| `GET /api/kudos/recipients` | Employee | `q` optional search text; `limit` optional | `200` active colleague summaries | Excludes current employee; minimum 3-character search may be used for large directories. |
| `POST /api/kudos` | Employee | `{ recipientId, message }` | `201` created kudos | Validates rules, rate limit, and duplicate window. A retry duplicate returns `200` with existing record and `duplicate: true`. |
| `GET /api/kudos` | Employee | `cursor`, `limit` (default/max 20) | `200` `{ items, nextCursor }` | Visible, non-deleted entries only; newest first. |
| `GET /api/admin/kudos` | Administrator | `status`, `cursor`, `limit` | `200` moderation records | `status` is `visible`, `hidden`, or `all`; excludes deleted unless explicitly supported by policy. |
| `PATCH /api/admin/kudos/{id}/visibility` | Administrator | `{ isVisible, reason? }` | `200` updated record | Reason required when `isVisible` is `false`; writes audit record. |
| `DELETE /api/admin/kudos/{id}` | Administrator | confirmation/idempotency header if supported | `204` | Soft-deletes operational record and writes audit record. |

Response kudos fields: `id`, `sender` (`id`, `displayName`), `recipient` (`id`, `displayName`), `message`, `createdAt`, and, for administrators only, visibility and moderation metadata. Never return hidden-message content through employee endpoints.

Status codes: `400` malformed request, `401` unauthenticated, `403` insufficient role, `404` missing/inaccessible resource, `409` state or duplicate conflict where an existing record cannot safely be returned, `422` validation failure, `429` rate limited, and `500` unexpected server failure.

### 4.4 Frontend Design

Dashboard composition:

```text
Dashboard
├── KudosComposer
│   ├── RecipientSearchSelect
│   ├── MessageTextArea
│   └── SubmissionFeedback
└── KudosFeed
    ├── KudosCard (repeated)
    ├── LoadMoreControl
    └── Empty/Error states

Administrator area
└── KudosModerationTable
    ├── StatusFilter
    ├── KudosModerationRow (repeated)
    └── Hide / Restore / Delete confirmation actions
```

- `KudosComposer` loads recipients on demand, performs client-side required/length checks, and calls `POST /api/kudos`.
- On successful creation it resets its state and prepends/refetches the first feed page.
- `KudosFeed` requests cursor pages, avoids duplicate cards when refreshing, and displays server errors in a non-destructive retry state.
- The moderation table is on an administrator-only route; it uses admin endpoints and requires explicit confirmation before deletion.
- The client treats visibility information as server-authoritative and does not attempt to hide content solely in browser state.

### 4.5 Security and Privacy

- Enforce authentication and role-based authorisation on every API endpoint; protect against insecure direct-object references by filtering based on role and visibility.
- Use the application's existing CSRF safeguards for cookie-based sessions; validate bearer tokens if token-based authentication is used.
- Validate all inputs server-side, encode user-provided text at render time, and render messages as text rather than HTML to prevent XSS.
- Use parameterised queries/ORM binding to prevent SQL injection.
- Apply rate limiting by authenticated user and, where suitable, network origin. Log suspected abuse without logging sensitive tokens.
- Do not expose email addresses or employee-directory fields beyond what the interface needs. Follow existing employee privacy and retention policies.
- Record moderation actions in an auditable, access-controlled log.

### 4.6 Performance, Reliability, and Observability

- Use cursor pagination based on `(created_at, id)` to avoid duplicates/missing records as new kudos arrive.
- Query only the fields needed for card rendering and join sender/recipient display names efficiently.
- Cache the first visible-feed page briefly (for example 30–60 seconds) only if cache invalidation occurs after create, hide, restore, or delete; do not cache moderation results across permission boundaries.
- Target p95 API response times below 500 ms under normal expected internal load.
- Log structured events for creation, validation rejection, rate limiting, moderation actions, and unexpected failures; include correlation/request IDs and actor IDs, never secrets or raw authentication credentials.
- Monitor error rate, latency, moderation volumes, and rate-limit events. Alert on sustained server errors or abnormal submission spikes.

## 5. Testing Strategy

- Unit tests: validation, whitespace normalisation, self-kudos rejection, duplicate-window logic, rate-limit logic, visibility transitions, and audit creation.
- API/integration tests: authentication and role gates; recipient filtering; create/feed ordering/pagination; hidden/deleted exclusion; moderation transitions; error response contract; concurrent/retried submissions.
- UI tests: recipient selection, character limit and field errors, disabled double-submit, success refresh, empty/error/loading feed states, admin controls and delete confirmation.
- Accessibility tests: keyboard-only form completion, focus management, labels, error announcements, contrast, and responsive layouts at mobile and desktop widths.
- Security tests: XSS payload rendering, authorisation bypass attempts, CSRF behaviour where applicable, injection attempts, and rate-limit enforcement.
- Performance test: feed pagination and create endpoint at anticipated peak internal traffic, verifying stated p95 target.

## 6. Implementation Plan and Dependencies

1. Confirm integration points: existing authentication/roles, user directory schema/API, dashboard route, database migration convention, UI framework, and deployment environment.
2. Create database migration for `kudos` and `kudos_moderation_audit`, including constraints and indexes. **Depends on:** step 1.
3. Implement server domain validation, duplicate/rate-limit policy, repository/service layer, and structured logging. **Depends on:** step 2.
4. Implement authenticated recipient, create, and visible-feed API endpoints; add automated unit and integration tests. **Depends on:** step 3.
5. Implement administrator moderation API endpoints, audit logging, and authorisation tests. **Depends on:** step 3.
6. Build the dashboard composer and paginated feed, including responsive and accessible states. **Depends on:** step 4.
7. Build the administrator moderation interface with hide/restore/delete confirmation. **Depends on:** step 5.
8. Complete end-to-end, accessibility, security, and performance testing; remediate findings. **Depends on:** steps 6–7.
9. Prepare migration rollback/backup plan, configuration for rate limits and cache, monitoring dashboards/alerts, and release notes. **Depends on:** step 8.
10. Deploy first to a non-production environment, run smoke tests with employee and administrator accounts, obtain approval, then release to production. **Depends on:** step 9.

## 7. Deployment and Rollback

- Deploy database migration before application code that writes Kudos. The migration is additive and does not modify existing identity data.
- Configure role mapping, rate-limit values, logging retention, and any cache invalidation hooks per environment.
- Verify a health check, employee create/feed journey, hidden-content exclusion, and administrator moderation journey after deployment.
- Roll back application code if needed. Avoid dropping Kudos tables as a normal rollback action; preserve records and audit history unless an approved data-removal process directs otherwise.

## 8. Open Decisions to Confirm Before Implementation

1. Whether all employees can view the dashboard feed or whether visibility is limited by business unit/location.
2. The authoritative source and exact role/permission name for administrators.
3. Whether the organisation requires notifications to the recipient or moderator (out of scope by default).
4. The data-retention period and whether a permanent delete must physically erase message content immediately.
5. Expected employee-directory size, which determines whether recipient search should be server-only from the first release.

## 9. Approval

This specification is complete enough to begin implementation once the open decisions are confirmed or consciously accepted as the listed defaults. No implementation code is included in this deliverable.
