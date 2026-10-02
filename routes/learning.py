"""What the agent learned (split out like the other routers; shared state is read as `srv.<name>`)."""

from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import server as srv

router = APIRouter()


class FactRequest(BaseModel):
    text: str = Field(min_length=3, max_length=500)


class FeedbackRequest(BaseModel):
    kind: Literal["answer", "root_cause"]
    rating: Literal[1, -1]
    node: Optional[str] = Field(None, max_length=255)
    question: Optional[str] = Field(None, max_length=2000)
    answer: Optional[str] = Field(None, max_length=8000)
    correction: Optional[str] = Field(None, max_length=2000)
    tools: List[str] = Field(default_factory=list, max_length=30)  # tools the rated answer used


class ResolutionRequest(BaseModel):
    node: str = Field(min_length=1, max_length=255)
    resolution: str = Field(min_length=3, max_length=1000)
    opened: Optional[str] = None


def known_nodes():
    return [r["d"] for r in srv.driver.execute_query("MATCH (n:Domain) RETURN n.domain AS d").records]


@router.get("/api/learning")
def learning_summary(full: bool = False):
    """Facts, patterns, recent closed outages (cases), feedback counts and the root-cause priors; with
    full=true, everything (all cases and patterns, lessons, recent feedback, root-cause weights)."""
    if not full:
        return srv.learner.summary()
    return {**srv.learner.everything(), "segmentAlerts": srv.metrics.segment_alerts()}


@router.post("/api/learning/facts")
def add_fact(body: FactRequest):
    return srv.learner.add_fact(body.text, known_nodes())


@router.delete("/api/learning/facts/{fact_id}")
def delete_fact(fact_id: int):
    gone = srv.learner.forget_fact(fact_id=fact_id)
    if not gone:
        raise HTTPException(404, f"No fact {fact_id}")
    return {"deleted": gone}


@router.post("/api/learning/feedback")
def add_feedback(body: FeedbackRequest):
    if body.kind == "root_cause" and not body.node:
        raise HTTPException(422, "A root-cause verdict needs the node")
    return srv.learner.add_feedback(body.kind, body.rating, node=body.node, question=body.question,
                                    answer=body.answer, correction=(body.correction or "").strip() or None,
                                    tools=[t[:64] for t in body.tools])


@router.post("/api/learning/resolution")
def set_resolution(body: ResolutionRequest):
    case = srv.learner.set_resolution(body.node, body.resolution, body.opened)
    if not case:
        raise HTTPException(404, f"No closed outage of {body.node} on record")
    return case


class SensitivityRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


@router.post("/api/learning/segment-sensitivity")
def segment_too_sensitive(body: SensitivityRequest):
    """The operator says this URL's segment-age warning is too sensitive: its line moves up a step."""
    alert = srv.metrics.loosen_segment_alert(body.url)
    if not alert:
        raise HTTPException(404, "No segment-age alert for that URL yet")
    return alert


class AlertOutcomeRequest(BaseModel):
    node: str = Field(min_length=1, max_length=255)
    outcome: Literal["settled_alone", "real_outage", "silenced_fast"]


@router.get("/api/learning/dynamic-alert-policy")
def get_dynamic_alert_policy():
    """Returns dynamically learned hold-down debounce periods and cascading suppression rules."""
    return srv.learner.get_dynamic_alert_policy()


@router.post("/api/learning/record-alert-outcome")
def record_alert_outcome(body: AlertOutcomeRequest):
    """Records whether a hold-down warning settled alone or turned into a real outage."""
    ok = srv.learner.record_alert_outcome(body.node, body.outcome)
    return {"success": ok, "node": body.node, "outcome": body.outcome}



