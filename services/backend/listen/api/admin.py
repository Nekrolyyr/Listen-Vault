"""The administration console's api. Administrators see counts and settings, never the titles of other people's papers."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import accounts, config, jobs, maintenance
from .deps import admin_only, pool


class UserPatch(BaseModel):
    page_quota: int | None = Field(default=None, ge=0, le=10_000_000)
    storage_quota_bytes: int | None = Field(default=None, ge=0, le=10 * 1024 ** 4)
    is_disabled: bool | None = None
    is_admin: bool | None = None


# The columns an administrator may change; the keys of UserPatch.
USER_FIELD_SQL = {
    "page_quota": "UPDATE users SET page_quota = %s WHERE id = %s",
    "storage_quota_bytes": "UPDATE users SET storage_quota_bytes = %s WHERE id = %s",
    "is_disabled": "UPDATE users SET is_disabled = %s WHERE id = %s",
    "is_admin": "UPDATE users SET is_admin = %s WHERE id = %s",
}


class RebuildTimeIn(BaseModel):
    time: str = Field(max_length=5)


class RegistrationIn(BaseModel):
    open: bool


def register_admin_routes(app: FastAPI, settings: config.Settings) -> None:

    @app.get("/api/admin/users")
    def list_users(request: Request):
        admin_only(request)
        with pool(request).connection() as conn:
            rows = conn.execute(
                "SELECT u.id, u.email, u.is_admin, u.is_disabled, u.email_verified_at, u.page_quota, u.created_at, "
                "u.last_login_at, coalesce(sum(d.pages), 0) AS pages_used, count(d.id) AS documents "
                "FROM users u LEFT JOIN library_entries le ON le.user_id = u.id "
                "LEFT JOIN documents d ON d.id = le.document_id "
                "GROUP BY u.id ORDER BY u.created_at, u.id").fetchall()
        with pool(request).connection() as conn:
            storage = {r["id"]: accounts.storage_status(conn, r["id"]) for r in rows}
        return [{
            "id": r["id"], "email": r["email"], "is_admin": r["is_admin"], "is_disabled": r["is_disabled"],
            "verified": r["email_verified_at"] is not None, "page_quota": r["page_quota"],
            "pages_used": int(r["pages_used"]), "documents": r["documents"],
            "storage_used": storage[r["id"]]["used"], "storage_quota": storage[r["id"]]["quota"],
            "created_at": r["created_at"], "last_login_at": r["last_login_at"],
        } for r in rows]

    @app.patch("/api/admin/users/{user_id}")
    def patch_user(request: Request, user_id: int, body: UserPatch):
        actor = admin_only(request)
        changes = body.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(422, "Nothing to change")
        with pool(request).connection() as conn, conn.transaction():
            target = conn.execute("SELECT * FROM users WHERE id = %s FOR UPDATE", (user_id,)).fetchone()
            if target is None:
                raise HTTPException(404, "User not found")
            if user_id == actor["id"] and (changes.get("is_disabled") or changes.get("is_admin") is False):
                raise HTTPException(409, "You cannot disable or demote your own account here")
            if target["is_admin"] and (changes.get("is_admin") is False or changes.get("is_disabled")) \
                    and accounts.admin_count(conn) <= 1:
                raise HTTPException(409, "This is the last administrator")
            for field, value in changes.items():
                old = target[field]
                if old == value:
                    continue
                conn.execute(USER_FIELD_SQL[field], (value, user_id))
                accounts.audit(conn, actor["id"], f"user.{field}", user_id, {"from": old, "to": value})
            if changes.get("is_disabled"):
                accounts.end_sessions(conn, user_id)
            row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        return {"id": row["id"], "page_quota": row["page_quota"], "is_admin": row["is_admin"],
                "is_disabled": row["is_disabled"]}

    @app.post("/api/admin/users/{user_id}/sessions/revoke")
    def revoke_sessions(request: Request, user_id: int):
        actor = admin_only(request)
        with pool(request).connection() as conn, conn.transaction():
            if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
                raise HTTPException(404, "User not found")
            count = accounts.end_sessions(conn, user_id)
            accounts.audit(conn, actor["id"], "sessions.revoked", user_id, {"sessions": count})
        return {"revoked": count}

    @app.get("/api/admin/registration")
    def get_registration(request: Request):
        admin_only(request)
        with pool(request).connection() as conn:
            return {"open": accounts.registration_open(conn), "email_verification": settings.smtp_configured}

    @app.put("/api/admin/registration")
    def put_registration(request: Request, body: RegistrationIn):
        actor = admin_only(request)
        with pool(request).connection() as conn, conn.transaction():
            accounts.set_registration_open(conn, body.open)
            accounts.audit(conn, actor["id"], "registration.open", None, {"open": body.open})
        return {"open": body.open, "email_verification": settings.smtp_configured}

    @app.get("/api/admin/audit")
    def audit_log(request: Request, limit: int = Query(100, ge=1, le=500)):
        admin_only(request)
        with pool(request).connection() as conn:
            rows = conn.execute(
                "SELECT a.id, a.action, a.target_user_id, a.detail, a.created_at, u.email AS actor "
                "FROM admin_audit a LEFT JOIN users u ON u.id = a.actor_id ORDER BY a.id DESC LIMIT %s",
                (limit,)).fetchall()
        return [{"id": r["id"], "action": r["action"], "target_user_id": r["target_user_id"],
                 "detail": r["detail"], "created_at": r["created_at"], "actor": r["actor"]} for r in rows]

    @app.get("/api/admin/stats")
    def stats(request: Request):
        admin_only(request)
        with pool(request).connection() as conn:
            row = conn.execute(
                "SELECT (SELECT count(*) FROM users) AS users, "
                "(SELECT count(*) FROM documents) AS documents, "
                "(SELECT count(*) FROM jobs WHERE state IN ('queued', 'running')) AS open_jobs, "
                "(SELECT count(DISTINCT user_id) FROM sessions WHERE expires_at > now()) AS signed_in").fetchone()
        stats = {k: int(v) for k, v in row.items()}
        with pool(request).connection() as conn:
            stats["stored_bytes"] = int(conn.execute(
                "SELECT coalesce(sum(pdf_bytes), 0) + (SELECT coalesce(sum(bytes), 0) FROM narration_chapters) AS n "
                "FROM documents").fetchone()["n"])
        return stats

    # ------------------------------------------------------------------ maintenance
    @app.get("/api/admin/maintenance")
    def maintenance_status(request: Request):
        admin_only(request)
        with pool(request).connection() as conn:
            return maintenance.status(conn, settings.timezone)

    @app.post("/api/admin/maintenance/{task}", status_code=202)
    def run_maintenance(request: Request, task: str):
        """Start a library-wide task now. The same tasks run on their own (areas when a paper is ready, topics daily)."""
        actor = admin_only(request)
        if task not in maintenance.TASKS:
            raise HTTPException(404, "Unknown maintenance task")
        with pool(request).connection() as conn, conn.transaction():
            if task == "topics":
                queued = maintenance.queue_topics(conn, actor["id"])
            elif task in ("areas", "areas_all"):
                queued = jobs.enqueue_library(conn, "areas", {"force": task == "areas_all"}, user_id=actor["id"]) is not None
                if queued:
                    maintenance.record_run(conn, task)
            else:  # keywords: ready papers that have none yet
                ids = [r["id"] for r in conn.execute(
                    "SELECT d.id FROM documents d WHERE d.status = 'ready' "
                    "AND NOT EXISTS (SELECT 1 FROM document_keywords k WHERE k.document_id = d.id) "
                    "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.document_id = d.id AND j.state IN ('queued', 'running'))")]
                for doc_id in ids:
                    jobs.enqueue(conn, doc_id, {"from": "enrich"}, user_id=actor["id"])
                maintenance.record_run(conn, task)
                return {"queued": len(ids)}
        return {"queued": queued}

    @app.put("/api/admin/maintenance/rebuild-time")
    def set_rebuild_time(request: Request, body: RebuildTimeIn):
        actor = admin_only(request)
        try:
            at = maintenance.parse_time(body.time)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        with pool(request).connection() as conn, conn.transaction():
            maintenance.set_value(conn, "topics_rebuild_at", at.strftime("%H:%M"))
            accounts.audit(conn, actor["id"], "maintenance.rebuild_time", None, {"time": at.strftime("%H:%M")})
        return {"rebuild_at": at.strftime("%H:%M")}

