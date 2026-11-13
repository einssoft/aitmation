"""MCP server: an installed desktop assistant as a *channel* into aitmation.

ADR-0024 settles what the assistants on a user's desktop (Claude Desktop,
and others that speak MCP) may be: a channel like chat or mail, never the
model behind the process. Their consumer subscriptions are not reusable
for automation, so nothing here calls a model - the tools below only read
pending work and cases and pass a human's decision on to the instance's
JSON API (``/api/v1``), which enforces the HumanGate invariant (ADR-0007)
and writes the audit trail exactly as the browser UI does.

The server speaks stdio (what desktop assistants spawn) and talks HTTP to
the launcher's mailroom or a single instance; the household is discovered
through ``/api/v1/household`` so one server covers every mandant.
Customer documents never leave the instance: tools return envelopes
(sender, subject, file name), never file contents.

Run with ``python -m aitmation_core mcp`` (``AITMATION_URL`` defaults to
``http://127.0.0.1:8420``). The ``mcp`` package (MIT) is an optional
extra: ``uv sync --extra mcp``. The gateway below is plain Python so it
can be tested without it.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8420"
SELF = "self"


class GatewayError(Exception):
    """A tool could not do what was asked; the message is for the assistant."""


@dataclass
class Gateway:
    """Household-aware access to the JSON API of every reachable member.

    ``client`` is any :class:`httpx.Client`; tests pass Starlette's
    TestClient, production passes a plain client with the base URL of the
    instance that was configured.
    """

    client: httpx.Client
    base_url: str = DEFAULT_URL

    # -- household -------------------------------------------------------

    def members(self) -> list[dict[str, Any]]:
        data = self._get(self.base_url, "/household")
        members = list(data.get("members") or [])
        if not members:
            members = [{"id": SELF, "title": "aitmation", "type": "", "url": self.base_url}]
        for member in members:
            if member.get("id") == data.get("self"):
                member["url"] = self.base_url
        return members

    def _url_of(self, instance: str | None) -> str:
        if not instance or instance == SELF:
            return self.base_url
        for member in self.members():
            if member.get("id") == instance:
                return member["url"] or self.base_url
        known = ", ".join(m.get("id", "?") for m in self.members())
        raise GatewayError(f"unknown instance {instance!r}; household has: {known}")

    def _targets(self, instance: str | None) -> list[tuple[str, str]]:
        if instance and instance != "all":
            return [(instance, self._url_of(instance))]
        return [(m["id"], m["url"]) for m in self.members()]

    # -- tools -----------------------------------------------------------

    def household(self) -> dict[str, Any]:
        rows = []
        for member in self.members():
            try:
                info = self._get(member["url"], "/instance")
                rows.append({**member, "reachable": True, "open_gates": info.get("open_gates", 0)})
            except (httpx.HTTPError, GatewayError):
                rows.append({**member, "reachable": False, "open_gates": None})
        return {"members": rows}

    def list_pending(self, instance: str | None = None) -> list[dict[str, Any]]:
        """Open human gates across the household (or of one instance)."""
        gates = []
        for member_id, url in self._targets(instance):
            try:
                rows = self._get(url, "/gates")
            except httpx.HTTPError as exc:
                gates.append({"instance": member_id, "error": f"unreachable: {exc}"})
                continue
            for gate in rows:
                gates.append(
                    {
                        "instance": member_id,
                        "case_id": gate["case_id"],
                        "step_id": gate["step_id"],
                        "template": gate.get("template_title"),
                        "gate_kind": gate.get("gate_kind"),
                        "outcomes": gate.get("outcomes", []),
                        "options": [
                            {"id": o["id"], "title": o.get("title")}
                            for o in gate.get("options", [])
                        ],
                        "suggested": gate.get("suggested"),
                        "document": gate.get("document"),
                        "deadline_at": gate.get("deadline_at"),
                        "started_at": gate.get("started_at"),
                    }
                )
        return gates

    def decide(
        self,
        case_id: str,
        step_id: str,
        *,
        instance: str | None = None,
        outcome: str | None = None,
        category: str | None = None,
        addressees: list[str] | None = None,
        discard: bool = False,
        comment: str = "",
        actor: str = "mcp",
    ) -> dict[str, Any]:
        """Pass a human's decision to the instance; exactly one of the choices."""
        given = [x for x in (outcome, category, addressees) if x] + ([True] if discard else [])
        if len(given) != 1:
            raise GatewayError(
                "give exactly one of outcome, category, addressees or discard=true"
            )
        body: dict[str, Any] = {"actor": actor, "comment": comment, "discard": discard}
        if outcome:
            body["outcome"] = outcome
        if category:
            body["category"] = category
        if addressees:
            body["addressees"] = addressees
        url = self._url_of(instance)
        return self._post(url, f"/cases/{case_id}/steps/{step_id}/decide", body)

    def list_cases(
        self, status: str | None = None, instance: str | None = None
    ) -> list[dict[str, Any]]:
        rows = []
        for member_id, url in self._targets(instance):
            query = f"?status={status}" if status else ""
            try:
                cases = self._get(url, f"/cases{query}")
            except httpx.HTTPError as exc:
                rows.append({"instance": member_id, "error": f"unreachable: {exc}"})
                continue
            for case in cases:
                rows.append(
                    {
                        "instance": member_id,
                        "case_id": case["id"],
                        "template": case.get("template_title"),
                        "status": case.get("status"),
                        "current_step": case.get("current_step_id"),
                        "document": case.get("document"),
                        "updated_at": case.get("updated_at"),
                    }
                )
        return rows

    def case_audit(self, case_id: str, instance: str | None = None) -> dict[str, Any]:
        detail = self._get(self._url_of(instance), f"/cases/{case_id}")
        return {
            "case_id": detail["id"],
            "instance": instance or SELF,
            "template": detail.get("template_title"),
            "status": detail.get("status"),
            "document": detail.get("document"),
            "summary": detail.get("summary"),
            "steps": [
                {
                    "step_id": s["step_id"],
                    "kind": s["kind"],
                    "status": s["status"],
                    "outcome": s.get("outcome"),
                    "model_ref": s.get("model_ref"),
                    "finished_at": s.get("finished_at"),
                }
                for s in detail.get("steps", [])
            ],
            "audit": [
                {
                    "seq": a["seq"],
                    "at": a["at"],
                    "actor": f"{a['actor_type']}:{a['actor_id']}",
                    "event": a["event"],
                    "details": a.get("details"),
                }
                for a in detail.get("audit", [])
            ],
        }

    # -- http ------------------------------------------------------------

    def _get(self, base: str, path: str) -> Any:
        resp = self.client.get(f"{base}/api/v1{path}")
        return self._unwrap(resp)

    def _post(self, base: str, path: str, body: dict[str, Any]) -> Any:
        resp = self.client.post(f"{base}/api/v1{path}", json=body)
        return self._unwrap(resp)

    @staticmethod
    def _unwrap(resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            detail: Any = resp.text
            with contextlib.suppress(ValueError, AttributeError):
                detail = resp.json().get("detail", detail)
            raise GatewayError(f"{resp.status_code}: {detail}")
        return resp.json()


def build_server(gateway: Gateway):
    """Register the tools on an MCP server (import deferred: optional extra)."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise SystemExit(
            "the MCP channel needs the optional 'mcp' package: uv sync --extra mcp"
        ) from exc

    server = MCPServer(
        "aitmation",
        instructions=(
            "aitmation runs document processes for a household of private and"
            " company mandants. Use list_pending to see what waits for a human"
            " decision, ask the person, then call decide with their choice."
            " Never decide on your own; the person is the gate."
        ),
    )

    @server.tool(description="Members of the household and how many decisions each has open.")
    def household() -> dict:
        return gateway.household()

    @server.tool(
        description=(
            "Open decisions (human gates). instance: member id, 'all' (default)."
            " Each row names the case, the step, the allowed outcomes or options"
            " and the document envelope."
        )
    )
    def list_pending(instance: str = "all") -> list[dict]:
        return gateway.list_pending(instance)

    @server.tool(
        description=(
            "Pass the person's decision for one open gate. Exactly one of: outcome"
            " (generic gate), category (categorization gate, option id),"
            " addressees (addressing gate, one or more member ids) or discard=true."
        )
    )
    def decide(
        case_id: str,
        step_id: str,
        instance: str = "self",
        outcome: str | None = None,
        category: str | None = None,
        addressees: list[str] | None = None,
        discard: bool = False,
        comment: str = "",
    ) -> dict:
        try:
            return gateway.decide(
                case_id,
                step_id,
                instance=instance,
                outcome=outcome,
                category=category,
                addressees=addressees,
                discard=discard,
                comment=comment,
            )
        except GatewayError as exc:
            return {"ok": False, "error": str(exc)}

    @server.tool(
        description="Cases; status: running|completed|failed|cancelled; instance or 'all'."
    )
    def list_cases(status: str | None = None, instance: str = "all") -> list[dict]:
        return gateway.list_cases(status, instance)

    @server.tool(description="One case with its steps and the audit trail.")
    def case_audit(case_id: str, instance: str = "self") -> dict:
        return gateway.case_audit(case_id, instance)

    return server


def main() -> None:
    base_url = os.environ.get("AITMATION_URL", DEFAULT_URL).rstrip("/")
    # ADR-0025: a household with users needs a bearer token; mint one with
    # `aitmation-launcher token -user <name>` and put it in AITMATION_TOKEN.
    headers = {}
    if token := os.environ.get("AITMATION_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    gateway = Gateway(httpx.Client(timeout=30.0, headers=headers), base_url)
    build_server(gateway).run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
